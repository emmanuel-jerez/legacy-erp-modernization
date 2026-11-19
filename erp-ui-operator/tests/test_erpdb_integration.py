"""Read-only checks against the live idempiere database. Skip when it is down."""

import datetime as dt

import pytest

from erp_ui_operator import erpdb
from erp_ui_operator.task import validate_task

pytestmark = pytest.mark.integration


def test_derived_spec_is_valid_and_uses_only_the_vendors_skus(erp_db):
    with erpdb.connect() as conn:
        data = erpdb.derive_task(conn, "Placeholder Contract Manufacturer A", dt.date(2026, 12, 5))
        spec = validate_task(data)
        assert not erpdb.unknown_skus(conn, [line.sku for line in spec.lines])
        vendors = conn.execute(
            """SELECT DISTINCT bp.name FROM adempiere.m_product_po po
                 JOIN adempiere.m_product p USING (m_product_id)
                 JOIN adempiere.c_bpartner bp ON bp.c_bpartner_id = po.c_bpartner_id
                WHERE po.iscurrentvendor = 'Y' AND p.value = ANY(%s)""",
            ([line.sku for line in spec.lines],),
        ).fetchall()
    assert vendors == [("Placeholder Contract Manufacturer A",)]
    assert all(line.qty % 50 == 0 for line in spec.lines)


def test_verifying_an_unknown_run_finds_nothing(erp_db):
    spec = validate_task({"vendor": "Placeholder Contract Manufacturer A", "promised_date": "2026-12-05",
                          "lines": [{"sku": "FH-017-1", "qty": 300}]})
    result = erpdb.verify_run(spec, erpdb.RunTag("nosuchrun"))
    assert not result.passed and result.failures[0].name == "exactly_one_tagged_order"
