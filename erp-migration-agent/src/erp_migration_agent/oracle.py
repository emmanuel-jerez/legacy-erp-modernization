"""The legacy function as oracle: normalize inputs, call it, and record golden outputs.

Every call runs in the pinned session from the metric contract. Calls are read-only in
effect: plain cases run in ``READ ONLY`` transactions, and cases with fixture rows insert
them in a transaction that always rolls back.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from erp_sandbox import db as sandbox_db
from psycopg import sql

from . import config, pgtypes
from .extract import LegacySource, extract

FIXTURE_LOCK_KEY = 990_000  # advisory lock id for fixture transactions (the reserved id range's start)
FIXTURE_NAMESPACE = uuid.UUID("6f1d3c2e-8a8b-4c55-9d0e-0c3a1f6b7e21")
# Unique columns the harness fills itself so fixture rows never collide with seed rows.
_PROTECTED_SUFFIXES = ("_uu",)
_PROTECTED_COLUMNS = {"name", "value", "iso_code"}


class CaseRejected(ValueError):
    """A proposed case cannot be run (bad arguments or fixture); it is dropped and logged."""


# Only these SQLSTATE classes are legacy behaviour worth pinning: data exceptions (22, e.g.
# division by zero), cardinality violations (21) and PL/pgSQL RAISE (P0). Anything else, such
# as a statement timeout (57014), a write in a read-only call (25006) or a client-side load
# error, is a harness problem and rejects the case instead of becoming a golden "error".
LEGACY_ERROR_CLASSES = ("22", "21", "P0")


def _cast(pg_type: str) -> sql.SQL:
    return sql.SQL(pgtypes.sql_cast(pg_type))


def _casts(arg_types: list[str]) -> sql.Composable:
    return sql.SQL(", ").join(sql.SQL("%s::{}").format(_cast(t)) for t in arg_types)


def _message(exc: psycopg.Error) -> str:
    diag = getattr(exc, "diag", None)
    return (diag.message_primary if diag is not None else None) or str(exc) or type(exc).__name__


def is_available() -> bool:
    try:
        with sandbox_db.connect(config.DB_NAME) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False


class Oracle:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        conn.execute(sql.SQL("SET TIME ZONE {}").format(sql.Literal(config.SESSION_TIMEZONE)))
        conn.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(config.SCHEMA)))
        conn.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(config.STATEMENT_TIMEOUT_MS)))
        conn.commit()

    @classmethod
    @contextmanager
    def open(cls) -> Iterator[Oracle]:
        with sandbox_db.connect(config.DB_NAME) as conn:
            yield cls(conn)

    def query_readonly(self, query: Any, params: Any = None) -> list[tuple[Any, ...]]:
        """Run a read-only query (sampling SQL) in a READ ONLY transaction."""
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("SET TRANSACTION READ ONLY")
            return self.conn.execute(query, params).fetchall()

    def extract(self, function_name: str) -> LegacySource:
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("SET TRANSACTION READ ONLY")
            return extract(self.conn, function_name, config.SCHEMA)

    # ------------------------------------------------------------------ snapshots
    def snapshot(self, source: LegacySource) -> dict[str, Any]:
        """Every row of each dependency table, limited to the columns the source names."""
        tables: dict[str, Any] = {}
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("SET TRANSACTION READ ONLY")
            for table, columns in source.tables.items():
                tables[table] = {"columns": columns, "rows": self._rows(table, columns, source.primary_keys[table])}
        return {"session_tz": config.SESSION_TIMEZONE, "tables": tables}

    def _rows(self, table: str, columns: dict[str, str], pk: str, where_id: Any = None) -> list[list[Any]]:
        query = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.Identifier(table)
        )
        params: tuple[Any, ...] = ()
        if where_id is not None:
            query += sql.SQL(" WHERE {} = %s").format(sql.Identifier(pk))
            params = (where_id,)
        # Order by every selected column (key first) so composite keys still give a fixed order.
        query += sql.SQL(" ORDER BY {}").format(sql.SQL(", ").join(sql.Identifier(c) for c in columns))
        return [[pgtypes.encode(v) for v in row] for row in self.conn.execute(query, params)]

    # ------------------------------------------------------------------ calls
    def _normalize(self, source: LegacySource, raw_args: list[Any]) -> list[Any]:
        fn = source.function
        if len(raw_args) != len(fn.arg_types):
            raise CaseRejected(f"expected {len(fn.arg_types)} arguments, got {len(raw_args)}")
        if not raw_args:
            return []
        for value in raw_args:
            if value is not None and not isinstance(value, str):
                raise CaseRejected(f"argument {value!r} must be a string or null")
        query = sql.SQL("SELECT {}").format(_casts(fn.arg_types))
        try:
            row = self.conn.execute(query, raw_args).fetchone()
        except psycopg.Error as exc:
            raise CaseRejected(f"PostgreSQL rejected the arguments: {_message(exc)}") from exc
        return [pgtypes.encode(v) for v in row]

    def _call(self, source: LegacySource, args: list[Any]) -> dict[str, Any]:
        fn = source.function
        query = sql.SQL("SELECT {}.{}({})").format(
            sql.Identifier(config.SCHEMA), sql.Identifier(fn.name), _casts(fn.arg_types)
        )
        try:
            value = self.conn.execute(query, args).fetchone()[0]
        except psycopg.Error as exc:
            state = exc.sqlstate or ""
            if state[:2] in LEGACY_ERROR_CLASSES:
                return {"error": state}
            raise CaseRejected(f"oracle call failed outside legacy behaviour ({state or 'client'}): {_message(exc)}") from exc
        return {"value": pgtypes.encode(value)}

    def record(
        self, source: LegacySource, raw_args: list[Any], fixture_specs: list[dict[str, Any]] | None = None
    ) -> tuple[list[Any], dict[str, Any], list[dict[str, Any]]]:
        """Normalize ``raw_args``, insert fixtures, call the legacy function, roll back.

        Returns ``(args, golden, fixtures)``: canonical arguments, the golden result, and the
        fixture rows as read back (limited to snapshot columns) for the port's data access.
        """
        fixture_specs = fixture_specs or []
        if len(fixture_specs) > config.MAX_FIXTURES_PER_CASE:
            raise CaseRejected(f"at most {config.MAX_FIXTURES_PER_CASE} fixture rows per case")
        with self.conn.transaction(force_rollback=True):
            if fixture_specs:
                # Fixture ids come from one reserved range for every function, so concurrent runs
                # (eval --jobs) would wait on each other's uncommitted keys or deadlock. A
                # transaction-scoped advisory lock serializes fixture cases instead.
                self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (FIXTURE_LOCK_KEY,))
            args = self._normalize(source, raw_args)
            fixtures = [self._insert_fixture(source, spec) for spec in fixture_specs]
            # The legacy call itself always runs read-only, with or without fixtures, so a
            # function that writes is rejected consistently (PostgreSQL allows switching a
            # transaction to read-only after it has written).
            self.conn.execute("SET TRANSACTION READ ONLY")
            golden = self._call(source, args)
        return args, golden, fixtures

    def _insert_fixture(self, source: LegacySource, spec: dict[str, Any]) -> dict[str, Any]:
        table = str(spec.get("table", "")).lower()
        if table not in source.tables:
            raise CaseRejected(f"fixtures are only allowed in tables the source reads: {sorted(source.tables)}")
        pk = source.primary_keys[table]
        if pk != f"{table}_id":
            raise CaseRejected(f"{table} has no numeric {table}_id key; fixtures are not supported there")
        try:
            new_id = int(spec["id"])
            clone_from = int(spec["clone_from"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CaseRejected("fixture needs integer 'id' and 'clone_from'") from exc
        if not config.FIXTURE_ID_MIN <= new_id <= config.FIXTURE_ID_MAX:
            raise CaseRejected(f"fixture id must be in {config.FIXTURE_ID_MIN}-{config.FIXTURE_ID_MAX}")

        columns = self.conn.execute(
            """SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
               WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""",
            (f"{config.SCHEMA}.{table}",),
        ).fetchall()
        types = dict(columns)
        overrides = {str(k).lower(): v for k, v in (spec.get("set") or {}).items()}
        unknown = set(overrides) - set(types)
        if unknown:
            raise CaseRejected(f"unknown column(s) for {table}: {sorted(unknown)}")
        exprs: list[sql.Composable] = []
        params: list[Any] = []
        for col, coltype in columns:
            typed = sql.SQL("%s::") + sql.SQL(coltype)
            if col == pk:
                exprs.append(typed)
                params.append(str(new_id))
            elif col.endswith(_PROTECTED_SUFFIXES):
                exprs.append(typed)
                params.append(str(uuid.uuid5(FIXTURE_NAMESPACE, f"{table}:{new_id}")))
            elif col in _PROTECTED_COLUMNS:
                exprs.append(typed)
                params.append(_unique_text(col, new_id))
            elif col in overrides:
                value = overrides[col]
                if value is not None and not isinstance(value, str):
                    raise CaseRejected(f"fixture value for {col} must be a string or null")
                exprs.append(typed)
                params.append(value)
            else:
                exprs.append(sql.Identifier(col))
        insert = sql.SQL("INSERT INTO {t} SELECT {exprs} FROM {t} WHERE {pk} = %s").format(
            t=sql.Identifier(table), exprs=sql.SQL(", ").join(exprs), pk=sql.Identifier(pk)
        )
        self.conn.execute("SAVEPOINT fixture")
        try:
            inserted = self.conn.execute(insert, [*params, clone_from]).rowcount
        except psycopg.Error as exc:
            self.conn.execute("ROLLBACK TO SAVEPOINT fixture")
            raise CaseRejected(f"fixture insert into {table} failed: {_message(exc)}") from exc
        if inserted != 1:
            raise CaseRejected(f"{table} has no row {clone_from} to clone")
        snap_cols = source.tables[table]
        (row,) = self._rows(table, snap_cols, pk, where_id=new_id)
        return {"table": table, "row": dict(zip(snap_cols, row))}


def _unique_text(column: str, new_id: int) -> str:
    if column == "iso_code":  # char(3); lower case never collides with real ISO codes
        letters = "abcdefghijklmnopqrstuvwxyz"
        n = new_id - config.FIXTURE_ID_MIN
        return "".join(letters[(n // 26**i) % 26] for i in (2, 1, 0))
    return f"Fixture {new_id}" if column == "name" else f"FIXTURE-{new_id}"
