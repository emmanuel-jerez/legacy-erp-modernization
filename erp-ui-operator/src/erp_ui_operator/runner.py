"""Run one task through the iDempiere web UI: deterministic first, LLM fallback second.

For each plan step the runner:

1. skips it if its ``when`` condition is false, or if ``skip_if_done`` and the
   postcondition already holds;
2. resolves the selector-map target and performs the action (the deterministic layer);
3. checks the postcondition;
4. on a missing locator or a failed postcondition, takes a normalized snapshot and asks
   the model for one action, checks that action against the guardrails, executes it if
   allowed and checks the postcondition again, within the step and run budgets;
5. when a fallback succeeds on a label- or title-based target, heals the selector map for
   the rest of the run, so later steps with the same target stay deterministic.

After the flow, success is decided by the database (``erpdb.verify_run``). Evidence goes
to ``runs/<run-id>/``: a screenshot per step, the Playwright trace and ``run.json``.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from erp_sandbox import config
from erp_sandbox.llm import ModelClient, ModelError, UsageTally, request_key
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

from . import checks, erpdb
from .fallback import ACTION_SCHEMA, SYSTEM_PROMPT, Attempt, build_prompt, propose
from .guardrails import ABORT, OK, BudgetExceeded, ProposedAction, RunBudget, check_action, is_destructive
from .plan import Step, build_plan
from .selectors import Target, bind, healed_target, resolve, selector_map
from .snapshot import COLLECT_JS, MAX_ELEMENTS, Snapshot, normalize
from .task import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "runs"
LOCATOR_TIMEOUT_MS = 4000
LOCATOR_FAILURE_PREFIXES = ("no visible element matches", "selector-map entry")
ACTION_TIMEOUT_MS = 10000
REAPPLY_ACTIONS = frozenset({"fill", "select", "type"})
# The text a person would read around an element: its own text and title, and the
# enclosing dialog's text (iDempiere puts confirm questions in the dialog body).
ELEMENT_CONTEXT_JS = """(e) => {
  const t = (n) => (n ? (n.innerText || n.textContent || '') : '');
  const d = e.closest('.z-window-highlighted, .z-window-modal, .z-messagebox-window');
  return [t(e), e.title || '', e.getAttribute('aria-label') || '', d ? t(d) : ''].join(' ').replace(/\\s+/g, ' ');
}"""


class GuardrailAbort(RuntimeError):
    """A guardrail refused an action with severity abort."""


class StepFailed(RuntimeError):
    """A step could not be completed by either layer."""


class LocatorMissing(RuntimeError):
    """The selector-map target matched no element, or more than one."""


@dataclass
class RunOptions:
    drift: str | None = None
    headed: bool = False
    runs_dir: Path = RUNS_DIR
    url: str = config.IDEMPIERE_URL
    budget: RunBudget = field(default_factory=RunBudget)
    trace: bool = True


@dataclass
class RunResult:
    run_id: str
    success: bool
    run_dir: Path
    log: dict[str, Any]


def new_run_id() -> str:
    return secrets.token_hex(4)


_ZK_BUSY = "() => !!(window.zAu && zAu.processing && zAu.processing())"


def settle(page: Page, quiet_ms: int = 600) -> None:
    """Wait until ZK has had no request in flight for ``quiet_ms``.

    iDempiere posts follow-up events (echo events, chained callouts) after a response, so
    one idle sample is not enough: the page must stay idle for a short window.
    """
    deadline = time.monotonic() + 20
    idle_since: float | None = None
    while time.monotonic() < deadline:
        try:
            busy = page.evaluate(_ZK_BUSY)
        except PlaywrightError:
            busy = False
        now = time.monotonic()
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = now
        elif (now - idle_since) * 1000 >= quiet_ms:
            return
        page.wait_for_timeout(100)


def select_option(page: Page, field: Locator, option: str) -> None:
    """Pick ``option`` in a ZK combobox whose text input is ``field``."""
    button = field.locator("xpath=following-sibling::a[contains(@class,'z-combobox-button')]")
    button.click(timeout=ACTION_TIMEOUT_MS)
    page.wait_for_timeout(400)
    items = page.locator(".z-combobox-popup.z-combobox-open li.z-comboitem").filter(visible=True)
    for i in range(items.count()):
        item = items.nth(i)
        if " ".join(item.inner_text().replace("\xa0", " ").split()) == option:
            item.click(timeout=ACTION_TIMEOUT_MS)
            return
    page.keyboard.press("Escape")
    raise LocatorMissing(f"option {option!r} is not in the dropdown")


def perform(page: Page, locator: Locator, action: str, value: str | None) -> None:
    """Execute one primitive action on a resolved element."""
    if action == "click":
        locator.click(timeout=ACTION_TIMEOUT_MS)
    elif action == "fill":
        locator.click(timeout=ACTION_TIMEOUT_MS)
        locator.fill(value or "", timeout=ACTION_TIMEOUT_MS)
        locator.press("Tab")  # ZK commits a field's value on blur
    elif action == "type":
        locator.click(timeout=ACTION_TIMEOUT_MS)
        locator.fill("", timeout=ACTION_TIMEOUT_MS)
        locator.press_sequentially(value or "", delay=30)  # key events trigger ZK's live search
    elif action == "select":
        select_option(page, locator, value or "")
    elif action == "press":
        locator.press(value or "", timeout=ACTION_TIMEOUT_MS)
    else:
        raise ValueError(f"unsupported action {action!r}")


class Operator:
    def __init__(self, page: Page, client: ModelClient, spec: TaskSpec, tag: erpdb.RunTag, options: RunOptions, run_dir: Path):
        self.page = page
        self.client = client
        self.spec = spec
        self.tag = tag
        self.options = options
        self.run_dir = run_dir
        self.selectors = selector_map(options.drift)
        self.healed: dict[str, dict[str, str]] = {}
        self.budget = options.budget
        self.usage = UsageTally()
        self.records: list[dict[str, Any]] = []
        self.guardrail_blocks: list[dict[str, Any]] = []
        self.plan: list[Step] = []
        self.last_db_detail = ""

    # ------------------------------------------------------------- targets
    def _target(self, step: Step) -> Target:
        assert step.target is not None
        return bind(self.selectors[step.target], **dict(step.bindings))

    def _resolve(self, target: Target) -> Locator:
        """Wait for exactly one visible match. ZK re-renders lists while it updates them,
        so a match can flicker; poll instead of trusting a single count."""
        loc = resolve(self.page, target)
        deadline = time.monotonic() + LOCATOR_TIMEOUT_MS / 1000
        count = 0
        while True:
            count = loc.count()
            if count == 1:
                return loc.first
            if time.monotonic() >= deadline:
                break
            self.page.wait_for_timeout(200)
        if count == 0:
            raise LocatorMissing(f"no visible element matches selector-map entry {target.describe()}")
        # No count in the message: it can flicker, and the message goes into the prompt.
        raise LocatorMissing(f"selector-map entry {target.describe()} matches more than one element")

    def _act_and_check(
        self, step: Step, locator: Locator, action: str, value: str | None, force_change: bool = False
    ) -> bool:
        """Perform an action and wait for the postcondition.

        A ZK field sometimes takes the typed text without the server applying it (its
        callouts never run). Value actions are idempotent, so one re-application of the
        same, already-validated action is allowed before the step counts as failed.
        Clicks are never repeated: a second click can undo the first (a row selection).
        """
        tries = 2 if action in REAPPLY_ACTIONS else 1
        for attempt in range(tries):
            if (attempt or force_change) and action == "fill":
                # ZK only sends a change when the value differs from what it last saw.
                self.budget.spend_action()
                perform(self.page, locator, "fill", "")
                settle(self.page)
            self.budget.spend_action()
            perform(self.page, locator, action, value)
            settle(self.page)
            if checks.wait_until(self.page, step.post, timeout_ms=step.wait_ms):
                return True
        return False

    # --------------------------------------------------------------- steps
    def run_step(self, index: int, step: Step, note: str | None = None, force_change: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        rec: dict[str, Any] = {"index": index, "key": step.key, "goal": step.goal, "action": step.action,
                               "value": step.shown_value, "postcondition": step.post.describe(), "fallback": []}
        if note:
            rec["note"] = note
        try:
            if step.when is not None and not checks.evaluate(self.page, step.when):
                rec["status"] = "skipped"
                rec["detail"] = f"condition not met: {step.when.describe()}"
                return rec
            if step.skip_if_done and checks.evaluate(self.page, step.post):
                rec["status"] = "skipped"
                rec["detail"] = "postcondition already held"
                return rec

            failure = self._deterministic(step, rec, force_change)
            if failure is None:
                rec["status"] = "ok"
                rec["layer"] = "deterministic"
                return rec
            rec["deterministic_failure"] = failure
            if not step.fallback:
                raise StepFailed(f"{step.key}: {failure} (no fallback for this step)")
            self._fallback(step, failure, rec)
            rec["status"] = "ok"
            rec["layer"] = "llm-fallback"
            return rec
        except Exception as exc:
            rec["status"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            rec["duration_ms"] = int((time.monotonic() - started) * 1000)
            rec["screenshot"] = self._screenshot(index, step.key)
            self.records.append(rec)

    def _deterministic(self, step: Step, rec: dict[str, Any], force_change: bool = False) -> str | None:
        """Run the step's own action. Returns None on success, else a failure reason for the prompt."""
        if step.action == "check":
            return self._check_with_repair(step, rec)
        if step.action == "goto":
            self.page.goto(step.value or self.options.url, wait_until="load")
            settle(self.page)
            return None if checks.wait_until(self.page, step.post, timeout_ms=20000) else f"postcondition not met: {step.post.describe()}"
        target = self._target(step)
        rec["target"] = target.describe()
        if is_destructive(target.name):
            raise GuardrailAbort(f"plan step {step.key} targets a destructive control {target.describe()}")
        try:
            loc = self._resolve(target)
            context = loc.evaluate(ELEMENT_CONTEXT_JS)
            if is_destructive(context):
                raise GuardrailAbort(f"plan step {step.key}: the element or its dialog reads as destructive")
            if self._act_and_check(step, loc, step.action, step.value, force_change):
                return None
        except LocatorMissing as exc:
            return str(exc)
        except PlaywrightError:
            return f"the action on selector-map entry {target.describe()} did not complete"
        return f"after {step.action} on {target.describe()}, postcondition not met: {step.post.describe()}"

    def _check_with_repair(self, step: Step, rec: dict[str, Any]) -> str | None:
        """A verify-only step. On a mismatch, re-run its repair steps once, then check again."""
        if checks.evaluate(self.page, step.post, db_probe=self.db_probe):
            return None
        if not step.repair:
            return f"check failed: {step.post.describe()}"
        rec["repair"] = {"reason": self.last_db_detail, "steps": list(step.repair)}
        by_key = {s.key: s for s in self.plan}
        for key in step.repair:  # a failing repair step raises out of run_step
            self.run_step(len(self.records), by_key[key], note=f"repair for {step.key}", force_change=True)
        if checks.evaluate(self.page, step.post, db_probe=self.db_probe):
            rec["repair"]["result"] = "fixed"
            return None
        return f"check failed after one repair: {self.last_db_detail}"

    def db_probe(self, post: Any) -> bool:
        """Database-backed postconditions. Only ``db_header`` exists."""
        if post.kind != "db_header":
            raise ValueError(f"unknown database postcondition {post.kind!r}")
        with erpdb.connect() as conn:
            orders, _ = erpdb.fetch_run(conn, self.tag)
            vendor_ids = erpdb.vendor_ids(conn, self.spec.vendor)
        result = erpdb.compare_header(self.spec, self.tag, orders, vendor_ids=vendor_ids)
        self.last_db_detail = "; ".join(f"{c.name}: {c.detail}" for c in result.failures) or "ok"
        return result.passed

    def _fallback(self, step: Step, failure: str, rec: dict[str, Any]) -> None:
        attempts: list[Attempt] = []
        while True:
            if not self.budget.can_fallback(step.key):
                raise StepFailed(f"{step.key}: fallback budget exhausted after {len(attempts)} proposal(s); {failure}")
            snapshot = self._take_snapshot()
            self.budget.spend_fallback(step.key)
            prompt = build_prompt(step, failure, snapshot, attempts)
            prompt_file = self.run_dir / "prompts" / f"{step.key}-{len(attempts) + 1}.txt"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(prompt + "\n", encoding="utf-8")
            try:
                proposal, result = propose(self.client, step, prompt)
            except ModelError as exc:
                rec["fallback"].append({
                    "prompt": str(prompt_file.relative_to(self.run_dir)),
                    "error": str(exc),
                    "request_key": request_key(getattr(self.client, "model", "?"), getattr(self.client, "effort", "?"),
                                               SYSTEM_PROMPT, prompt, ACTION_SCHEMA)[:12],
                })
                raise StepFailed(f"{step.key}: model call failed: {exc}") from exc
            self.usage.add(result)
            verdict = check_action(proposal, snapshot, step.scope)
            entry: dict[str, Any] = {
                "prompt": str(prompt_file.relative_to(self.run_dir)),
                "proposal": proposal.as_dict(),
                "verdict": verdict.as_dict(),
                "snapshot_elements": len(snapshot.elements),
                "from_cassette": result.from_cassette,
                "cassette_key": result.key[:32],
                "model_ms": result.duration_ms,
            }
            rec["fallback"].append(entry)
            if verdict.severity != OK:
                self.guardrail_blocks.append({"step": step.key, **verdict.as_dict(), "proposal": proposal.as_dict()})
                if verdict.severity == ABORT:
                    raise GuardrailAbort(f"{step.key}: {verdict.reason}")
                attempts.append(Attempt(proposal, f"refused by guardrail: {verdict.reason}"))
                continue
            heal = None
            if failure.startswith(LOCATOR_FAILURE_PREFIXES) and proposal.action == step.action:
                heal = self._heal_candidate(step, proposal, snapshot)  # before the action re-renders the field
            executed, passed = self._execute_proposal(step, proposal)
            entry["executed"] = executed
            entry["postcondition_after"] = passed
            if passed:
                if heal is not None:
                    self._commit_heal(step, heal, rec)
                return
            outcome = "executed, but the success check still fails" if executed else "could not be executed on the page"
            attempts.append(Attempt(proposal, outcome))

    def _take_snapshot(self) -> Snapshot:
        raw = self.page.evaluate(COLLECT_JS, MAX_ELEMENTS)
        return normalize(raw, volatile=(self.tag.run_id,))

    def _execute_proposal(self, step: Step, proposal: ProposedAction) -> tuple[bool, bool]:
        """Run an allowed proposal. Returns (executed, postcondition held)."""
        if proposal.action == "wait":
            self.page.wait_for_timeout(1000)
            return True, checks.wait_until(self.page, step.post, timeout_ms=step.wait_ms)
        value = proposal.value
        if value is not None and value == step.shown_value:
            value = step.value  # map masked display values back to the real ones
        loc = self.page.locator(f'[data-uiop-idx="{proposal.element_index}"]').first
        try:
            return True, self._act_and_check(step, loc, proposal.action, value)
        except (PlaywrightError, LocatorMissing):
            return False, False

    def _heal_candidate(self, step: Step, proposal: ProposedAction, snapshot: Snapshot) -> Target | None:
        """A replacement selector-map entry built from the label of the element the model picked.

        Checked before the action runs, while the element still carries its snapshot mark:
        the candidate must resolve to exactly that element, not a lookalike.
        """
        if step.target is None or proposal.element_index is None:
            return None
        element = snapshot.get(proposal.element_index)
        if element is None or "<" in element.label:
            return None
        healed = healed_target(self.selectors[step.target], element.label)
        if healed is None:
            return None
        try:
            loc = self._resolve(healed)
            if loc.get_attribute("data-uiop-idx") != str(proposal.element_index):
                return None
        except (LocatorMissing, PlaywrightError):
            return None
        return healed

    def _commit_heal(self, step: Step, healed: Target, rec: dict[str, Any]) -> None:
        """Use the candidate for the rest of the run; only after the step's postcondition held."""
        assert step.target is not None
        original = self.selectors[step.target]
        self.selectors[step.target] = healed
        self.healed[step.target] = {"from": original.describe(), "to": healed.describe(), "step": step.key}
        rec["healed"] = self.healed[step.target]

    def _screenshot(self, index: int, key: str) -> str | None:
        path = self.run_dir / "screens" / f"{index:02d}-{key}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path))
        except PlaywrightError:
            return None
        return str(path.relative_to(self.run_dir))

    def logout(self) -> None:
        """Best effort: end the iDempiere session. No fallback, never fails the run."""
        try:
            link = self.page.locator(".desktop-user-panel").get_by_text("Log Out", exact=True)
            if link.count():
                link.first.click(timeout=5000)
                settle(self.page)
        except PlaywrightError:
            pass



def run_task(
    spec: TaskSpec,
    client: ModelClient,
    options: RunOptions | None = None,
    *,
    run_id: str | None = None,
) -> RunResult:
    """Execute a task end to end and verify it in the database."""
    options = options or RunOptions()
    run_id = run_id or new_run_id()
    tag = erpdb.RunTag(run_id)
    run_dir = options.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    plan = build_plan(spec, tag, options.url)
    started = time.monotonic()
    log: dict[str, Any] = {
        "run_id": run_id,
        "tag": {"description": tag.description(spec), "po_reference": tag.po_reference},
        "task": spec.as_dict(),
        "drift": options.drift,
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "budget": {"max_actions": options.budget.max_actions, "max_fallback_calls": options.budget.max_fallback_calls,
                   "max_fallback_per_step": options.budget.max_fallback_per_step},
    }
    with erpdb.connect() as conn:
        before = erpdb.fingerprint(conn)

    flow_error: str | None = None
    operator: Operator | None = None
    try:  # setup failures (no Chromium, UI down) also end with evidence and a verdict
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not options.headed)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
            if options.trace:
                context.tracing.start(screenshots=True, snapshots=True)
            page = context.new_page()
            operator = Operator(page, client, spec, tag, options, run_dir)
            operator.plan = plan
            try:
                for step in plan:
                    operator.run_step(len(operator.records), step)
            except (GuardrailAbort, StepFailed, BudgetExceeded) as exc:
                flow_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - any failure must still leave evidence and a verdict
                first_line = (str(exc).splitlines() or [""])[0]
                flow_error = f"{type(exc).__name__}: {first_line}"
            finally:
                operator.logout()
                try:
                    if options.trace:
                        context.tracing.stop(path=str(run_dir / "trace.zip"))
                except PlaywrightError:
                    pass
                finally:
                    browser.close()

    except Exception as exc:  # noqa: BLE001
        flow_error = flow_error or f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}"

    try:
        verification = erpdb.verify_run(spec, tag, fingerprint_before=before)
    except Exception as exc:  # noqa: BLE001 - report it; a run without a verdict is a failure
        verification = erpdb.Verification(passed=False, checks=[erpdb.Check("verification_ran", False, f"{type(exc).__name__}: {exc}")])
    success = flow_error is None and verification.passed
    if operator is None:  # the browser never started
        operator = Operator(None, client, spec, tag, options, run_dir)  # type: ignore[arg-type]
    steps = operator.records
    log.update({
        "duration_ms": int((time.monotonic() - started) * 1000),
        "success": success,
        "flow_error": flow_error,
        "steps": steps,
        "totals": {
            "steps_planned": len(plan),
            "repairs": sum(1 for s in steps if s.get("repair")),
            "steps_run": sum(1 for s in steps if s.get("status") != "skipped"),
            "steps_skipped": sum(1 for s in steps if s.get("status") == "skipped"),
            "actions": operator.budget.actions,
            "fallback_steps": sum(1 for s in steps if s.get("fallback")),
            "fallback_calls": operator.budget.fallback_calls,
            "guardrail_blocks": len(operator.guardrail_blocks),
        },
        "guardrail_blocks": operator.guardrail_blocks,
        "healed_selectors": operator.healed,
        "usage": operator.usage.as_dict(),
        "verification": verification.as_dict(),
        "diagnosis": diagnose(flow_error, verification, steps),
    })
    (run_dir / "run.json").write_text(json.dumps(log, indent=2, default=str) + "\n", encoding="utf-8")
    if operator.healed:
        (run_dir / "healed-selectors.json").write_text(json.dumps(operator.healed, indent=2) + "\n", encoding="utf-8")
    return RunResult(run_id=run_id, success=success, run_dir=run_dir, log=log)


def diagnose(flow_error: str | None, verification: erpdb.Verification, steps: list[dict[str, Any]]) -> str | None:
    """A one-paragraph explanation of a failed run, built from the evidence."""
    if flow_error is None and verification.passed:
        return None
    parts: list[str] = []
    if flow_error:
        failed = next((s for s in reversed(steps) if s.get("status") == "failed"), None)
        where = f" at step {failed['key']}" if failed else ""
        parts.append(f"UI flow stopped{where}: {flow_error}.")
        if failed and failed.get("deterministic_failure"):
            parts.append(f"Deterministic layer: {failed['deterministic_failure']}.")
        last = (failed or {}).get("fallback", [])[-1:]
        if last and "proposal" in last[0]:
            parts.append(f"Last fallback proposal {last[0]['proposal']} -> {last[0]['verdict']['rule']}.")
    if not verification.passed:
        parts.append("Database check failed: " + "; ".join(f"{c.name} ({c.detail})" for c in verification.failures) + ".")
    return " ".join(parts)
