# Working in erp-sandbox

The shared foundation for the portfolio's agent demos. Read [README.md](README.md) first.

## Contract other repos rely on

- `erp_sandbox.llm.ModelClient.complete_json(system=, prompt=, schema=, label=)` returns a
  `ModelResult` whose `data` already satisfies the schema. Keep that signature stable; the
  three demo repos depend on it through a uv path dependency.
- `erp_sandbox.config` and `erp_sandbox.db` define ports, credentials and database names.
  Change them only together with the README tables.
- The seed is deterministic (`RANDOM_SEED`). Any change to `catalog.py` or `seed.py`
  changes eval answer keys in other repos: regenerate the template, re-clone, and tell the
  owners of those evals.

## Rules

- All brand, partner and order data is fictional or synthetic and labelled so in the data
  itself. Never add real company, customer or employee data.
- Live model calls go through `ClaudeCodeClient` only, never a direct `claude` subprocess
  elsewhere, so the isolation flags and cassettes apply everywhere.
- Do not change the Claude Code isolation flags without rerunning `uv run erp-sandbox llm-smoke`.
- Treat `idempiere_seed` as read-only. Write tests against per-project clones.
- Tests: `uv run pytest`. Tests marked `integration` need the running stack and skip without it.
