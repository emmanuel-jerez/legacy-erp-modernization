"""Seam: the whole LangGraph workflow, with ScriptedModelClient standing in for the model.

Needs the sandbox database (the oracle); skips without it. The scripted responses play a
model that first ignores the costing flag and then fixes it, or one that never converges.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from erp_sandbox.llm import ScriptedModelClient

from erp_migration_agent.graph import Deps, build_graph, memory_checkpointer, resume, sqlite_checkpointer, start

pytestmark = pytest.mark.integration

SPEC = {
    "summary": "Round an amount to the target currency's standard or costing precision.",
    "inputs": [
        {"name": "p_amount", "pg_type": "numeric", "meaning": "amount", "null_behavior": "returned as is"},
        {"name": "p_curto_id", "pg_type": "numeric", "meaning": "currency", "null_behavior": "amount returned"},
        {"name": "p_costing", "pg_type": "character varying", "meaning": "'Y' for costing", "null_behavior": "standard"},
    ],
    "output": {"pg_type": "numeric", "meaning": "rounded amount", "null_behavior": "NULL amount gives NULL"},
    "branches": [{"condition": "currency not found", "behavior": "amount unchanged"}],
    "table_dependencies": [{"table": "c_currency", "columns": ["stdprecision", "costingprecision"], "purpose": "precision"}],
    "null_semantics": "NULL amount or currency returns the amount.",
    "rounding_semantics": "ROUND half away from zero.",
    "date_time_semantics": "none",
    "edge_cases": ["ties", "negative tiny amounts"],
    "porting_risks": ["banker's rounding", "negative zero"],
}
EDGES = {
    "cases": [
        {"name": "tie", "rationale": "half away from zero", "args": ["1.005", "100", "N"], "fixtures": []},
        {"name": "tiny negative", "rationale": "no negative zero", "args": ["-0.001", "100", "N"], "fixtures": []},
        {"name": "null amount", "rationale": "NULL passes through", "args": [None, "100", "Y"], "fixtures": []},
        {"name": "unknown currency", "rationale": "unchanged", "args": ["1.23456", "999", "N"], "fixtures": []},
        {"name": "precision 3 fixture", "rationale": "fixture currency", "args": ["2.0005", "990001", "N"],
         "fixtures": [{"table": "c_currency", "id": 990001, "clone_from": 100,
                       "set": [{"column": "stdprecision", "value": "3"}]}]},
        {"name": "bad", "rationale": "rejected by PostgreSQL", "args": ["x", "100", "N"], "fixtures": []},
    ]
}
_BODY = '''from decimal import Decimal, ROUND_HALF_UP


def currencyround(p_amount, p_curto_id, p_costing, *, data):
    if p_amount is None or p_curto_id is None:
        return p_amount
    rows = data.select("c_currency", c_currency_id=p_curto_id)
    if not rows:
        return p_amount
    precision = {precision}
    result = p_amount.quantize(Decimal(1).scaleb(-int(precision)), rounding=ROUND_HALF_UP)
    return result.copy_abs() if result == 0 else result
'''
IGNORES_COSTING = _BODY.format(precision='rows[0]["stdprecision"]')
CORRECT = _BODY.format(precision='rows[0]["costingprecision"] if p_costing == "Y" else rows[0]["stdprecision"]')


@pytest.fixture
def deps(oracle, tmp_path):
    def make(responses):
        model = ScriptedModelClient(responses)
        return model, Deps(model=model, runs_dir=tmp_path / "runs")
    return make


def held_out_ids(state):
    return [c["id"] for c in state["cases"] if c["split"] == "held_out"]


def test_repair_converges_and_durable_approval_publishes_runnable_tests(deps, tmp_path):
    model, d = deps([SPEC, EDGES, {"module_source": IGNORES_COSTING, "notes": ""},
                     {"diagnosis": "costing flag ignored", "module_source": CORRECT}])
    out = tmp_path / "migrated"
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        state = start(build_graph(d, saver), "currencyround", run_id="t-1", out_dir=out)
    assert state["status"] == "awaiting_approval"
    assert state["iteration"] == 1 and state["history"][0]["visible_matched"] < state["history"][0]["visible_total"]
    assert state["held_out_report"]["passed"] and state["visible_report"]["passed"]
    assert any(r["name"] == "bad" for r in state["rejected"])
    assert not out.exists()  # nothing is written before a human decides

    prompts = "\n".join(c["prompt"] for c in model.calls)
    assert held_out_ids(state) and not any(cid in prompts for cid in held_out_ids(state))

    # A new checkpointer connection stands in for a separate `approve` process.
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        final = resume(build_graph(d, saver), "t-1", approved=True, approver="reviewer")
    assert final["status"] == "published"
    target = out / "currencyround"
    approval = json.loads((target / "approval.json").read_text())
    assert approval["approved"] and approval["approver"] == "reviewer" and not approval["auto"]
    assert approval["parity"]["held_out"]["passed"]
    assert (target / "currencyround.py").read_text() == CORRECT
    assert list(target.glob("parity-report-*.md")) and (target / "audit.jsonl").exists()

    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(target)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 0, run.stdout[-2000:]
    assert f"{2 * len(state['cases'])} passed" in run.stdout


def test_a_port_that_never_converges_stops_after_three_repairs_and_writes_nothing(deps, tmp_path):
    wrong = "def currencyround(p_amount, p_curto_id, p_costing, *, data):\n    return p_amount\n"
    repairs = [{"diagnosis": "still unsure", "module_source": wrong}] * 3
    _, d = deps([SPEC, EDGES, {"module_source": wrong, "notes": ""}, *repairs])
    state = start(build_graph(d, memory_checkpointer()), "currencyround", run_id="t-2", out_dir=tmp_path / "m")
    assert state["status"] == "not_converged"
    assert state["iteration"] == 3 and len(state["history"]) == 4
    assert not state["held_out_report"]["passed"]
    assert not (tmp_path / "m").exists()


def test_a_failed_publish_keeps_the_decision_and_can_be_retried(deps, tmp_path):
    _, d = deps([SPEC, EDGES, {"module_source": CORRECT, "notes": ""}])
    out = tmp_path / "blocked"
    out.write_text("a file where the output directory should be")
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = build_graph(d, saver)
        assert start(graph, "currencyround", run_id="t-3", out_dir=out)["status"] == "awaiting_approval"
        with pytest.raises(OSError):
            resume(graph, "t-3", approved=True, approver="reviewer")
    decision = json.loads((tmp_path / "runs" / "t-3" / "decision.json").read_text())
    assert decision["approved"] and decision["approver"] == "reviewer"

    out.unlink()
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        final = resume(build_graph(d, saver), "t-3", approved=True, approver="reviewer")
    assert final["status"] == "published"
    assert json.loads((out / "currencyround" / "approval.json").read_text())["approver"] == "reviewer"


def test_publish_never_replaces_a_folder_it_did_not_generate(deps, tmp_path):
    _, d = deps([SPEC, EDGES, {"module_source": CORRECT, "notes": ""}])
    out = tmp_path / "migrated"
    (out / "currencyround").mkdir(parents=True)
    (out / "currencyround" / "NOTES.md").write_text("hand-written")
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as saver:
        graph = build_graph(d, saver)
        start(graph, "currencyround", run_id="t-4", out_dir=out)
        with pytest.raises(RuntimeError, match="refusing to replace"):
            resume(graph, "t-4", approved=True, approver="reviewer")
    assert [p.name for p in out.iterdir()] == ["currencyround"]  # no staging or backup left behind
    assert (out / "currencyround" / "NOTES.md").read_text() == "hand-written"
