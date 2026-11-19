# Working in erp-migration-agent

A LangGraph workflow that ports legacy iDempiere PL/pgSQL functions to Python and proves parity
with characterization tests, using the legacy function as the oracle. Read
[README.md](README.md) and [the metric contract](docs/metric-contract.md) first. The shared
portfolio rules live in `../BUILD-BRIEF.md`; the sandbox contract in `../erp-sandbox/AGENTS.md`.

## Rules

- **The metric contract leads.** Change `docs/metric-contract.md` first (same commit) whenever
  parity, case generation, the split or the pass bar changes. Code that disagrees with it is a bug.
- **Model calls go through `erp_sandbox.llm` only**: `ClaudeCodeClient` for live and cassette
  runs, `ScriptedModelClient` in unit tests. Never call `claude` directly.
- **Prompts must be byte-stable** for cassette replay: build them only from the legacy source,
  the snapshot, the spec and cases sorted by id. No run ids, timestamps or paths.
- **Held-out cases never reach a prompt.** Only `prompts.visible_only` cases and visible parity
  reports feed the port and repair prompts.
- **The oracle is read-only in effect.** Use `Oracle.query_readonly` for sampling SQL and
  `Oracle.record` for calls; never write to `erp_migration_agent` outside a rolled-back
  transaction. Do not touch `idempiere` or `idempiere_seed`.
- **Generated code is untrusted.** It runs only through `runner.run_port` (guard, subprocess,
  limits) during a migration. Approved modules in `migrated/` are reviewed code and may run in-process.
- **Cassettes are evidence.** Record once with `uv run migration-agent eval --record` (at most 3
  functions in parallel, the portfolio's live-call cap), commit `cassettes/` with the eval report,
  and re-record only functions whose prompts changed. Delete cassettes no replay uses.
  `tests/test_cassette_replay.py` fails when a prompt drifts from its cassette.
- **`migrated/` holds approved work only.** Write it through `migrate` + `approve` (or the labelled
  `--approve` flag), never by hand; `demo` writes to `runs/demo/` instead.
- **Committed evidence carries no machine paths.** Audit entries and reports record paths relative
  to the repo; scan `cassettes/`, `migrated/` and `evals/` for `/home/` before committing.
- Tests: `uv run pytest`. `integration` tests need the sandbox database and skip without it.

## Lessons from review

- **Separate "the port raised" from "the port never ran".** A guard rejection, timeout, crash or
  bad case input is marked `harness: True` and is a mismatch on every case, even where the oracle
  raised; `ParityReport.passed` also requires no runner failure. Otherwise a module that fails to
  load "matches" every legacy error and inflates parity.
- **Pin only legacy errors.** The oracle records an error as golden only for SQLSTATE classes 22,
  21 and P0. Timeouts, read-only violations and client-side errors reject the case. Recording
  them would let any raising port match harness noise. The legacy call always runs `READ ONLY`,
  also after fixture inserts, so a writing function is rejected the same way in both paths.
- **Allowlist module members; never denylist attribute names.** Allowlisted modules re-export
  other modules under arbitrary names (`calendar.sys`, `enum.bltns`, `typing.operator`), and some
  functions read attributes from strings, so a denylist keeps missing escapes. The guard allows
  only public, non-module members of each allowed module (`guard.public_members`), and the runner
  enforces the same list at runtime by handing the port proxy modules, which also catches aliasing
  (`m = typing; m.operator`) that static checks cannot follow. Dunder-looking string constants and
  `type` are rejected so classes cannot be forged with dunder dictionaries.
- **Check result types by identity** with the real classes (`pgtypes.python_type_name`), not by
  name: a port-made class can claim `__module__ = "decimal"`, and subclasses must not pass either.
- **Cast `character` arguments to `bpchar`.** `::character` means `char(1)` and truncates; function
  parameters of type `character` do not.
- **Deduplicate cases on what the oracle saw** (normalized arguments plus fixture rows read back),
  not on the proposal text, and log every rejected case, sampled or proposed, instead of aborting.
- **Order snapshot rows by every selected column**, not just the first key column, so composite
  keys cannot reorder rows and change a prompt.
- **PostgreSQL numeric has NaN, Infinity and -Infinity.** Parity compares special values by text
  instead of rejecting every non-finite Decimal; a live run showed a correct port failing on a NaN
  golden output. Python's `-NaN` and `sNaN` have no PostgreSQL counterpart and never match.
- **Make every durable step retryable on its own.** The approval node writes `decision.json` as
  soon as a human decides; `publish` builds the output in a staging directory and swaps it in
  through a backup (no window without a publication, no leftovers on failure), refuses to replace a
  folder that holds anything it did not generate, and the CLI prints the retry command; and
  `resume` re-runs a failed publish instead of refusing because the gate is already answered. A
  decision that only lived in the checkpoint left a run stuck after a disk error.
- **Fail cleanly when the oracle is down.** CLI commands check `oracle.is_available()` first and
  map database errors to exit code 2 with a message, not a traceback.
- **A human approval names a human.** `approve` requires `--by`; an agent deciding at the gate
  must pass `--agent` (label "BY AN AGENT (owner review pending)"), and only the labelled
  auto-approve flag skips the gate. A plain `APPROVED` label always means a person decided.
- **Replay can diverge only through timing.** Prompts are byte-stable, but a port that hits the
  runner timeout on a loaded machine changes the branch taken. Keep the timeout generous (30 s).
- **Store labels, don't only print them.** The auto-approve label lives in the decision record
  itself (`label`, `approver`), so `approval.json`, `decision.json` and the audit log say "not a
  human review" without relying on the console or Markdown.
- **Serialize fixture transactions across connections.** Every function draws fixture ids from one
  reserved range, so `eval --jobs` would block on or deadlock over uncommitted keys and turn cases
  into rejections, changing replays. `Oracle.record` takes a transaction-scoped advisory lock when a
  case has fixtures; per-function id ranges were rejected because the model picks the ids.
- **A failed eval never replaces the committed report.** If any target errors (for example a
  cassette miss after a prompt change), reports go to `runs/eval/failed-report/`; stale audit copies
  are deleted before new ones are written.
- **An eval at another effort writes over the committed evidence.** `eval` always writes
  `eval-report.{json,md}` and `audit/`, whatever `ERP_LLM_EFFORT` is. After a run at a
  non-default effort, move both to effort-specific names (`eval-report-high.*`,
  `audit-high/`) and restore the defaults with `git checkout`. Instructions that moved only
  the report would have overwritten the committed medium-effort audit logs. Renaming won
  over a code change because the owner asked for evals only, with no code changes.

