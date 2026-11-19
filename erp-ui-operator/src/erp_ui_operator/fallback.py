"""The LLM fallback: ask the model for one UI action when a deterministic step fails.

The prompt holds only deterministic text: the step's goal, the value to enter (with the
run id and secrets masked), a failure reason the code writes itself (never a Playwright
exception message, which carries timings and ZK ids), earlier refused or ineffective
proposals for this step, and the normalized snapshot. Identical UI states therefore
produce identical prompts and replay from cassettes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from erp_sandbox.llm import ModelClient, ModelResult

from .guardrails import ALLOWED_ACTIONS, ALLOWED_KEYS, ProposedAction
from .plan import Step
from .snapshot import Snapshot

SYSTEM_PROMPT = f"""You are the fallback layer of a UI automation agent working in iDempiere, a legacy ERP web application.
A deterministic step of a fixed plan failed: its locator found nothing, or its success check did not hold afterwards.
Propose exactly ONE next UI action that achieves the step's goal on the current page.

You get a numbered list of the visible interactive elements: [index] role "name" value=... @region.
Rules:
- element_index must be an index from the list (null only for "wait").
- "fill": type the step's value into a textbox, combobox, date or number field; the executor commits it with Tab.
- "select": choose an option in a dropdown field; value is the option's text.
- "click": buttons, tabs, list rows, checkboxes.
- "press": only the keys {", ".join(sorted(ALLOWED_KEYS))}, on the given element.
- "wait": only if the page is visibly still loading.
- Use the step's value exactly as given. Never invent or change values.
- Controls that delete, void, complete, process, post, reverse or close documents are blocked and end the run.
- The UI may have changed since the plan was written (renamed labels, new tooltips). Match by meaning.
Give a one-sentence reason."""

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "element_index", "value", "reason"],
    "properties": {
        "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
        "element_index": {"type": ["integer", "null"], "minimum": 0},
        "value": {"type": ["string", "null"], "maxLength": 300},
        "reason": {"type": "string", "maxLength": 300},
    },
}


@dataclass(frozen=True)
class Attempt:
    """An earlier proposal for the same step and what came of it."""

    proposal: ProposedAction
    outcome: str


def build_prompt(step: Step, failure: str, snapshot: Snapshot, attempts: list[Attempt]) -> str:
    lines = [
        f"Step: {step.key}",
        f"Goal: {step.goal}",
        f"Value to enter: {step.shown_value!r}" if step.shown_value is not None else "Value to enter: (none)",
        f"Why the deterministic step failed: {failure}",
        f"Success check: {step.post.describe()}",
    ]
    if attempts:
        lines.append("Earlier proposals for this step:")
        for a in attempts:
            p = a.proposal
            lines.append(f"- {p.action} element={p.element_index} value={p.value!r} -> {a.outcome}")
    lines.append("Page elements:")
    lines.append(snapshot.render())
    return "\n".join(lines)


def propose(client: ModelClient, step: Step, prompt: str) -> tuple[ProposedAction, ModelResult]:
    """Ask the model for one action given a prompt from ``build_prompt``."""
    result = client.complete_json(system=SYSTEM_PROMPT, prompt=prompt, schema=ACTION_SCHEMA, label=f"fallback:{step.key}")
    return ProposedAction.from_model(result.data), result
