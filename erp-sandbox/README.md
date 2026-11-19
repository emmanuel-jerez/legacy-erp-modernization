# erp-sandbox

This repository is a local legacy ERP for the agent demos in this portfolio.
It is iDempiere 12 on PostgreSQL 16 in Docker.
Its data is for a **fictional** direct-to-consumer skincare brand, Fernhollow Skincare.
The data also includes **synthetic** order history for one year.
The three demo projects use this ERP:

| Project | Shows |
|---|---|
| `../erp-schema-agent` | A LangGraph agent that uses the ERP schema to answer business questions. It has guardrails, an MCP server and evals. |
| `../erp-migration-agent` | An agent that makes ports of legacy business rules from the ERP. The agent uses characterization tests to show parity. |
| `../erp-ui-operator` | A tool that uses Playwright and an LLM together to do ERP tasks through the legacy web UI. It compares the results with the database. |

## Decision: iDempiere

iDempiere comes from Compiere (2000) through ADempiere.
It has a 900-table schema (`C_BPartner`, `M_Product`, `C_OrderLine`), an application dictionary (`AD_Table`, `AD_Column`) and PL/pgSQL business functions.
These parts show the design of an ERP that has 20 years of history.

## Data

All data in this repository is fictional.

- **Catalog:** 28 Fernhollow products and 56 SKUs (`FH-…`).
  [`catalog.py`](src/erp_sandbox/catalog.py) contains them.
  Two SKUs are retired.
- **History:** 320 `Demo Customer` partners, three `Placeholder` vendors, approximately 9,000 sales orders (Dec 2025 to Nov 15, 2026), 36 purchase orders, on-hand quantities and reorder points.
  The sales orders include orders with the status "Voided" and orders with the status "In Progress".
  All order descriptions start with "Synthetic placeholder".
- **Determinism:** The generator in [`seed.py`](src/erp_sandbox/seed.py) uses a random seed that does not change.
  Thus, a new stack on each computer gets the same business data and the same record IDs.
  Also, the eval answer keys stay correct.
  Only the `*_uu` UUID columns are different.
- **How the seed loads the data:** The seed uses SQL to write the rows.
  It does not use the iDempiere document engine.
  Thus, the seeded orders have no accounting entries.
  The IDs come from `AD_Sequence`.
  Thus, a record that the web UI makes after the seed does not get the ID of a different record at any time.
- **GardenWorld:** The demo tenant data that comes with iDempiere stays in the database.
  This data is noise, as in an ERP in production.

## Run it

You must have Docker (with amd64 emulation for the iDempiere image), [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync
uv run erp-sandbox bootstrap   # start containers, seed once, freeze the seed template
uv run erp-sandbox status
```

When the stack starts for the first time, it imports the iDempiere seed database.
Approximately 10 minutes are necessary for this import.

| Service | Address | Login |
|---|---|---|
| iDempiere web UI | https://127.0.0.1:58443/webui/ (self-signed certificate) | `GardenAdmin` / `GardenAdmin` |
| PostgreSQL | `127.0.0.1:56432` | `adempiere` / `adempiere` (ERP owner), `postgres` / `postgres` (administrator) |

All credentials are the default credentials of the image.
The services are available only on `127.0.0.1`.
These credentials are only for local demos.

### Databases

| Database | Used by |
|---|---|
| `idempiere` | The iDempiere server in operation. `erp-ui-operator` writes to this database through the web UI. |
| `idempiere_seed` | A frozen copy of the database immediately after the seed (a PostgreSQL template). |
| `erp_schema_agent`, `erp_migration_agent` | Copies of the seed, one for each project |

`uv run erp-sandbox snapshot` freezes the template one time.
If there is a template, the command does not replace it, unless you add `--force`.
When you replace the template, the eval answer keys of the other repositories change.
Then you must make all clones again.
Thus, replace the template only after you make a decision to change the seed.

You can make a project database again when you want:

```sh
uv run erp-sandbox clone-db erp_schema_agent --force
```

## Model adapter

The demos call Claude only through [`erp_sandbox.llm`](src/erp_sandbox/llm.py).
`complete_json(system, prompt, schema)` gives JSON that agrees with the schema.
The model gives a proposal.
Then the code that calls the function validates the proposal and runs it.

For this local demo, `ClaudeCodeClient` calls the Claude Code CLI (`claude -p`) with the login of that CLI.
It does not call the Claude API.
By default, the client uses `claude-opus-5-5` at **medium** effort.
`ERP_LLM_EFFORT` changes the effort.

Each call removes the context that Claude Code adds.
It replaces the system prompt and uses an empty working directory.
It uses no built-in tools, no user or project settings, no hooks, no MCP servers and no skills.
To show this on a computer, run this command:

```sh
uv run erp-sandbox llm-smoke
```

In a production build, an Anthropic SDK client is behind the same interface.

The adapter records each response as a cassette.
The key of each cassette is a hash of the model, effort, prompts and schema.
Thus, evals and demos replay without a network connection, with the same results each time.
The value of `ERP_LLM_MODE` is `replay`, `record` (default) or `live`.
The cost values in the cassettes are client-side estimates from Claude Code.

## Tests

```sh
uv run pytest
```

## Reset

`docker compose down -v` deletes all of the stack.
This includes the seed template.
To make the stack again, run `uv run erp-sandbox bootstrap`.
