"""The migration workflow as a LangGraph state machine.

extract -> analyze -> characterize -> port -> check -(mismatch, < 3 repairs)-> repair -> check
                                               \\-(visible 100% or out of repairs)-> verify
verify -(held-out 100%)-> approval (durable human interrupt) -(approved)-> publish

The model proposes (spec, edge cases, module source); deterministic code decides
(oracle, guard, runner, parity). State holds only JSON-compatible values so the SQLite
checkpointer can persist a run paused at the approval gate across processes.
"""

from __future__ import annotations

import json
import operator
import secrets
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

from erp_sandbox.llm import ModelClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import config, prompts
from .artifacts import decision_label, publish
from .audit import AuditLog
from .characterize import edge_cases, sample_cases, split_cases
from .extract import LegacySource
from .oracle import Oracle
from .parity import evaluate
from .runner import run_port
from .targets import TARGETS


class MigrationState(TypedDict, total=False):
    run_id: str
    function: str
    out_dir: str
    auto_approve: bool
    evaluate_only: bool
    source: dict[str, Any]
    snapshot_path: str
    spec: dict[str, Any]
    cases: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    module_source: str
    iteration: int
    visible_report: dict[str, Any]
    held_out_report: dict[str, Any]
    history: list[dict[str, Any]]
    status: str
    decision: dict[str, Any]
    artifacts: list[str]
    calls: Annotated[list[dict[str, Any]], operator.add]
    diagnoses: Annotated[list[str], operator.add]


Reporter = Callable[[str, str], None]


@dataclass
class Deps:
    model: ModelClient
    runs_dir: Path = config.RUNS_DIR
    reporter: Reporter = field(default=lambda stage, msg: None)
    open_oracle: Callable[[], AbstractContextManager[Oracle]] = Oracle.open


def new_run_id(function: str) -> str:
    return f"{function}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


def _summary(report: dict[str, Any]) -> str:
    if report.get("runner_failure"):
        return f"0/{report['total']} ({report['runner_failure'][:120]})"
    return f"{report['matched']}/{report['total']}"


def build_graph(deps: Deps, checkpointer: Any) -> Any:
    def audit(state: MigrationState) -> AuditLog:
        return AuditLog(deps.runs_dir / state["run_id"] / "audit.jsonl")

    def load(state: MigrationState) -> tuple[LegacySource, dict[str, Any]]:
        snapshot = json.loads(Path(state["snapshot_path"]).read_text(encoding="utf-8"))
        return LegacySource.from_dict(state["source"]), snapshot

    def ask(state: MigrationState, stage: str, prompt: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        label = f"{state['function']}:{stage}"
        result = deps.model.complete_json(system=prompts.SYSTEM, prompt=prompt, schema=schema, label=label)
        audit(state).proposal(stage, label, result)
        call = {
            "label": label,
            "model": result.model,
            "effort": result.effort,
            "from_cassette": result.from_cassette,
            "duration_ms": result.duration_ms or 0,
            "cost_usd_estimate": result.cost_usd_estimate or 0.0,
            "usage": result.usage,
        }
        return result.data, call

    def extract_node(state: MigrationState) -> dict[str, Any]:
        with deps.open_oracle() as oracle:
            source = oracle.extract(state["function"])
            snapshot = oracle.snapshot(source)
        run_dir = deps.runs_dir / state["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "snapshot.json"
        path.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
        fn = source.function
        deps.reporter("extract", f"adempiere.{fn.name}({fn.identity_args}) -> {fn.return_type}; "
                      f"{len(source.helpers)} helper function(s); tables: {', '.join(source.tables) or 'none'}")
        if source.db_default_timezone != config.SESSION_TIMEZONE:
            deps.reporter("extract", f"note: database default TimeZone is {source.db_default_timezone}; "
                          f"the oracle pins {config.SESSION_TIMEZONE}")
        audit(state).verdict("extract", {"function": fn.name, "helpers": [h.name for h in source.helpers],
                                         "tables": source.tables, "notes": source.notes})
        return {"source": source.as_dict(), "snapshot_path": str(path), "status": "running"}

    def analyze_node(state: MigrationState) -> dict[str, Any]:
        source, snapshot = load(state)
        spec, call = ask(state, "analyze", prompts.analyze_prompt(source, snapshot), prompts.ANALYZE_SCHEMA)
        deps.reporter("analyze", f"{len(spec['branches'])} branches, {len(spec['edge_cases'])} edge-case ideas, "
                      f"{len(spec['porting_risks'])} porting risks")
        return {"spec": spec, "calls": [call]}

    def characterize_node(state: MigrationState) -> dict[str, Any]:
        source, snapshot = load(state)
        proposals, call = ask(state, "edge_cases", prompts.edge_prompt(source, snapshot, state["spec"]), prompts.EDGE_SCHEMA)
        with deps.open_oracle() as oracle:
            sampled, rejected_samples = sample_cases(oracle, source, TARGETS[state["function"]])
            edges, rejected_edges = edge_cases(oracle, source, proposals["cases"], sampled)
        rejected = [{"source": "sampled", **r} for r in rejected_samples] + [{"source": "edge", **r} for r in rejected_edges]
        cases = split_cases(sampled + edges)
        visible = sum(c["split"] == "visible" for c in cases)
        deps.reporter("characterize", f"{len(sampled)} sampled + {len(edges)} edge cases "
                      f"({len(rejected)} rejected); {visible} visible, {len(cases) - visible} held out")
        if len(cases) < 30:
            deps.reporter("characterize", f"warning: {len(cases)} cases is below the contract's target of 30")
        audit(state).verdict("characterize", {"sampled": len(sampled), "edge": len(edges), "rejected": rejected,
                                              "visible": visible, "held_out": len(cases) - visible})
        return {"cases": cases, "rejected": rejected, "calls": [call]}

    def port_node(state: MigrationState) -> dict[str, Any]:
        source, snapshot = load(state)
        out, call = ask(state, "port", prompts.port_prompt(source, snapshot, state["spec"], state["cases"]),
                        prompts.PORT_SCHEMA)
        deps.reporter("port", f"module proposed ({len(out['module_source'].splitlines())} lines)")
        return {"module_source": out["module_source"], "iteration": 0, "history": [], "calls": [call]}

    def run_split(state: MigrationState, split: str) -> dict[str, Any]:
        source, snapshot = load(state)
        fn = source.function
        cases = [c for c in state["cases"] if c["split"] == split]
        outcome = run_port(state["module_source"], fn.name, fn.arg_types, cases, snapshot)
        return evaluate(split, fn.return_type, cases, outcome).as_dict()

    def check_node(state: MigrationState) -> dict[str, Any]:
        report = run_split(state, "visible")
        it = state["iteration"]
        deps.reporter("parity", f"iteration {it}: visible {_summary(report)}")
        audit(state).verdict("parity", {"iteration": it, "split": "visible", "report": report,
                                        "module_source": state["module_source"]})
        history = [*state.get("history", []), {"iteration": it, "visible_matched": report["matched"],
                                                "visible_total": report["total"],
                                                "runner_failure": report["runner_failure"]}]
        return {"visible_report": report, "history": history}

    def repair_node(state: MigrationState) -> dict[str, Any]:
        source, snapshot = load(state)
        prompt = prompts.repair_prompt(source, snapshot, state["spec"], state["module_source"],
                                       state["visible_report"], state["cases"])
        out, call = ask(state, f"repair_{state['iteration'] + 1}", prompt, prompts.REPAIR_SCHEMA)
        deps.reporter("repair", f"iteration {state['iteration'] + 1}: {out['diagnosis'][:160]}")
        return {"module_source": out["module_source"], "iteration": state["iteration"] + 1, "calls": [call],
                "diagnoses": [out["diagnosis"]]}

    def verify_node(state: MigrationState) -> dict[str, Any]:
        report = run_split(state, "held_out")
        accepted = state["visible_report"]["passed"] and report["passed"]
        deps.reporter("verify", f"held-out {_summary(report)} -> {'ACCEPTED' if accepted else 'NOT CONVERGED'}")
        audit(state).verdict("verify", {"split": "held_out", "report": report, "accepted": accepted})
        return {"held_out_report": report, "status": "accepted" if accepted else "not_converged"}

    def approval_node(state: MigrationState) -> dict[str, Any]:
        if state.get("auto_approve"):
            decision = {"approved": True, "approver": "auto-approve flag (not a human review)", "note": "", "auto": True}
        else:
            answer = interrupt({"run_id": state["run_id"], "function": state["function"],
                                "visible": _summary(state["visible_report"]),
                                "held_out": _summary(state["held_out_report"])})
            decision = {"approved": bool(answer.get("approved")), "approver": str(answer.get("approver", "")),
                        "note": str(answer.get("note", "")), "auto": False}
        decision["decided_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        audit(state).verdict("approval", decision)
        # The decision is durable on its own, even if publishing later fails and is retried.
        record = deps.runs_dir / state["run_id"] / "decision.json"
        record.write_text(json.dumps({"function": state["function"], "run_id": state["run_id"], **decision}, indent=2) + "\n",
                          encoding="utf-8")
        deps.reporter("approval", f"{decision_label(decision)} by {decision['approver']} at {decision['decided_at']}")
        return {"decision": decision, "status": "approved" if decision["approved"] else "rejected"}

    def publish_node(state: MigrationState) -> dict[str, Any]:
        audit(state).verdict("publish", {"out_dir": state["out_dir"], "function": state["function"]})
        paths = publish(state, Path(state["out_dir"]), deps.runs_dir / state["run_id"])
        deps.reporter("publish", f"wrote {len(paths)} files to {state['out_dir']}/{state['function']}/")
        return {"artifacts": paths, "status": "published"}

    def after_check(state: MigrationState) -> str:
        if state["visible_report"]["passed"]:
            return "verify"
        return "repair" if state["iteration"] < config.MAX_REPAIR_ITERATIONS else "verify"

    def after_verify(state: MigrationState) -> str:
        if state["status"] != "accepted" or state.get("evaluate_only"):
            return END
        return "approval"

    def after_approval(state: MigrationState) -> str:
        return "publish" if state["status"] == "approved" else END

    g = StateGraph(MigrationState)
    for name, node in [("extract", extract_node), ("analyze", analyze_node), ("characterize", characterize_node),
                       ("port", port_node), ("check", check_node), ("repair", repair_node), ("verify", verify_node),
                       ("approval", approval_node), ("publish", publish_node)]:
        g.add_node(name, node)
    g.add_edge(START, "extract")
    g.add_edge("extract", "analyze")
    g.add_edge("analyze", "characterize")
    g.add_edge("characterize", "port")
    g.add_edge("port", "check")
    g.add_conditional_edges("check", after_check, ["verify", "repair"])
    g.add_edge("repair", "check")
    g.add_conditional_edges("verify", after_verify, ["approval", END])
    g.add_conditional_edges("approval", after_approval, ["publish", END])
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)


@contextmanager
def sqlite_checkpointer(path: Path) -> Iterator[Any]:
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    try:
        yield SqliteSaver(conn)
    finally:
        conn.close()


def memory_checkpointer() -> Any:
    return InMemorySaver()


def _thread(run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": run_id}}


def start(graph: Any, function: str, *, run_id: str, out_dir: Path, auto_approve: bool = False,
          evaluate_only: bool = False) -> dict[str, Any]:
    if function not in TARGETS:
        raise KeyError(f"unknown target {function!r}; choose from {sorted(TARGETS)}")
    graph.invoke({"run_id": run_id, "function": function, "out_dir": str(out_dir), "auto_approve": auto_approve,
                  "evaluate_only": evaluate_only, "calls": [], "diagnoses": []}, _thread(run_id))
    return run_state(graph, run_id)


def resume(graph: Any, run_id: str, *, approved: bool, approver: str, note: str = "") -> dict[str, Any]:
    """Answer the approval gate. An approved run whose publish step failed is retried instead."""
    state = graph.get_state(_thread(run_id))
    if not state.interrupts:
        if "publish" in state.next and approved:
            graph.invoke(None, _thread(run_id))  # retry the failed publish with the recorded decision
            return run_state(graph, run_id)
        raise RuntimeError(f"run {run_id} is not waiting for approval (status: {state.values.get('status', 'unknown')})")
    graph.invoke(Command(resume={"approved": approved, "approver": approver, "note": note}), _thread(run_id))
    return run_state(graph, run_id)


def run_state(graph: Any, run_id: str) -> dict[str, Any]:
    state = graph.get_state(_thread(run_id))
    values = dict(state.values)
    if state.interrupts:
        values["status"] = "awaiting_approval"
    return values
