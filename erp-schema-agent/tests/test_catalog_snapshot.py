from __future__ import annotations

import gzip
import json

import psycopg
import pytest

from erp_schema_agent import settings
from erp_schema_agent.retrieval.catalog import CATALOG_PATH, build_catalog

pytestmark = pytest.mark.integration


def test_committed_catalog_matches_the_database(readonly_db):
    with psycopg.connect(settings.ro_dsn()) as conn:
        live = json.loads(json.dumps(build_catalog(conn)))
    with gzip.open(CATALOG_PATH, "rt", encoding="utf-8") as fh:
        committed = json.load(fh)
    assert live == committed, "catalog snapshot is stale: run `uv run schema-agent build-catalog`"
