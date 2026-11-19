"""SQL layer of the guardrails: deterministic checks before anything reaches the database."""

from __future__ import annotations

import pytest

from erp_schema_agent.guardrails import validate_sql
from erp_schema_agent.limits import MAX_ROWS

ALLOWED = {"c_order", "c_orderline", "m_product", "c_bpartner", "ad_user"}


def check(sql: str):
    return validate_sql(sql, allowed_tables=ALLOWED, sensitive_tables={"ad_user"})


def test_accepts_select_and_injects_limit():
    verdict = check("SELECT documentno FROM c_order WHERE docstatus = 'CO'")
    assert verdict.ok, verdict.reason
    assert verdict.sql.rstrip().endswith(f"LIMIT {MAX_ROWS}")
    assert verdict.tables == ["c_order"]


def test_caps_a_larger_limit_and_keeps_a_smaller_one():
    assert check("SELECT 1 FROM c_order LIMIT 100000").sql.endswith(f"LIMIT {MAX_ROWS}")
    assert check("SELECT 1 FROM c_order LIMIT 5").sql.endswith("LIMIT 5")


def test_accepts_cte_and_schema_qualified_tables():
    verdict = check(
        "WITH totals AS (SELECT c_bpartner_id, sum(grandtotal) AS t FROM adempiere.c_order GROUP BY 1) "
        "SELECT b.name, totals.t FROM totals JOIN c_bpartner b USING (c_bpartner_id) ORDER BY 2 DESC LIMIT 5"
    )
    assert verdict.ok, verdict.reason
    assert verdict.tables == ["c_bpartner", "c_order"]


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM c_order", "only a single SELECT"),
        ("UPDATE m_product SET name = 'x'", "only a single SELECT"),
        ("DROP TABLE c_order", "only a single SELECT"),
        ("SELECT 1 FROM c_order; DROP TABLE c_order", "exactly one statement"),
        ("SELECT * INTO scratch FROM c_order", "SELECT INTO"),
        ("SELECT * FROM c_order FOR UPDATE", "locking"),
        ("WITH d AS (DELETE FROM c_order RETURNING *) SELECT * FROM d", "only a single SELECT"),
        ("SELECT * FROM pg_shadow", "not allowed"),
        ("SELECT * FROM pg_catalog.pg_authid", "not allowed"),
        ("SELECT * FROM information_schema.columns", "not allowed"),
        ("SELECT * FROM public.anything", "not allowed"),
        ("SELECT * FROM ad_table_secret_copy", "not allowed"),
        ("SELECT pg_sleep(10) FROM c_order", "function"),
        ("SELECT pg_read_file('/etc/passwd')", "function"),
        ("SELECT set_config('default_transaction_read_only', 'off', false)", "function"),
        ("SELECT nextval('c_order_sq')", "function"),
        ("SELECT dblink_exec('x', 'DROP TABLE c_order')", "function"),
        ("SELECT password FROM ad_user", "sensitive column"),
        ("SELECT u.salt FROM ad_user u", "sensitive column"),
        ("SELECT * FROM ad_user", "SELECT *"),
        ("COPY c_order TO '/tmp/x'", "only a single SELECT"),
        ("SET ROLE postgres", "only a single SELECT"),
        ("SELECT FROM WHERE", "could not parse"),
        ("", "empty"),
    ],
)
def test_rejects_with_a_reason_the_model_can_act_on(sql, reason):
    verdict = check(sql)
    assert not verdict.ok
    assert reason in verdict.reason


def test_allows_ordinary_functions():
    verdict = check(
        "SELECT date_trunc('month', dateordered)::date AS month, round(sum(grandtotal), 2), "
        "count(DISTINCT c_bpartner_id), coalesce(max(documentno), '') FROM c_order GROUP BY 1"
    )
    assert verdict.ok, verdict.reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM c_order LIMIT ALL",
        "SELECT 1 FROM c_order FETCH FIRST 5000 ROWS ONLY",
        "SELECT 1 FROM c_order LIMIT 100000 + 1",
        "SELECT documentno FROM c_order UNION SELECT name FROM m_product",
    ],
)
def test_caps_every_limit_form(sql):
    verdict = check(sql)
    assert verdict.ok, verdict.reason
    assert verdict.sql.endswith(f"LIMIT {MAX_ROWS}")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_catalog.pg_sleep(10)",
        "SELECT * FROM (SELECT * FROM c_order FOR UPDATE) x",
        "SELECT query_to_xml('select password from ad_user', true, true, '')",
        "SELECT x FROM generate_series(1, 10) AS g(x) JOIN pg_roles ON true",
        # A CTE name must not unlock the real catalog where the CTE is out of scope.
        "SELECT * FROM (WITH pg_roles AS (SELECT 1) SELECT 1 FROM pg_roles) a, pg_roles",
        "SELECT (WITH pg_settings AS (SELECT 1) SELECT 1), name, setting FROM pg_settings",
        "WITH pg_roles AS (SELECT rolname FROM pg_roles) SELECT * FROM pg_roles",
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1) SELECT * FROM a",
        # Whole-row and composite access to credential tables.
        "SELECT u FROM ad_user u",
        "SELECT (u).password FROM ad_user u",
        "SELECT array_agg(u) FROM ad_user u",
        "SELECT ad_user FROM ad_user",
        "SELECT emailuserpw FROM ad_user",
        "SELECT userpin FROM ad_user",
        # Server and session details.
        "SELECT version()",
        "SELECT current_user",
        "SELECT current_database()",
        "SELECT user",
        "SELECT current_role",
        # reg* casts look names up in the catalogs.
        "SELECT g::regrole::text FROM generate_series(10, 30000) g",
        "SELECT 'pg_authid'::regclass::oid",
        "SELECT CAST(documentno AS regclass) FROM c_order",
    ],
)
def test_rejects_nested_and_qualified_tricks(sql):
    assert not check(sql).ok


def test_ctes_resolve_in_scope():
    verdict = check("WITH a AS (SELECT c_order_id FROM c_order), b AS (SELECT * FROM a) SELECT count(*) FROM b")
    assert verdict.ok, verdict.reason
    assert verdict.tables == ["c_order"]


def test_count_star_is_fine_on_tables_with_credentials():
    assert check("SELECT count(*) FROM ad_user").ok
