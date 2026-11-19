"""``schema-agent`` command line."""

from __future__ import annotations

import argparse
import json
import sys

from . import settings


def _cmd_setup_db(args: argparse.Namespace) -> int:
    from .roles import setup_readonly_role

    report = setup_readonly_role()
    print(json.dumps({"database": settings.DB_NAME, "role": settings.RO_ROLE, **report}, indent=2))
    return 0


def _cmd_build_catalog(args: argparse.Namespace) -> int:
    import psycopg

    from .retrieval.catalog import CATALOG_PATH, build_catalog, write_catalog

    with psycopg.connect(settings.ro_dsn()) as conn:
        data = build_catalog(conn)
    write_catalog(data)
    print(f"wrote {len(data['tables'])} tables and {len(data['fks'])} foreign keys to {CATALOG_PATH.name}")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    from .agent.graph import run_agent
    from .agent.model import make_model
    from .trace import format_run

    run = run_agent(args.question, model=make_model(args.mode))
    if args.json:
        print(json.dumps(run.as_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(format_run(run))
    return 0 if run.status in ("answered", "refused") else 1


def _cmd_build_golden(args: argparse.Namespace) -> int:
    from .evals.harness import GOLDEN_PATH, build_golden

    print(f"computed expected rows for {build_golden()} cases in {GOLDEN_PATH.relative_to(settings.REPO_ROOT)}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from .evals.harness import render_markdown, run_eval, write_reports

    only = set(args.only.split(",")) if args.only else None
    report = run_eval("record" if args.record else "replay", only=only)
    g, gd = report["golden"], report["guardrails"]
    if only and not (g["cases"] or gd["cases"] or report["sql_guardrails"]["cases"]):
        print(f"no eval case matches {args.only!r}", file=sys.stderr)
        return 2
    if only:
        print(render_markdown(report))
    else:
        json_path, md_path = write_reports(report)
        sg = report["sql_guardrails"]
        print(f"golden accuracy {g['correct']}/{g['cases']}; adversarial prompts passed {gd['passed']}/{gd['cases']}; "
              f"SQL attacks stopped {sg['passed']}/{sg['cases']}; model calls {report['usage']['calls']} ({report['usage']['live_calls']} live)")
        print(f"wrote {json_path.relative_to(settings.REPO_ROOT)} and {md_path.relative_to(settings.REPO_ROOT)}")
    if report["missing_cassettes"]:
        print(f"{report['missing_cassettes']} cases had no cassette; run with --record", file=sys.stderr)
    # Golden misses are measured results, not errors; a guardrail failure or a missing
    # cassette is.
    sql = report["sql_guardrails"]
    return 1 if report["missing_cassettes"] or gd["passed"] < gd["cases"] or sql["passed"] < sql["cases"] else 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(args.mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schema-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup-db", help="create or refresh the read-only database role (idempotent)")
    setup.set_defaults(func=_cmd_setup_db)
    catalog = sub.add_parser("build-catalog", help="snapshot the data dictionary and FK graph for retrieval")
    catalog.set_defaults(func=_cmd_build_catalog)
    ask = sub.add_parser("ask", help="answer a business question and print the trace")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true", help="print the full run as JSON")
    ask.add_argument("--mode", choices=["replay", "record", "live"], help="cassette mode (default ERP_LLM_MODE)")
    ask.set_defaults(func=_cmd_ask)
    golden = sub.add_parser("build-golden", help="compute expected rows for the golden set from reference SQL")
    golden.set_defaults(func=_cmd_build_golden)
    ev = sub.add_parser("eval", help="run the golden and guardrail sets (offline from cassettes by default)")
    ev.add_argument("--record", action="store_true", help="call Claude for cases without cassettes and save them")
    ev.add_argument("--only", help="comma-separated case ids; prints the report instead of writing it")
    ev.set_defaults(func=_cmd_eval)
    demo = sub.add_parser("demo", help="showcase questions plus one blocked attack (offline by default)")
    demo.add_argument("--mode", choices=["replay", "record", "live"], default="replay",
                      help="replay (default, offline), record (call Claude for missing cassettes) or live")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
