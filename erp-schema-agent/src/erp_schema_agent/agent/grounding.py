"""Deterministic grounding check for the figures the answer step cites.

The answer model lists every number it states, with the kind of derivation it used.
Each figure is checked only against candidates of its kind, after rounding to the
precision the figure is written with (10,067.55 grounds 10067.55 and 10,068, never
10,110):

* ``value``: a cell of the returned rows (identifier columns such as ``*_id``
  excluded) or the row count;
* ``total``: a column's sum, average, minimum, maximum or count, or the sum of its
  first k rows (a "top 3 total");
* ``share``: a cell's or first-k-rows' percentage share of its column total;
* ``change``: the absolute difference or absolute percentage change between two cells
  of the same column or the same row;
* ``ratio``: the ratio of two cells of the same row or column, or of two column totals
  (average order value = revenue / orders).

Typing the figure keeps each candidate pool small, so an invented number rarely matches
by coincidence; ``change`` and ``ratio`` over many rows are the loosest kinds. Anything
else is flagged ungrounded in the trace and the JSON; the answer is never silently trusted.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

KINDS = ("value", "total", "share", "change", "ratio")
MAX_PAIR_VALUES = 60  # pairwise change/ratio candidates use at most this many values per column


@dataclass(frozen=True)
class FigureCheck:
    label: str
    value: float
    kind: str
    grounded: bool
    evidence: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    return None


def _decimals(value: float) -> int:
    exponent = Decimal(repr(value)).normalize().as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 0


class _Pools:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        keep = [c for c, name in enumerate(columns) if not name.lower().endswith("_id")]
        self.columns = {columns[c]: [_number(row[c]) for row in rows] for c in keep}
        self.numeric = {name: [v for v in vals if v is not None] for name, vals in self.columns.items()}
        self.numeric = {name: vals for name, vals in self.numeric.items() if vals}
        self.rows = [[v for name in self.numeric for v in [self.columns[name][r]] if v is not None]
                     for r in range(len(rows))]
        self.row_count = len(rows)

    def value(self):
        yield float(self.row_count), "row count"
        for name, vals in self.numeric.items():
            for r, v in enumerate(vals):
                yield v, f"{name}[row {r + 1}]"

    def total(self):
        for name, vals in self.numeric.items():
            total = math.fsum(vals)
            yield total, f"sum({name})"
            yield total / len(vals), f"avg({name})"
            yield min(vals), f"min({name})"
            yield max(vals), f"max({name})"
            yield float(len(vals)), f"count({name})"
            for k in range(2, len(vals)):
                yield math.fsum(vals[:k]), f"sum of first {k} rows of {name}"

    def share(self):
        for name, vals in self.numeric.items():
            total = math.fsum(vals)
            if not total:
                continue
            for r, v in enumerate(vals):
                yield v / total * 100, f"row {r + 1} share of {name} total, %"
            for k in range(2, len(vals)):
                yield math.fsum(vals[:k]) / total * 100, f"first {k} rows' share of {name} total, %"

    def _pairs(self):
        for name, vals in self.numeric.items():
            for a, b in itertools.permutations(vals[:MAX_PAIR_VALUES], 2):
                yield a, b, f"within {name}"
        for r, row in enumerate(self.rows):
            for a, b in itertools.permutations(row, 2):
                yield a, b, f"within row {r + 1}"

    def change(self):
        for a, b, where in self._pairs():
            yield abs(a - b), f"difference {where}"
            if b:
                yield abs(a - b) / abs(b) * 100, f"% change {where}"

    def ratio(self):
        for a, b, where in self._pairs():
            if b:
                yield a / b, f"ratio {where}"
        totals = {name: math.fsum(vals) for name, vals in self.numeric.items()}
        for (na, ta), (nb, tb) in itertools.permutations(totals.items(), 2):
            if tb:
                yield ta / tb, f"sum({na}) / sum({nb})"


def check_figures(figures: list[dict[str, Any]], columns: list[str], rows: list[list[Any]]) -> list[FigureCheck]:
    pools = _Pools(columns, rows)
    checks = []
    for fig in figures:
        value = float(fig["value"])
        kind = fig.get("kind", "value")
        if kind not in KINDS:
            checks.append(FigureCheck(str(fig.get("label", "")), value, kind, False, f"unknown kind {kind!r}"))
            continue
        tolerance = 0.5 * 10 ** -_decimals(value) + 1e-9
        target = abs(value) if kind == "change" else value
        hit = next((why for cand, why in getattr(pools, kind)() if abs(target - cand) <= tolerance), None)
        checks.append(FigureCheck(str(fig.get("label", "")), value, kind, hit is not None,
                                  hit or f"no {kind} computable from the rows matches"))
    return checks
