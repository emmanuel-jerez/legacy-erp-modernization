"""Action guardrails: every proposed UI action is checked here before it runs.

The model only proposes. Code decides, using rules that do not depend on the model:

* an allowlist of action types and keys;
* a block on destructive or irreversible targets (Delete, Void, Complete, Process,
  Post, Reverse, Close, Document Action and similar): the order must stay in Draft;
* scope: the target must sit in a region the current step may touch, and any text typed
  or selected must be a value the task itself supplies;
* budgets: caps on total actions, fallback calls per run and fallback calls per step.

A verdict is ``ok``, ``retry`` or ``abort``. Anything destructive or outside the task's
scope aborts the run: a disallowed action type or key, a destructive target (by its
label, text, dialog title or the value to enter), a target outside the step's regions,
or a value the task does not supply. Failing closed is cheaper than undoing a posted
document. Only harmless mistakes get a retry within the step's budget: an index that is
not in the snapshot, a disabled element, or an action this step does not use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .snapshot import INPUT_ROLES, Element, Snapshot

ALLOWED_ACTIONS = frozenset({"click", "fill", "press", "select", "wait"})
ALLOWED_KEYS = frozenset({"Tab", "Enter", "Escape"})
VALUE_ACTIONS = frozenset({"fill", "select"})

# Word stems, so "Deleting", "Processed" or "Voided" are caught as well as "Delete".
DESTRUCTIVE = re.compile(
    r"\b(delet\w*|void\w*|complet\w*|process\w*|(un)?post(s|ed|ing)?|revers\w*|clos(e|ed|es|ing)"
    r"|document action|re-?activat\w*|prepar\w*|approv\w*|reject\w*|unlock\w*|archiv\w*|remov\w*|discard\w*)\b",
    re.IGNORECASE,
)

OK, RETRY, ABORT = "ok", "retry", "abort"


@dataclass(frozen=True)
class ProposedAction:
    action: str
    element_index: int | None
    value: str | None
    reason: str = ""

    @classmethod
    def from_model(cls, data: dict[str, Any]) -> "ProposedAction":
        index = data.get("element_index")
        value = data.get("value")
        return cls(
            action=str(data.get("action", "")),
            element_index=index if isinstance(index, int) and not isinstance(index, bool) else None,
            value=value if isinstance(value, str) else None,
            reason=str(data.get("reason", ""))[:300],
        )

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "element_index": self.element_index, "value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class StepScope:
    """What one step may do. Regions match exactly or by prefix (``"dialog: "``)."""

    regions: frozenset[str]
    values: frozenset[str] = frozenset()
    actions: frozenset[str] = ALLOWED_ACTIONS

    def region_allowed(self, region: str) -> bool:
        return any(region == r or (r.endswith(" ") and region.startswith(r)) for r in self.regions)


@dataclass(frozen=True)
class Verdict:
    severity: str
    rule: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.severity == OK

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "rule": self.rule, "reason": self.reason}


def is_destructive(*names: str) -> bool:
    return any(DESTRUCTIVE.search(n or "") for n in names)


def destructive_target(element: Element, value: str | None = None) -> bool:
    """True when the element, the dialog it sits in, or the value to enter is destructive."""
    return is_destructive(element.full_text or element.name, element.region, value or "")


def check_action(action: ProposedAction, snapshot: Snapshot, scope: StepScope) -> Verdict:
    """Decide whether a proposed action may run. Pure: no browser, no model."""
    if action.action not in ALLOWED_ACTIONS:
        return Verdict(ABORT, "action_not_allowed", f"action {action.action!r} is not in the allowlist")
    if action.action not in scope.actions:
        return Verdict(RETRY, "action_not_for_step", f"action {action.action!r} is not used by this step")
    if action.action == "wait":
        return Verdict(OK, "ok", "wait has no target")

    if action.element_index is None:
        return Verdict(RETRY, "missing_element", f"{action.action} needs an element_index")
    element = snapshot.get(action.element_index)
    if element is None:
        return Verdict(RETRY, "unknown_element", f"element {action.element_index} is not in the snapshot")

    if destructive_target(element, action.value):
        return Verdict(
            ABORT, "destructive_target",
            f"element {element.index} {element.name!r} in {element.region!r} (value {action.value!r}) is destructive",
        )
    if not scope.region_allowed(element.region):
        return Verdict(ABORT, "out_of_scope_region", f"element {element.index} is in region {element.region!r}, outside this step")
    if element.disabled:
        return Verdict(RETRY, "disabled_element", f"element {element.index} {element.name!r} is disabled")

    if action.action == "press":
        if action.value not in ALLOWED_KEYS:
            return Verdict(ABORT, "key_not_allowed", f"key {action.value!r} is not in {sorted(ALLOWED_KEYS)}")
    elif action.action in VALUE_ACTIONS:
        if action.value is None or action.value not in scope.values:
            return Verdict(ABORT, "value_not_in_task", f"value {action.value!r} is not a value this step supplies")
        if action.action == "fill" and (element.role not in INPUT_ROLES or element.role == "password"):
            return Verdict(RETRY, "not_fillable", f"element {element.index} is a {element.role}, not a text field")
    return Verdict(OK, "ok", "allowed")


class BudgetExceeded(RuntimeError):
    """A run or step cap was reached; the run stops."""


@dataclass
class RunBudget:
    max_actions: int = 150
    max_fallback_calls: int = 10
    max_fallback_per_step: int = 3
    actions: int = 0
    fallback_calls: int = 0
    per_step: dict[str, int] = field(default_factory=dict)

    def spend_action(self) -> None:
        if self.actions >= self.max_actions:
            raise BudgetExceeded(f"action cap reached ({self.max_actions})")
        self.actions += 1

    def can_fallback(self, step_key: str) -> bool:
        return (
            self.fallback_calls < self.max_fallback_calls
            and self.per_step.get(step_key, 0) < self.max_fallback_per_step
        )

    def spend_fallback(self, step_key: str) -> None:
        if not self.can_fallback(step_key):
            raise BudgetExceeded(
                f"fallback cap reached (run {self.fallback_calls}/{self.max_fallback_calls}, "
                f"step {self.per_step.get(step_key, 0)}/{self.max_fallback_per_step})"
            )
        self.fallback_calls += 1
        self.per_step[step_key] = self.per_step.get(step_key, 0) + 1
