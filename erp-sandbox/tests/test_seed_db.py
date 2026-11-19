"""Integration checks on a throwaway clone of the seed template (skip when the stack is down)."""

from __future__ import annotations

import os

import pytest

from erp_sandbox import config, db

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn():
    try:
        if not db.database_exists(config.SEED_TEMPLATE_DB):
            pytest.skip("seed template missing; run `uv run erp-sandbox bootstrap`")
    except Exception as exc:  # noqa: BLE001 - any connection failure means the stack is down
        pytest.skip(f"sandbox database unreachable: {exc}")
    name = f"erp_sandbox_test_{os.getpid()}"
    db.clone_database(name, force=True)
    try:
        with db.connect(name) as connection:
            connection.execute("SET search_path TO adempiere")
            yield connection
    finally:
        db.drop_database(name)


def scalar(conn, query: str) -> int:
    return conn.execute(query).fetchone()[0]


def test_seed_counts_are_stable(conn):
    assert scalar(conn, "SELECT count(*) FROM m_product WHERE value LIKE 'FH-%'") == 56
    assert scalar(conn, "SELECT count(*) FROM m_product WHERE value LIKE 'FH-%' AND isactive = 'N'") == 2
    assert scalar(conn, "SELECT count(*) FROM c_order WHERE documentno LIKE 'FH-SO-%'") == 9000
    assert scalar(conn, "SELECT count(*) FROM c_order WHERE documentno LIKE 'FH-PO-%'") == 36
    assert scalar(conn, "SELECT count(*) FROM c_bpartner WHERE value LIKE 'DEMO-C%'") == 320
    assert scalar(conn, "SELECT count(*) FROM c_bpartner WHERE value LIKE 'PH-VEND-%'") == 3


def test_every_seeded_order_is_labelled_synthetic(conn):
    assert scalar(conn, "SELECT count(*) FROM c_order WHERE documentno LIKE 'FH-%' "
                        "AND description NOT LIKE 'Synthetic placeholder%'") == 0


def test_seeded_records_have_accounting_companions(conn):
    schemas = scalar(conn, f"SELECT count(*) FROM c_acctschema WHERE ad_client_id = {config.CLIENT_ID}")
    assert scalar(conn, "SELECT count(*) FROM m_product_acct a JOIN m_product p USING (m_product_id) "
                        "WHERE p.value LIKE 'FH-%'") == 56 * schemas
    assert scalar(conn, "SELECT count(*) FROM m_product_category_acct a JOIN m_product_category c "
                        "USING (m_product_category_id) WHERE c.value = 'FERNHOLLOW'") == schemas
    for table in ("c_bp_customer_acct", "c_bp_vendor_acct", "c_bp_employee_acct"):
        assert scalar(conn, f"SELECT count(*) FROM {table} a JOIN c_bpartner b USING (c_bpartner_id) "
                            "WHERE b.value LIKE 'DEMO-C%' OR b.value LIKE 'PH-VEND-%'") == 323 * schemas, table


def test_protected_databases_are_refused():
    for name in (config.ERP_DB, config.SEED_TEMPLATE_DB, "postgres"):
        with pytest.raises(ValueError):
            db.clone_database(name)
        with pytest.raises(ValueError):
            db.drop_database(name)
