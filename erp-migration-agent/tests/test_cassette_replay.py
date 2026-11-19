"""Seam: committed cassettes still replay the committed eval results (offline, no live calls).

Fails when a prompt changes without re-recording, because replay then misses a cassette.
Needs the sandbox database (the oracle); skips without it.
"""

from __future__ import annotations

import json

import pytest

from erp_migration_agent import config
from erp_migration_agent.evals import run_eval

pytestmark = pytest.mark.integration


def test_replay_reproduces_the_committed_eval_report(oracle, tmp_path):
    committed = json.loads((config.EVAL_REPORTS_DIR / "eval-report.json").read_text())
    functions = [f["function"] for f in committed["functions"]]
    assert run_eval(record=False, functions=functions, jobs=3, reports_dir=tmp_path) == 0
    replayed = json.loads((tmp_path / "eval-report.json").read_text())
    assert replayed["totals"]["live_calls"] == 0
    for want, got in zip(committed["functions"], replayed["functions"], strict=True):
        assert got["status"] == want["status"]
        assert got["held_out_parity"] == want["held_out_parity"]
        assert got["repair_iterations"] == want["repair_iterations"]
        assert got["cases"] == want["cases"]
        assert got["visible_parity"] == want["visible_parity"]
        assert got["usage"]["calls"] == got["usage"]["cassette_hits"] == want["usage"]["calls"]
