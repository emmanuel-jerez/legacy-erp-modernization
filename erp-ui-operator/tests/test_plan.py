from erp_ui_operator.erpdb import RunTag
from erp_ui_operator.guardrails import is_destructive
from erp_ui_operator.plan import LOGIN_PASSWORD, PASSWORD_TOKEN, build_plan, plan_values
from erp_ui_operator.selectors import DRIFT_PROFILES, SELECTOR_MAP, selector_map
from erp_ui_operator.task import validate_task

SPEC = validate_task({
    "name": "Restock", "vendor": "Placeholder Contract Manufacturer A", "promised_date": "2026-12-05",
    "lines": [{"sku": "FH-017-1", "qty": 300}, {"sku": "FH-022-1P2", "qty": 50}],
})
TAG = RunTag("3fa9c2d1")
PLAN = build_plan(SPEC, TAG, "https://127.0.0.1:58443/webui/")


def test_plan_covers_header_then_each_line_in_order():
    keys = [s.key for s in PLAN]
    assert keys.index("po.save") < keys.index("po.lines_tab") < keys.index("line1.product") < keys.index("line2.product")
    assert [s.value for s in PLAN if s.key.endswith(".qty")] == ["300", "50"]
    assert [s.value for s in PLAN if s.key.endswith(".product")] == ["FH-017-1", "FH-022-1P2"]


def test_no_selector_map_entry_clean_or_drifted_targets_a_destructive_control():
    for drift in [None, *DRIFT_PROFILES]:
        for key, target in selector_map(drift).items():
            assert not is_destructive(target.name), (drift, key, target)


def test_the_model_may_only_enter_task_values_with_secrets_and_run_id_masked():
    values = plan_values(PLAN)
    assert values == {
        "GardenAdmin", "GardenWorld Admin", "HQ", "HQ Warehouse", "Purchase Order",
        "Placeholder Contract Manufacturer A", "erp-ui-operator run <run>: Restock", "UIOP-<run>",
        "12/05/2026", "FH-017-1", "FH-022-1P2", "300", "50",
    }
    assert not any(TAG.run_id in v for v in values)
    password_step = next(s for s in PLAN if s.key == "login.password")
    assert password_step.value == LOGIN_PASSWORD and password_step.shown_value == PASSWORD_TOKEN
    assert not password_step.fallback  # the secret never reaches the model


def test_fallback_actions_are_limited_to_the_step_kind():
    by_key = {s.key: s for s in PLAN}
    assert by_key["po.save"].scope.actions == frozenset({"click", "press", "wait"})
    assert "fill" in by_key["po.vendor"].scope.actions and "select" not in by_key["po.vendor"].scope.actions
    assert by_key["po.readback"].scope.actions == frozenset()


def test_product_dialog_steps_run_only_when_the_dialog_opens():
    dialog_steps = [s for s in PLAN if s.key.endswith((".product_pick", ".product_ok"))]
    assert len(dialog_steps) == 4 and all(s.when is not None for s in dialog_steps)
    assert all(s.scope.regions == frozenset({"dialog: "}) for s in dialog_steps)


def test_drift_perturbs_only_the_selector_map():
    drifted = selector_map("drifted")
    changed = {k for k in SELECTOR_MAP if drifted[k] != SELECTOR_MAP[k]}
    assert changed == {"po.vendor", "po.save", "line.qty"}
    # Postconditions come from the plan and read instancename attributes, which drift never touches.
    for key in ("po.vendor", "po.save", "line1.qty"):
        post = next(s.post for s in PLAN if s.key == key)
        assert "instancename" in str(post) and "Vendor" not in str(post) and "Order Quantity" not in str(post)
