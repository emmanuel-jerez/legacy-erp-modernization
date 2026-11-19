# Legacy ERP Modernization with Verified Agents

Three AI agents working against a real 20-year-old ERP, each built so its output can be
trusted the way production software has to be:

| Agent | What it does | How its output is verified |
|---|---|---|
| [`erp-schema-agent`](erp-schema-agent) | Answers business questions in plain English over a 1,000-table ERP schema | Every query is re-rendered by a SQL validator and runs under a read-only role; every number in the answer is checked against the returned rows |
| [`erp-migration-agent`](erp-migration-agent) | Ports legacy PL/pgSQL business rules to modern Python | The legacy function is the oracle: 100% parity on held-out cases the model never saw, then a human approval gate |
| [`erp-ui-operator`](erp-ui-operator) | Completes ERP tasks through the legacy web UI, like a person would | Success is defined by the database, not the screen; the model may only propose allowlisted actions |

All three run on [`erp-sandbox`](erp-sandbox): iDempiere 12, a direct descendant of
Compiere (2000), on PostgreSQL 16, with a fictional brand and synthetic history.

## The idea: the model proposes, deterministic code decides

Getting an LLM to produce a SQL query, a Python port or a click is easy. Shipping it is not,
because a model's output is a guess. Everything in this repository is organized around one
rule: **the model never acts directly**. It returns schema-validated JSON, and deterministic
code decides whether that proposal is safe, correct and done.

Production readiness comes from the parameters set around an agent and the verification
that gates its output. These are the parameters used here.

### 1. Contracts before code

- **Model contract.** One interface for every model call:
  `complete_json(system, prompt, schema)`. The answer is validated against a JSON Schema
  before any code sees it ([`erp-sandbox/src/erp_sandbox/llm.py`](erp-sandbox/src/erp_sandbox/llm.py)).
- **Metric contract.** The migration agent's definition of "equivalent" was written down
  before the agent: exact decimal scale, NULL rules, how errors compare, the held-out split
  and the pass bar ([`erp-migration-agent/docs/metric-contract.md`](erp-migration-agent/docs/metric-contract.md)).
- **Data contract.** The seed is deterministic, so eval answer keys stay valid on any
  machine and any change to them is deliberate.

### 2. Ground truth lives outside the model

| Agent | Ground truth |
|---|---|
| Schema agent | Hand-written reference SQL; result sets compared, not query text |
| Migration agent | The legacy function itself, called in PostgreSQL |
| UI operator | The ERP database after the run |

No agent grades its own work.

### 3. Defense in depth, with each layer able to stand alone

- **SQL:** a least-privilege PostgreSQL role (SELECT on one schema, no credential columns, read-only transactions,
  statement timeout) **and** a sqlglot validator (one SELECT, allowlisted tables and
  functions, catalog and credential access blocked, LIMIT capped). The validator's
  re-rendered SQL is what runs, never the model's text. Attack queries are tested against
  each layer separately.
- **Generated code:** a static import allowlist, the same allowlist enforced at runtime, and
  execution in a separate process with a timeout. The README says plainly this is defense in
  depth, not a sandbox.
- **UI actions:** a deterministic, selector-first plan. The model is used only when a step
  fails, and its proposed action must pass an allowlist that forbids completing, posting,
  voiding or deleting documents.

### 4. Bounded autonomy

Every loop has a ceiling, and every irreversible step has a gate:
- SQL repair is capped at 2 retries and code repair at 3;
- UI runs have a step cap and a cap on model fallbacks;
- live model calls are capped at 3 in flight;
- a durable approval gate pauses the migration workflow until someone approves or
  rejects it from a separate process.

### 5. Evals are release gates, not demos

| Agent | Eval sets |
|---|---|
| Schema agent | A golden set, an adversarial prompt set, and attack queries that bypass the model entirely |
| Migration agent | Visible cases for repair, held-out cases for acceptance |
| UI operator | A task matrix run on the clean UI and on a deliberately "drifted" UI |

Every report records the model, effort, call counts and cost estimate. Drift tests fail the
build when a limit quoted in a prompt, tool description or README stops matching the code.

### 6. Reproducibility

- **Recorded model responses.** Each response is stored as a cassette keyed by a hash of
  model, effort, prompts and schema. Evals and demos replay offline with identical results,
  and a test fails when a prompt drifts from its recording.
- **Isolated model context.** Each model call sees only what the code sends: no editor
  instructions, tools, plugins or memory. A one-command smoke test proves this.

## Results

Measured 2026-11-19 with `claude-opus-5-5` at medium effort, replayed offline from the
committed cassettes.

| | Result |
|---|---|
| Schema agent: golden questions, result-set accuracy | **36 / 38 (94.7%)** |
| Schema agent: adversarial prompts handled safely | 10 / 10 |
| Schema agent: attack queries blocked without the model | 18 / 18 |
| Schema agent: answers with every figure grounded | 37 / 38 (1 false flag) |
| Migration agent: `currencyround`, held-out parity | 14 / 14, no repairs |
| Migration agent: `paymenttermduedate`, held-out parity | 13 / 13, no repairs |
| Migration agent: `paymenttermdiscount` (8 tables), held-out parity | 14 / 14, no repairs |
| UI operator: draft purchase orders verified in the database | **6 / 6** (clean UI 3 / 3, drifted UI 3 / 3) |
| UI operator: drifted selectors repaired by the model | 9 model calls across the 3 drifted runs, 0 guardrail blocks |
| UI operator: guardrail probe (a "Delete Record" action) | Blocked; no order created |

The same evals at high effort, side by side with these results: [EFFORT-COMPARISON.md](EFFORT-COMPARISON.md).

The two schema-agent misses are reported in the eval report, not hidden: both returned
extra rows. One example of what the parity harness caught: the legacy due-date function adds
86,400 seconds per day in Los Angeles time, so due dates shift an hour across daylight
saving. The port reproduces that behavior, because the oracle, not intuition, defines
correct.

## How it was built: agentic engineering applied to the build itself

The code was written by AI coding agents working in parallel, one per project, under an
architecture, constraints and review process I designed. The approach that governs the
agents in the product also governed the agents that built it.

- **A written brief as the contract** ([`BUILD-BRIEF.md`](BUILD-BRIEF.md)): scope, shared
  interfaces, model budget, data rules, git workflow and a definition of done.
- **Small slices.** Each change was built in its own worktree and squash-merged to `main`, giving a linear history.
- **Independent review on every slice** before merge, with a fresh reviewer after
  non-trivial fixes. Reviews caught real defects, for example:
  - a CTE name that let a query reach the PostgreSQL catalog;
  - two escapes from the generated-code import guard;
  - NaN results that could never pass parity;
  - seeded ERP records missing the accounting rows the ERP creates on save.
- **Lessons become rules.** Every review fix adds a general rule, with its reason, to that
  project's `AGENTS.md`, so the next agent doesn't repeat the mistake.

## Technologies

Python 3.12, LangGraph, the official MCP Python SDK, sqlglot, Playwright, PostgreSQL 16,
iDempiere 12, Docker, uv and pytest, with Claude as the model.

Several of these (LangGraph, iDempiere, sqlglot, the MCP SDK) were new to me for this
project. The working rule was to read current documentation before writing code against
any library, and each project's README lists what it relied on.

## Run it

Requires Docker (the iDempiere image runs under amd64 emulation on ARM), [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
cd erp-sandbox && uv sync && uv run erp-sandbox bootstrap   # first start takes ~10 minutes
cd ../erp-schema-agent    && uv sync && uv run schema-agent demo
cd ../erp-migration-agent && uv sync && uv run migration-agent demo
```

Demos replay offline from cassettes. Each folder's README covers its tests, evals and live
runs.

## Scope and honesty notes

- **Local demo.** For this demo the model is reached through the locally installed Claude
  Code CLI; a production build would put the Anthropic SDK behind the same interface.
- **No real data.** "Fernhollow Skincare" is fictional, and every partner and order is
  synthetic and labelled as such in the data.
- **Built and published separately.** The projects were built as separate local
  repositories and combined here with their full commit history.

The patterns here mirror production work I've done; that code is proprietary and not
included.
