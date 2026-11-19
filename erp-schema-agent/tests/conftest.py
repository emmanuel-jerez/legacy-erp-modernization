from __future__ import annotations

import psycopg
import pytest

from erp_schema_agent import limits, settings


def _database_up() -> bool:
    try:
        with psycopg.connect(settings.admin_dsn(), connect_timeout=limits.CONNECT_TIMEOUT_S):
            return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def readonly_db() -> None:
    """The project database with the read-only role set up (idempotent)."""
    if not _database_up():
        pytest.skip("erp_schema_agent database is not reachable")
    from erp_schema_agent.roles import setup_readonly_role

    setup_readonly_role()
