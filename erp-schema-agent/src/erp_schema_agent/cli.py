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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
