"""Eval harness: run every target through the workflow up to verification and report.

``migration-agent eval`` replays model calls from committed cassettes (no live calls) and
uses the local sandbox database as the oracle. ``--record`` fills missing cassettes with
live calls. Reports go to ``evals/reports/eval-report.{json,md}``.

Per function: status, held-out and visible parity, repair iterations, case counts
(sampled versus edge), model calls and usage. Non-convergence is a result, not an error.
"""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from erp_sandbox.llm import ClaudeCodeClient, ModelError

from . import config
from .artifacts import parity_summary, usage_summary
from .graph import Deps, build_graph, memory_checkpointer, start
from .targets import TARGETS

MAX_JOBS = 3  # the portfolio's cap on live model calls in flight from this repo
EVAL_RUNS = config.RUNS_DIR / "eval"


def _audit_log(function: str) -> Path:
    return EVAL_RUNS / f"eval-{function}" / "audit.jsonl"


def _mismatches(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"case_id": m["case_id"], "source": m["source"], "reason": m["reason"]} for m in report.get("mismatches", [])]


def _evaluate(function: str, mode: str) -> dict[str, Any]:
    def report(stage: str, message: str) -> None:
        print(f"[{function}:{stage}] {message}", flush=True)

    run_id = f"eval-{function}"
    deps = Deps(model=ClaudeCodeClient(cassette_dir=config.CASSETTE_DIR, mode=mode), runs_dir=EVAL_RUNS, reporter=report)
    _audit_log(function).unlink(missing_ok=True)  # one audit log per eval run
    graph = build_graph(deps, memory_checkpointer())
    target = TARGETS[function]
    base = {"function": function, "difficulty": target.difficulty, "why": target.why}
    try:
        # evaluate_only stops after verification, so nothing is ever published to out_dir.
        state = start(graph, function, run_id=run_id, out_dir=EVAL_RUNS, evaluate_only=True)
    except Exception as exc:  # noqa: BLE001 - one failing target must not discard the others

        partial = graph.get_state({"configurable": {"thread_id": run_id}}).values
        return {**base, "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "usage": usage_summary(partial.get("calls", []))}
    summary = parity_summary(state)
    held = state.get("held_out_report", {})
    return {
        **base,
        "status": "converged" if state["status"] == "accepted" else "not_converged",
        "held_out_parity": summary["held_out"],
        "visible_parity": summary["visible"],
        "repair_iterations": summary["repair_iterations"],
        "history": summary["history"],
        "cases": summary["cases"],
        "rejected_cases": state.get("rejected", []),
        "held_out_mismatches": _mismatches(held),
        "visible_mismatches": _mismatches(state.get("visible_report", {})),
        "repair_diagnoses": state.get("diagnoses", []),
        "usage": usage_summary(state.get("calls", [])),
    }


def _pct(part: dict[str, Any]) -> str:
    if not part or part.get("total") is None:
        return "n/a"
    return f"{part['matched']}/{part['total']} ({100 * part['matched'] / part['total']:.0f}%)" if part["total"] else "0/0"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Eval report: legacy-to-Python migration",
        "",
        f"- Generated: {report['generated_at']} (mode: {report['mode']})",
        f"- Model: {', '.join(report['totals']['models']) or 'n/a'}; effort: {', '.join(report['totals']['efforts']) or 'n/a'}",
        f"- Oracle: legacy PL/pgSQL functions in `{config.DB_NAME}`, TimeZone {config.SESSION_TIMEZONE}",
        "- Pass bar: 100% held-out parity (docs/metric-contract.md)",
        "",
        "| Function | Difficulty | Status | Held-out parity | Visible parity | Repairs | Cases (sampled + edge) | Model calls |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for f in report["functions"]:
        if f["status"] == "error":
            lines.append(f"| `{f['function']}` | {f['difficulty']} | error | n/a | n/a | n/a | n/a | {f['usage']['calls']} |")
            continue
        c = f["cases"]
        lines.append(
            f"| `{f['function']}` | {f['difficulty']} | {f['status'].replace('_', ' ')} | {_pct(f['held_out_parity'])} | "
            f"{_pct(f['visible_parity'])} | {f['repair_iterations']} | {c['total']} ({c['sampled']} + {c['edge']}) | "
            f"{f['usage']['calls']} |"
        )
    t = report["totals"]
    lines += [
        "",
        f"Totals: {t['calls']} model calls ({t['live_calls']} live, {t['cassette_hits']} from cassettes), "
        f"recorded model time {t['model_duration_ms'] / 1000:.0f} s, recorded cost estimate "
        f"${t['cost_usd_estimate_as_recorded']:.2f} (Claude Code client-side estimate at record time).",
        "",
    ]
    for f in report["functions"]:
        lines += [f"## `{f['function']}` ({f['difficulty']})", "", f["why"], ""]
        if f["status"] == "error":
            lines += [f"Error: {f['error']}", ""]
            continue
        c = f["cases"]
        lines.append(f"- Cases: {c['total']} ({c['sampled']} sampled, {c['edge']} edge; {c['visible']} visible, "
                     f"{c['held_out']} held out); {c['rejected']} candidate cases rejected")
        lines.append("- Visible parity by iteration: " + ", ".join(
            f"{h['iteration']}: {h['visible_matched']}/{h['visible_total']}" for h in f["history"]))
        for i, d in enumerate(f["repair_diagnoses"], 1):
            lines.append(f"- Repair {i} diagnosis: {d.strip().splitlines()[0][:300]}")
        if f["status"] == "not_converged":
            lines.append("- **Did not converge.** Remaining mismatches:")
            for m in (f["visible_mismatches"] + f["held_out_mismatches"])[:8]:
                lines.append(f"  - `{m['case_id']}` ({m['source']}): {m['reason'][:200]}")
        u = f["usage"]
        tokens = ", ".join(f"{k} {v}" for k, v in u["tokens_as_recorded"].items()) or "n/a"
        lines += [f"- Model calls: {u['calls']} ({u['live_calls']} live, {u['cassette_hits']} cassette); "
                  f"model {', '.join(u['models'])}, effort {', '.join(u['efforts'])}; recorded cost estimate "
                  f"${u['cost_usd_estimate_as_recorded']:.2f}; tokens as recorded: {tokens}", ""]
    return "\n".join(lines)


def run_eval(*, record: bool, functions: list[str], jobs: int = 1, reports_dir: Path = config.EVAL_REPORTS_DIR) -> int:
    unknown = [f for f in functions if f not in TARGETS]
    if unknown:
        raise KeyError(f"unknown target(s) {unknown}; choose from {sorted(TARGETS)}")
    mode = "record" if record else "replay"
    workers = max(1, min(jobs, MAX_JOBS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda f: _evaluate(f, mode), functions))
    totals: dict[str, Any] = {k: 0 for k in ("calls", "live_calls", "cassette_hits", "model_duration_ms")}
    cost = 0.0
    tokens: dict[str, int] = {}
    models: set[str] = set()
    efforts: set[str] = set()
    for r in results:
        u = r["usage"]
        for k in ("calls", "live_calls", "cassette_hits", "model_duration_ms"):
            totals[k] += u[k]
        cost += u["cost_usd_estimate_as_recorded"]
        for k, v in u["tokens_as_recorded"].items():
            tokens[k] = tokens.get(k, 0) + v
        models.update(u["models"])
        efforts.update(u["efforts"])
    totals.update(cost_usd_estimate_as_recorded=round(cost, 4), models=sorted(models), efforts=sorted(efforts),
                  tokens_as_recorded=dict(sorted(tokens.items())))
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "metric_contract": "docs/metric-contract.md",
        "pass_bar": "100% held-out parity",
        "totals": totals,
        "functions": results,
    }
    failed = [r["function"] for r in results if r["status"] == "error"]
    if failed:
        # Never replace the committed report with a broken run (e.g. a cassette miss after a prompt change).
        reports_dir = (EVAL_RUNS if reports_dir == config.EVAL_REPORTS_DIR else reports_dir) / "failed-report"
        print(f"\n{len(failed)} target(s) errored ({', '.join(failed)}); the committed report is left as it was.")
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Version every function's audit log (proposals and verdicts), converged or not.
    audit_dir = reports_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    for function in functions:
        copy = audit_dir / f"{function}.jsonl"
        copy.unlink(missing_ok=True)  # never leave a stale log next to a new report
        if _audit_log(function).exists():
            shutil.copyfile(_audit_log(function), copy)
    (reports_dir / "eval-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (reports_dir / "eval-report.md").write_text(_markdown(report), encoding="utf-8")
    print(f"\nWrote {reports_dir / 'eval-report.md'} and eval-report.json")
    for r in results:
        print(f"  {r['function']}: {r['status']}"
              + (f", held-out {_pct(r['held_out_parity'])}" if r["status"] != "error" else f" ({r['error'][:120]})"))
    return 1 if failed else 0
