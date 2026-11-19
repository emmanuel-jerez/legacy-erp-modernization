"""Run a guarded port in a separate process with a timeout and resource limits.

Honest scope: this is process isolation plus limits, not a security sandbox. The child
runs as the same user, with the same filesystem view. What it adds over running the code
in-process: the guard runs first, builtins are restricted and imports re-checked at
runtime, CPU time, memory and file writes are capped (Linux ``setrlimit``), the
environment is emptied, and a hung port is killed.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guard import check_source

DEFAULT_TIMEOUT_S = 30.0
MEMORY_LIMIT_BYTES = 1 << 30


@dataclass
class RunOutcome:
    """Per-case results keyed by case id, or a reason no case ran."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure: str | None = None  # guard, load, timeout or crash; applies to every case

    def result_for(self, case_id: str) -> dict[str, Any]:
        if self.failure:
            return {"ok": False, "harness": True, "error": self.failure}
        return self.results.get(case_id, {"ok": False, "harness": True, "error": "no result from runner"})


def _limits(timeout_s: float) -> None:
    cpu = int(timeout_s) + 1
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))  # no file writes; stdout is a pipe


def run_port(
    source: str,
    function: str,
    arg_types: list[str],
    cases: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RunOutcome:
    """Guard ``source``, then run ``function`` on ``cases`` in a child process.

    Each case is ``{"id", "args", "fixtures"}`` with encoded arguments.
    """
    verdict = check_source(source, function)
    if not verdict.ok:
        return RunOutcome(failure="guard rejected the module: " + "; ".join(verdict.violations))

    payload = {
        "source": source,
        "function": function,
        "arg_types": arg_types,
        "cases": [{"id": c["id"], "args": c["args"], "fixtures": c.get("fixtures", [])} for c in cases],
        "snapshot": snapshot,
    }
    with tempfile.TemporaryDirectory(prefix="port-run-") as tmp:
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-m", "erp_migration_agent._child", str(payload_path)],
                capture_output=True,
                text=True,
                cwd=tmp,
                env={},
                timeout=timeout_s,
                preexec_fn=lambda: _limits(timeout_s),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(failure=f"timed out after {timeout_s:.0f}s")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        return RunOutcome(failure=f"runner exited {proc.returncode}: {detail[0][:300]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return RunOutcome(failure="runner printed no JSON result")
    if out.get("load_error"):
        return RunOutcome(failure=f"module failed to load: {out['load_error']}")
    return RunOutcome(results=out["results"])
