"""Database helpers: connections, the frozen seed template, and per-project clones.

Each demo project works on its own copy of the seeded ERP database so one project's
writes can never change another project's eval answers. Copies come from the
``idempiere_seed`` template with ``CREATE DATABASE ... TEMPLATE``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

from . import config

_NAME = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
PROTECTED = {config.ERP_DB, config.SEED_TEMPLATE_DB, "postgres", "template0", "template1"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def connect(dbname: str | None = None, *, admin: bool = False, autocommit: bool = False) -> psycopg.Connection:
    """Connect as the ERP owner (default) or the cluster admin."""
    name = dbname or config.ERP_DB
    if admin:
        return psycopg.connect(config.dsn(name), autocommit=autocommit)
    return psycopg.connect(
        config.dsn(name, user=config.ERP_DB_USER, password=config.ERP_DB_PASSWORD), autocommit=autocommit
    )


def database_exists(name: str) -> bool:
    with connect("postgres", admin=True, autocommit=True) as conn:
        return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def _terminate(conn: psycopg.Connection, name: str) -> None:
    conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )


def drop_database(name: str) -> None:
    if not _NAME.match(name) or name in PROTECTED:
        raise ValueError(f"refusing to drop database {name!r}")
    with connect("postgres", admin=True, autocommit=True) as conn:
        _terminate(conn, name)
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def clone_database(name: str, *, template: str = config.SEED_TEMPLATE_DB, force: bool = False) -> None:
    """Create ``name`` as a copy of the seed template. ``force`` drops an existing copy first."""
    if not _NAME.match(name) or name in PROTECTED:
        raise ValueError(f"refusing to use database name {name!r}")
    if template == config.ERP_DB:
        raise ValueError("clone from the seed template, not the live ERP database (it would end iDempiere's sessions)")
    if not database_exists(template):
        raise RuntimeError(f"template {template!r} does not exist; run `erp-sandbox snapshot` first")
    with connect("postgres", admin=True, autocommit=True) as conn:
        if database_exists(name):
            if not force:
                raise RuntimeError(f"database {name!r} exists; pass force=True to recreate it")
            _terminate(conn, name)
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        _terminate(conn, template)  # CREATE DATABASE ... TEMPLATE needs a template with no sessions
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(template), sql.Identifier(config.ERP_DB_USER)
            )
        )


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=REPO_ROOT, check=True)


def snapshot_seed(*, force: bool = False) -> None:
    """Freeze the current ERP database as the seed template.

    ``CREATE DATABASE ... TEMPLATE`` needs the source to have no other sessions, so the
    iDempiere server stops for the copy and starts again afterwards. An existing template
    is only replaced with ``force``: after the web UI has written to ``idempiere``, a new
    snapshot would carry those writes into every clone and change other repos' answer keys.
    """
    if database_exists(config.SEED_TEMPLATE_DB) and not force:
        raise RuntimeError(f"{config.SEED_TEMPLATE_DB!r} already exists; pass force=True to replace it")
    _compose("stop", "idempiere")
    try:
        with connect("postgres", admin=True, autocommit=True) as conn:
            if database_exists(config.SEED_TEMPLATE_DB):
                _terminate(conn, config.SEED_TEMPLATE_DB)
                conn.execute(sql.SQL("ALTER DATABASE {} IS_TEMPLATE false").format(
                    sql.Identifier(config.SEED_TEMPLATE_DB)))
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(config.SEED_TEMPLATE_DB)))
            _terminate(conn, config.ERP_DB)
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
                    sql.Identifier(config.SEED_TEMPLATE_DB),
                    sql.Identifier(config.ERP_DB),
                    sql.Identifier(config.ERP_DB_USER),
                )
            )
            conn.execute(sql.SQL("ALTER DATABASE {} IS_TEMPLATE true").format(
                sql.Identifier(config.SEED_TEMPLATE_DB)))
    finally:
        _compose("start", "idempiere")
