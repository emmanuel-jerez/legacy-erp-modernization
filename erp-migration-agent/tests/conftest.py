from __future__ import annotations

import pytest

from erp_migration_agent.oracle import Oracle, is_available


@pytest.fixture(scope="session")
def oracle():
    if not is_available():
        pytest.skip("sandbox database is not running")
    with Oracle.open() as o:
        yield o
