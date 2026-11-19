"""Connection settings: the project database and the agent's read-only role.

Local demo values bound to 127.0.0.1; override with environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

from erp_sandbox import config

DB_NAME = os.environ.get("SCHEMA_AGENT_DB", "erp_schema_agent")
SCHEMA = config.ERP_SCHEMA  # "adempiere"
RO_ROLE = os.environ.get("SCHEMA_AGENT_RO_ROLE", "erp_schema_agent_ro")
RO_PASSWORD = os.environ.get("SCHEMA_AGENT_RO_PASSWORD", "erp_schema_agent_ro")

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTE_DIR = Path(os.environ.get("SCHEMA_AGENT_CASSETTES", REPO_ROOT / "cassettes"))


def ro_dsn() -> str:
    return config.dsn(DB_NAME, user=RO_ROLE, password=RO_PASSWORD)

