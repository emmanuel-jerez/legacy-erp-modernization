"""Execution-accuracy scoring: compare result sets, not SQL text.

A prediction is correct when, for some assignment of the expected columns to distinct
predicted columns, the predicted rows projected onto those columns equal the expected
rows as a multiset. So column names and order, row order and extra predicted columns
(a SKU next to a product name) do not matter; row count and values do.

Values are normalised before comparing: numbers match within ``ABS_TOLERANCE``, enough
for rounding to cents and no more (no relative tolerance, so large totals must match to
the cent); timestamps at midnight compare as dates
and ``YYYY-MM`` as the first of the month; strings are compared trimmed and
case-sensitively.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from itertools import permutations
from typing import Any

ABS_TOLERANCE = 0.0051
MAX_ASSIGNMENTS = 5_000
_MIDNIGHT = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]00:00(:00(\.0+)?)?([+-]\d{2}(:?\d{2})?|Z)?$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True)
class Score:
    correct: bool
    detail: str


def normalise(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if m := _MIDNIGHT.match(text):
        return m.group(1)
    if m := _MONTH.match(text):
        return f"{m.group(1)}-{m.group(2)}-01"
    try:
        return float(text) if re.fullmatch(r"-?(0|[1-9]\d*)(\.\d+)?", text) else text  # keeps "007" a code
    except ValueError:
        return text


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=0.0, abs_tol=ABS_TOLERANCE)
    return a == b


def _column_matches(expected: list[Any], got: list[Any]) -> bool:
    if len(expected) != len(got):
        return False
    num_e = sorted(v for v in expected if isinstance(v, float))
    num_g = sorted(v for v in got if isinstance(v, float))
    if len(num_e) != len(num_g) or not all(_equal(a, b) for a, b in zip(num_e, num_g, strict=True)):
        return False
    return Counter(repr(v) for v in expected if not isinstance(v, float)) == Counter(
        repr(v) for v in got if not isinstance(v, float)
    )


def _rows_match(expected: list[tuple], got: list[tuple]) -> bool:
    remaining = list(got)
    for row in sorted(expected, key=repr):
        for i, candidate in enumerate(remaining):
            if all(_equal(a, b) for a, b in zip(row, candidate, strict=True)):
                del remaining[i]
                break
        else:
            return False
    return True


def score_result(expected_columns: list[str], expected_rows: list[list[Any]],
                 got_columns: list[str], got_rows: list[list[Any]]) -> Score:
    exp = [[normalise(v) for v in row] for row in expected_rows]
    got = [[normalise(v) for v in row] for row in got_rows]
    if len(got_rows) != len(expected_rows):
        detail = f"row count {len(got_rows)}, expected {len(expected_rows)}"
        if len(got_rows) > len(expected_rows) and exp and _contains(exp, got, len(expected_columns), len(got_columns)):
            detail += "; every expected row is present, so the query over-answered (extra rows)"
        return Score(False, detail)
    if not exp:
        return Score(True, "both empty")
    n_exp, n_got = len(expected_columns), len(got_columns)
    if n_got < n_exp:
        return Score(False, f"{n_got} columns, expected at least {n_exp}")
    candidates: list[list[int]] = []
    for j in range(n_exp):
        col = [row[j] for row in exp]
        options = [k for k in range(n_got) if _column_matches(col, [row[k] for row in got])]
        if not options:
            return Score(False, f"no column matches expected column {expected_columns[j]!r}")
        candidates.append(options)
    tried = 0
    for combo in _assignments(candidates):
        tried += 1
        if tried > MAX_ASSIGNMENTS:
            return Score(False, f"gave up after {MAX_ASSIGNMENTS} column assignments")
        projected = [tuple(row[k] for k in combo) for row in got]
        if _rows_match([tuple(r) for r in exp], projected):
            return Score(True, "result set matches")
    return Score(False, "columns match individually but rows do not line up")


def _contains(exp: list[list[Any]], got: list[list[Any]], n_exp: int, n_got: int) -> bool:
    """Whether some projection of the predicted rows contains every expected row."""
    options = []
    for j in range(n_exp):
        fits = [k for k in range(n_got) if all(any(_equal(e[j], g[k]) for g in got) for e in exp)]
        if not fits:
            return False
        options.append(fits)
    for tried, combo in enumerate(_assignments(options)):
        if tried >= MAX_ASSIGNMENTS:
            return False
        projected = [tuple(row[k] for k in combo) for row in got]
        if all(any(all(_equal(a, b) for a, b in zip(e, p, strict=True)) for p in projected) for e in exp):
            return True
    return False


def _assignments(candidates: list[list[int]]):
    def walk(j: int, used: tuple[int, ...]):
        if j == len(candidates):
            yield used
            return
        for k in candidates[j]:
            if k not in used:
                yield from walk(j + 1, used + (k,))
    yield from walk(0, ())
