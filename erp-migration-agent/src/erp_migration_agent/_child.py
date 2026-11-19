"""Port runner child process: ``python -I -m erp_migration_agent._child <payload.json>``.

Loads a guarded port module with restricted builtins, runs it on every case and prints
one JSON document with the per-case results. Only the parent decides parity.
"""

from __future__ import annotations

import builtins
import io
import json
import sys
import traceback
import types
from typing import Any

from erp_migration_agent import pgtypes
from erp_migration_agent.dataaccess import SnapshotData
from erp_migration_agent.guard import ALLOWED_IMPORTS, public_members

REMOVED_BUILTINS = {
    "open", "exec", "eval", "compile", "input", "breakpoint", "help", "exit", "quit",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview", "type",
}


def _guarded_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    """Hand the port a proxy exposing only the module's allowed public members."""
    if level or name not in ALLOWED_IMPORTS:
        raise ImportError(f"import of {name!r} is blocked in the port runner")
    module = builtins.__import__(name, globals_, locals_, fromlist, level)
    if name == "__future__":
        return module
    return types.SimpleNamespace(**{member: getattr(module, member) for member in public_members(name)})


def _load(source: str, module_name: str) -> types.ModuleType:
    safe = {k: v for k, v in vars(builtins).items() if k not in REMOVED_BUILTINS}
    safe["__import__"] = _guarded_import
    module = types.ModuleType(module_name)
    module.__dict__["__builtins__"] = safe
    sys.modules[module_name] = module  # dataclasses looks the module up by name
    exec(compile(source, f"<port {module_name}>", "exec"), module.__dict__)
    return module


def _short_error(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith("<port")]
    where = f" (port line {frames[-1].lineno})" if frames else ""
    return f"{type(exc).__name__}: {exc}{where}"[:500]


def main() -> int:
    real_stdout = sys.stdout
    sys.stdout = io.StringIO()  # anything the port prints must not corrupt the result
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)
    snapshot = payload["snapshot"]
    tz = pgtypes.zone(snapshot["session_tz"])
    arg_types = payload["arg_types"]
    result: dict[str, Any] = {"load_error": None, "results": {}}
    try:
        module = _load(payload["source"], f"port_{payload['function']}")
        fn = getattr(module, payload["function"])
    except BaseException as exc:  # noqa: BLE001 - report every load failure as data
        result["load_error"] = _short_error(exc)
        real_stdout.write(json.dumps(result))
        return 0

    for case in payload["cases"]:
        try:
            args = [pgtypes.decode(t, v, tz) for t, v in zip(arg_types, case["args"])]
            data = SnapshotData.from_snapshot(snapshot, case.get("fixtures"))
        except Exception as exc:  # noqa: BLE001 - the harness, not the port, failed
            result["results"][case["id"]] = {"ok": False, "harness": True, "error": f"bad case input: {exc}"}
            continue
        try:
            value = fn(*args, data=data)
            result["results"][case["id"]] = {
                "ok": True,
                "py_type": pgtypes.python_type_name(value),
                "value": pgtypes.encode(value),
            }
        except Exception as exc:  # noqa: BLE001 - a raised exception is a recorded outcome
            result["results"][case["id"]] = {"ok": False, "error": _short_error(exc)}
    real_stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
