"""Parity between a port's results and the oracle's golden outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import pgtypes
from .runner import RunOutcome


@dataclass
class Mismatch:
    case_id: str
    source: str
    args: list[Any]
    fixtures: list[dict[str, Any]]
    expected: dict[str, Any]
    actual: dict[str, Any]
    reason: str


@dataclass
class ParityReport:
    split: str
    total: int
    matched: int
    mismatches: list[Mismatch] = field(default_factory=list)
    runner_failure: str | None = None

    @property
    def rate(self) -> float:
        return self.matched / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.matched == self.total and self.runner_failure is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "split": self.split,
            "total": self.total,
            "matched": self.matched,
            "rate": round(self.rate, 4),
            "passed": self.passed,
            "runner_failure": self.runner_failure,
            "mismatches": [asdict(m) for m in self.mismatches],
        }


def evaluate(split: str, return_type: str, cases: list[dict[str, Any]], outcome: RunOutcome) -> ParityReport:
    """Compare every case's port result with its golden output (metric contract rules)."""
    report = ParityReport(split=split, total=len(cases), matched=0, runner_failure=outcome.failure)
    for case in cases:
        actual = outcome.result_for(case["id"])
        ok, reason = pgtypes.compare(return_type, case["golden"], actual)
        if ok:
            report.matched += 1
        else:
            report.mismatches.append(
                Mismatch(
                    case_id=case["id"],
                    source=case["source"],
                    args=case["args"],
                    fixtures=case.get("fixtures", []),
                    expected=case["golden"],
                    actual=actual,
                    reason=reason,
                )
            )
    return report
