"""Connection settings for the local sandbox. Local demo values; override with env vars."""

from __future__ import annotations

import os

PG_HOST = os.environ.get("ERP_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("ERP_PG_PORT", "56432"))
PG_ADMIN_USER = os.environ.get("ERP_PG_ADMIN_USER", "postgres")
PG_ADMIN_PASSWORD = os.environ.get("ERP_PG_ADMIN_PASSWORD", "postgres")

# iDempiere's own database and owner (image defaults).
ERP_DB = os.environ.get("ERP_DB", "idempiere")
ERP_DB_USER = os.environ.get("ERP_DB_USER", "adempiere")
ERP_DB_PASSWORD = os.environ.get("ERP_DB_PASSWORD", "adempiere")
ERP_SCHEMA = "adempiere"

# Frozen copy of the seeded ERP database; per-project databases are cloned from it.
SEED_TEMPLATE_DB = os.environ.get("ERP_SEED_TEMPLATE_DB", "idempiere_seed")

# Per-project copies of the seed template.
PROJECT_DBS = ("erp_schema_agent", "erp_migration_agent")

IDEMPIERE_URL = os.environ.get("ERP_IDEMPIERE_URL", "https://127.0.0.1:58443/webui/")

# Demo tenant: iDempiere's bundled GardenWorld client, extended with the fictional Fernhollow catalog.
CLIENT_ID = 11
ORG_ID = 11  # "HQ"


def dsn(dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    return (
        f"host={PG_HOST} port={PG_PORT} dbname={dbname} "
        f"user={user or PG_ADMIN_USER} password={password or PG_ADMIN_PASSWORD}"
    )
