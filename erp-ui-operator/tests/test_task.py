import pytest

from erp_ui_operator.task import TaskSpecError, validate_task

VALID = {
    "name": "Restock sunscreen",
    "vendor": "Placeholder Contract Manufacturer A",
    "promised_date": "2026-12-05",
    "lines": [{"sku": "FH-017-1", "qty": 300}, {"sku": "FH-022-1P2", "qty": 50}],
}


def test_valid_spec_parses_lines_and_ui_date():
    spec = validate_task(VALID)
    assert [(line.sku, line.qty) for line in spec.lines] == [("FH-017-1", 300), ("FH-022-1P2", 50)]
    assert spec.ui_promised_date == "12/05/2026"
    assert validate_task(spec.as_dict()) == spec


@pytest.mark.parametrize(
    "change, message",
    [
        ({"lines": []}, "non-empty"),
        ({"lines": [{"sku": "GW-Rake", "qty": 1}]}, "not a Fernhollow SKU"),
        ({"lines": [{"sku": "FH-017-1", "qty": 0}]}, "whole number"),
        ({"lines": [{"sku": "FH-017-1", "qty": 2.5}]}, "whole number"),
        ({"lines": [{"sku": "FH-017-1", "qty": True}]}, "whole number"),
        ({"lines": [{"sku": "FH-017-1", "qty": 1}, {"sku": "FH-017-1", "qty": 2}]}, "more than once"),
        ({"lines": [{"sku": "FH-017-1", "qty": 1, "price": 0}]}, "exactly the fields"),
        ({"promised_date": "12/05/2026"}, "ISO date"),
        ({"vendor": "Robert'); DROP TABLE c_order;--"}, "vendor"),
        ({"docaction": "CO"}, "unknown fields"),
    ],
)
def test_invalid_specs_are_rejected_with_a_reason(change, message):
    with pytest.raises(TaskSpecError, match=message):
        validate_task({**VALID, **change})


def test_every_problem_is_reported_at_once():
    with pytest.raises(TaskSpecError) as err:
        validate_task({"vendor": "", "promised_date": "soon", "lines": "FH-017-1"})
    text = str(err.value)
    assert "vendor" in text and "promised_date" in text and "lines" in text
