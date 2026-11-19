"""Independent differential review of the migrated functions.

Fresh random inputs (not from the golden cases) go through the legacy PL/pgSQL function
(oracle) and the accepted Python port; results are compared with the repo's own rules.
Usage: python diffcheck.py <migrated-dir> <n-per-function> <seed>
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from erp_migration_agent import pgtypes
from erp_migration_agent.oracle import CaseRejected, Oracle
from erp_migration_agent.pinned import load_json, load_module, port_result

LA = ZoneInfo("America/Los_Angeles")


def _s(v):
    return None if v is None else str(v)


def rand_amount(rng: random.Random) -> str:
    kind = rng.random()
    if kind < 0.1:
        return rng.choice(["0", "-0", "0.005", "-0.005", "0.0049999", "999999999.995", "1e-10", "-12.345"])
    scale = rng.randint(0, 8)
    value = Decimal(rng.randint(-10**9, 10**9)).scaleb(-scale)
    return str(value)


def rand_ts(rng: random.Random) -> str:
    start = datetime(2023, 1, 1, tzinfo=LA)
    moment = start + timedelta(days=rng.randint(0, 1460), minutes=rng.randint(0, 1439))
    if rng.random() < 0.15:  # around DST changes
        moment = rng.choice([datetime(2024, 3, 10, 1, 30, tzinfo=LA), datetime(2024, 11, 3, 1, 30, tzinfo=LA),
                             datetime(2025, 3, 9, 3, 15, tzinfo=LA), datetime(2026, 11, 1, 0, 45, tzinfo=LA)])
        moment += timedelta(days=rng.randint(-35, 35))
    return moment.isoformat()


def main() -> int:
    migrated, n, seed = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    rng = random.Random(seed)
    summary = {}
    with Oracle.open() as oracle:
        currencies = [r[0] for r in oracle.query_readonly("SELECT c_currency_id FROM adempiere.c_currency")]
        terms = [r[0] for r in oracle.query_readonly("SELECT c_paymentterm_id FROM adempiere.c_paymentterm")]
        makers = {
            "currencyround": lambda: [rand_amount(rng), _s(rng.choice(currencies + [None, 999999])),
                                      rng.choice(["Y", "N", None, "y", ""])],
            "paymenttermduedate": lambda: [_s(rng.choice(terms + [None, 999999])), rand_ts(rng)],
            "paymenttermdiscount": lambda: _discount_args(rng, currencies, terms),
        }
        for fn, make in makers.items():
            golden_meta = load_json(migrated / fn / "golden_cases.json")
            snapshot = load_json(migrated / fn / "snapshot.json")
            module = load_module(migrated / fn / f"{fn}.py")
            source = oracle.extract(fn)
            seen = {json.dumps(c["args"]) for c in golden_meta["cases"]}
            ok = rejected = 0
            mismatches = []
            for _ in range(n):
                raw = make()
                if json.dumps(raw) in seen:
                    continue
                try:
                    args, golden, _ = oracle.record(source, raw, None)
                except CaseRejected:
                    rejected += 1
                    continue
                result = port_result(module, golden_meta, snapshot, {"args": args})
                same, reason = pgtypes.compare(golden_meta["return_type"], golden, result)
                if same:
                    ok += 1
                else:
                    mismatches.append({"args": args, "legacy": golden, "port": result, "reason": reason})
            summary[fn] = {"compared": ok + len(mismatches), "matched": ok, "rejected": rejected,
                           "mismatches": mismatches[:5]}
    print(json.dumps(summary, indent=2, default=str))
    return 0 if all(not s["mismatches"] for s in summary.values()) else 1


def _discount_args(rng: random.Random, currencies: list, terms: list) -> list:
    doc = datetime.fromisoformat(rand_ts(rng))
    pay = doc + timedelta(days=rng.randint(-3, 45), hours=rng.randint(0, 23))
    return [rand_amount(rng), str(rng.choice([100, 102, 0] + rng.sample(currencies, 2))),
            _s(rng.choice(terms + [None])), doc.isoformat(), pay.isoformat()]


if __name__ == "__main__":
    sys.exit(main())
