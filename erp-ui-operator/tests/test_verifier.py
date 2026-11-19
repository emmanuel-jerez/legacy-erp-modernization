import datetime as dt

import pytest

from erp_ui_operator.erpdb import LineRow, OrderRow, RunTag, compare, reorder_qty
from erp_ui_operator.task import validate_task

SPEC = validate_task({
    "name": "Restock", "vendor": "Placeholder Contract Manufacturer A", "promised_date": "2026-12-05",
    "lines": [{"sku": "FH-017-1", "qty": 300}, {"sku": "FH-022-1P2", "qty": 50}],
})
TAG = RunTag("3fa9c2d1")
VENDORS = [1000000]


def order(**kw):
    base = dict(c_order_id=1009036, documentno="800003", docstatus="DR", issotrx="N", processed="N",
                c_bpartner_id=1000000, vendor="Placeholder Contract Manufacturer A",
                description=TAG.description(SPEC), poreference=TAG.po_reference, date_promised=dt.date(2026, 12, 5))
    return OrderRow(**{**base, **kw})


LINES = [LineRow(1009036, 10, "FH-017-1", 300), LineRow(1009036, 20, "FH-022-1P2", 50)]
FP = {"c_order": [36, "2025-11-20T09:00:00"], "m_product": [111, "2025-11-20T09:00:00"]}


def check(orders, lines, **kw):
    return compare(SPEC, TAG, orders, lines, vendor_ids=kw.pop("vendor_ids", VENDORS), **kw)


def failed(result):
    return {c.name for c in result.failures}


def test_matching_draft_order_passes_every_check():
    result = check([order()], LINES, fingerprint_before=FP, fingerprint_after=dict(FP))
    assert result.passed, result.as_dict()
    assert result.order["documentno"] == "800003" and len(result.order["lines"]) == 2


@pytest.mark.parametrize("change, name", [
    ({"docstatus": "CO", "processed": "Y"}, "still_draft"),
    ({"issotrx": "Y"}, "purchase_order"),
    ({"c_bpartner_id": 1000002, "vendor": "Placeholder Co-Packer C"}, "vendor"),
    ({"date_promised": dt.date(2026, 11, 19)}, "promised_date"),
    ({"poreference": None}, "po_reference"),
])
def test_wrong_header_fails_the_named_check(change, name):
    assert failed(check([order(**change)], LINES)) == {name}


def test_vendor_must_resolve_to_exactly_one_partner():
    assert failed(check([order()], LINES, vendor_ids=[1000000, 1000099])) == {"vendor"}
    assert failed(check([order()], LINES, vendor_ids=[])) == {"vendor"}


def test_wrong_missing_extra_or_productless_lines_fail():
    assert failed(check([order()], LINES[:1])) == {"lines_match"}
    assert failed(check([order()], [LINES[0], LineRow(1009036, 20, "FH-022-1P2", 5)])) == {"lines_match"}
    extra = LINES + [LineRow(1009036, 30, "FH-001-1", 1)]
    assert "unexpected=[('FH-001-1', 1)]" in check([order()], extra).failures[0].detail
    charge_line = LINES + [LineRow(1009036, 30, None, 1)]
    assert failed(check([order()], charge_line)) == {"lines_match"}


def test_no_order_or_two_orders_fail_without_guessing():
    assert failed(check([], [])) == {"exactly_one_tagged_order"}
    assert failed(check([order(), order(c_order_id=2)], LINES)) == {"exactly_one_tagged_order"}


def test_changes_to_existing_records_fail():
    after = {**FP, "c_order": [36, "2026-11-19T10:00:00"]}
    assert failed(check([order()], LINES, fingerprint_before=FP, fingerprint_after=after)) == {"existing_records_untouched"}


@pytest.mark.parametrize("run_id", ["%", "a_b", "x" * 16, "", "ABC"])
def test_run_ids_that_could_widen_a_like_match_are_rejected(run_id):
    with pytest.raises(ValueError):
        RunTag(run_id)


def test_run_tag_fits_po_reference_column():
    assert len(RunTag("a" * 15).po_reference) <= 20


@pytest.mark.parametrize("on_hand, expected", [(35, 300), (0, 300), (290, 100), (-5, 350)])
def test_reorder_qty_tops_up_to_max_in_vendor_packs(on_hand, expected):
    assert reorder_qty(on_hand, level_max=300, order_min=100, order_pack=50) == expected


def test_header_readback_ignores_lines_but_checks_the_run_tag():
    from erp_ui_operator.erpdb import compare_header
    assert compare_header(SPEC, TAG, [order()], vendor_ids=VENDORS).passed
    lost = compare_header(SPEC, TAG, [order(poreference=None)], vendor_ids=VENDORS)
    assert {c.name for c in lost.failures} == {"po_reference"}
    assert {c.name for c in compare_header(SPEC, TAG, [order(description="x")], vendor_ids=VENDORS).failures} == {"description"}
