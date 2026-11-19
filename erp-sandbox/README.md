# erp-sandbox

A local legacy ERP for the agent demos in this portfolio: iDempiere 12 on PostgreSQL 16 in
Docker, loaded with a **fictional** direct-to-consumer skincare brand, Fernhollow Skincare,
and a year of **synthetic** order history. The three demo projects build on it:

| Project | Shows |
|---|---|
| `../erp-schema-agent` | A LangGraph agent that answers business questions over the ERP schema, with guardrails, an MCP server and evals |
| `../erp-migration-agent` | An agent that ports legacy business rules out of the ERP and proves parity with characterization tests |
| `../erp-ui-operator` | A Playwright and LLM hybrid that completes ERP tasks through the legacy web UI, checked against the database |

## Why iDempiere

iDempiere descends from Compiere (2000) through ADempiere. Its 900-table schema
(`C_BPartner`, `M_Product`, `C_OrderLine`), its application dictionary (`AD_Table`,
`AD_Column`) and its PL/pgSQL business functions are a real 20-year-old ERP design.

## Data

Nothing here is real.

- **Catalog:** 28 Fernhollow products, 56 SKUs (`FH-…`), defined in
  [`catalog.py`](src/erp_sandbox/catalog.py). Two SKUs are retired.
- **History:** 320 `Demo Customer` partners, three `Placeholder` vendors, about 9,000 sales orders
  (Dec 2025 to Nov 15, 2026, including voided and in-progress orders), 36 purchase orders, on-hand stock and
  reorder points. Every order description starts with "Synthetic placeholder".
- **Determinism:** the generator in [`seed.py`](src/erp_sandbox/seed.py) uses a fixed random
  seed, so every machine produces the same database and the eval answer keys stay valid.
- **How it's loaded:** rows are inserted with SQL, not through iDempiere's document engine, so
  seeded orders have no accounting entries. IDs come from `AD_Sequence`, so records created later in the web UI never collide.
- **GardenWorld:** iDempiere's own demo tenant data stays in place as realistic noise.

## Run it

Requires Docker (amd64 emulation for the iDempiere image), [uv](https://docs.astral.sh/uv/)
and Python 3.12.

```sh
uv sync
uv run erp-sandbox bootstrap   # start containers, seed once, freeze the seed template
uv run erp-sandbox status
```

The first start imports iDempiere's seed database, which takes about 10 minutes.

| Service | Address | Login |
|---|---|---|
| iDempiere web UI | https://127.0.0.1:58443/webui/ (self-signed certificate) | `GardenAdmin` / `GardenAdmin` |
| PostgreSQL | `127.0.0.1:56432` | `adempiere` / `adempiere` (ERP owner), `postgres` / `postgres` (admin) |

All credentials are the image defaults, bound to `127.0.0.1`, for local demos only.

### Databases

| Database | Used by |
|---|---|
| `idempiere` | The running iDempiere server. `erp-ui-operator` writes here through the web UI. |
| `idempiere_seed` | Frozen copy taken right after seeding. Never modified. |
| `erp_schema_agent`, `erp_migration_agent` | Per-project copies of the seed |

A project database can be recreated at any time:

```sh
uv run erp-sandbox clone-db erp_schema_agent --force
```

## Model adapter

[`erp_sandbox.llm`](src/erp_sandbox/llm.py) is the single path from the demos to Claude.
`complete_json(system, prompt, schema)` returns schema-valid JSON; the model proposes and the
calling code validates and executes.

For this local demo, `ClaudeCodeClient` calls the logged-in Claude Code CLI (`claude -p`)
instead of the Claude API. Defaults: `claude-opus-5-5` at **medium** effort (`ERP_LLM_EFFORT`
overrides). Each call strips Claude Code's own context: a replacement system prompt, no built-in tools, no user or project settings,
hooks, MCP servers or skills, and an empty working directory. To prove it on a machine:

```sh
uv run erp-sandbox llm-smoke
```

A production build would put an Anthropic SDK client behind the same interface.

Responses are recorded as cassettes keyed by a hash of model, effort, prompts and schema, so evals and demos replay offline and
identically. `ERP_LLM_MODE` is `replay`, `record` (default) or `live`. Cost figures in
cassettes are Claude Code's client-side estimates.

## Tests

```sh
uv run pytest
```

## Reset

`docker compose down -v` deletes everything, including the seed template. Run
`uv run erp-sandbox bootstrap` to rebuild.
