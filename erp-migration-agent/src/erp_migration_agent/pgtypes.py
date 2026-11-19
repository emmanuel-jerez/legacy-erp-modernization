"""PostgreSQL value semantics shared by the oracle, the port runner and parity.

Values travel between the database, the subprocess that runs a port, case files and
model prompts in one **encoded** form: JSON scalars, with ``numeric`` as its exact
PostgreSQL text form and dates and timestamps as ISO 8601 strings. The encoded form is
the canonical form; parity compares it under the rules in ``docs/metric-contract.md``.

This module is imported by the port subprocess, so it uses the standard library only.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

NUMERIC = "numeric"
INTEGER = "integer"
BIGINT = "bigint"
TIMESTAMPTZ = "timestamp with time zone"
TIMESTAMP = "timestamp without time zone"
DATE = "date"
VARCHAR = "character varying"
TEXT = "text"
CHAR = "character"
BOOLEAN = "boolean"

SUPPORTED = (NUMERIC, INTEGER, BIGINT, TIMESTAMPTZ, TIMESTAMP, DATE, VARCHAR, TEXT, CHAR, BOOLEAN)
_ALIASES = {
    "timestamptz": TIMESTAMPTZ,
    "timestamp": TIMESTAMP,
    "int": INTEGER,
    "int4": INTEGER,
    "int8": BIGINT,
    "varchar": VARCHAR,
    "bpchar": CHAR,
    "char": CHAR,
    "bool": BOOLEAN,
    "name": TEXT,
}
_TYPMOD = re.compile(r"\(.*?\)")


class UnsupportedType(ValueError):
    pass


def normalize_type(pg_type: str) -> str:
    """Canonical type name: lower case, typmods removed, aliases resolved."""
    name = " ".join(_TYPMOD.sub("", pg_type).lower().split())
    name = _ALIASES.get(name, name)
    if name not in SUPPORTED:
        raise UnsupportedType(f"unsupported PostgreSQL type {pg_type!r}")
    return name


def encode(value: Any) -> Any:
    """Encode a Python value (from psycopg or from a port) into its canonical JSON form.

    Encoding never coerces: a ``float`` or a negative-zero ``Decimal`` keeps a form that
    will not match a PostgreSQL value, so parity can report it.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return repr(value)


_KNOWN_TYPES = {
    Decimal: "decimal.Decimal",
    datetime: "datetime.datetime",
    date: "datetime.date",
    bool: "builtins.bool",
    int: "builtins.int",
    str: "builtins.str",
    type(None): "builtins.NoneType",
}


def python_type_name(value: Any) -> str:
    """Name of the value's exact type, decided by identity with the real classes.

    A port-made class that calls itself ``decimal.Decimal`` (or subclasses it) gets an
    ``other:`` name and never matches.
    """
    kind = type(value)
    return _KNOWN_TYPES.get(kind) or f"other:{getattr(kind, '__module__', '?')}.{getattr(kind, '__qualname__', '?')}"


def sql_cast(pg_type: str) -> str:
    """Type to cast an argument to. ``bpchar`` keeps the full string; ``::character`` means char(1)."""
    kind = normalize_type(pg_type)
    return "bpchar" if kind == CHAR else kind


def decode(pg_type: str, encoded: Any, session_tz: tzinfo) -> Any:
    """Turn an encoded value back into the Python value psycopg would return."""
    if encoded is None:
        return None
    kind = normalize_type(pg_type)
    if kind == NUMERIC:
        return Decimal(str(encoded))
    if kind in (INTEGER, BIGINT):
        return int(encoded)
    if kind == TIMESTAMPTZ:
        parsed = datetime.fromisoformat(encoded)
        if parsed.tzinfo is None:
            raise ValueError(f"timestamptz value without offset: {encoded!r}")
        return parsed.astimezone(session_tz)
    if kind == TIMESTAMP:
        return datetime.fromisoformat(encoded)
    if kind == DATE:
        return date.fromisoformat(encoded)
    if kind == BOOLEAN:
        return bool(encoded)
    return str(encoded)


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _matches(kind: str, expected: Any, py_type: str, actual: Any) -> tuple[bool, str]:
    if kind == NUMERIC:
        if py_type != "decimal.Decimal":
            return False, f"expected decimal.Decimal, port returned {py_type}"
        try:
            number = Decimal(actual)
        except (InvalidOperation, TypeError):
            return False, f"port returned non-numeric Decimal {actual!r}"
        if not number.is_finite():
            return False, f"port returned non-finite Decimal {actual}"
        if number.is_signed() and number == 0:
            return False, f"negative zero {actual} (PostgreSQL numeric has none; expected {expected})"
        if actual != expected:
            return False, f"expected {expected}, got {actual}"
        return True, ""
    if kind == TIMESTAMPTZ:
        if py_type != "datetime.datetime":
            return False, f"expected aware datetime, port returned {py_type}"
        got = datetime.fromisoformat(actual)
        if got.tzinfo is None:
            return False, f"port returned naive datetime {actual}"
        want = datetime.fromisoformat(expected)
        if got.astimezone(timezone.utc) != want.astimezone(timezone.utc):
            return False, f"expected {expected}, got {actual}"
        return True, ""
    if kind == TIMESTAMP:
        if py_type != "datetime.datetime":
            return False, f"expected naive datetime, port returned {py_type}"
        got = datetime.fromisoformat(actual)
        if got.tzinfo is not None:
            return False, f"port returned aware datetime {actual}"
        if got != datetime.fromisoformat(expected):
            return False, f"expected {expected}, got {actual}"
        return True, ""
    if kind == DATE:
        if py_type != "datetime.date":
            return False, f"expected date, port returned {py_type}"
    elif kind in (INTEGER, BIGINT):
        if py_type != "builtins.int":
            return False, f"expected int, port returned {py_type}"
    elif kind == BOOLEAN:
        if py_type != "builtins.bool":
            return False, f"expected bool, port returned {py_type}"
    elif py_type != "builtins.str":
        return False, f"expected str, port returned {py_type}"
    if actual != expected:
        return False, f"expected {expected!r}, got {actual!r}"
    return True, ""


def compare(pg_type: str, golden: dict[str, Any], actual: dict[str, Any]) -> tuple[bool, str]:
    """Apply the metric contract's parity rules to one case.

    ``golden`` is ``{"value": encoded}`` or ``{"error": sqlstate}``. ``actual`` is the port
    runner's per-case result: ``{"ok": True, "py_type": ..., "value": encoded}`` or
    ``{"ok": False, "error": message}``. A result marked ``"harness": True`` means the port
    never ran on the case (guard rejection, load failure, timeout, crash, bad input): it is a
    mismatch even where the legacy function raised.
    """
    if actual.get("harness"):
        return False, f"port did not run: {actual.get('error')}"
    if "error" in golden:
        if actual.get("ok"):
            return False, f"legacy raised SQLSTATE {golden['error']}, port returned {actual.get('value')!r}"
        return True, ""
    if not actual.get("ok"):
        return False, f"port raised {actual.get('error')}"
    expected = golden["value"]
    py_type = actual.get("py_type")
    if expected is None:
        if py_type != "builtins.NoneType":
            return False, f"expected NULL (None), got {py_type} {actual.get('value')!r}"
        return True, ""
    if py_type == "builtins.NoneType":
        return False, f"expected {expected!r}, got None"
    return _matches(normalize_type(pg_type), expected, py_type, actual.get("value"))
