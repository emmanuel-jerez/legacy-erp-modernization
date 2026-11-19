"""Seam: guarded execution of a port plus parity against golden outputs.

Golden values here are hand-written PostgreSQL results (what ``round(1.005, 2)`` and
friends return), not values recomputed the way the code computes them.
"""

from __future__ import annotations

from erp_migration_agent.parity import evaluate
from erp_migration_agent.runner import run_port

SNAPSHOT = {
    "session_tz": "America/Los_Angeles",
    "tables": {
        "c_currency": {
            "columns": {"c_currency_id": "numeric", "stdprecision": "numeric"},
            "rows": [["100", "2"], ["102", "2"]],
        }
    },
}
ARG_TYPES = ["numeric", "numeric"]


def case(cid, args, golden, source="sampled", fixtures=()):
    return {"id": cid, "args": args, "golden": golden, "source": source, "fixtures": list(fixtures)}


CASES = [
    case("half-up", ["1.005", "100"], {"value": "1.01"}),
    case("scale", ["2.2", "100"], {"value": "2.20"}),
    case("tiny-negative", ["-0.001", "100"], {"value": "0.00"}),
    case("unknown-currency", ["1.23456", "999"], {"value": "1.23456"}),
    case("null-amount", [None, "100"], {"value": None}),
    case("fixture-currency", ["1.23456", "990001"], {"value": "1.2346"}, source="edge",
         fixtures=[{"table": "c_currency", "row": {"c_currency_id": "990001", "stdprecision": "4"}}]),
]

GOOD_PORT = '''
from decimal import Decimal, ROUND_HALF_UP

def roundit(amount, currency_id, *, data):
    if amount is None or currency_id is None:
        return amount
    rows = data.select("c_currency", c_currency_id=currency_id)
    if not rows:
        return amount
    result = amount.quantize(Decimal(1).scaleb(-int(rows[0]["stdprecision"])), rounding=ROUND_HALF_UP)
    return result.copy_abs() if result == 0 else result
'''


def run(source, cases=CASES, **kw):
    outcome = run_port(source, "roundit", ARG_TYPES, cases, SNAPSHOT, **kw)
    return evaluate("visible", "numeric", cases, outcome)


def test_a_faithful_port_matches_every_golden_output():
    report = run(GOOD_PORT)
    assert report.passed, report.as_dict()["mismatches"]


def test_banker_rounding_scale_loss_and_negative_zero_are_mismatches():
    naive = '''
def roundit(amount, currency_id, *, data):
    if amount is None or currency_id is None:
        return amount
    rows = data.select("c_currency", c_currency_id=currency_id)
    if not rows:
        return amount
    return round(amount, int(rows[0]["stdprecision"])).normalize()
'''
    report = run(naive)
    failed = {m.case_id: m.reason for m in report.mismatches}
    assert set(failed) == {"half-up", "scale", "tiny-negative"}
    assert "negative zero" in failed["tiny-negative"]


def test_postgresql_nan_matches_only_an_unsigned_quiet_nan():
    port = "from decimal import Decimal\n\ndef roundit(amount, currency_id, *, data):\n    return Decimal(amount)\n"
    cases = [case("nan", ["NaN", "100"], {"value": "NaN"}), case("inf", ["-Infinity", "100"], {"value": "-Infinity"})]
    assert run(port, cases).passed
    negated = port.replace("Decimal(amount)", "Decimal(amount).copy_negate()")
    assert run(negated, cases).matched == 0


def test_wrong_python_type_never_matches():
    report = run("def roundit(amount, currency_id, *, data):\n    return None if amount is None else float(amount)\n")
    assert [m.case_id for m in report.mismatches if "float" in m.reason]
    assert report.matched == 1  # only the NULL case


def test_guard_rejection_fails_every_case_without_running_code():
    cases = [*CASES, case("legacy-raised", ["1", "100"], {"error": "22012"})]
    report = run("import os\n\ndef roundit(amount, currency_id, *, data):\n    os.system('true')\n", cases)
    assert report.matched == 0  # a port that never ran does not "raise like the legacy function"
    assert not report.passed
    assert "guard rejected" in report.runner_failure and "'os'" in report.runner_failure


def test_a_hung_port_is_killed_by_the_timeout():
    report = run("def roundit(amount, currency_id, *, data):\n    while True:\n        pass\n", timeout_s=2)
    assert report.matched == 0
    assert "timed out" in report.runner_failure or "exited" in report.runner_failure


def test_an_exception_is_recorded_per_case_and_matches_a_legacy_error():
    port = "def roundit(amount, currency_id, *, data):\n    return amount / 0\n"
    cases = [case("div", ["1", "100"], {"error": "22012"}), case("ok", ["1", "100"], {"value": "1"})]
    report = run(port, cases)
    assert report.matched == 1
    assert "DivisionByZero" in report.mismatches[0].reason or "ZeroDivision" in report.mismatches[0].reason
