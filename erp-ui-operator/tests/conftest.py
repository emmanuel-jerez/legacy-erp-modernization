import pytest


def _db_up() -> bool:
    try:
        from erp_ui_operator.erpdb import connect
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def erp_db():
    if not _db_up():
        pytest.skip("iDempiere database is not reachable")
