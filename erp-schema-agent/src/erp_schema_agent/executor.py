"""Execute already-validated SQL under the read-only role, with a row cap and timeout.

This module never connects as anything but ``erp_schema_agent_ro``. Every call opens
a fresh READ ONLY transaction, sets the statement timeout again (in case the role
default was changed), fetches at most ``MAX_ROWS`` rows and rolls back.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg

from . import limits, settings


class QueryError(RuntimeError):
    """The database rejected or failed the query; the message is safe to show the model."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def run_readonly(query: str, *, max_rows: int = limits.MAX_ROWS) -> QueryResult:
    """Run one statement as the read-only role. Raises ``QueryError`` on any failure."""
    max_rows = min(max_rows, limits.MAX_ROWS)
    started = time.monotonic()
    try:
        with psycopg.connect(settings.ro_dsn(), connect_timeout=limits.CONNECT_TIMEOUT_S) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(limits.STATEMENT_TIMEOUT_MS)}")
                cur.execute(query)
                if cur.description is None:
                    columns, raw = [], []
                else:
                    columns = [d.name for d in cur.description]
                    raw = cur.fetchmany(max_rows + 1)
            conn.rollback()
    except psycopg.errors.QueryCanceled as exc:
        raise QueryError(f"query cancelled after the {limits.STATEMENT_TIMEOUT_MS} ms statement timeout") from exc
    except psycopg.Error as exc:
        diag = getattr(exc, "diag", None)
        message = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
        raise QueryError(message.splitlines()[0] if message else type(exc).__name__) from exc
    rows = [[to_json_value(v) for v in row] for row in raw[:max_rows]]
    return QueryResult(
        columns=columns,
        rows=rows,
        truncated=len(raw) > max_rows,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
