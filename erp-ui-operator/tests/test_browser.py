"""End-to-end runs against the live web UI. Marked ``browser``; skipped when the UI is down.

They write real draft purchase orders to ``idempiere`` (this repo's job) and delete their
own run's order afterwards. The drift test replays committed cassettes and never calls the
live model: a cassette miss fails the test, which flags a snapshot-determinism regression.
"""

import ssl
import urllib.request
from pathlib import Path

import pytest
from erp_sandbox import config
from erp_sandbox.llm import ClaudeCodeClient, ScriptedModelClient

from erp_ui_operator import erpdb
from erp_ui_operator.runner import RunOptions, new_run_id, run_task
from erp_ui_operator.task import load_task

pytestmark = pytest.mark.browser
REPO = Path(__file__).resolve().parents[1]


def _ui_up() -> bool:
    try:
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(config.IDEMPIERE_URL, timeout=5, context=ctx) as r:
            return r.status == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def ui():
    if not _ui_up():
        pytest.skip("iDempiere web UI is not reachable")


def _run(spec, client, drift, tmp_path):
    run_id = new_run_id()
    try:
        return run_task(spec, client, RunOptions(drift=drift, runs_dir=tmp_path, trace=False), run_id=run_id)
    finally:
        erpdb.cleanup(tag=erpdb.RunTag(run_id))


def test_clean_run_needs_no_model_and_passes_the_database_check(ui, tmp_path):
    spec = load_task(REPO / "tasks" / "restock-1-line.json")
    result = _run(spec, ScriptedModelClient([]), None, tmp_path)
    assert result.success, result.log["diagnosis"]
    assert result.log["totals"]["fallback_calls"] == 0


def test_drifted_run_recovers_from_cassettes(ui, tmp_path):
    spec = load_task(REPO / "tasks" / "restock-1-line.json")
    client = ClaudeCodeClient(cassette_dir=REPO / "cassettes", mode="replay")
    result = _run(spec, client, "drifted", tmp_path)
    assert result.success, result.log["diagnosis"]
    assert set(result.log["healed_selectors"]) == {"po.vendor", "po.save", "line.qty"}
    assert result.log["usage"]["live_calls"] == 0
