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
- Tests: `uv run pytest`.

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
