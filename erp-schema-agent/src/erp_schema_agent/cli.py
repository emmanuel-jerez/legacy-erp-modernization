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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schema-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup-db", help="create or refresh the read-only database role (idempotent)")
    setup.set_defaults(func=_cmd_setup_db)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
