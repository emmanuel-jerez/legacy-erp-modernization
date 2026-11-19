"""MCP server (stdio) exposing the schema agent's read-only tools.

Tools: ``search_schema``, ``describe_table``, ``run_readonly_query`` and ``ask``. Every
query goes through the same sqlglot guardrails and the same read-only role and row cap
as the agent. This module imports no write-capable code: not ``roles`` (admin role
setup) and nothing from ``erp_sandbox.db`` (database cloning); a test checks that.

Run with ``uv run schema-agent-mcp`` (or ``python -m erp_schema_agent.mcp_server``).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .executor import QueryError, run_readonly
from .guardrails import validate_sql
from .limits import ANSWER_ROWS, MAX_REPAIR_ATTEMPTS, MAX_ROWS, STATEMENT_TIMEOUT_S
from .retrieval.catalog import load_catalog
from .retrieval.search import MAX_TABLES, render_table
from .retrieval.search import search_schema as lexical_search

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

SEARCH_DESCRIPTION = (
    "Search the iDempiere ERP schema (schema adempiere, 1,000+ tables with cryptic names "
    "such as C_BPartner or M_StorageOnHand) by business words. Ranks tables with BM25 over "
    f"iDempiere's own data dictionary. Returns up to {MAX_TABLES} tables with dictionary names."
)
DESCRIBE_DESCRIPTION = (
    "Describe one adempiere table: dictionary name and description, row count, every "
    "column with type, label, description, allowed list values and foreign-key target. "
    "Credential columns are never shown."
)
QUERY_DESCRIPTION = (
    "Run one read-only PostgreSQL SELECT against the ERP. The SQL is validated first: one "
    "SELECT/WITH statement, adempiere tables only, no system catalogs, credential columns "
    "or side-effecting functions. LIMIT is injected or capped at "
    f"{MAX_ROWS} rows and the statement times out after {STATEMENT_TIMEOUT_S} seconds. It runs as a "
    "read-only database role. A rejected query returns the reason."
)
ASK_DESCRIPTION = (
    "Answer a business question about the Fernhollow Skincare data in plain English. Runs "
    "the full agent: schema retrieval, SQL generation by Claude, the same validation, "
    f"execution capped at {MAX_ROWS} rows, up to {MAX_REPAIR_ATTEMPTS} retries on errors, "
    "and an answer whose figures are checked against the rows. Returns the answer, the SQL "
    f"and up to {ANSWER_ROWS} rows. Calls Claude through the local Claude Code CLI unless a "
    "recorded response exists (ERP_LLM_MODE: replay, record or live)."
)

server = MCPServer(
    "erp-schema-agent",
    instructions="Read-only access to a local iDempiere ERP demo (fictional Fernhollow Skincare data).",
)


@server.tool(description=SEARCH_DESCRIPTION, annotations=READ_ONLY)
def search_schema(query: Annotated[str, Field(description="Business words, e.g. 'reorder point stock'")]) -> list[dict[str, Any]]:
    catalog = load_catalog()
    return [
        {"table": hit.table, "label": hit.label, "description": catalog.tables[hit.table].description,
         "rows": catalog.tables[hit.table].rows, "score": hit.score}
        for hit in lexical_search(query)
    ]


@server.tool(description=DESCRIBE_DESCRIPTION, annotations=READ_ONLY)
def describe_table(table: Annotated[str, Field(description="Table name, e.g. c_order or C_Order")]) -> str:
    catalog = load_catalog()
    name = table.strip().lower().removeprefix("adempiere.")
    if name not in catalog.tables:
        raise ToolError(f"unknown table {table!r}; use search_schema to find tables")
    return render_table(catalog.tables[name], max_described=None)


@server.tool(description=QUERY_DESCRIPTION, annotations=READ_ONLY)
def run_readonly_query(sql: Annotated[str, Field(description="One PostgreSQL SELECT statement")]) -> dict[str, Any]:
    catalog = load_catalog()
    verdict = validate_sql(sql, allowed_tables=catalog.tables, sensitive_tables=catalog.sensitive_tables)
    if not verdict.ok:
        raise ToolError(f"blocked by the SQL guardrails: {verdict.reason}")
    try:
        result = run_readonly(verdict.sql)
    except QueryError as exc:
        raise ToolError(f"database error: {exc}") from exc
    return {
        "executed_sql": verdict.sql,
        "limit_applied": verdict.limit_applied,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }


# ask reaches an outside service (Claude), so it is not closed-world.
@server.tool(description=ASK_DESCRIPTION, annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                                                       open_world_hint=True))
def ask(question: Annotated[str, Field(description="A business question in plain English")]) -> dict[str, Any]:
    from .agent.graph import run_agent
    from .agent.model import make_model

    run = run_agent(question, model=make_model())
    return {
        "status": run.status,
        "blocked_by": run.blocked_by,
        "reason": run.reason,
        "answer": run.answer,
        "caveats": run.caveats,
        "executed_sql": run.executed_sql,
        "columns": run.columns,
        "rows": run.rows[:ANSWER_ROWS],
        "row_count": len(run.rows),
        "figures": [{"label": f.label, "value": f.value, "kind": f.kind, "grounded": f.grounded}
                    for f in run.figures],
        "retrieved_tables": run.retrieved_tables,
        "repairs": run.repairs,
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING)  # stderr; stdout carries the protocol
    server.run("stdio")


if __name__ == "__main__":
    main()
