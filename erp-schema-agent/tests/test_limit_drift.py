"""Limits quoted in prompts, MCP tool metadata and docs must match ``limits.py``.

Each pattern finds a number quoted with a limit's unit ("200 rows", "200-row",
"5 seconds", "5s", "2 retries", "8,000 characters"), and the number must equal that
limit's constant. Docs therefore never quote a row count that is not a limit.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio
import pytest
from mcp import Client

from erp_schema_agent import limits, mcp_server
from erp_schema_agent.agent import prompts
from erp_schema_agent.retrieval.search import MAX_TABLES

WORDS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten".split(), start=1)}
NUM = r"\b(\d[\d,_]*|" + "|".join(WORDS) + ")"
PATTERNS = [
    (re.compile(NUM + r"[- ]rows?\b", re.I), "rows", {limits.MAX_ROWS, limits.ANSWER_ROWS}),
    # "5s" is a quote but "ones" is not: the bare "s" unit only follows digits.
    (re.compile(r"\b(\d[\d,_]*)\s*(?:s|secs?|seconds?)\b", re.I), "seconds", {limits.STATEMENT_TIMEOUT_S}),
    (re.compile(r"\b(" + "|".join(WORDS) + r")\s+(?:secs?|seconds?)\b", re.I), "seconds",
     {limits.STATEMENT_TIMEOUT_S}),
    (re.compile(NUM + r"\s*ms\b", re.I), "ms",
     {limits.STATEMENT_TIMEOUT_MS, limits.LOCK_TIMEOUT_MS, limits.IDLE_IN_TRANSACTION_TIMEOUT_MS}),
    (re.compile(NUM + r"\s+(?:retries|retry|repairs?|repair attempts?)\b", re.I), "retries",
     {limits.MAX_REPAIR_ATTEMPTS}),
    (re.compile(NUM + r"\s+characters\b", re.I), "characters", {limits.MAX_SQL_CHARS}),
    (re.compile(NUM + r"\s+connections\b", re.I), "connections", {limits.ROLE_CONNECTION_LIMIT}),
    (re.compile(r"up to " + NUM + r"\s+tables\b", re.I), "tables", {MAX_TABLES}),
]
REPO = Path(__file__).resolve().parents[1]
DOCS = ["AGENTS.md"]


def quoted_limits(text: str) -> list[tuple[int, str]]:
    found = []
    for pattern, unit, _ in PATTERNS:
        for number in pattern.findall(text):
            value = WORDS.get(number.lower()) or int(number.replace(",", "").replace("_", ""))
            found.append((value, unit))
    return found


def drift(text: str) -> list[str]:
    allowed = {unit: values for _, unit, values in PATTERNS}
    return [f"{v} {u}" for v, u in quoted_limits(text) if v not in allowed[u]]


def tool_texts() -> dict[str, str]:
    async def go():
        async with Client(mcp_server.server) as client:
            return (await client.list_tools()).tools

    texts = {"server instructions": mcp_server.server.instructions or ""}
    for tool in anyio.run(go):
        params = " ".join(p.get("description", "") for p in tool.input_schema.get("properties", {}).values())
        texts[f"tool {tool.name}"] = f"{tool.description} {params}"
    return texts


def all_texts() -> dict[str, str]:
    texts = {"generate system prompt": prompts.GENERATE_SYSTEM, "answer system prompt": prompts.ANSWER_SYSTEM}
    texts.update(tool_texts())
    for doc in DOCS:
        texts[doc] = (REPO / doc).read_text(encoding="utf-8")
    return texts


@pytest.mark.parametrize("name", sorted(all_texts()))
def test_quoted_limits_match_the_constants(name):
    assert drift(all_texts()[name]) == []


def test_the_scanner_catches_each_form_of_drift():
    text = ("capped at 500 rows, a 500-row cap, times out after 9 s or nine seconds, three retries, "
            "4 repair attempts, 9,000 characters, 7 connections, 250 ms, returns up to 25 tables")
    assert sorted(drift(text)) == sorted(["500 rows", "500 rows", "9 seconds", "9 seconds", "3 retries",
                                          "4 retries", "9000 characters", "7 connections", "250 ms",
                                          "25 tables"])
    assert drift("capped at 200 rows after 5 seconds and 2 retries") == []
    assert drift("the ones it imports, someones") == []


def test_prompts_and_tools_state_the_limits():
    tools = tool_texts()
    assert f"{limits.MAX_ROWS} rows" in prompts.GENERATE_SYSTEM
    assert f"{limits.MAX_REPAIR_ATTEMPTS} retries" in prompts.GENERATE_SYSTEM
    assert f"{limits.STATEMENT_TIMEOUT_S} seconds" in tools["tool run_readonly_query"]
    assert f"{limits.MAX_REPAIR_ATTEMPTS} retries" in tools["tool ask"]
