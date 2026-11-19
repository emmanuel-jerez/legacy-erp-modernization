"""The MCP server over real stdio, driven by the official SDK client."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

from erp_schema_agent import settings
from erp_schema_agent.limits import MAX_ROWS

QUESTION = "Which 5 customers had the highest booked revenue in Q3 2026?"  # has committed cassettes


def _params() -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if k.startswith(("ERP_", "SCHEMA_AGENT_")) or k in ("PATH", "HOME")}
    env["ERP_LLM_MODE"] = "replay"
    env["SCHEMA_AGENT_CASSETTES"] = str(settings.CASSETTE_DIR)
    return StdioServerParameters(command=sys.executable, args=["-m", "erp_schema_agent.mcp_server"], env=env)


def _call(tool: str, args: dict) -> tuple[bool, object]:
    async def go():
        async with Client(_params()) as client:
            result = await client.call_tool(tool, args)
            text = result.content[0].text if result.content else ""
            return result.is_error, (result.structured_content or text) if not result.is_error else text

    return anyio.run(go)


# Every project module the server may load, including what ask imports lazily.
# Write-capable code (erp_schema_agent.roles, erp_sandbox.db/seed/cli) must not appear.
ALLOWED_MODULES = {
    "erp_schema_agent", "erp_schema_agent.settings", "erp_schema_agent.limits", "erp_schema_agent.policy",
    "erp_schema_agent.executor", "erp_schema_agent.guardrails", "erp_schema_agent.mcp_server",
    "erp_schema_agent.retrieval", "erp_schema_agent.retrieval.catalog", "erp_schema_agent.retrieval.search",
    "erp_schema_agent.agent", "erp_schema_agent.agent.graph", "erp_schema_agent.agent.model",
    "erp_schema_agent.agent.prompts", "erp_schema_agent.agent.grounding",
    "erp_sandbox", "erp_sandbox.config", "erp_sandbox.llm",
}


def test_server_loads_only_read_only_project_code():
    probe = (
        "import sys, json, erp_schema_agent.mcp_server, erp_schema_agent.agent.graph, "
        "erp_schema_agent.agent.model; "
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in ('erp_schema_agent', 'erp_sandbox'))))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = set(json.loads(out.stdout))
    assert loaded <= ALLOWED_MODULES, loaded - ALLOWED_MODULES
    assert "erp_schema_agent.roles" not in loaded


def test_lists_four_read_only_tools():
    async def go():
        async with Client(_params()) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"search_schema", "describe_table", "run_readonly_query", "ask"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert f"{MAX_ROWS} rows" in tools["run_readonly_query"].description


def test_search_and_describe_use_the_committed_catalog():
    error, hits = _call("search_schema", {"query": "storage on hand quantity"})
    assert not error
    assert "m_storageonhand" in [h["table"] for h in hits["result"]][:3]
    error, text = _call("describe_table", {"table": "M_Replenish"})
    assert not error and "level_min" in text["result"]
    error, text = _call("describe_table", {"table": "pg_shadow"})
    assert error and "unknown table" in text


@pytest.mark.integration
def test_query_tool_applies_the_guardrails(readonly_db):
    error, result = _call("run_readonly_query", {"sql": "SELECT documentno FROM c_order"})
    assert not error
    assert result["row_count"] == MAX_ROWS
    assert result["limit_applied"] == "injected"
    assert result["executed_sql"].endswith(f"LIMIT {MAX_ROWS}")
    error, text = _call("run_readonly_query", {"sql": "SELECT 1; DROP TABLE c_order"})
    assert error and "blocked by the SQL guardrails" in text
    error, text = _call("run_readonly_query", {"sql": "SELECT password FROM ad_user"})
    assert error and "sensitive column" in text


@pytest.mark.integration
def test_ask_runs_the_full_agent_from_cassettes(readonly_db):
    error, result = _call("ask", {"question": QUESTION})
    assert not error, result
    assert result["status"] == "answered"
    assert result["row_count"] == 5
    assert all(f["grounded"] for f in result["figures"])
    json.dumps(result)
