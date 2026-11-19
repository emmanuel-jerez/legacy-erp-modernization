"""Adapter behavior at its seam: the `claude -p` process boundary, faked with a script."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from erp_sandbox.llm import (
    CassetteMiss,
    ClaudeCodeClient,
    ModelError,
    ScriptedModelClient,
    UsageTally,
    request_key,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def fake_claude(tmp_path: Path, payload: dict, exit_code: int = 0) -> Path:
    """A stand-in CLI that records its argv/stdin/cwd and prints a canned result."""
    script = tmp_path / "fake-claude"
    log = tmp_path / "calls.jsonl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
        "'cwd': os.getcwd()}) + '\\n')\n"
        f"print(json.dumps({payload!r}))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def calls(tmp_path: Path) -> list[dict]:
    log = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


OK = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"answer": 42},
      "duration_ms": 1234, "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 7}}


def test_record_then_replay_hits_cassette_without_calling_cli(tmp_path):
    cli = fake_claude(tmp_path, OK)
    cassettes = tmp_path / "cassettes"
    live = ClaudeCodeClient(cassette_dir=cassettes, mode="record", claude_bin=str(cli))
    first = live.complete_json(system="sys", prompt="q", schema=SCHEMA, label="t")
    assert first.data == {"answer": 42} and not first.from_cassette

    replay = ClaudeCodeClient(cassette_dir=cassettes, mode="replay", claude_bin="/nonexistent")
    second = replay.complete_json(system="sys", prompt="q", schema=SCHEMA, label="t")
    assert second.data == {"answer": 42} and second.from_cassette
    assert len(calls(tmp_path)) == 1


def test_cli_is_isolated_and_receives_prompt_on_stdin(tmp_path):
    cli = fake_claude(tmp_path, OK)
    ClaudeCodeClient(cassette_dir=tmp_path / "c", mode="live", claude_bin=str(cli)).complete_json(
        system="SYSTEM", prompt="PROMPT", schema=SCHEMA)
    (call,) = calls(tmp_path)
    argv = call["argv"]
    assert call["stdin"] == "PROMPT"
    assert "--system-prompt-file" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--effort") + 1] == "medium"
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv)
    assert list(Path(call["cwd"]).iterdir()) == []


def test_replay_miss_raises(tmp_path):
    client = ClaudeCodeClient(cassette_dir=tmp_path, mode="replay")
    with pytest.raises(CassetteMiss):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)


def test_schema_violation_is_an_error_not_data(tmp_path):
    bad = {**OK, "structured_output": {"answer": "forty-two"}}
    client = ClaudeCodeClient(cassette_dir=tmp_path / "c", mode="live", max_attempts=1,
                              claude_bin=str(fake_claude(tmp_path, bad)))
    with pytest.raises(ModelError, match="fails schema"):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert not (tmp_path / "c").exists()


def test_missing_structured_output_is_reported_as_refusal(tmp_path):
    refused = {"type": "result", "subtype": "success", "is_error": False, "result": "I can't help with that."}
    client = ClaudeCodeClient(cassette_dir=tmp_path / "c", mode="live", max_attempts=1,
                              claude_bin=str(fake_claude(tmp_path, refused)))
    with pytest.raises(ModelError, match="refusal"):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)


def test_scripted_client_and_tally():
    client = ScriptedModelClient([{"answer": 1}])
    tally = UsageTally()
    tally.add(client.complete_json(system="s", prompt="p", schema=SCHEMA))
    assert tally.as_dict()["calls"] == 1
    with pytest.raises(ModelError):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)


def test_corrupt_cassette_is_a_model_error(tmp_path):
    client = ClaudeCodeClient(cassette_dir=tmp_path, mode="replay")
    key = request_key(client.model, client.effort, "s", "p", SCHEMA)
    (tmp_path / f"{key[:32]}.json").write_text("{truncated")
    with pytest.raises(ModelError, match="unreadable cassette"):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)


def test_large_system_prompt_goes_through_a_file(tmp_path):
    cli = fake_claude(tmp_path, OK)
    big = "x" * 300_000  # larger than the single-argument limit on Linux
    ClaudeCodeClient(cassette_dir=tmp_path / "c", mode="live", claude_bin=str(cli)).complete_json(
        system=big, prompt="p", schema=SCHEMA)
    (call,) = calls(tmp_path)
    assert "--system-prompt" not in call["argv"]
    system_file = Path(call["argv"][call["argv"].index("--system-prompt-file") + 1])
    assert system_file.name == "system.txt"
    assert Path(call["cwd"]) not in system_file.parents  # the CLI's working directory stays empty


def test_unrunnable_cli_is_a_model_error(tmp_path):
    not_executable = tmp_path / "claude"
    not_executable.write_text("not a program")
    client = ClaudeCodeClient(cassette_dir=tmp_path / "c", mode="live", max_attempts=1,
                              claude_bin=str(not_executable))
    with pytest.raises(ModelError, match="could not run"):
        client.complete_json(system="s", prompt="p", schema=SCHEMA)
