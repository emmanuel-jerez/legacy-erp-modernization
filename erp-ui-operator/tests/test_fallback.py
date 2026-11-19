from erp_sandbox.llm import ScriptedModelClient

from erp_ui_operator.erpdb import RunTag
from erp_ui_operator.fallback import ACTION_SCHEMA, Attempt, build_prompt, propose
from erp_ui_operator.guardrails import ProposedAction
from erp_ui_operator.plan import build_plan
from erp_ui_operator.snapshot import normalize
from erp_ui_operator.task import validate_task

SPEC = validate_task({"name": "Restock", "vendor": "Placeholder Contract Manufacturer A",
                      "promised_date": "2026-12-05", "lines": [{"sku": "FH-017-1", "qty": 300}]})
URL = "https://127.0.0.1:58443/webui/"


def _snapshot(run_id: str):
    return normalize([
        {"role": "combobox", "label": "Business Partner", "text": "", "value": "", "region": "form"},
        {"role": "textbox", "label": "Description", "text": "", "value": f"erp-ui-operator run {run_id}: Restock",
         "region": "form"},
        {"role": "textbox", "label": "Document No", "text": "", "value": "<800117>", "region": "form"},
    ], volatile=(run_id,))


def _step(run_id: str, key: str):
    return next(s for s in build_plan(SPEC, RunTag(run_id), URL) if s.key == key)


def test_prompt_is_identical_across_runs_so_cassettes_replay():
    a = build_prompt(_step("aaaa1111", "po.description"), "no element", _snapshot("aaaa1111"), [])
    b = build_prompt(_step("bbbb2222", "po.description"), "no element", _snapshot("bbbb2222"), [])
    assert a == b
    assert "aaaa1111" not in a and "<run>" in a


def test_scripted_model_proposal_is_parsed_and_schema_checked():
    client = ScriptedModelClient([{"action": "fill", "element_index": 0,
                                   "value": "Placeholder Contract Manufacturer A", "reason": "vendor field"}])
    step = _step("aaaa1111", "po.vendor")
    prompt = build_prompt(step, "no visible element matches form_field 'Vendor'", _snapshot("aaaa1111"), [])
    proposal, result = propose(client, step, prompt)
    assert proposal == ProposedAction("fill", 0, "Placeholder Contract Manufacturer A", "vendor field")
    assert client.calls[0]["schema"] == ACTION_SCHEMA and client.calls[0]["label"] == "fallback:po.vendor"
    assert "form_field 'Vendor'" in prompt and '[0] combobox "Business Partner"' in prompt


def test_earlier_refusals_are_shown_to_the_model():
    attempt = Attempt(ProposedAction("click", 2, None), "refused by guardrail: element 2 is disabled")
    prompt = build_prompt(_step("aaaa1111", "po.save"), "x", _snapshot("aaaa1111"), [attempt])
    assert "- click element=2 value=None -> refused by guardrail: element 2 is disabled" in prompt
