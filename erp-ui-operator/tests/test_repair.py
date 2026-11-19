"""The verify-only step re-runs its repair steps once, then fails closed."""

from pathlib import Path

from erp_sandbox.llm import ScriptedModelClient

from erp_ui_operator.erpdb import RunTag
from erp_ui_operator.guardrails import StepScope
from erp_ui_operator.plan import Post, Step
from erp_ui_operator.runner import Operator, RunOptions
from erp_ui_operator.task import validate_task

SPEC = validate_task({"vendor": "Placeholder Contract Manufacturer A", "promised_date": "2026-12-05",
                      "lines": [{"sku": "FH-017-1", "qty": 300}]})


class StubPage:
    def screenshot(self, path):
        Path(path).write_bytes(b"")


def _operator(tmp_path, answers):
    op = Operator(StubPage(), ScriptedModelClient([]), SPEC, RunTag("abc"), RunOptions(), tmp_path)
    probes = iter(answers)
    op.db_probe = lambda post: next(probes)  # stands in for the database read-back
    fix = Step("po.fix", "re-enter", "check", None, Post("db_header", text="fixed"), StepScope(frozenset()))
    readback = Step("po.readback", "check", "check", None, Post("db_header", text="header ok"),
                    StepScope(frozenset()), fallback=False, repair=("po.fix",))
    op.plan = [fix, readback]
    return op, readback


def test_mismatch_triggers_one_repair_then_passes(tmp_path):
    op, readback = _operator(tmp_path, [False, True, True])  # readback, repair step, re-check
    rec = op.run_step(0, readback)
    assert rec["status"] == "ok" and rec["repair"]["result"] == "fixed"
    assert [r["key"] for r in op.records] == ["po.fix", "po.readback"]
    assert op.records[0]["note"] == "repair for po.readback"


def test_second_mismatch_fails_the_step(tmp_path):
    import pytest

    from erp_ui_operator.runner import StepFailed
    op, readback = _operator(tmp_path, [False, True, False])
    with pytest.raises(StepFailed, match="after one repair"):
        op.run_step(0, readback)
