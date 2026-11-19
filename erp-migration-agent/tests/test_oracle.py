"""Seam: the legacy function as oracle (needs the running sandbox; skips without it).

Expected values were worked out by hand from the PL/pgSQL source, not read back from code.
"""

from __future__ import annotations

import pytest

from erp_migration_agent.characterize import edge_cases, split_cases
from erp_migration_agent.oracle import CaseRejected

pytestmark = pytest.mark.integration


def test_net_days_cross_daylight_saving_with_legacy_86400_second_days(oracle):
    # TRUNC(2026-02-20) is midnight PST; adddays adds 30 * 86400 s, landing at 01:00 PDT.
    src = oracle.extract("paymenttermduedate")
    args, golden, _ = oracle.record(src, ["107", "2026-02-20"])
    assert args == ["107", "2026-02-20T00:00:00-08:00"]
    assert golden == {"value": "2026-03-22T01:00:00-07:00"}


def test_fixture_rows_reach_the_oracle_and_are_rolled_back(oracle):
    src = oracle.extract("currencyround")
    spec = [{"table": "c_currency", "id": 990001, "clone_from": 100, "set": {"stdprecision": "3"}}]
    _, golden, fixtures = oracle.record(src, ["1.2345", "990001", "N"], spec)
    assert golden == {"value": "1.235"}  # PostgreSQL rounds half away from zero
    assert fixtures[0]["row"]["stdprecision"] == "3"
    count = oracle.conn.execute("SELECT count(*) FROM c_currency WHERE c_currency_id = 990001").fetchone()[0]
    oracle.conn.rollback()
    assert count == 0


def test_bad_proposals_are_rejected_not_recorded(oracle):
    src = oracle.extract("currencyround")
    with pytest.raises(CaseRejected, match="invalid input syntax"):
        oracle.record(src, ["not a number", "100", "N"])
    with pytest.raises(CaseRejected, match="only allowed in tables the source reads"):
        oracle.record(src, ["1", "100", "N"], [{"table": "c_order", "id": 990001, "clone_from": 1}])
    with pytest.raises(CaseRejected, match="fixture id must be"):
        oracle.record(src, ["1", "100", "N"], [{"table": "c_currency", "id": 5, "clone_from": 100}])


def test_split_holds_out_every_third_case_per_source_reproducibly(oracle):
    src = oracle.extract("currencyround")
    proposals = [{"name": f"p{i}", "args": [f"{i}.005", "100", "N"]} for i in range(9)]
    cases, rejected = edge_cases(oracle, src, proposals, [])
    assert not rejected
    first = split_cases(cases)
    again = split_cases(list(reversed(cases)))
    held = sorted(c["id"] for c in first if c["split"] == "held_out")
    assert len(held) == 3
    assert held == sorted(c["id"] for c in again if c["split"] == "held_out")
