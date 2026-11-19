from erp_ui_operator.evals import HostileModelClient
from erp_ui_operator.fallback import ACTION_SCHEMA
from erp_ui_operator.guardrails import ABORT, ProposedAction, StepScope, check_action
from erp_ui_operator.snapshot import normalize


def test_probe_double_proposes_a_destructive_control_that_the_guardrail_aborts():
    snap = normalize([
        {"role": "combobox", "label": "Business Partner", "text": "", "value": "", "region": "form"},
        {"role": "button", "label": "Save Changes", "text": "", "value": "", "region": "window-toolbar"},
        {"role": "button", "label": "Delete Record", "text": "", "value": "", "region": "window-toolbar"},
    ])
    result = HostileModelClient().complete_json(system="s", prompt="Page elements:\n" + snap.render(),
                                                schema=ACTION_SCHEMA)
    proposal = ProposedAction.from_model(result.data)
    assert proposal.element_index == 2
    verdict = check_action(proposal, snap, StepScope(frozenset({"form", "window-toolbar"})))
    assert (verdict.severity, verdict.rule) == (ABORT, "destructive_target")


def test_summary_counts_successes_per_selector_map():
    from erp_ui_operator.evals import summarize

    def case(drift, success, duration):
        return {"drift": drift, "success": success, "fallback_calls": 3 if drift != "clean" else 0,
                "guardrail_blocks": 0, "repairs": 0, "steps_run": 20, "duration_s": duration}

    s = summarize([case("clean", True, 20.0), case("drifted", True, 40.0), case("drifted", False, 60.0)])
    assert s["db_verified_success"] == {"passed": 2, "total": 3, "rate": 0.667}
    assert s["clean"]["passed"] == 1 and s["drifted"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert (s["fallback_calls"], s["duration_s_min"], s["duration_s_median"], s["duration_s_max"]) == (6, 20.0, 40.0, 60.0)
