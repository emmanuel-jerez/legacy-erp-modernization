# Legacy ERP Modernization with Verified Agents

This repository has three AI agents that operate on an iDempiere ERP that has 20 years of
history. I made each agent to give output that you can be sure of, as you must be sure of
production software:

| Agent | Function | How the agent makes sure that its output is correct |
|---|---|---|
| [`erp-schema-agent`](erp-schema-agent) | Gives answers to business questions in plain English. It uses an ERP schema of 1,000 tables | A SQL validator renders each query again, and the query runs with a read-only role. A check compares each number in the answer with the rows from the query |
| [`erp-migration-agent`](erp-migration-agent) | Changes legacy PL/pgSQL business rules into new Python code | The legacy function is the oracle. The port must have 100% parity on held-out cases that the model did not see at any time. Then, a person must accept the port at an approval gate |
| [`erp-ui-operator`](erp-ui-operator) | Completes ERP tasks through the legacy web UI, as a person does | The database, not the screen, shows if the task is correct. The model can only recommend allowlisted actions |

All three agents run on [`erp-sandbox`](erp-sandbox). This sandbox is iDempiere 12 on
PostgreSQL 16. iDempiere comes directly from Compiere (2000). The sandbox uses a fictional
brand and synthetic history data.

## The rule: the model recommends, deterministic code makes the decision

It is easy to get a SQL query, a Python port or a click from an LLM. But it is not easy to use
that output in production, because the output of a model is an estimate. All parts of this
repository follow one rule: **the model does not do operations directly at any time**.

The model gives schema-validated JSON. Then deterministic code makes the decision: is that
output safe, is it correct, and did the agent complete the task?

The parameters around an agent and the checks that its output must pass make the agent
satisfactory for production. This repository uses the parameters that follow.

### 1. Contracts before code

- **Model contract.** All model calls use one interface:
  `complete_json(system, prompt, schema)`. The interface validates the answer against a JSON
  Schema before other code sees it ([`erp-sandbox/src/erp_sandbox/llm.py`](erp-sandbox/src/erp_sandbox/llm.py)).
- **Metric contract.** I wrote the definition of "equivalent" for the migration agent before I
  made the agent. It gives these conditions: the same decimal scale, the NULL rules, the
  procedure to compare errors, the held-out split and the pass limit
  ([`erp-migration-agent/docs/metric-contract.md`](erp-migration-agent/docs/metric-contract.md)).
- **Data contract.** The seed is deterministic. Thus, the eval answer keys stay correct on all
  computers, and each change to them comes from a decision, not from an accident.

### 2. Ground truth is external to the model

| Agent | Ground truth |
|---|---|
| Schema agent | SQL that a person wrote as the standard. The check compares result sets, not query text |
| Migration agent | The legacy function, which the check calls in PostgreSQL |
| UI operator | The ERP database after the run |

No agent makes the decision if its work is correct.

### 3. Defense in depth: each layer can operate independently

- **SQL:** a least-privilege PostgreSQL role (SELECT on one schema, no credential columns,
  read-only transactions, statement timeout) **and** a sqlglot validator (one SELECT,
  allowlisted tables and functions, no access to the catalog or to credentials, a maximum
  value for LIMIT). The SQL that runs is the SQL that the validator renders again. The text
  from the model does not run at any time. Tests send attack queries to each layer
  independently.
- **Generated code:** a static import allowlist, the same allowlist at runtime, and a different
  process with a timeout to run the code. The README tells you clearly that this is defense
  in depth, not a sandbox.
- **UI actions:** a deterministic sequence of steps that uses selectors first. The operator
  uses the model only when the code cannot do a step. The action that the model recommends
  must pass an allowlist. This allowlist does not let the model complete, post, void or delete
  documents.

### 4. Bounded autonomy

Each loop has a limit, and each step that has a permanent effect has a gate:
- SQL repair stops at 2 retries, and code repair stops at 3.
- UI runs have a maximum number of steps and a maximum number of model fallbacks.
- A maximum of 3 live model calls can run at the same time.
- An approval gate, which stores its condition, stops the migration workflow until a person
  accepts or rejects it from a different process.

### 5. Evals are release gates, not demos

| Agent | Eval sets |
|---|---|
| Schema agent | A golden set, an adversarial prompt set, and attack queries that do not go through the model at any point |
| Migration agent | Visible cases for repair, and held-out cases to accept the port |
| UI operator | A task matrix that runs on the clean UI and on a "drifted" UI that I changed for this eval |

Each report records the model, the effort, the number of calls and the cost estimate. A limit
can be in a prompt, a tool description or a README. When that limit becomes different from the
code, the drift tests fail, and the build fails.

### 6. Reproducibility

- **Recorded model responses.** The code stores each response as a cassette. The key of each
  cassette is a hash of the model, the effort, the prompts and the schema. Evals and demos
  replay offline and give the same results. A test fails when a prompt becomes different from
  the prompt in its cassette.
- **Isolated model context.** Each model call gets only the data that the code sends. It gets
  no editor instructions, tools, plugins or memory. A smoke test with one command shows this.

## Results

I measured these results on 2026-11-19 with `claude-opus-5-5` at the "medium" effort level. The
evals replayed offline from the committed cassettes.

| | Result |
|---|---|
| Schema agent: golden questions, correct result sets | **36 / 38 (94.7%)** |
| Schema agent: adversarial prompts with a safe result | 10 / 10 |
| Schema agent: attack queries blocked without the model | 18 / 18 |
| Schema agent: answers in which all numbers agree with the data | 37 / 38 (1 incorrect flag) |
| Migration agent: `currencyround`, held-out parity | 14 / 14, no repairs |
| Migration agent: `paymenttermduedate`, held-out parity | 13 / 13, no repairs |
| Migration agent: `paymenttermdiscount` (8 tables), held-out parity | 14 / 14, no repairs |
| UI operator: draft purchase orders, with a check in the database | **6 / 6** (clean UI 3 / 3, drifted UI 3 / 3) |
| UI operator: drifted selectors that the model repaired | 9 model calls in the 3 drifted runs, 0 guardrail blocks |
| UI operator: guardrail test (a "Delete Record" action) | Blocked, and the operator made no order |

The eval report shows the two incorrect schema-agent results, and does not remove them. The
two results each had more rows than necessary.

This is one example of a problem that the parity harness found. The legacy due-date function
adds 86,400 seconds for each day in Los Angeles time. Thus, due dates move by an hour when daylight
saving time starts or stops. The port gives the same results, because the oracle, not an
assumption, gives the correct result.

## How I made it: agentic engineering applied to the build

AI coding agents wrote the code. They ran in parallel, one agent for each project. They
followed an architecture, constraints and a review procedure that I designed. The method that
controls the agents in this repository also controlled the agents that made this repository.

- **A written brief as the contract** ([`BUILD-BRIEF.md`](BUILD-BRIEF.md)). The brief gives the scope,
  the shared interfaces, the model budget, the data rules, the git workflow and a definition of
  done.
- **Small slices.** Each change had a new worktree, and a squash merge put it into `main`.
  Thus, the history is linear.
- **A review of each slice, done independently,** before merge. After a fix that was not
  small, a new reviewer did the review again. The reviews found defects that were in the
  code, for example:
  - a CTE name that let a query get to the PostgreSQL catalog.
  - two escapes from the import guard for generated code.
  - NaN results that could not pass parity at any time.
  - seeded ERP records without the accounting rows that the ERP makes when it saves a record.
- **Lessons become rules.** Each review fix adds a general rule to the `AGENTS.md` file
  of that project. The rule includes the cause for it. This prevents the same error by the
  next agent.

## Technologies

This repository uses Python 3.12, LangGraph, the official MCP Python SDK, sqlglot, Playwright,
PostgreSQL 16, iDempiere 12, Docker, uv and pytest. It uses Claude as the model.

Some of these (LangGraph, iDempiere, sqlglot, the MCP SDK) were new to me in this project. My
rule was to read the newest documentation for a library before I wrote code for it. The README of each project gives a list of the sources that it used.

## Run it

You must have Docker (on ARM, the iDempiere image runs in amd64 emulation), [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
cd erp-sandbox && uv sync && uv run erp-sandbox bootstrap   # first start takes ~10 minutes
cd ../erp-schema-agent    && uv sync && uv run schema-agent demo
cd ../erp-migration-agent && uv sync && uv run migration-agent demo
```

The demos replay offline from cassettes. The README in each folder gives the data about its
tests, evals and live runs.

## Scope and honesty notes

- **Local demo.** In this demo, the code calls the model through the Claude Code CLI that is
  installed locally. In a production build, the Anthropic SDK is behind the same interface.
- **Synthetic data only.** "Fernhollow Skincare" is a fictional brand. All
  partners and orders are synthetic data, and the data identifies them as synthetic.
- **Made and published independently.** The projects started as different local repositories.
  This repository puts them together with their full commit history.

The patterns here come from production work that I did. That code is proprietary, and this
repository does not include it.
