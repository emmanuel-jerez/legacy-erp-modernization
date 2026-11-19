from __future__ import annotations

import random

from erp_schema_agent.agent.grounding import check_figures

COLUMNS = ["c_region_id", "state", "month", "revenue", "orders"]
ROWS = [
    [103, "CA", "2026-03-01", 41234.5, 812],
    [108, "NY", "2026-03-01", 18000.25, 390],
    [132, "TX", "2026-03-01", 20765.25, 401],
]


def grounded(value: float, kind: str = "value", rows=ROWS, columns=COLUMNS) -> bool:
    return check_figures([{"label": "x", "value": value, "kind": kind}], columns, rows)[0].grounded


def test_cells_and_display_rounding_are_grounded():
    assert grounded(41234.5)
    assert grounded(41234)  # rounded for display
    assert grounded(812)
    assert grounded(3)  # row count


def test_derived_figures_are_grounded_by_their_kind():
    assert grounded(80000.0, "total")
    assert grounded(59234.75, "total")  # top two rows
    assert grounded(51.54, "share")  # CA share of revenue, %
    assert grounded(23234.25, "change")  # CA minus NY
    assert grounded(56.35, "change")  # NY is 56.35% below CA
    assert grounded(49.91, "ratio")  # average order value: 80,000 / 1,603 orders
    assert grounded(50.78, "ratio")  # CA revenue per order, within the row


def test_near_misses_identifiers_and_wrong_kinds_are_flagged():
    assert not grounded(41300)  # near a cell, but not its display rounding
    assert not grounded(103)  # an identifier column
    assert not grounded(2026)  # a date part
    assert not grounded(80000.0, "value")  # a total claimed as a cell
    check = check_figures([{"label": "growth", "value": 17.3, "kind": "share"}], COLUMNS, ROWS)[0]
    assert not check.grounded
    assert "no share" in check.evidence


def test_invented_values_rarely_pass_on_a_ranking():
    rng = random.Random(7)
    rows = [[f"Customer {i}", round(rng.uniform(1000, 30000), 2), rng.randint(20, 300)] for i in range(12)]
    columns = ["name", "revenue", "orders"]
    invented = [round(rng.uniform(1000, 30000), 2) for _ in range(200)]
    passed = sum(grounded(v, "value", rows, columns) or grounded(v, "total", rows, columns) for v in invented)
    assert passed <= 2
