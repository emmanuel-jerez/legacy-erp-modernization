"""Eval matrix: task variants x {clean, drifted} selector maps, live against the web UI.

Each case is a real run through iDempiere's UI; success means the database check passed.
Fallback decisions replay from cassettes when the normalized snapshot (and so the prompt)
matches a recorded one, and are recorded live otherwise (``ERP_LLM_MODE=record``).

A separate guardrail probe swaps the model for a hostile test double that always proposes
a destructive control, to show the guardrail aborting the run before anything is touched.
It is reported apart from the success rate because it measures the guardrail, not the
model.

The report (JSON and Markdown) goes to ``evals/reports/``; the drafts the eval's own runs
created are deleted afterwards, by run tag.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from erp_sandbox.llm import ModelClient, ModelResult, request_key

from . import erpdb
from .runner import RUNS_DIR, RunOptions, new_run_id, run_task
from .task import load_task

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = REPO_ROOT / "tasks"
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
TASK_FILES = ("restock-1-line.json", "restock-2-lines.json", "restock-3-lines.json")
DRIFTS: tuple[str | None, ...] = (None, "drifted")


class HostileModelClient:
    """Test double for the guardrail probe: always proposes clicking a destructive control.

    It reads the snapshot in the prompt and picks the first element whose name contains
    "Delete", "Complete", "Process" or "Document Action". It never calls a model.
    """

    model = "hostile-test-double"
    effort = "n/a"
    _TARGET = re.compile(r'^\[(\d+)\] \w+ "[^"]*(Delete|Complete|Process|Document Action)[^"]*"', re.MULTILINE)

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *, system: str, prompt: str, schema: dict[str, Any], label: str = "") -> ModelResult:
        self.calls += 1
        m = self._TARGET.search(prompt)
        data = {"action": "click", "element_index": int(m.group(1)) if m else 0, "value": None,
                "reason": "probe: attempt a destructive control"}
        return ModelResult(data=data, model=self.model, effort=self.effort,
                           key=request_key(self.model, self.effort, system, prompt, schema), from_cassette=False)


@dataclass
class Case:
    task_file: str
    drift: str | None

    @property
    def name(self) -> str:
        return f"{Path(self.task_file).stem}/{self.drift or 'clean'}"


def _case_record(case: Case, log: dict[str, Any]) -> dict[str, Any]:
    t = log["totals"]
    return {
        "case": case.name,
        "task": log["task"]["name"],
        "vendor": log["task"]["vendor"],
        "lines": len(log["task"]["lines"]),
        "drift": case.drift or "clean",
        "run_id": log["run_id"],
        "db_verified": log["verification"]["passed"],
        "success": log["success"],
        "flow_error": log["flow_error"],
        "steps_run": t["steps_run"],
        "actions": t["actions"],
        "fallback_calls": t["fallback_calls"],
        "guardrail_blocks": t["guardrail_blocks"],
        "repairs": t.get("repairs", 0),
        "healed": sorted(log["healed_selectors"]),
        "duration_s": round(log["duration_ms"] / 1000, 1),
        "usage": log["usage"],
        "live_cost_usd": sum(f.get("cost_usd_estimate") or 0.0 for s in log["steps"] for f in s.get("fallback", [])
                             if f.get("from_cassette") is False),
        "documentno": (log["verification"].get("order") or {}).get("documentno"),
        "failed_checks": [c["name"] for c in log["verification"]["checks"] if not c["passed"]],
        "diagnosis": log["diagnosis"],
    }


def _sum_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals across runs. ``cost_usd_live_calls`` counts only calls made live in this eval;
    ``cost_usd_estimate_as_recorded`` repeats the recorded cost of every cassette used."""
    total: dict[str, Any] = {"calls": 0, "live_calls": 0, "cassette_hits": 0, "model_duration_ms": 0,
                             "cost_usd_estimate_as_recorded": 0.0, "cost_usd_live_calls": 0.0,
                             "models": set(), "efforts": set()}
    for r in records:
        u = r["usage"]
        for k in ("calls", "live_calls", "cassette_hits", "model_duration_ms"):
            total[k] += u.get(k, 0)
        total["cost_usd_estimate_as_recorded"] += u.get("cost_usd_estimate_as_recorded", 0.0)
        total["cost_usd_live_calls"] += r.get("live_cost_usd", 0.0)
        total["models"].update(u.get("models", []))
        total["efforts"].update(u.get("efforts", []))
    total["cost_usd_estimate_as_recorded"] = round(total["cost_usd_estimate_as_recorded"], 4)
    total["cost_usd_live_calls"] = round(total["cost_usd_live_calls"], 4)
    total["models"] = sorted(total["models"])
    total["efforts"] = sorted(total["efforts"])
    return total


def success_rate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = sum(1 for r in rows if r["success"])
    return {"passed": ok, "total": len(rows), "rate": round(ok / len(rows), 3) if rows else None}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case records into the report's summary. Pure."""
    durations = [r["duration_s"] for r in records]
    return {
        "db_verified_success": success_rate(records),
        "clean": success_rate([r for r in records if r["drift"] == "clean"]),
        "drifted": success_rate([r for r in records if r["drift"] != "clean"]),
        "fallback_calls": sum(r["fallback_calls"] for r in records),
        "guardrail_blocks": sum(r["guardrail_blocks"] for r in records),
        "repairs": sum(r["repairs"] for r in records),
        "steps_run_total": sum(r["steps_run"] for r in records),
        "duration_s_total": round(sum(durations), 1),
        "duration_s_min": min(durations) if durations else None,
        "duration_s_median": round(statistics.median(durations), 1) if durations else None,
        "duration_s_max": max(durations) if durations else None,
    }


def run_probe(runs_dir: Path, run_id: str) -> dict[str, Any]:
    """Drifted run with a hostile 'model': the guardrail must abort before any action runs."""
    spec = load_task(TASKS_DIR / TASK_FILES[0])
    client = HostileModelClient()
    result = run_task(spec, client, RunOptions(drift="drifted", runs_dir=runs_dir), run_id=run_id)
    log = result.log
    blocks = log["guardrail_blocks"]
    order = log["verification"].get("order")
    # Pass only if the destructive-control rule itself fired and nothing was created.
    passed = (
        len(blocks) == 1
        and blocks[0]["rule"] == "destructive_target"
        and (log["flow_error"] or "").startswith("GuardrailAbort")
        and order is None
    )
    return {
        "case": "guardrail-probe/drifted",
        "run_id": result.run_id,
        "passed": passed,
        "hostile_proposals": client.calls,
        "blocks": blocks,
        "flow_error": log["flow_error"],
        "order_after": order,
    }


def run_eval(client: ModelClient, *, headed: bool = False, probe: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    stamp = dt.datetime.now().astimezone()
    runs_dir = RUNS_DIR / f"eval-{stamp:%Y%m%d-%H%M%S}"
    cases = [Case(t, d) for t in TASK_FILES for d in DRIFTS]
    records: list[dict[str, Any]] = []
    run_ids: list[str] = []
    probe_result: dict[str, Any] | None = None
    cleanup_report: dict[str, Any] = {"drafts_found": 0, "deleted": 0, "errors": []}
    try:
        for case in cases:
            spec = load_task(TASKS_DIR / case.task_file)
            run_ids.append(new_run_id())
            result = run_task(spec, client, RunOptions(drift=case.drift, headed=headed, runs_dir=runs_dir),
                              run_id=run_ids[-1])
            rec = _case_record(case, result.log)
            records.append(rec)
            print(f"  {case.name:32} {'PASS' if rec['success'] else 'FAIL'}  {rec['duration_s']:5.1f}s  "
                  f"fallback {rec['fallback_calls']}  blocks {rec['guardrail_blocks']}", flush=True)
        if probe:
            run_ids.append(new_run_id())
            probe_result = run_probe(runs_dir, run_ids[-1])
            verdict = "PASS (guardrail aborted the hostile proposal)" if probe_result["passed"] else "FAIL"
            print(f"  {probe_result['case']:32} {verdict}", flush=True)
    finally:
        for run_id in run_ids:  # only this eval's own drafts, even if a case raised
            try:
                drafts, n = erpdb.cleanup(tag=erpdb.RunTag(run_id))
                cleanup_report["drafts_found"] += len(drafts)
                cleanup_report["deleted"] += n
            except Exception as exc:  # noqa: BLE001 - keep cleaning the other runs
                cleanup_report["errors"].append(f"{run_id}: {type(exc).__name__}: {exc}")

    durations = [r["duration_s"] for r in records]
    report = {
        "generated_at": stamp.isoformat(timespec="seconds"),
        "evidence_dir": str(runs_dir.relative_to(REPO_ROOT)),
        "llm_mode": getattr(client, "mode", "n/a"),
        "model": getattr(client, "model", "n/a"),
        "effort": getattr(client, "effort", "n/a"),
        "success_definition": "UI flow completed and the database check passed (draft PO, vendor, lines, tags, nothing else changed)",
        "summary": {**summarize(records), "wall_clock_s": round(time.monotonic() - started, 1)},
        "usage": _sum_usage(records),
        "cases": records,
        "failures": [{"case": r["case"], "diagnosis": r["diagnosis"]} for r in records if not r["success"]],
        "guardrail_probe": probe_result,
        "cleanup": cleanup_report,
    }
    return report


def write_report(report: dict[str, Any], reports_dir: Path = REPORTS_DIR) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eval-{report['generated_at'][:10]}-{report['llm_mode']}"
    if (reports_dir / f"{stem}.json").exists() or (reports_dir / f"{stem}.md").exists():
        stem += "-" + report["generated_at"][11:19].replace(":", "")  # never overwrite a report
    json_path = reports_dir / f"{stem}.json"
    md_path = reports_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    u = report["usage"]

    def pct(r: dict[str, Any]) -> str:
        return f"{r['passed']}/{r['total']} ({r['rate']:.0%})" if r["total"] else "n/a"

    lines = [
        f"# erp-ui-operator eval, {report['generated_at'][:10]}",
        "",
        f"Generated {report['generated_at']}. Model `{report['model']}`, effort `{report['effort']}`, "
        f"cassette mode `{report['llm_mode']}`. Live against the local iDempiere 12 web UI.",
        "",
        f"Success means: {report['success_definition']}.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Database-verified success | {pct(s['db_verified_success'])} |",
        f"| Clean selector map | {pct(s['clean'])} |",
        f"| Drifted selector map | {pct(s['drifted'])} |",
        f"| LLM fallback calls | {s['fallback_calls']} |",
        f"| Guardrail blocks (matrix) | {s['guardrail_blocks']} |",
        f"| Database read-back repairs | {s['repairs']} |",
        f"| Steps run (total) | {s['steps_run_total']} |",
        f"| Run duration (total; min / median / max) | {s['duration_s_total']} s; {s['duration_s_min']} / "
        f"{s['duration_s_median']} / {s['duration_s_max']} s |",
        f"| Model calls (live / cassette) | {u['calls']} ({u['live_calls']} / {u['cassette_hits']}) |",
        f"| Cost of live calls in this eval | ${u.get('cost_usd_live_calls', 0.0):.4f} |",
        f"| Recorded cost of all cassettes used | ${u['cost_usd_estimate_as_recorded']:.4f} |",
        f"| Model time as recorded | {u['model_duration_ms'] / 1000:.1f} s |",
        "",
        "Cost and model time are Claude Code's client-side estimates stored with each response;",
        "replayed calls cost nothing now and only repeat what was recorded.",
        "",
        "## Cases",
        "",
        "| Case | Vendor | Lines | DB verified | Steps | Fallback calls | Healed selectors | Blocks | Duration | Order |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["cases"]:
        lines.append(
            f"| {r['case']} | {r['vendor']} | {r['lines']} | {'yes' if r['db_verified'] else 'NO'} | {r['steps_run']} | "
            f"{r['fallback_calls']} | {', '.join(r['healed']) or '-'} | {r['guardrail_blocks']} | {r['duration_s']} s | "
            f"{r['documentno'] or '-'} |"
        )
    lines += ["", "## Failures", ""]
    if report["failures"]:
        lines += [f"- **{f['case']}**: {f['diagnosis']}" for f in report["failures"]]
    else:
        lines.append("None.")
    probe = report.get("guardrail_probe")
    if probe:
        lines += [
            "",
            "## Guardrail probe",
            "",
            "A drifted run where the model is replaced by a hostile test double that always proposes a",
            "destructive control. It is not part of the success rate.",
            "",
            f"- Result: {'PASS' if probe['passed'] else 'FAIL'}; hostile proposals: {probe['hostile_proposals']}",
        ]
        for b in probe["blocks"]:
            lines.append(f"- Blocked at `{b['step']}`: rule `{b['rule']}`, {b['reason']}")
        lines.append(f"- Order after the probe: {probe['order_after'] or 'none created'}")
    c = report["cleanup"]
    lines += ["", "## Cleanup", "", f"Deleted {c['deleted']} of {c['drafts_found']} draft purchase orders created by this eval's runs."]
    lines += [f"Cleanup error: {e}" for e in c.get("errors", [])] + [""]
    lines += [f"Evidence for each run: `{report['evidence_dir']}/<run-id>/` (gitignored).", ""]
    return "\n".join(lines)
