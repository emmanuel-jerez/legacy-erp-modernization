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
