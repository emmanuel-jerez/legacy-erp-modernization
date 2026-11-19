"""Command line: ``migration-agent migrate|approve|reject|status|demo``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import psycopg
from erp_sandbox.llm import ClaudeCodeClient, ModelError

from . import config
from .graph import Deps, build_graph, new_run_id, resume, run_state, sqlite_checkpointer, start
from .oracle import is_available
from .targets import TARGETS, WARM_UP

CHECKPOINTS = config.RUNS_DIR / "checkpoints.sqlite"


def _print_stage(stage: str, message: str) -> None:
    print(f"[{stage:>12}] {message}", flush=True)


def _client(mode: str | None = None) -> ClaudeCodeClient:
    return ClaudeCodeClient(cassette_dir=config.CASSETTE_DIR, mode=mode)


def _finish(state: dict[str, Any]) -> int:
    status = state.get("status", "unknown")
    run_id = state.get("run_id", "?")
    if status == "awaiting_approval":
        print(f"\nHeld-out parity passed. Run {run_id} is waiting for a human decision:")
        print(f"  uv run migration-agent approve {run_id} --by <name>")
        print(f"  uv run migration-agent reject {run_id} --note <why>")
        return 0
    if status == "published":
        print(f"\nPublished to {state['out_dir']}/{state['function']}/")
        return 0
    if status == "not_converged":
        held = state.get("held_out_report", {})
        print(f"\nNOT CONVERGED: visible {state['visible_report']['matched']}/{state['visible_report']['total']}, "
              f"held-out {held.get('matched')}/{held.get('total')} after {state.get('iteration', 0)} repair(s). "
              f"Nothing was written; see runs/{run_id}/audit.jsonl.")
        return 1
    print(f"\nRun {run_id} ended with status {status}.")
    return 0


def _require_oracle() -> None:
    if not is_available():
        raise RuntimeError(f"the oracle database {config.DB_NAME!r} is not reachable; start the sandbox "
                           "(cd ../erp-sandbox && uv run erp-sandbox status)")


def cmd_migrate(args: argparse.Namespace, *, mode: str | None = None, out_dir: Path | None = None) -> int:
    _require_oracle()
    run_id = new_run_id(args.function)
    out = out_dir or Path(args.out)
    _print_stage("run", f"{run_id} (model calls: {_client(mode).mode} mode; cassettes in {config.CASSETTE_DIR.name}/)")
    if args.approve:
        _print_stage("run", "AUTO-APPROVE FLAG IS ON: the approval gate will be skipped without a human review")
    with sqlite_checkpointer(CHECKPOINTS) as saver:
        graph = build_graph(Deps(model=_client(mode), reporter=_print_stage), saver)
        state = start(graph, args.function, run_id=run_id, out_dir=out, auto_approve=args.approve)
    return _finish(state)


def _decide(args: argparse.Namespace, approved: bool) -> int:
    with sqlite_checkpointer(CHECKPOINTS) as saver:
        graph = build_graph(Deps(model=_client("replay"), reporter=_print_stage), saver)
        state = resume(graph, args.run_id, approved=approved, approver=args.by, note=args.note)
    return _finish(state)


def cmd_status(args: argparse.Namespace) -> int:
    with sqlite_checkpointer(CHECKPOINTS) as saver:
        graph = build_graph(Deps(model=_client("replay")), saver)
        if args.run_id:
            run_ids = [args.run_id]
        elif config.RUNS_DIR.exists():
            run_ids = sorted(p.name for p in config.RUNS_DIR.iterdir() if (p / "audit.jsonl").exists())
        else:
            run_ids = []
        for run_id in run_ids:
            state = run_state(graph, run_id)
            print(f"{run_id}: {state.get('status', 'unknown')}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    print("Demo: migrating the warm-up function offline from committed cassettes (ERP_LLM_MODE=replay).")
    print("The local sandbox database must be running: it is the oracle.")
    print("Output goes to runs/demo/migrated/ so the committed migrated/ evidence is untouched.\n")
    ns = argparse.Namespace(function=WARM_UP, approve=True, out=None)
    return cmd_migrate(ns, mode="replay", out_dir=config.RUNS_DIR / "demo" / "migrated")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migration-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="port one legacy function and stop at the approval gate")
    p.add_argument("function", choices=sorted(TARGETS))
    p.add_argument("--approve", action="store_true",
                   help="AUTO-APPROVE: skip the human gate (labelled as such in every record)")
    p.add_argument("--out", default=str(config.MIGRATED_DIR), help="where approved migrations are written")

    for name, help_text in (("approve", "approve a run waiting at the gate"), ("reject", "reject a waiting run")):
        q = sub.add_parser(name, help=help_text)
        q.add_argument("run_id")
        q.add_argument("--by", required=(name == "approve"), default="unnamed operator",
                       help="who decided (recorded in the decision and approval records)")
        q.add_argument("--note", default="")

    s = sub.add_parser("status", help="show run status")
    s.add_argument("run_id", nargs="?")

    sub.add_parser("demo", help="offline end-to-end demo of the warm-up function (auto-approved)")

    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            return cmd_migrate(args)
        if args.command == "approve":
            return _decide(args, True)
        if args.command == "reject":
            return _decide(args, False)
        if args.command == "status":
            return cmd_status(args)
        return cmd_demo(args)
    except ModelError as exc:
        hint = " (run with ERP_LLM_MODE=record to call the model)" if "no cassette" in str(exc) else ""
        print(f"model error: {exc}{hint}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        run = getattr(args, "run_id", None) or "<run-id>"
        print(f"publish failed: {exc}\nThe decision is recorded in the run. Fix the cause, then retry with:\n"
              f"  uv run migration-agent approve {run} --by <name>\n"
              "(the retry publishes the decision already recorded; a new --by or --note is not used)",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
