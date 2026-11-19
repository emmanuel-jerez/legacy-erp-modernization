"""Settings for this repo. The metric contract (docs/metric-contract.md) fixes the values that affect parity."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# This repo's clone of the frozen seed. Oracle calls are read-only in effect: sampled cases run
# in READ ONLY transactions and fixture rows are inserted in transactions that always roll back.
DB_NAME = "erp_migration_agent"
SCHEMA = "adempiere"

# Pinned oracle session. America/Los_Angeles is the cluster default observed on 2026-11-19.
SESSION_TIMEZONE = "America/Los_Angeles"
STATEMENT_TIMEOUT_MS = 5000

# Reserved primary-key range for fixture rows (never present in the seed).
FIXTURE_ID_MIN = 990000
FIXTURE_ID_MAX = 999999
MAX_FIXTURES_PER_CASE = 3

MAX_SAMPLED_CASES = 24
MAX_EDGE_CASES = 20
HELD_OUT_EVERY = 3  # every third case per source, in sha256(case id) order, is held out
MAX_REPAIR_ITERATIONS = 3

CASSETTE_DIR = REPO_ROOT / "cassettes"
RUNS_DIR = REPO_ROOT / "runs"
MIGRATED_DIR = REPO_ROOT / "migrated"
EVAL_REPORTS_DIR = REPO_ROOT / "evals" / "reports"
