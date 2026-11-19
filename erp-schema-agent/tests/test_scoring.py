from __future__ import annotations

from erp_schema_agent.evals.scoring import score_result

EXP_COLS = ["state", "revenue"]
EXP_ROWS = [["CA", 256255], ["NY", 130512.3], ["TX", 128917.85]]


def test_order_names_and_extra_columns_do_not_matter():
    got_cols = ["total_revenue", "region_code", "orders"]
    got_rows = [[130512.30, "NY", 10], [128917.85, "TX", 9], [256255.00, "CA", 20]]
    assert score_result(EXP_COLS, EXP_ROWS, got_cols, got_rows).correct


def test_display_rounding_is_tolerated_but_wrong_values_are_not():
    assert score_result(["aov"], [[87.9612]], ["avg"], [[87.96]]).correct
    assert not score_result(["aov"], [[87.96]], ["avg"], [[86.63]]).correct


def test_row_count_and_misaligned_rows_fail():
    assert "row count" in score_result(EXP_COLS, EXP_ROWS, EXP_COLS, EXP_ROWS[:2]).detail
    swapped = [["CA", 130512.3], ["NY", 256255], ["TX", 128917.85]]
    result = score_result(EXP_COLS, EXP_ROWS, EXP_COLS, swapped)
    assert not result.correct
    assert "do not line up" in result.detail


def test_dates_compare_by_day_and_month_strings_as_first_of_month():
    exp = [["2026-07-01", 11034]]
    assert score_result(["month", "revenue"], exp, ["m", "r"], [["2026-07-01T00:00:00", 11034.0]]).correct
    assert score_result(["month", "revenue"], exp, ["m", "r"], [["2026-07", 11034.0]]).correct


def test_a_missing_expected_column_fails_with_its_name():
    result = score_result(EXP_COLS, EXP_ROWS, ["revenue"], [[256255], [130512.3], [128917.85]])
    assert not result.correct


def test_over_wide_answers_are_scored_quickly():
    import time

    exp_cols = [f"e{i}" for i in range(5)]
    exp = [[i * 10 + j for j in range(5)] for i in range(20)]
    got = [[1000 + i * 100 + k for k in range(16)] for i in range(25)]
    started = time.monotonic()
    assert not score_result(exp_cols, exp, [f"g{k}" for k in range(16)], got).correct
    assert time.monotonic() - started < 1
