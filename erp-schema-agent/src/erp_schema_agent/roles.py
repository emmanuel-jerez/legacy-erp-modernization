"""Database layer of the guardrails: a least-privilege, read-only role for the agent.

``setup_readonly_role`` is idempotent. It connects as the cluster admin and gives
``erp_schema_agent_ro``:

* LOGIN only: no superuser, database creation, role creation, replication or RLS bypass;
* CONNECT on the project database and USAGE on the ``adempiere`` schema;
* SELECT on every ``adempiere`` table and view, except credential columns
  (passwords, salts, secrets, tokens), which are left out with column-level grants;
* session defaults in the project database: read-only transactions, a statement
  timeout, a lock timeout and ``search_path = adempiere``.

What actually prevents writes is the privilege set: the role owns nothing and holds no
INSERT/UPDATE/DELETE/TRUNCATE/CREATE grant. ``default_transaction_read_only`` is only a
session default the session could switch off (the validator blocks ``SET`` and
``set_config``), and the executor opens every transaction READ ONLY explicitly as well.

The role keeps PostgreSQL's default PUBLIC access to the system catalogs
(``pg_class``, ``pg_roles``, ``pg_stat_activity`` ...), which every login has and which
holds metadata, not ERP data or passwords (``pg_authid``/``pg_shadow`` need superuser).
Blocking catalog reads is the validator's job.

PostgreSQL roles are cluster-wide, but grants are per database: the role has no table
privileges anywhere else. Recloning the project database drops the grants (the role
survives), so run ``schema-agent setup-db`` again after ``erp-sandbox clone-db``.
"""

from __future__ import annotations

import psycopg
from psycopg import sql

from erp_sandbox import config

from . import limits, settings
from .policy import is_sensitive_column


def admin_dsn() -> str:
    """Cluster-admin connection to the project database: role setup and test fixtures only."""
    return config.dsn(settings.DB_NAME)


def _relations(conn: psycopg.Connection) -> dict[str, list[str]]:
    """Every table, view, materialized view and foreign table in the schema, with columns."""
    rows = conn.execute(
        "SELECT c.relname, a.attname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        "ORDER BY c.relname, a.attnum",
        (settings.SCHEMA,),
    ).fetchall()
    relations: dict[str, list[str]] = {}
    for table, column in rows:
        relations.setdefault(table, []).append(column)
    return relations


def setup_readonly_role() -> dict[str, int]:
    """Create or refresh the read-only role. Returns counts for the CLI report."""
    role = sql.Identifier(settings.RO_ROLE)
    db = sql.Identifier(settings.DB_NAME)
    schema = sql.Identifier(settings.SCHEMA)
    with psycopg.connect(admin_dsn()) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.RO_ROLE,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE ROLE {}").format(role))
        conn.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
                "NOBYPASSRLS CONNECTION LIMIT {} PASSWORD {}"
            ).format(role, sql.Literal(limits.ROLE_CONNECTION_LIMIT), sql.Literal(settings.RO_PASSWORD))
        )
        for name, value in (
            ("default_transaction_read_only", "on"),
            ("statement_timeout", str(limits.STATEMENT_TIMEOUT_MS)),
            ("lock_timeout", str(limits.LOCK_TIMEOUT_MS)),
            ("idle_in_transaction_session_timeout", str(limits.IDLE_IN_TRANSACTION_TIMEOUT_MS)),
            ("search_path", settings.SCHEMA),
        ):
            conn.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} = {}").format(
                    role, db, sql.Identifier(name), sql.Literal(value)
                )
            )
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db, role))
        # PostgreSQL grants TEMPORARY on every database to PUBLIC; take it back here so the
        # role cannot create even session-local tables. Affects only this project database.
        conn.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(db))
        conn.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(db, role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
        # Start clean: table-level REVOKE also drops column-level grants.
        conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(schema, role))
        conn.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))

        full = partial = hidden = 0
        for table, columns in _relations(conn).items():
            rel = sql.Identifier(settings.SCHEMA, table)
            allowed = [c for c in columns if not is_sensitive_column(c)]
            if len(allowed) == len(columns):
                conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(rel, role))
                full += 1
            elif allowed:
                conn.execute(
                    sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                        sql.SQL(", ").join(map(sql.Identifier, allowed)), rel, role
                    )
                )
                partial += 1
                hidden += len(columns) - len(allowed)
        conn.commit()
    return {"relations_full": full, "relations_column_limited": partial, "columns_hidden": hidden}
