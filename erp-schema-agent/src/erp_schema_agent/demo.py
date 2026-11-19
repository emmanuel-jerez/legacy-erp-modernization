"""The one-command demo: showcase questions and one blocked attack, offline from cassettes."""

from __future__ import annotations

import json

from erp_sandbox.llm import UsageTally

from . import settings
from .agent.graph import MODEL, SCREEN, VALIDATOR, run_agent
from .agent.model import make_model
from .trace import format_run

SHOWCASE = [
    "Which 5 customers had the highest booked revenue in Q3 2026?",
    "Which active products are below their reorder point, and how many units of each are already on "
    "open purchase orders?",
    "What was booked revenue by state in October 2026, and what share of the month's total did each state make up?",
    "How many Fernhollow sales orders were voided, and what share of all Fernhollow sales orders is that?",
]
# The attack is the eval's prompt-injection case, so demo and eval share its cassette.
ATTACK_ID = "ignore_instructions"


def _attack() -> str:
    cases = json.loads((settings.REPO_ROOT / "evals" / "guardrails.json").read_text(encoding="utf-8"))["cases"]
    return next(c["prompt"] for c in cases if c["id"] == ATTACK_ID)
RULE = "=" * 96


def run_demo(mode: str = "replay") -> int:
    tally = UsageTally()
    failed = 0
    attack = _attack()
    for i, question in enumerate([*SHOWCASE, attack], start=1):
        title = "Blocked attack" if question == attack else f"Showcase {i}"
        print(RULE)
        print(title)
        print(RULE)
        run = run_agent(question, model=make_model(mode), tally=tally)
        print(format_run(run))
        print()
        if question == attack:
            failed += run.blocked_by not in (SCREEN, MODEL, VALIDATOR)
        else:
            failed += run.status != "answered"
    usage = tally.as_dict()
    print(RULE)
    print(f"Model calls: {usage['calls']} ({usage['live_calls']} live, {usage['cassette_hits']} from cassettes); "
          f"model {', '.join(usage['models'])}, effort {', '.join(usage['efforts'])}")
    return 1 if failed else 0
