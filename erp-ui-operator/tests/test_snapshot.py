import copy
import json
from pathlib import Path

from erp_ui_operator.snapshot import normalize

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "raw_snapshot_po_line.json").read_text())


def _rec(**kw):
    base = {"role": "textbox", "label": "", "text": "", "value": "", "region": "form",
            "disabled": False, "checked": None, "selected": False}
    return {**base, **kw}


def test_volatile_values_are_replaced_by_placeholders():
    snap = normalize(
        [
            _rec(role="combobox", label="Purchase Order", value="800003_11/19/2026", disabled=True),
            _rec(role="date", label="Date Ordered", value="11/19/2026"),
            _rec(label="Description", value="erp-ui-operator run 3fa9c2d1: Restock"),
            _rec(role="button", label="Save Changes    Alt+S"),
            _rec(role="button", label="Record Info", text="[ 2/2 ]", region="breadcrumb"),
            _rec(role="number", label="Line Amount", value="2,229.00"),
        ],
        volatile=("3fa9c2d1",),
    )
    assert snap.render().splitlines() == [
        '[0] combobox "Purchase Order" value="<n>_<date>" disabled @form',
        '[1] date "Date Ordered" value="<date>" @form',
        '[2] textbox "Description" value="erp-ui-operator run <run>: Restock" @form',
        '[3] button "Save Changes" @form',
        '[4] button "Record Info" text="[ 2/2 ]" @breadcrumb',
        '[5] number "Line Amount" value="2,229.00" @form',
    ]


def test_same_ui_state_in_another_run_renders_identically():
    other = copy.deepcopy(FIXTURE)
    for rec in other:
        rec["value"] = rec["value"].replace("800003", "800117").replace("11/19/2026", "02/03/2027")
    assert normalize(other).render() == normalize(FIXTURE).render()


def test_indexes_follow_page_order_and_cap_is_reported():
    snap = normalize(FIXTURE, cap=10)
    assert [e.index for e in snap.elements] == list(range(10))
    assert snap.truncated and snap.render().endswith("(list capped at 10 elements)")
    assert snap.get(10) is None


def test_rendered_snapshot_carries_no_zk_ids_or_raw_dates():
    text = normalize(FIXTURE).render()
    assert "wFTB" not in text and "11/19/2026" not in text and "800003" not in text
