# erp-ui-operator

A Playwright and LLM hybrid that creates draft purchase orders through iDempiere's legacy
web UI and proves the result in the database. Work in progress: this slice holds the task
specs, page-snapshot normalization, action guardrails, the database verifier, reorder-point
derivation and cleanup.

```sh
uv run pytest
uv run ui-operator derive --vendor "Placeholder Contract Manufacturer A" --promised 2026-12-04
uv run ui-operator cleanup --dry-run
```
