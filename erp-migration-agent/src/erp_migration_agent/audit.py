"""Append-only audit log: every model proposal and every verdict, one JSON object per line."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from erp_sandbox.llm import ModelResult


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _seq(self) -> int:
        if not self.path.exists():
            return 1
        with self.path.open(encoding="utf-8") as fh:
            return sum(1 for _ in fh) + 1

    def write(self, stage: str, kind: str, data: dict[str, Any]) -> None:
        entry = {"seq": self._seq(), "at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "stage": stage, "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def proposal(self, stage: str, label: str, result: ModelResult) -> None:
        self.write(stage, "model_proposal", {
            "label": label,
            "model": result.model,
            "effort": result.effort,
            "cassette_key": result.key[:32],
            "from_cassette": result.from_cassette,
            "response": result.data,
        })

    def verdict(self, stage: str, data: dict[str, Any]) -> None:
        self.write(stage, "verdict", data)
