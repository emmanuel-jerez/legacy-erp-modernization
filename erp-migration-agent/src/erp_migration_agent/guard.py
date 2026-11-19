"""Static check on model-written port modules, run before any of their code executes.

This is an allowlist, not a sandbox. Imports are limited to a few standard-library modules,
and only their **public, non-module members** may be used (``calendar.sys`` and
``enum.bltns`` are modules, so they are out). Dynamic code, file access, dunder names and
dunder-looking strings are rejected. The runner repeats the member allowlist at runtime by
handing the port proxy modules, adds a subprocess, a timeout, resource limits and
restricted builtins, and checks result types by identity. The threat model is a
well-meaning model making a mistake, not a determined adversary.
"""

from __future__ import annotations

import ast
import importlib
import re
import types
from dataclasses import dataclass, field
from functools import cache

ALLOWED_IMPORTS = frozenset(
    {"decimal", "datetime", "math", "dataclasses", "typing", "enum", "calendar", "__future__"}
)
# Public members still denied: string evaluation of annotations, and a CLI entry point.
DENIED_MEMBERS = frozenset({"get_type_hints", "get_annotations", "ForwardRef", "evaluate_forward_ref", "main"})
BANNED_NAMES = frozenset(
    {
        "exec", "eval", "compile", "open", "__import__", "globals", "locals", "vars", "type",
        "getattr", "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit",
        "memoryview", "os", "sys", "subprocess", "socket", "importlib", "builtins",
    }
)
# Special methods a port may define on its own classes; any other dunder is rejected.
ALLOWED_DUNDER_DEFS = frozenset(
    {"__init__", "__post_init__", "__repr__", "__str__", "__eq__", "__hash__",
     "__lt__", "__le__", "__gt__", "__ge__"}
)
# Attribute names rejected on any object: frame and code introspection, and the clock
# (ports must be deterministic). Leading-underscore attributes are rejected too.
BANNED_ATTRS = frozenset(
    {"f_globals", "f_locals", "f_builtins", "f_back", "f_code", "gi_frame", "gi_code",
     "cr_frame", "cr_code", "ag_frame", "co_code", "tb_frame", "tb_next", "func_globals",
     "now", "today", "utcnow"}
)
_DUNDER_STRING = re.compile(r"^__\w+__$")


@cache
def public_members(module_name: str) -> frozenset[str]:
    """Members a port may use from an allowlisted module: public, not modules, not denied."""
    module = importlib.import_module(module_name)
    return frozenset(
        name for name in dir(module)
        if not name.startswith("_")
        and name not in DENIED_MEMBERS
        and not isinstance(getattr(module, name), types.ModuleType)
    )


MAX_SOURCE_CHARS = 60_000


@dataclass
class GuardVerdict:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "violations": list(self.violations)}


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def check_source(source: str, function_name: str) -> GuardVerdict:
    """Return the guard verdict for a port module that must define ``function_name``."""
    if len(source) > MAX_SOURCE_CHARS:
        return GuardVerdict(False, [f"source is {len(source)} characters; limit {MAX_SOURCE_CHARS}"])
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return GuardVerdict(False, [f"syntax error at line {exc.lineno}: {exc.msg}"])

    violations: list[str] = []

    def bad(node: ast.AST, message: str) -> None:
        violations.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    bad(node, f"import of {alias.name!r} is not allowed")
                else:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                bad(node, "relative imports are not allowed")
            elif node.module not in ALLOWED_IMPORTS:
                bad(node, f"import from {node.module!r} is not allowed")
            else:
                allowed = public_members(node.module)
                for alias in node.names:
                    if alias.name not in allowed and node.module != "__future__":
                        bad(node, f"{node.module}.{alias.name} is not an allowed member")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                bad(node, f"use of {node.id!r} is not allowed")
            elif _is_dunder(node.id):
                bad(node, f"dunder name {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                bad(node, f"dunder attribute {node.attr!r} is not allowed")
            elif node.attr in BANNED_ATTRS or node.attr.startswith("_"):
                bad(node, f"attribute {node.attr!r} is not allowed")
            elif isinstance(node.value, ast.Name) and node.value.id in module_aliases:
                module = module_aliases[node.value.id]
                if node.attr not in public_members(module):
                    bad(node, f"{module}.{node.attr} is not an allowed member")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DUNDER_STRING.match(node.value):
            bad(node, f"dunder-like string {node.value!r} is not allowed")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _is_dunder(node.name) and node.name not in ALLOWED_DUNDER_DEFS:
                bad(node, f"definition of {node.name!r} is not allowed")
            if isinstance(node, ast.AsyncFunctionDef):
                bad(node, "async functions are not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bad(node, "global/nonlocal statements are not allowed")

    top_level = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    if function_name not in top_level:
        violations.append(f"module must define a top-level function {function_name!r}")
    return GuardVerdict(not violations, violations)
