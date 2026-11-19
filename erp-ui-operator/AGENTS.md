# Working in erp-ui-operator

A Playwright and LLM hybrid that creates **draft** purchase orders through iDempiere's ZK
web UI and verifies them in the database. Read [README.md](README.md) first, then the
shared rules in `../BUILD-BRIEF.md` and `../erp-sandbox/AGENTS.md`.

## Rules

- This is the only portfolio repo that writes to the shared `idempiere` database, and only
  through the web UI, and only draft purchase orders. Never complete, post, void or edit
  existing documents or master data. Never stop, restart or snapshot the containers.
- Every order a run creates carries both tags from `erpdb.RunTag` (Description prefix and
  POReference). `ui-operator cleanup` deletes only unprocessed drafts with both tags.
- Model calls go through `erp_sandbox.llm` only. Unit tests use `ScriptedModelClient`.
  Cassettes live in `cassettes/` and are committed.
- Integration tests read the live `idempiere` database (read-only), because the UI writes
  there; they skip when it is down. Browser tests are marked `browser` and skip when the
  UI is unreachable.
- Tests: `uv run pytest`. Evals: `uv run ui-operator eval` (live UI; records cassette
  misses), `--llm-mode replay` to prove offline reproducibility. Commit the report and new
  cassettes; refresh `docs/evidence/` from a drifted eval run if the run log format changes.

## Driving iDempiere's ZK UI

- **Never locate by ZK id.** Ids like `wFTB10` change per page load. Use label cells,
  login-row labels, toolbar titles, visible text, and `instancename` attributes
  (`C_Order0C_BPartner_ID`), as `selectors.py` does.
- **Postconditions must prove server state, not client text.** A ZK input shows typed
  text before the server applies it, and iDempiere skips a callout while another is still
  running. Check a field the server computes from the value (PO Quantity after Quantity,
  Unit Price after Product, invoice partner and price list after Business Partner), and
  re-check values after each save. Reason: client-only checks let a draft be saved with
  quantity 1 and without its POReference; the database check caught both.
- **Read saved headers back from the database mid-run.** The `po.readback` check step
  compares the stored header with the task and re-runs the header fills and save once
  (clearing each field first so ZK sends a change) before failing. A slow partner
  callout can reset POReference after the UI already showed the typed value.
- **Settle means "quiet for a window", not "idle once".** iDempiere posts follow-up
  events after a response, so `settle()` waits for ZK to stay idle for 600 ms.
- **Repeat only idempotent actions.** A fill, select or type may be re-applied once (a
  re-applied fill clears the field and types key by key, because ZK sends no change for an
  unchanged value and iDempiere's search fields sometimes ignore a programmatic fill). Clicks are never repeated: a
  second click can undo a row selection.
- **Prompt text is part of the cassette key.** Changing a step goal, a postcondition's
  description, the snapshot format or the system prompt invalidates cassettes. Re-record
  only the misses (`ERP_LLM_MODE=record`), and say so in the commit.
- **`/tmp/pytest-of-<user>` is shared** with the other portfolio agents; use
  `--basetemp` when you need a run's evidence after the fact.

## Lessons from review

- **Check destructive intent on every signal, not just the button text.** A harmless
  "OK" can confirm a "Delete record?" dialog, and a combobox value can be "Complete".
  The guardrail matches word stems ("Deleting", "Processed") against the element's full
  label and text, its region (the dialog title), the enclosing dialog's body text (where
  iDempiere message boxes put the question) and the value to enter, before any
  truncation. Reason: labels alone let a confirm button through.
- **Out-of-scope means abort, not retry.** A model value the task did not supply aborts
  the run, like a destructive target or a region outside the step. Only harmless slips
  (unknown index, disabled element, wrong action kind) get a retry. Reason: the spec says
  abort on anything outside the task scope, and failing closed is recoverable.
- **Validate identifiers that reach a LIKE pattern.** Run ids match `^[a-z0-9]{1,15}$`,
  so they cannot widen a match with `%` or `_` and always fit POReference (20 chars),
  which also keeps them fully replaceable in snapshots. Reason: an unescaped id widened
  cleanup and verification.
- **Scope destructive SQL by more than one user-editable field, and never hold locks
  while waiting for a person.** Cleanup requires the Description prefix and the
  POReference tag, lists and confirms first, then re-selects `FOR UPDATE` in one
  transaction and refuses if the set changed.
- **Compare ids, not names, and see every row.** The verifier checks `C_BPartner_ID`
  against the one vendor with that name, and reads lines with a LEFT JOIN so a line
  without a product counts as unexpected. iDempiere ids are NUMERIC; cast them to int.
- **Heal a selector only when the selector was the problem.** Heal after a missing
  locator, for a proposal of the step's own action, and only if the candidate entry
  resolves to the element the model picked, checked before the action re-renders it.
  Reason: a late callout can make a postcondition pass after the model touched a
  different field, and healing then would retarget later steps.
- **Every run ends with evidence and a verdict.** The run boundary, including browser
  setup, catches every exception, always stops the trace and closes the browser, and records a failed
  `verification_ran` check if the database check itself errors.
- **Deterministic actions get the same destructive check as model actions,** on the real
  element's text, title and enclosing dialog, not just the selector-map name.
- **Least privilege per step:** a fallback may only use actions that fit the step's kind
  (`plan.STEP_ACTIONS`). Steps that carry a secret have no fallback, so the secret never
  reaches the model.
- **Keep flickering values out of prompts.** Failure reasons that go into a prompt carry
  no counts or timings ("matches more than one element"), or cassette keys drift.
- **Commands clean up only what they created.** `demo` and `eval` pre-assign run ids and
  delete those runs' drafts by run tag in a `finally` (a failed or interrupted case still
  cleans the earlier ones; one failing delete does not stop the rest). `demo` replays
  cassettes by default; only `cleanup` sweeps every tagged draft.
- **A probe must assert the exact rule it demonstrates.** The guardrail probe passes only
  if the `destructive_target` rule fired, the run aborted and no order exists; a failed
  probe fails `eval`. Reason: accepting any block let it pass without proving the
  destructive-control rule.
- **Reports never overwrite each other and never blur cost.** Report files are named by
  date and cassette mode, with the time appended if that name exists, and a hand-added
  `-high` (or other effort) suffix for runs at a non-default `ERP_LLM_EFFORT`; the report separates the cost of calls made live in this eval
  from the recorded cost of replayed cassettes. README figures come from those files only.
