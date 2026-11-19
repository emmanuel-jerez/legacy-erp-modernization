"""Database layer of the guardrails: the role itself must refuse what the validator also blocks.

These queries go straight to the executor, bypassing the SQL validator on purpose.
"""

from __future__ import annotations

import pytest

from erp_schema_agent.executor import QueryError, run_readonly
from erp_schema_agent.limits import MAX_ROWS, STATEMENT_TIMEOUT_MS

pytestmark = pytest.mark.integration


def test_role_reads_business_tables(readonly_db):
    result = run_readonly("SELECT count(*) AS n FROM c_order WHERE documentno LIKE 'FH-SO-%'")
    assert result.rows == [[9000]]


def test_role_cannot_read_password_columns(readonly_db):
    with pytest.raises(QueryError, match="permission denied"):
        run_readonly("SELECT password FROM ad_user")
    with pytest.raises(QueryError, match="permission denied"):
        run_readonly("SELECT salt FROM ad_allusers_v")


@pytest.mark.parametrize(
    "query",
    [
        "SELECT requestuserpw FROM ad_client",
        "SELECT emailuserpw, userpin FROM ad_user",
        "SELECT encryptionkey FROM ad_system",
        "SELECT connectionkey FROM m_shippingprocessor",
        "SELECT creditcardnumber FROM c_payment",
        "SELECT u FROM ad_user u",
    ],
)
def test_role_cannot_read_other_credential_columns(readonly_db, query):
    with pytest.raises(QueryError, match="permission denied"):
        run_readonly(query)


def test_role_reads_other_columns_of_sensitive_tables(readonly_db):
    result = run_readonly("SELECT name FROM ad_user WHERE name = 'GardenAdmin'")
    assert result.rows == [["GardenAdmin"]]


def test_role_cannot_write(readonly_db):
    with pytest.raises(QueryError):
        run_readonly("UPDATE m_product SET name = 'x' WHERE value = 'FH-NOPE'")
    with pytest.raises(QueryError):
        run_readonly("CREATE TABLE adempiere.agent_scratch (id int)")


def test_role_session_defaults(readonly_db):
    assert run_readonly("SHOW default_transaction_read_only").rows == [["on"]]
    assert run_readonly("SHOW statement_timeout").rows == [[f"{STATEMENT_TIMEOUT_MS // 1000}s"]]


def test_executor_caps_rows(readonly_db):
    result = run_readonly("SELECT c_order_id FROM c_order")
    assert len(result.rows) == MAX_ROWS
    assert result.truncated
