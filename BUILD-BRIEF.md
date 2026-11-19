# Build brief for the coding agents

This is the brief the three project agents received, lightly edited for publication
(machine paths removed). It is the "parameters" half of the agentic engineering approach
described in the [README](README.md): scope, contracts, budgets, guardrails, git workflow
and a definition of done that every agent had to meet. The projects were built as
separate local repositories and later combined here, so "repo" below means a top-level
folder of this repository.

**Why this exists.** Three sibling projects show agentic modernization of a real 20-year-old ERP: agents and agentic workflows (LangGraph, Playwright-LLM hybrids) that navigate complex ERP schemas and business logic, legacy-to-modern migration, and a deterministic mindset (automated validation, evals and guardrails), using Python, SQL, MCP and retrieval. You own one project; two other agents build the other two at the same time.

**Workspace.** The repository root:
- `erp-sandbox/` is the shared foundation and is already done. **Read its README.md and AGENTS.md first, then `src/erp_sandbox/llm.py`, `config.py` and `db.py`.** Do not modify erp-sandbox. If you need a change there, finish what you can and put the exact request in your final report.
- `erp-schema-agent/`, `erp-migration-agent/`, `erp-ui-operator/` are the three project repos. Each has a scaffold commit on `main`: a uv project with `erp-sandbox` as an editable path dependency (`../erp-sandbox`), plus `.gitignore`. Touch only your own repo.

**The ERP.** iDempiere 12 (Compiere/ADempiere lineage) on PostgreSQL 16, already running in Docker:
- PostgreSQL at `127.0.0.1:56432`; ERP owner `adempiere`/`adempiere`, admin `postgres`/`postgres`; schema `adempiere`.
- Web UI at `https://127.0.0.1:58443/webui/` (self-signed certificate); `GardenAdmin`/`GardenAdmin`.

The data is a **fictional** brand, "Fernhollow Skincare", in the GardenWorld tenant (AD_Client_ID 11):
- 56 SKUs `FH-…`, 2 retired;
- 320 `Demo Customer NNNN` partners and 3 `Placeholder …` vendors;
- 9,000 sales orders `FH-SO-…` (Dec 2025 to Nov 15, 2026; statuses CO/IP/VO) and 36 purchase orders `FH-PO-…`;
- stock in HQ Warehouse (M_Warehouse_ID 103, locator 101) and reorder points (M_Replenish).

GardenWorld's own demo data sits alongside as realistic noise. The seed is deterministic; `idempiere_seed` is the frozen template. Use `uv run erp-sandbox status` to see state.

**Model access.** Every model call goes through `erp_sandbox.llm.ClaudeCodeClient(...).complete_json(system=, prompt=, schema=, label=)`, never a direct `claude` subprocess or an Anthropic API key (there isn't one). It uses the locally logged-in Claude Code CLI with `claude-opus-5-5` at **medium** effort; keep those defaults. The model proposes structured JSON; your code validates and executes.
- **Cassettes:** keep them in your repo under `cassettes/` and commit them, so demos and evals replay offline (`ERP_LLM_MODE=replay`).
- **Deterministic prompts:** cassette replay works only if prompts are identical across runs, so leave volatile content (timestamps, random IDs, dynamic DOM ids) out of prompts or normalize it.
- **Unit tests:** use `ScriptedModelClient` and never call the live model.
- **Usage budget:** live calls draw on one shared Claude subscription, used by three agents at once. At most 3 live model calls in flight from your repo at once. Keep eval sets around 30–50 cases. Record each eval once and re-record only what changed. Each call takes about 7 s or more.
- Put the model, effort, call counts, cassette hits and the recorded cost estimate in every eval report (`UsageTally`).

**Data and provenance rules.**
- All data is fictional or synthetic. Never add real company, customer or employee data. Never mention any real employer in the repo.
- Do not read unrelated directories on the machine.
- The README must describe the work honestly. Say:
  - this is a local demo;
  - the model is Claude through the local Claude Code CLI, and a production build would use the Anthropic SDK behind the same interface;
  - which parts are deterministic;
  - measured results only.
- Include one short line: "The patterns here mirror production work I've done; that code is proprietary and not included."

**Tooling.**
- Python 3.12, managed with `uv`. Add dependencies with `uv add`; keep the repo's `.venv`.
- Before writing code against a library (LangGraph, the `mcp` Python SDK, Playwright, sqlglot, …), load the `find-docs` skill and check current docs and APIs for the version you install. Name the sources in your final report.
- Write tests at seams you choose up front (`tdd` skill), and only tests that earn their keep.

**Git workflow (git-proceed, adapted: no remote, no PRs).** Load the `git-proceed` skill for its scope, verification and review rules. Then follow these adaptations:
- **No remote:** never add a remote, fetch, push or create PRs.
- **Worktree per slice:** work in a worktree that is a sibling of the repos, so `../erp-sandbox` still resolves. For example: `cd <repo> && git worktree add ../<repo>--<slug> -b <type>/<slug> main`, then `uv sync` inside it.
- **Small slices:** one coherent outcome per branch, for example "SQL guardrails", "eval harness" or "MCP server".
- **Commits:** present-tense messages. Never `--no-verify`. End every commit message with this trailer line:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- **Before merging a slice:**
  1. Rebase onto `main` and rerun the repo's checks.
  2. Get an independent review with the `code-review` skill against `main`. You may spawn subagents for that review and for independent sub-work.
  3. Fix only actionable issues. Record each fix's lesson as a general rule in your repo's `AGENTS.md`, with a one-line reason.
  4. Re-review once after fixes if the fix was non-trivial.
- **Merge:** `git merge --squash <branch>` from the main checkout. Commit with a message carrying a short summary and the checks that ran, plus the trailer. Then `git worktree remove` the worktree and `git branch -D` the branch.
- **Never `git merge` without `--squash`, and never create merge commits.** If `main` moved, rebase the branch.

**Your repo must end with:**
1. `README.md`: what it shows and why (map it to the capabilities above), an architecture diagram (Mermaid), a one-command demo, how to run the tests and evals, measured eval results (model, effort, date), guardrails, limitations, and the provenance line.
2. `AGENTS.md`: how to work in the repo, plus lessons from review.
3. A one-command demo that works offline from committed cassettes. A live run must also work.
4. `uv run pytest` passing, and an eval command that writes a JSON and Markdown report under `evals/reports/` (committed).
5. Everything squash-merged to `main`, with no leftover worktrees or branches.

**Operating notes.**
- You run unattended. Make reasonable decisions yourself, preferring least privilege and recoverable behaviour, and record them in the README or AGENTS.md. Don't stop to ask.
- Long commands: use Bash foreground timeouts up to 600000 ms.
- If a hard blocker needs the owner (for example, a system package that needs sudo), don't work around it. Finish everything else and report the exact command needed.
- Do not stop, restart, re-seed or snapshot the shared Docker stack.

**Final report** (your last message, concise):
- repo path and final `main` log;
- what was built;
- the checks you ran, with results;
- eval results;
- review rounds and lessons recorded;
- live model calls made;
- limitations;
- any erp-sandbox change request;
- documentation sources consulted.
