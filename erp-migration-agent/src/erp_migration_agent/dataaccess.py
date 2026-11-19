"""The explicit data-access interface a port uses instead of SQL.

A port never opens a database connection. It receives a ``DataAccess`` object holding a
snapshot of exactly the tables and columns the legacy source reads, plus any fixture rows
the case inserted, and the session time zone the oracle ran with.

Snapshots are plain JSON so they can be committed next to a migrated module and loaded
by its characterization tests. This module is imported by the port subprocess, so it uses
the standard library only.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any, Protocol

from . import pgtypes


class DataAccess(Protocol):
    session_tz: tzinfo

    def select(self, table: str, **equals: Any) -> list[dict[str, Any]]: ...


class SnapshotData:
    """``DataAccess`` over an in-memory snapshot.

    ``select("c_currency", c_currency_id=100)`` returns copies of the matching rows in
    primary-key order. Table and column names are lower case. Equality follows SQL: a
    ``None`` filter value matches nothing, as ``col = NULL`` does.
    """

    def __init__(self, tables: dict[str, dict[str, Any]], session_tz: tzinfo) -> None:
        self.session_tz = session_tz
        self._columns: dict[str, list[str]] = {}
        self._rows: dict[str, list[dict[str, Any]]] = {}
        for name, table in tables.items():
            columns = dict(table["columns"])
            names = list(columns)
            rows = [
                {col: pgtypes.decode(columns[col], value, session_tz) for col, value in zip(names, raw)}
                for raw in table["rows"]
            ]
            pk = names[0]
            rows.sort(key=lambda r: (r[pk] is None, r[pk]))
            self._columns[name] = names
            self._rows[name] = rows

    @classmethod
    def from_snapshot(
        cls, snapshot: dict[str, Any], fixtures: list[dict[str, Any]] | None = None
    ) -> SnapshotData:
        """Build from a snapshot document, appending a case's fixture rows to their tables."""
        tables = {
            name: {"columns": t["columns"], "rows": list(t["rows"])}
            for name, t in snapshot["tables"].items()
        }
        for fixture in fixtures or []:
            target = tables[fixture["table"]]
            cols = list(target["columns"])
            target["rows"].append([fixture["row"].get(c) for c in cols])
        return cls(tables, pgtypes.zone(snapshot["session_tz"]))

    @property
    def tables(self) -> list[str]:
        return sorted(self._rows)

    def select(self, table: str, **equals: Any) -> list[dict[str, Any]]:
        name = table.lower()
        if name not in self._rows:
            raise KeyError(f"table {table!r} is not in the snapshot; available: {self.tables}")
        filters = {k.lower(): v for k, v in equals.items()}
        unknown = set(filters) - set(self._columns[name])
        if unknown:
            raise KeyError(f"unknown column(s) {sorted(unknown)} for {name}; columns: {self._columns[name]}")
        out = []
        for row in self._rows[name]:
            if all(v is not None and row[c] is not None and row[c] == v for c, v in filters.items()):
                out.append(dict(row))
        return out
