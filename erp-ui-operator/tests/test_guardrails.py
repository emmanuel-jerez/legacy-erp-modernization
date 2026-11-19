import pytest

from erp_ui_operator.guardrails import (
    ABORT, OK, RETRY, BudgetExceeded, ProposedAction, RunBudget, StepScope, check_action,
)
from erp_ui_operator.snapshot import normalize


def _rec(role, label, region="form", text="", disabled=False):
    return {"role": role, "label": label, "text": text, "value": "", "region": region,
            "disabled": disabled, "checked": None, "selected": False}


SNAP = normalize([
    _rec("combobox", "Business Partner"),                          # 0
    _rec("button", "Save Changes", "window-toolbar"),               # 1
    _rec("button", "Delete Record", "window-toolbar"),              # 2
    _rec("button", "", "form", text="Document Action"),             # 3
    _rec("button", "Process", "window-toolbar"),                    # 4
    _rec("button", "Log Out", "page"),                              # 5
    _rec("number", "Quantity", disabled=True),                      # 6
    _rec("button", "Close", "dialog: Product Info"),                # 7
    _rec("checkbox", "Drop Shipment"),                              # 8
])
SCOPE = StepScope(regions=frozenset({"form", "window-toolbar"}), values=frozenset({"Placeholder Contract Manufacturer A"}))


def verdict(action, index=None, value=None, scope=SCOPE):
    return check_action(ProposedAction(action, index, value), SNAP, scope)


def test_in_scope_fill_and_click_are_allowed():
    assert verdict("fill", 0, "Placeholder Contract Manufacturer A").severity == OK
    assert verdict("click", 1).severity == OK
    assert verdict("click", 8).severity == OK  # "Drop Shipment" is not a destructive control
    assert verdict("wait").severity == OK


@pytest.mark.parametrize("index", [2, 3, 4, 7])
def test_destructive_controls_abort_the_run(index):
    v = verdict("click", index)
    assert (v.severity, v.rule) == (ABORT, "destructive_target")


def test_actions_outside_the_allowlist_or_scope_abort():
    assert verdict("navigate", 0).rule == "action_not_allowed"
    v = verdict("click", 5)
    assert (v.severity, v.rule) == (ABORT, "out_of_scope_region")
    v = verdict("press", 0, "Alt+D")
    assert (v.severity, v.rule) == (ABORT, "key_not_allowed")


def test_values_the_task_does_not_supply_abort():
    v = verdict("fill", 0, "Some Other Vendor")
    assert (v.severity, v.rule) == (ABORT, "value_not_in_task")


def test_destructive_context_is_caught_in_dialog_titles_values_and_word_forms():
    snap = normalize([
        {**_rec("button", "OK", "dialog: Purchase Order"), "context": "Purchase Order Delete record? OK Cancel"},
        _rec("combobox", "Document Action", "form"),
        _rec("button", "Processing...", "window-toolbar"),
        _rec("combobox", "Target Document Type", "form"),
        _rec("button", "Save " + "x" * 70 + " and Complete", "window-toolbar"),
    ])
    scope = StepScope(regions=frozenset({"dialog: ", "form", "window-toolbar"}), values=frozenset({"Complete"}))
    for index, value in [(0, None), (1, "Complete"), (2, None), (3, "Complete"), (4, None)]:
        action = ProposedAction("select" if value else "click", index, value)
        assert check_action(action, snap, scope).rule == "destructive_target", index


def test_recoverable_mistakes_ask_the_model_again():
    assert verdict("click", 42).rule == "unknown_element"
    assert verdict("click", None).rule == "missing_element"
    v = verdict("fill", 6, "Placeholder Contract Manufacturer A")
    assert (v.severity, v.rule) == (RETRY, "disabled_element")
    assert verdict("fill", 1, "Placeholder Contract Manufacturer A").rule == "not_fillable"


def test_dialog_regions_match_by_prefix():
    scope = StepScope(regions=frozenset({"dialog: "}))
    snap = normalize([_rec("row", "", "dialog: Product Info", text="FH-017-1 Tinted Sunscreen")])
    assert check_action(ProposedAction("click", 0, None), snap, scope).severity == OK


def test_budget_caps_actions_and_fallbacks():
    budget = RunBudget(max_actions=2, max_fallback_calls=3, max_fallback_per_step=2)
    budget.spend_action(); budget.spend_action()
    with pytest.raises(BudgetExceeded):
        budget.spend_action()
    budget.spend_fallback("save"); budget.spend_fallback("save")
    assert not budget.can_fallback("save") and budget.can_fallback("qty")
    budget.spend_fallback("qty")
    with pytest.raises(BudgetExceeded):
        budget.spend_fallback("other")
