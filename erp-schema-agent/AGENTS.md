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
- Every enforced limit (row cap, timeouts, connection limits, retries, SQL length) lives
  in `limits.py`, including test and connect timeouts. Context budgets (how many tables
  or columns retrieval shows the model) are named constants in `retrieval/`. Prompts, tool descriptions and docs
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
- **Give everything that feeds a prompt a total order.** Prompts are cassette keys, so
  catalog queries need `ORDER BY`, loops over sets go through `sorted()`, and ties get an
  explicit key. A column with two FK targets made the snapshot depend on row order.
- **Verify glossary facts against the database, not against what the seed intends.**
  The seed meant to mark retired products `Discontinued = 'Y'` but never writes it; a
  glossary that repeated the intent would have taught the model a filter that matches
  nothing. Query each convention before writing it down.
- **A verification check must be able to fail, and must accept what is true.** The first
  grounding check matched any figure within 0.5% of hundreds of derived values, so most
  invented numbers passed; tightening it alone then rejected legitimate "top 3 total"
  or "revenue per order" figures. The fix that held: the answer model declares each
  figure's kind (value, total, share, change, ratio) and the check compares it only with
  candidates of that kind, by display rounding. When adding a deterministic check, test
  both sides: near misses that must fail and real derivations that must pass.
- **Screen user text narrowly; leave judgement to the layered guardrails.** Single words
  such as "pins", "tokens" or "secret" are product names as often as credentials, so the
  intent screen matches credential phrases only. Plain-English write requests go to the
  model's refusal, then the validator and the role.
- **Each repair attempt replaces the previous attempt's state.** `generate` clears the
  generation, verdict and rows (also on a model error), so a failed run never reports an
  earlier attempt's SQL. Every node that can set a terminal status needs a conditional
  edge to END; a plain edge carries a failed run into the next node.
- **Screen patterns must cover the syntax they claim.** `update \w+ set` missed
  `update adempiere.c_order set`; identifiers in screen and validator patterns allow
  schema qualification and quoting (`[\w."]+`).
- **Name every number a user or model can see, and scan for it.** The MCP `ask` tool
  sliced rows with a hand-typed 20 and the timeout was derived in three places. Limits
  are constants in `limits.py` (derived ones too, such as `STATEMENT_TIMEOUT_S`), and
  `tests/test_limit_drift.py` scans prompts, MCP tool and parameter descriptions and the
  docs for numbers quoted with a limit's unit ("200-row", "5 s", "two retries") so a
  retyped number fails. Docs must not quote non-limit numbers in those units.
- **Prove isolation with an allow-list, not a deny-list.** The MCP import test checks the
  exact set of project modules the server loads, including the ones `ask` imports
  lazily; the admin DSN lives in `roles.py` so read-only code cannot reach it.
