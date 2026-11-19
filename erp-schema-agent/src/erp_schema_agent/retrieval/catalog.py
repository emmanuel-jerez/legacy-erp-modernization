"""The schema catalog: a committed snapshot of iDempiere's data dictionary and FK graph.

``build_catalog`` reads, through the read-only role:

* ``AD_Table`` / ``AD_Column``: iDempiere's own dictionary (Name, Description, Help);
* ``AD_Tab`` / ``AD_Window``: the screens each table appears on (business vocabulary);
* ``AD_Ref_List``: the allowed values of list columns such as ``DocStatus``;
* ``information_schema`` and ``pg_constraint``: the physical columns, types and the real
  foreign-key graph (iDempiere's dictionary does not mark every FK);
* exact row counts per table in the frozen seed.

Credential columns (see ``policy.is_sensitive_column``) are left out of the catalog, so
the model never sees them. The snapshot is written to ``data/schema_catalog.json.gz``
and committed: retrieval is then deterministic, works without the database, and keeps
prompts byte-identical for cassette replay. Rebuild it with ``schema-agent build-catalog``.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from .. import settings
from ..policy import is_sensitive_column

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "schema_catalog.json.gz"
# Context budgets for the dictionary text kept per table and column (not enforced limits).
MAX_TEXT = 160
MAX_LIST_VALUES = 20


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    label: str = ""
    description: str = ""
    fk_table: str = ""
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Table:
    name: str
    ad_name: str
    kind: str  # "table" or "view"
    label: str
    description: str
    help: str
    windows: list[str]
    rows: int
    columns: list[Column]

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class Catalog:
    tables: dict[str, Table]
    fks: list[tuple[str, str, str, str]]  # (table, column, ref_table, ref_column)
    hidden_columns: dict[str, list[str]] = field(default_factory=dict)  # credential columns left out

    @property
    def sensitive_tables(self) -> frozenset[str]:
        return frozenset(t for t, cols in self.hidden_columns.items() if cols)

    def joins_between(self, tables: set[str]) -> list[tuple[str, str, str, str]]:
        return [fk for fk in self.fks if fk[0] in tables and fk[2] in tables and fk[0] != fk[2]]


def _clip(text: Any, limit: int = MAX_TEXT) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def build_catalog(conn: psycopg.Connection) -> dict[str, Any]:
    """Read the dictionary and FK graph into a JSON-ready dict (deterministic order)."""
    schema = settings.SCHEMA
    phys: dict[str, dict[str, Any]] = {}
    for table, kind in conn.execute(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s",
        (schema,),
    ):
        if kind in ("BASE TABLE", "VIEW"):
            phys[table] = {"kind": "table" if kind == "BASE TABLE" else "view", "columns": {}}
    # information_schema shows the read-only role only the columns it may read, so the
    # hidden credential columns are listed from pg_attribute instead.
    hidden: dict[str, list[str]] = {}
    for table, column in conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'p') AND a.attnum > 0 AND NOT a.attisdropped",
        (schema,),
    ):
        if table in phys and is_sensitive_column(column):
            hidden.setdefault(table, []).append(column)
    for table, column, data_type in conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (schema,),
    ):
        if table not in phys:
            continue
        if is_sensitive_column(column):
            continue
        phys[table]["columns"][column] = {"data_type": data_type}

    ad_tables = {
        row[0].lower(): row
        for row in conn.execute(
            "SELECT tablename, name, description, help, ad_table_id FROM ad_table WHERE isactive = 'Y'"
        )
    }
    table_ids = {row[4]: name for name, row in ad_tables.items()}
    windows: dict[str, set[str]] = {}
    for table_id, window, tab in conn.execute(
        "SELECT tb.ad_table_id, w.name, tb.name FROM ad_tab tb JOIN ad_window w ON w.ad_window_id = tb.ad_window_id "
        "WHERE tb.isactive = 'Y' AND w.isactive = 'Y'"
    ):
        if table_id in table_ids:
            windows.setdefault(table_ids[table_id], set()).update({window, tab})
    lists: dict[int, dict[str, str]] = {}
    for ref_id, value, name in conn.execute(
        "SELECT ad_reference_id, value, name FROM ad_ref_list WHERE isactive = 'Y' ORDER BY ad_reference_id, value"
    ):
        lists.setdefault(int(ref_id), {})[value] = name
    for table_id, column, label, description, ref_id, ref_value_id in conn.execute(
        "SELECT ad_table_id, columnname, name, description, ad_reference_id, ad_reference_value_id "
        "FROM ad_column WHERE isactive = 'Y'"
    ):
        table = table_ids.get(table_id)
        col = phys.get(table or "", {}).get("columns", {}).get(column.lower())
        if col is None:
            continue
        col["label"] = _clip(label, 60)
        col["description"] = _clip(description)
        if ref_id == 17 and ref_value_id and len(lists.get(int(ref_value_id), {})) <= MAX_LIST_VALUES:
            col["values"] = lists.get(int(ref_value_id), {})

    fks: set[tuple[str, str, str, str]] = set()
    for src, src_col, dst, dst_col in conn.execute(
        """
        SELECT cl.relname, a.attname, rcl.relname, ra.attname
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_class rcl ON rcl.oid = c.confrelid
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1]
        WHERE c.contype = 'f' AND n.nspname = %s AND array_length(c.conkey, 1) = 1
        ORDER BY 1, 2, 3, 4
        """,
        (schema,),
    ):
        if src in phys and dst in phys and src_col in phys[src]["columns"]:
            fks.add((src, src_col, dst, dst_col))
            # A few columns reference two tables; keep the first in a total order.
            phys[src]["columns"][src_col].setdefault("fk_table", dst)

    tables: dict[str, Any] = {}
    for name in sorted(phys):
        info = phys[name]
        ad = ad_tables.get(name)
        if info["kind"] == "table":
            rows = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))).fetchone()[0]
        else:
            rows = -1  # views are not counted (some are expensive)
        tables[name] = {
            "ad_name": ad[0] if ad else name,
            "kind": info["kind"],
            "label": _clip(ad[1], 80) if ad else "",
            "description": _clip(ad[2]) if ad else "",
            "help": _clip(ad[3], 300) if ad else "",
            "windows": sorted(windows.get(name, set())),
            "rows": int(rows),
            "columns": [{"name": c, **v} for c, v in info["columns"].items()],  # ordinal order
        }
    return {
        "schema": schema,
        "tables": tables,
        "fks": sorted(fks),
        "hidden_columns": {t: sorted(c) for t, c in sorted(hidden.items())},
    }


def write_catalog(data: dict[str, Any], path: Path = CATALOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(path, "wb", mtime=0) as fh:  # mtime=0: byte-identical rebuilds
        fh.write(payload)


def parse_catalog(data: dict[str, Any]) -> Catalog:
    tables = {
        name: Table(
            name=name,
            ad_name=t["ad_name"],
            kind=t["kind"],
            label=t["label"],
            description=t["description"],
            help=t["help"],
            windows=list(t["windows"]),
            rows=int(t["rows"]),
            columns=[
                Column(
                    name=v["name"],
                    data_type=v["data_type"],
                    label=v.get("label", ""),
                    description=v.get("description", ""),
                    fk_table=v.get("fk_table", ""),
                    values=dict(v.get("values", {})),
                )
                for v in t["columns"]
            ],
        )
        for name, t in data["tables"].items()
    }
    return Catalog(
        tables=tables,
        fks=[tuple(fk) for fk in data["fks"]],  # type: ignore[misc]
        hidden_columns={t: list(c) for t, c in data.get("hidden_columns", {}).items()},
    )


@lru_cache(maxsize=1)
def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return parse_catalog(json.load(fh))
