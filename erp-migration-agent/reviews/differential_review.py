"""Independent differential review of the migrated functions.

Inputs that the oracle did not already see in the golden cases go through the legacy
PL/pgSQL function (the oracle) and through the accepted Python port. Results are compared
with the repository's own parity rules (``pgtypes.compare``).

Every currency and payment term ID in the database is used at least once (in ID order),
the remaining draws are random from a seeded generator, and NULL plus an unknown ID are
always included. Cases are deduplicated on the oracle's normalized arguments, against the
golden cases and within the run. Rejected cases are kept with their reasons.

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


def inputs(ids: list[Any], n: int, make: Callable[[Any], list[Any]], rng: random.Random) -> Iterator[list[Any]]:
    """Each ID once (plus NULL and an unknown ID), then random IDs up to ``n`` inputs."""
    pool = [*ids, None, UNKNOWN_ID]
    for count in range(n):
        yield make(pool[count] if count < len(pool) else rng.choice(pool))


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

        plans = {
            "currencyround": (currencies, lambda cur: [rand_amount(rng), id_text(cur), rng.choice(COSTING_FLAGS)]),
            "paymenttermduedate": (terms, lambda term: [id_text(term), rand_ts(rng).isoformat()]),
            "paymenttermdiscount": (terms, discount_args),
        }
        for fn, (ids, make) in plans.items():
            golden_meta = load_json(migrated / fn / "golden_cases.json")
            snapshot = load_json(migrated / fn / "snapshot.json")
            module = load_module(migrated / fn / f"{fn}.py")
            source = oracle.extract(fn)
            seen = {json.dumps(c["args"]) for c in golden_meta["cases"]}
            matched, duplicates = 0, 0
            mismatches: list[dict[str, Any]] = []
            rejected: list[dict[str, Any]] = []
            examples: list[list[Any]] = []
            for raw in inputs(ids, n, make, rng):
                try:
                    args, golden, _ = oracle.record(source, raw, None)
                except CaseRejected as exc:
                    rejected.append({"args": raw, "reason": str(exc)})
                    continue
                key = json.dumps(args)
                if key in seen:  # deduplicate on what the oracle saw
                    duplicates += 1
                    continue
                seen.add(key)
                if len(examples) < 5:
                    examples.append(args)
                result = port_result(module, golden_meta, snapshot, {"args": args})
                same, reason = pgtypes.compare(golden_meta["return_type"], golden, result)
                if same:
                    matched += 1
                else:
                    mismatches.append({"args": args, "legacy": golden, "port": result, "reason": reason})
            summary[fn] = {
                "compared": matched + len(mismatches),
                "matched": matched,
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
