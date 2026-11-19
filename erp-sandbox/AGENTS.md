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
- READMEs are written in ASD-STE100 Simplified Technical English (Issue 9); keep new text compliant. Reason: short sentences and a controlled vocabulary keep the claims unambiguous for readers who are not native English speakers and for machine translation.
- Tests: `uv run pytest`. Tests marked `integration` need the running stack and skip without it.

## Lessons from review

- **Seed through SQL, but add what iDempiere adds on save.** Raw inserts skip the model
  classes' `afterSave`, which creates accounting companion rows. Mirror each one from the
  same source: `M_Product_Category_Acct` and `C_BP_Employee_Acct` from
  `C_AcctSchema_Default`, `M_Product_Acct` from the category's accounts, and
  `C_BP_Customer_Acct` plus `C_BP_Vendor_Acct` (for every partner, whatever its flags) from
  the partner group's accounts. Without them, documents created later in the web UI fail
  to complete or post. When seeding a new table, compare a GardenWorld record's related
  rows with yours. SQL seeding won over the web UI or an import process because it is
  fast, deterministic and needs no UI automation.
- **The seed template changes only on purpose.** `snapshot` refuses to replace
  `idempiere_seed` without `--force`, and the template is flagged `IS_TEMPLATE`. Once
  `erp-ui-operator` has written to `idempiere`, a casual re-snapshot would carry those
  writes into every clone and change other repos' answer keys.
- **Integration tests clone their own database.** `CREATE DATABASE ... TEMPLATE` fails
  while anyone is connected to the source, so tests never hold sessions on the template.
  Per-project clones belong to their projects; this repo's tests use a throwaway clone.
- **Never pass unbounded text as a command-line argument.** Linux rejects a single
  argument over about 128 KiB (E2BIG), so the system prompt goes through
  `--system-prompt-file` and the user prompt through stdin.
- **Every adapter failure is a `ModelError`.** Callers' eval harnesses catch that type
  only, so launching the CLI, writing the prompt file, decoding output, reading or saving
  a cassette, and unexpected JSON shapes all re-raise as `ModelError`.
- **Every row in a batch insert carries the same keys.** `_insert` takes its column list
  from the first row and raises if any row's keys differ, because a key set on only some
  rows would otherwise be dropped silently. Set the default on every row (for example
  `"discontinued": "N"`) and override it where needed.
