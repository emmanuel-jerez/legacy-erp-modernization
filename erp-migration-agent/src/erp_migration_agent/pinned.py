"""Helpers for the generated characterization tests in ``migrated/<function>/``.

Each migrated function ships its golden cases and data snapshot. The tests check two
things: the accepted port still reproduces every golden output (offline), and the legacy
function still produces them (needs the sandbox database; skips cleanly without it).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from . import pgtypes
from .dataaccess import SnapshotData


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migrated_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def port_result(module: ModuleType, golden: dict[str, Any], snapshot: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Run the accepted port in-process on one case; same result shape as the runner."""
    tz = pgtypes.zone(snapshot["session_tz"])
    args = [pgtypes.decode(t, v, tz) for t, v in zip(golden["arg_types"], case["args"])]
    data = SnapshotData.from_snapshot(snapshot, case.get("fixtures"))
    try:
        value = getattr(module, golden["function"])(*args, data=data)
    except Exception as exc:  # noqa: BLE001 - an exception is a recorded outcome
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "py_type": pgtypes.python_type_name(value), "value": pgtypes.encode(value)}


def check_port(module: ModuleType, golden: dict[str, Any], snapshot: dict[str, Any], case: dict[str, Any]) -> None:
    ok, reason = pgtypes.compare(golden["return_type"], case["golden"], port_result(module, golden, snapshot, case))
    assert ok, f"{case['id']} ({case['name']}): {reason}"


def check_legacy(oracle: Any, source: Any, case: dict[str, Any]) -> None:
    args, golden, _ = oracle.record(source, case["args"], case.get("fixture_spec"))
    assert args == case["args"], f"{case['id']}: PostgreSQL now normalizes the arguments to {args}"
    assert golden == case["golden"], f"{case['id']} ({case['name']}): legacy returned {golden}, pinned {case['golden']}"
