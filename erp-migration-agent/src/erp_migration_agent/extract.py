"""Read a legacy function, everything it calls, and the schema of every table it reads.

Extraction is deterministic: it uses the PostgreSQL catalog and word matching over the
source with comments removed. It deliberately over-includes (every overload of a called
helper, every schema operator) because a missing dependency breaks the port silently,
while an extra one only costs prompt space.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

from . import pgtypes

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def strip_comments(source: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


@dataclass
class FunctionDef:
    name: str
    identity_args: str
    arg_names: list[str]
    arg_types: list[str]
    return_type: str
    volatility: str
    definition: str


@dataclass
class LegacySource:
    function: FunctionDef
    helpers: list[FunctionDef]
    operators: list[str]
    # table -> {column: normalized type}; primary key first, then referenced columns.
    tables: dict[str, dict[str, str]]
    primary_keys: dict[str, str]
    db_default_timezone: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegacySource:
        return cls(
            function=FunctionDef(**data["function"]),
            helpers=[FunctionDef(**h) for h in data["helpers"]],
            operators=list(data["operators"]),
            tables={t: dict(c) for t, c in data["tables"].items()},
            primary_keys=dict(data["primary_keys"]),
            db_default_timezone=data["db_default_timezone"],
            notes=list(data.get("notes", [])),
        )


_FUNCS_SQL = """
SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid),
       coalesce(p.proargnames, ARRAY[]::text[]),
       ARRAY(SELECT format_type(t, NULL) FROM unnest(p.proargtypes) AS t),
       pg_get_function_result(p.oid), p.provolatile, pg_get_functiondef(p.oid)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = %s AND p.prokind = 'f'
ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
"""
_VOLATILITY = {"i": "IMMUTABLE", "s": "STABLE", "v": "VOLATILE"}


def _load_functions(conn: psycopg.Connection, schema: str) -> dict[str, list[FunctionDef]]:
    out: dict[str, list[FunctionDef]] = {}
    for _oid, name, ident, argnames, argtypes, result, vol, definition in conn.execute(_FUNCS_SQL, (schema,)):
        out.setdefault(name, []).append(
            FunctionDef(
                name=name,
                identity_args=ident,
                arg_names=[a.lower() for a in argnames][: len(argtypes)] or [f"arg{i + 1}" for i in range(len(argtypes))],
                arg_types=list(argtypes),
                return_type=result,
                volatility=_VOLATILITY.get(vol, vol),
                definition=definition,
            )
        )
    return out


def extract(conn: psycopg.Connection, function_name: str, schema: str = "adempiere") -> LegacySource:
    functions = _load_functions(conn, schema)
    overloads = functions.get(function_name.lower())
    if not overloads:
        raise LookupError(f"no function {schema}.{function_name}")
    if len(overloads) > 1:
        raise LookupError(f"{function_name} is overloaded; this tool ports single-signature functions")
    target = overloads[0]
    for t in target.arg_types + [target.return_type]:
        pgtypes.normalize_type(t)  # raises UnsupportedType early

    # Transitive closure of called schema functions (all overloads of each called name).
    operator_rows = conn.execute(
        """SELECT o.oprname, format_type(o.oprleft, NULL), format_type(o.oprright, NULL), p.proname
           FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
           JOIN pg_proc p ON p.oid = o.oprcode WHERE n.nspname = %s ORDER BY 1, 2, 3""",
        (schema,),
    ).fetchall()
    operators = [f"{left} {op} {right} -> {schema}.{proc}()" for op, left, right, proc in operator_rows]
    seen = {target.name}
    helpers: list[FunctionDef] = []

    def close_over(queue: list[FunctionDef]) -> None:
        while queue:
            body = strip_comments(queue.pop(0).definition)
            for word in sorted({w.lower() for w in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", body)}):
                if word in functions and word not in seen:
                    seen.add(word)
                    helpers.extend(functions[word])
                    queue.extend(functions[word])

    close_over([target])
    # Schema operators are implicit calls (timestamptz + numeric runs adddays). Include an
    # operator's function when the source works with the operator's left-hand type.
    text = " ".join(strip_comments(f.definition).lower() for f in [target, *helpers])
    for _op, left, _right, proc in operator_rows:
        if left in text and proc not in seen:
            seen.add(proc)
            helpers.extend(functions.get(proc, []))
            close_over(list(functions.get(proc, [])))

    bodies = " ".join(strip_comments(f.definition) for f in [target, *helpers])
    words = {w.lower() for w in _WORD.findall(bodies)}
    table_names = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema,),
        )
    }
    tables: dict[str, dict[str, str]] = {}
    pks: dict[str, str] = {}
    notes: list[str] = []
    for table in sorted(words & table_names):
        cols = conn.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""",
            (schema, table),
        ).fetchall()
        pk_row = conn.execute(
            """SELECT a.attname FROM pg_index i
               JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
               WHERE i.indrelid = %s::regclass AND i.indisprimary""",
            (f"{schema}.{table}",),
        ).fetchone()
        pk = pk_row[0] if pk_row else cols[0][0]
        chosen: dict[str, str] = {}
        for col, dtype in [c for c in cols if c[0] == pk] + [c for c in cols if c[0] != pk and c[0] in words]:
            try:
                chosen[col] = pgtypes.normalize_type(dtype)
            except pgtypes.UnsupportedType:
                notes.append(f"{table}.{col} has unsupported type {dtype}; left out of the snapshot")
        tables[table] = chosen
        pks[table] = pk

    tz = conn.execute("SELECT reset_val FROM pg_settings WHERE name = 'TimeZone'").fetchone()[0]
    return LegacySource(
        function=target,
        helpers=helpers,
        operators=[o for o, row in zip(operators, operator_rows) if row[3] in seen],
        tables=tables,
        primary_keys=pks,
        db_default_timezone=tz,
        notes=notes,
    )
