# Working in erp-schema-agent

A LangGraph agent that answers business questions over the iDempiere schema in the
shared erp-sandbox stack. Read [README.md](README.md) and `../erp-sandbox/AGENTS.md` first.

## Setup

```sh
uv sync
uv run schema-agent setup-db      # idempotent; rerun after `erp-sandbox clone-db erp_schema_agent --force`
uv run pytest                     # integration tests skip when the database is down
```

## Rules

- The agent and the MCP server only ever touch the database through
  `executor.run_readonly`, which connects as the read-only role. Admin credentials are
  used by `roles.setup_readonly_role` and by tests' setup only.
- Every limit (row cap, timeouts, connection limits, retries, SQL length) lives in
  `limits.py`, including test and connect timeouts. Prompts, tool descriptions and docs
  interpolate those constants instead of retyping numbers; so do tests.
- Model calls go through `erp_sandbox.llm` (`ClaudeCodeClient` live, `ScriptedModelClient`
  in unit tests). Never call the live model from tests.
- Keep prompts deterministic so cassettes replay: no timestamps, random IDs or
  machine-specific paths in anything sent to the model.
- Never modify `../erp-sandbox` or other repos. Never add real company or person data.
- The SQL validator executes its own re-rendered SQL, not the model's text.

## Lessons from review

- **Resolve names the way PostgreSQL does before allow-listing them.** A CTE name only
  shadows a table inside its own WITH query (and in later CTEs of that WITH). Collecting
  CTE names from the whole tree let `(WITH pg_roles AS ...) ..., pg_roles` read the real
  catalog. Any name-based check in the validator must be scope-aware; add a test with
  the name used out of scope. A scope walk won over banning CTE names that look like
  catalogs because a deny-list of names is never complete.
- **Derive the credential-column policy from a scan of the real schema, and share it.**
  `policy.SENSITIVE_COLUMN` feeds both the role grants and the validator. A hand-written
  regex missed `*userpw`, `userpin`, `encryptionkey`, `connectionkey` and card numbers.
  When the schema changes, rescan `information_schema.columns` for pass/pw/pin/key/
  token/secret/card/ssn/account names and add a role test per new pattern. Over-matching
  is the accepted failure mode.
- **Check every syntactic route to a column, not just `exp.Column`.** Whole-row
  references (`SELECT u FROM ad_user u`, `array_agg(u)`) and composite field access
  (`(u).password`, parsed as `Dot`/`Identifier`) bypass column checks. The validator
  rejects any matching identifier and any bare column named like a table alias, so the
  model gets a reason instead of a raw permission error. The same goes for catalog
  lookups hidden in types and keywords: `::regclass`/`::regrole` casts and bare `user`
  or `current_role` (parsed as columns) are rejected explicitly.
- **Describe each guardrail by what really enforces it.** Read-only rests on the role
  holding no write grants; `default_transaction_read_only` is a session default the
  session could change. PUBLIC catalog access remains at the database layer and only
  the validator blocks it; docstrings and the README must say so.
