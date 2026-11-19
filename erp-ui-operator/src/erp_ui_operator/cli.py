"""Command line: ``uv run ui-operator <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import erpdb
from .task import TaskSpecError, load_task, validate_task

REPO_ROOT = Path(__file__).resolve().parents[2]


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _cmd_derive(args: argparse.Namespace) -> int:
    promised = dt.date.fromisoformat(args.promised)
    if promised < dt.date.today():
        print(f"promised date {promised} is in the past", file=sys.stderr)
        return 2
    with erpdb.connect() as conn:
        if len(erpdb.vendor_ids(conn, args.vendor)) != 1:
            print(f"expected exactly one active vendor named {args.vendor!r}", file=sys.stderr)
            return 2
        data = erpdb.derive_task(conn, args.vendor, promised, max_lines=args.max_lines, name=args.name)
    if not data["lines"]:
        print(f"no products for {args.vendor!r} are below their reorder point", file=sys.stderr)
        return 1
    validate_task(data)
    text = json.dumps(data, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(data['lines'])} line(s))", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    spec = load_task(args.task)
    result = erpdb.verify_run(spec, erpdb.RunTag(args.run_id))
    print(json.dumps(result.as_dict(), indent=2, default=str))
    return 0 if result.passed else 1


def _cmd_cleanup(args: argparse.Namespace) -> int:
    def confirm(drafts: list[erpdb.TaggedDraft]) -> bool:
        print(f"{len(drafts)} draft purchase order(s) created by erp-ui-operator:")
        for d in drafts:
            print(f"  {d.documentno}  {d.lines} line(s)  {d.description}")
        if args.dry_run:
            print("dry run: nothing deleted")
            return False
        if args.yes:
            return True
        if not sys.stdin.isatty():
            print("refusing to delete without --yes (stdin is not a terminal)")
            return False
        return input(f"Delete these {len(drafts)} draft(s)? [y/N] ").strip().lower() == "y"

    tag = erpdb.RunTag(args.run_id) if args.run_id else None
    drafts, deleted = erpdb.cleanup(tag=tag, confirm=confirm)
    if not drafts:
        print("0 draft purchase orders created by erp-ui-operator; nothing to delete")
    elif deleted:
        print(f"deleted {deleted} draft purchase order(s) in one transaction")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ui-operator",
        description="Create draft purchase orders through iDempiere's web UI, verified in the database.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("derive", help="derive a task spec from products below their reorder point")
    p.add_argument("--vendor", required=True, help="vendor (business partner) name")
    p.add_argument("--promised", required=True, help="promised date, YYYY-MM-DD")
    p.add_argument("--max-lines", type=_positive_int, default=None)
    p.add_argument("--name", default=None, help="task name (default: derived from the vendor)")
    p.add_argument("--out", default=None, help="write the spec here instead of stdout")
    p.set_defaults(func=_cmd_derive)

    p = sub.add_parser("verify", help="check a run's order in the database")
    p.add_argument("task", help="task spec JSON")
    p.add_argument("run_id")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("cleanup", help="delete only the draft orders this tool created")
    p.add_argument("--run-id", default=None, help="only this run's order")
    p.add_argument("--yes", action="store_true", help="delete without asking")
    p.add_argument("--dry-run", action="store_true", help="list and count only")
    p.set_defaults(func=_cmd_cleanup)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskSpecError as exc:
        print(f"invalid task spec: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
