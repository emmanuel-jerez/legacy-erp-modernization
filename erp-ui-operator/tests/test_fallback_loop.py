"""The fallback loop with the browser stubbed out and ScriptedModelClient as the model."""

from pathlib import Path

import pytest
from erp_sandbox.llm import ScriptedModelClient

from erp_ui_operator.erpdb import RunTag
from erp_ui_operator.guardrails import RunBudget
from erp_ui_operator.plan import build_plan
from erp_ui_operator.runner import GuardrailAbort, Operator, RunOptions, StepFailed
from erp_ui_operator.snapshot import normalize
from erp_ui_operator.task import validate_task

SPEC = validate_task({"vendor": "Placeholder Contract Manufacturer A", "promised_date": "2026-12-05",
                      "lines": [{"sku": "FH-017-1", "qty": 300}]})
TAG = RunTag("abc")
STEPS = {s.key: s for s in build_plan(SPEC, TAG, "https://127.0.0.1:58443/webui/")}
SNAP = normalize([
    {"role": "combobox", "label": "Business Partner", "text": "", "value": "", "region": "form"},
    {"role": "button", "label": "Save Changes", "text": "", "value": "", "region": "window-toolbar"},
    {"role": "button", "label": "Delete Record", "text": "", "value": "", "region": "window-toolbar"},
])
VENDOR = "Placeholder Contract Manufacturer A"


class StubPage:
    def screenshot(self, path):
        Path(path).write_bytes(b"")


class StubbedOperator(Operator):
    """Replaces only the browser seams; the loop, guardrail and budget are real."""

    def __init__(self, tmp_path, responses, post_results, failure):
        super().__init__(StubPage(), ScriptedModelClient(responses), SPEC, TAG,
                         RunOptions(budget=RunBudget(max_fallback_per_step=3)), tmp_path)
        self.post_results = iter(post_results)
        self.failure = failure
        self.executed = []
        self.healed_calls = []

    def _deterministic(self, step, rec, force_change=False):
        return self.failure

    def _take_snapshot(self):
        return SNAP

    def _execute_proposal(self, step, proposal):
        self.executed.append(proposal)
        return True, next(self.post_results)

    def _heal_candidate(self, step, proposal, snapshot):
        self.healed_calls.append(step.key)
        return None


MISSING = "no visible element matches selector-map entry form_field 'Vendor'"


def test_destructive_proposal_aborts_before_anything_runs(tmp_path):
    op = StubbedOperator(tmp_path, [{"action": "click", "element_index": 2, "value": None, "reason": "x"}], [], MISSING)
    with pytest.raises(GuardrailAbort):
        op.run_step(0, STEPS["po.save"])
    assert op.executed == [] and op.guardrail_blocks[0]["rule"] == "destructive_target"


def test_refused_slip_is_retried_with_the_refusal_in_the_prompt_then_heals(tmp_path):
    responses = [
        {"action": "fill", "element_index": 7, "value": VENDOR, "reason": "x"},  # not in the snapshot
        {"action": "fill", "element_index": 0, "value": VENDOR, "reason": "vendor field"},
    ]
    op = StubbedOperator(tmp_path, responses, [True], MISSING)
    rec = op.run_step(0, STEPS["po.vendor"])
    assert rec["status"] == "ok" and rec["layer"] == "llm-fallback"
    assert [e["verdict"]["rule"] for e in rec["fallback"]] == ["unknown_element", "ok"]
    assert "refused by guardrail: element 7 is not in the snapshot" in op.client.calls[1]["prompt"]
    assert op.healed_calls == ["po.vendor"]


def test_no_healing_when_the_locator_worked_but_the_postcondition_failed(tmp_path):
    responses = [{"action": "fill", "element_index": 0, "value": VENDOR, "reason": "x"}]
    op = StubbedOperator(tmp_path, responses, [True], "after fill on form_field 'Business Partner', postcondition not met")
    op.run_step(0, STEPS["po.vendor"])
    assert op.healed_calls == []


def test_per_step_cap_stops_a_step_that_never_succeeds(tmp_path):
    click = {"action": "click", "element_index": 1, "value": None, "reason": "save"}
    op = StubbedOperator(tmp_path, [click] * 5, [False] * 5, MISSING)
    with pytest.raises(StepFailed, match="fallback budget exhausted"):
        op.run_step(0, STEPS["po.save"])
    assert len(op.client.calls) == 3 and len(op.executed) == 3


def test_value_the_task_did_not_supply_aborts(tmp_path):
    op = StubbedOperator(tmp_path, [{"action": "fill", "element_index": 0, "value": "Other Vendor", "reason": "x"}],
                         [], MISSING)
    with pytest.raises(GuardrailAbort, match="not a value this step supplies"):
        op.run_step(0, STEPS["po.vendor"])
