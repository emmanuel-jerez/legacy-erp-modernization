"""Task specs: the structured input the operator executes.

A task spec says which draft purchase order to create: the vendor, the lines and the
promised date. Validation is strict and runs before any browser starts, so a malformed
or out-of-scope spec never reaches the UI.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Fernhollow SKUs only: FH-<product>-<size>, optionally a 2-pack (P2).
SKU_PATTERN = re.compile(r"^FH-\d{3}-\d(P2)?$")
MAX_LINES = 10
MAX_QTY = 100_000
NAME_PATTERN = re.compile(r"^[A-Za-z0-9 .,&'()-]{1,60}$")


class TaskSpecError(ValueError):
    """The task spec is malformed or outside the operator's scope."""


@dataclass(frozen=True)
class TaskLine:
    sku: str
    qty: int


@dataclass(frozen=True)
class TaskSpec:
    name: str
    vendor: str
    promised_date: dt.date
    lines: tuple[TaskLine, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "promised_date": self.promised_date.isoformat(),
            "lines": [{"sku": line.sku, "qty": line.qty} for line in self.lines],
        }

    @property
    def ui_promised_date(self) -> str:
        """The date as iDempiere's en_US date fields expect it (MM/DD/YYYY)."""
        return self.promised_date.strftime("%m/%d/%Y")


def validate_task(data: Any) -> TaskSpec:
    """Return a TaskSpec, or raise TaskSpecError naming every problem found."""
    if not isinstance(data, dict):
        raise TaskSpecError("task spec must be a JSON object")
    problems: list[str] = []
    allowed = {"name", "vendor", "promised_date", "lines"}
    extra = sorted(set(data) - allowed)
    if extra:
        problems.append(f"unknown fields: {', '.join(extra)}")

    name = data.get("name", "task")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        problems.append("name must be 1-60 plain characters")

    vendor = data.get("vendor")
    if not isinstance(vendor, str) or not NAME_PATTERN.match(vendor):
        problems.append("vendor must be a business partner name (1-60 plain characters)")

    promised: dt.date | None = None
    raw_date = data.get("promised_date")
    if not isinstance(raw_date, str):
        problems.append("promised_date must be an ISO date string (YYYY-MM-DD)")
    else:
        try:
            promised = dt.date.fromisoformat(raw_date)
        except ValueError:
            problems.append(f"promised_date {raw_date!r} is not an ISO date (YYYY-MM-DD)")

    lines: list[TaskLine] = []
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        problems.append("lines must be a non-empty list")
        raw_lines = []
    elif len(raw_lines) > MAX_LINES:
        problems.append(f"at most {MAX_LINES} lines per task")
    seen: set[str] = set()
    for i, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict) or set(raw) != {"sku", "qty"}:
            problems.append(f"line {i} must have exactly the fields sku and qty")
            continue
        sku, qty = raw["sku"], raw["qty"]
        if not isinstance(sku, str) or not SKU_PATTERN.match(sku):
            problems.append(f"line {i}: sku {sku!r} is not a Fernhollow SKU (FH-NNN-N or FH-NNN-NP2)")
            continue
        if sku in seen:
            problems.append(f"line {i}: sku {sku} appears more than once")
        seen.add(sku)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            problems.append(f"line {i}: qty must be a whole number from 1 to {MAX_QTY}")
            continue
        lines.append(TaskLine(sku=sku, qty=qty))

    if problems:
        raise TaskSpecError("; ".join(problems))
    assert promised is not None and isinstance(vendor, str) and isinstance(name, str)
    return TaskSpec(name=name, vendor=vendor, promised_date=promised, lines=tuple(lines))


def load_task(path: str | Path) -> TaskSpec:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskSpecError(f"cannot read task spec {path}: {exc}") from exc
    return validate_task(data)
