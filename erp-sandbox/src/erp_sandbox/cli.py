"""``erp-sandbox`` command line: bring up, seed, snapshot, clone, and check the sandbox."""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import config, db, llm, seed

COUNTS_SQL = """
SELECT
  (SELECT count(*) FROM m_product WHERE m_product_category_id =
     (SELECT m_product_category_id FROM m_product_category WHERE value = 'FERNHOLLOW')) AS products,
  (SELECT count(*) FROM c_order WHERE documentno LIKE 'FH-SO-%%') AS sales_orders,
  (SELECT count(*) FROM c_order WHERE documentno LIKE 'FH-PO-%%') AS purchase_orders,
  (SELECT count(*) FROM c_bpartner WHERE value LIKE 'DEMO-C%%') AS demo_customers
"""


def _webui_up(timeout_s: float = 5.0) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # local self-signed certificate
    try:
        with urllib.request.urlopen(config.IDEMPIERE_URL, timeout=timeout_s, context=ctx) as resp:  # noqa: S310
            return resp.status == 200
    except OSError:
        return False


def _wait_for_webui(limit_s: int = 900) -> None:
    deadline = time.monotonic() + limit_s
    while not _webui_up():
        if time.monotonic() > deadline:
            raise SystemExit(f"iDempiere web UI not reachable at {config.IDEMPIERE_URL} after {limit_s}s")
        time.sleep(10)


def _counts(dbname: str) -> dict[str, int] | None:
    if not db.database_exists(dbname):
        return None
    with db.connect(dbname) as conn:
        conn.execute("SET search_path TO adempiere")
        row = conn.execute(COUNTS_SQL).fetchone()
    return dict(zip(("products", "sales_orders", "purchase_orders", "demo_customers"), row, strict=True))


def cmd_status(_: argparse.Namespace) -> int:
    report = {
        "webui": config.IDEMPIERE_URL if _webui_up() else "unreachable",
        "postgres": f"{config.PG_HOST}:{config.PG_PORT}",
        "databases": {
            name: _counts(name)
            for name in (config.ERP_DB, config.SEED_TEMPLATE_DB, *config.PROJECT_DBS)
        },
    }
    print(json.dumps(report, indent=2))
    return 0


def cmd_seed(_: argparse.Namespace) -> int:
    with db.connect(config.ERP_DB) as conn:
        conn.execute("SET search_path TO adempiere")
        counts = seed.seed(conn)
    print(json.dumps(counts, indent=2))
    return 0


def cmd_reseed(_: argparse.Namespace) -> int:
    if db.database_exists(config.SEED_TEMPLATE_DB):
        raise SystemExit("seed template already frozen; reset the stack instead of reseeding")
    with db.connect(config.ERP_DB) as conn:
        conn.execute("SET search_path TO adempiere")
        seed.unseed(conn)
        counts = seed.seed(conn)
    print(json.dumps(counts, indent=2))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    db.snapshot_seed(force=getattr(args, "force", False))
    _wait_for_webui()
    print(f"froze {config.ERP_DB} as {config.SEED_TEMPLATE_DB}; iDempiere restarted")
    return 0


def cmd_clone(args: argparse.Namespace) -> int:
    db.clone_database(args.name, force=args.force)
    print(f"created {args.name} from {config.SEED_TEMPLATE_DB}")
    return 0


def cmd_bootstrap(_: argparse.Namespace) -> int:
    subprocess.run(["docker", "compose", "up", "-d"], cwd=db.REPO_ROOT, check=True)
    print("waiting for iDempiere (first start imports its seed database; allow ~10 minutes)…")
    _wait_for_webui()
    counts = _counts(config.ERP_DB)
    if not counts or counts["products"] == 0:
        cmd_seed(argparse.Namespace())
    if not db.database_exists(config.SEED_TEMPLATE_DB):
        cmd_snapshot(argparse.Namespace(force=False))
    return cmd_status(argparse.Namespace())


SMOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "instruction_headings": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"}},
        "sees_global_agent_instructions": {"type": "boolean"},
    },
    "required": ["instruction_headings", "tools", "sees_global_agent_instructions"],
    "additionalProperties": False,
}


def cmd_llm_smoke(args: argparse.Namespace) -> int:
    """One live call proving the model sees only what the adapter sends."""
    client = llm.ClaudeCodeClient(cassette_dir=Path(args.cassettes), mode="live")
    result = client.complete_json(
        system="You are a component of a local demo. Answer only from what is literally present in your context.",
        prompt=(
            "Audit your context. List the markdown headings of every instructions file, CLAUDE.md, AGENTS.md, "
            "memory or skill text you were given (empty list if none). List every tool you can call. Say whether "
            "your context contains the phrase 'Global agent instructions'."
        ),
        schema=SMOKE_SCHEMA,
        label="llm-smoke",
    )
    data = result.data
    leaked_tools = [t for t in data["tools"] if t != "StructuredOutput"]
    ok = not data["instruction_headings"] and not leaked_tools and not data["sees_global_agent_instructions"]
    print(json.dumps({"ok": ok, "model": result.model, "effort": result.effort, "answer": data,
                      "duration_ms": result.duration_ms}, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erp-sandbox", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show web UI, databases and seed counts").set_defaults(func=cmd_status)
    sub.add_parser("bootstrap", help="start containers, seed once, freeze the seed template").set_defaults(
        func=cmd_bootstrap)
    sub.add_parser("seed", help="load Fernhollow catalog and placeholder history").set_defaults(func=cmd_seed)
    sub.add_parser("reseed", help="remove and regenerate seeded data (before the snapshot only)").set_defaults(
        func=cmd_reseed)
    snap = sub.add_parser("snapshot", help="freeze the ERP database as the seed template")
    snap.add_argument("--force", action="store_true", help="replace an existing template (changes answer keys)")
    snap.set_defaults(func=cmd_snapshot)
    clone = sub.add_parser("clone-db", help="create a project database from the seed template")
    clone.add_argument("name")
    clone.add_argument("--force", action="store_true", help="drop and recreate an existing copy")
    clone.set_defaults(func=cmd_clone)
    smoke = sub.add_parser("llm-smoke", help="one live model call proving Claude Code isolation")
    smoke.add_argument("--cassettes", default=str(Path(tempfile.gettempdir()) / "erp-sandbox-llm-smoke"))
    smoke.set_defaults(func=cmd_llm_smoke)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
