"""Independent differential review of the migrated functions.

Inputs that the oracle did not already see in the golden cases go through the legacy
PL/pgSQL function (the oracle) and through the accepted Python port. Results are compared
with the repository's own parity rules (``pgtypes.compare``).

When ``n`` is large enough, every currency and payment term ID in the database is used at
least once (in ID order), plus NULL and an unknown ID; the remaining draws are random from a
seeded generator. The real payment terms never exercise some branches (fixed due dates, the
second discount tier, a next-business-day shift that changes a discount), so half of the
payment-term cases use a synthetic term: a fixture row cloned from a real term with random
settings, inserted inside the oracle's always-rolled-back transaction. Cases are
deduplicated on what the oracle saw (normalized arguments plus fixture rows read back),
against the golden cases and within the run. Rejected and duplicate cases are kept.

Usage, from ``erp-migration-agent`` with the sandbox stack up:
    uv run python -I reviews/differential_review.py "$PWD/migrated" <n-per-function> <seed>
"""

from __future__ import annotations

import json
import random
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from erp_migration_agent import pgtypes
from erp_migration_agent.oracle import CaseRejected, Oracle
from erp_migration_agent.pinned import load_json, load_module, port_result

LA = ZoneInfo("America/Los_Angeles")
UNKNOWN_ID = 9_999_999  # no such row; outside the fixture-reserved range 990000-999999
BOUNDARY_AMOUNTS = ["0", "-0", "0.005", "-0.005", "0.0049999", "999999999.995", "1e-10", "-12.345"]
COSTING_FLAGS = ["Y", "N", None, "y", ""]
SYNTHETIC_TERM_ID = 990_900  # inside the fixture-reserved range; the row never commits


def id_text(value: Any) -> str | None:
    """Arguments travel as text, as in the golden cases; NULL stays NULL."""
    return None if value is None else str(value)


def rand_amount(rng: random.Random) -> str:
    if rng.random() < 0.1:
        return rng.choice(BOUNDARY_AMOUNTS)
    return str(Decimal(rng.randint(-10**9, 10**9)).scaleb(-rng.randint(0, 8)))


def rand_ts(rng: random.Random) -> datetime:
    moment = datetime(2023, 1, 1, tzinfo=LA) + timedelta(days=rng.randint(0, 1460), minutes=rng.randint(0, 1439))
    if rng.random() < 0.15:  # weight dates around daylight-saving changes
        moment = rng.choice([datetime(2024, 3, 10, 1, 30, tzinfo=LA), datetime(2024, 11, 3, 1, 30, tzinfo=LA),
                             datetime(2025, 3, 9, 3, 15, tzinfo=LA), datetime(2026, 11, 1, 0, 45, tzinfo=LA)])
        moment += timedelta(days=rng.randint(-35, 35))
    return moment


def synthetic_term(rng: random.Random, terms: list[Any]) -> list[dict[str, Any]]:
    """One fixture payment term with random settings, cloned from a real term."""
    return [{
        "table": "c_paymentterm",
        "id": SYNTHETIC_TERM_ID,
        "clone_from": int(rng.choice(terms)),
        "set": {
            "isduefixed": rng.choice(["Y", "N"]),
            "netdays": str(rng.randint(0, 60)),
            "fixmonthday": str(rng.randint(1, 31)),
            "fixmonthoffset": str(rng.randint(-1, 2)),
            "fixmonthcutoff": str(rng.randint(0, 31)),
            "discount": str(rng.choice([0, 1, 1.5, 2, 3, 5])),
            "discountdays": str(rng.randint(0, 20)),
            "discount2": str(rng.choice([0, 0.5, 1, 2])),
            "discountdays2": str(rng.randint(0, 45)),
            "gracedays": str(rng.randint(0, 5)),
            "isnextbusinessday": rng.choice(["Y", "N"]),
        },
    }]


def inputs(ids: list[Any], n: int, make: Callable[[Any], list[Any]], rng: random.Random,
           synthetic: Callable[[], list[dict[str, Any]]] | None = None) -> Iterator[tuple[list[Any], list | None]]:
    """Each ID once (plus NULL and an unknown ID), then random IDs, up to ``n`` inputs.

    With ``synthetic``, every second input after the real IDs uses a synthetic payment term.
    """
    pool = [*ids, None, UNKNOWN_ID]
    for count in range(n):
        if synthetic is not None and count >= len(pool) and count % 2 == 0:
            yield make(SYNTHETIC_TERM_ID), synthetic()
        else:
            yield make(pool[count] if count < len(pool) else rng.choice(pool)), None


def main() -> int:
    migrated, n, seed = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    rng = random.Random(seed)
    summary: dict[str, Any] = {"seed": seed, "n_per_function": n}
    with Oracle.open() as oracle:
        currencies = [r[0] for r in oracle.query_readonly(
            "SELECT c_currency_id FROM adempiere.c_currency ORDER BY c_currency_id")]
        terms = [r[0] for r in oracle.query_readonly(
            "SELECT c_paymentterm_id FROM adempiere.c_paymentterm ORDER BY c_paymentterm_id")]

        def discount_args(term: Any) -> list[Any]:
            doc = rand_ts(rng)
            pay = doc + timedelta(days=rng.randint(-3, 45), hours=rng.randint(0, 23))
            # 0 makes the legacy function look the currency up from the client's accounting
            # schema; 100 (USD) and 102 (EUR) are the tenant's own currencies.
            currency = rng.choice([100, 102, 0, None, UNKNOWN_ID, *rng.sample(currencies, 2)])
            return [rand_amount(rng), id_text(currency), id_text(term), doc.isoformat(), pay.isoformat()]

        def term_fixture() -> list[dict[str, Any]]:
            return synthetic_term(rng, terms)

        plans = {
            "currencyround": (currencies, lambda cur: [rand_amount(rng), id_text(cur), rng.choice(COSTING_FLAGS)],
                              None),
            "paymenttermduedate": (terms, lambda term: [id_text(term), rand_ts(rng).isoformat()], term_fixture),
            "paymenttermdiscount": (terms, discount_args, term_fixture),
        }
        for fn, (ids, make, synthetic) in plans.items():
            golden_meta = load_json(migrated / fn / "golden_cases.json")
            snapshot = load_json(migrated / fn / "snapshot.json")
            module = load_module(migrated / fn / f"{fn}.py")
            source = oracle.extract(fn)
            seen = {json.dumps([c["args"], c.get("fixtures") or []], default=str) for c in golden_meta["cases"]}
            matched, synthetic_compared = 0, 0
            mismatches: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            duplicates: list[dict[str, Any]] = []
            examples: list[list[Any]] = []
            for raw, spec in inputs(ids, n, make, rng, synthetic):
                try:
                    args, golden, fixtures = oracle.record(source, raw, spec)
                except CaseRejected as exc:
                    rejected.append({"args": raw, "fixture_spec": spec, "reason": str(exc)})
                    continue
                key = json.dumps([args, fixtures], default=str)
                if key in seen:  # deduplicate on what the oracle saw
                    duplicates.append({"args": args, "fixtures": fixtures})
                    continue
                seen.add(key)
                synthetic_compared += bool(spec)
                if len(examples) < 5:
                    examples.append(args)
                result = port_result(module, golden_meta, snapshot, {"args": args, "fixtures": fixtures})
                same, reason = pgtypes.compare(golden_meta["return_type"], golden, result)
                if same:
                    matched += 1
                else:
                    mismatches.append({"args": args, "fixtures": fixtures, "legacy": golden, "port": result,
                                       "reason": reason})
            summary[fn] = {
                "compared": matched + len(mismatches),
                "matched": matched,
                "with_synthetic_term": synthetic_compared,
                "duplicates_skipped": duplicates,
                "rejected": rejected,
                "mismatches": mismatches,
                "ids_in_database": len(ids),
                "example_args": examples,
            }
    print(json.dumps(summary, indent=2, default=str))
    clean = all(not summary[fn]["mismatches"] for fn in plans)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
