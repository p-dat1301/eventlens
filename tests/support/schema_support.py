"""Build test schemas along the selected migration path (fresh or adopted)."""

import os
from pathlib import Path

import pytest

from backend.rag.infra.postgres.alembic_runner import adopt_legacy, upgrade_head
from backend.rag.infra.postgres.migrations import migrate

MIGRATIONS = Path(__file__).parents[2] / "src/backend/rag/infra/postgres/migrations"
# ``make rag-db-test`` runs the whole DB suite once per path. fresh: Alembic
# from empty (the production authority). adopted: historical 001-007 runner,
# then the verified baseline stamp, then Alembic upgrade.
SCHEMA_PATH_ENV = "EVENTLENS_RAG_TEST_SCHEMA_PATH"


def schema_path() -> str:
    """Selected path; anything but fresh/adopted fails the test run."""
    path = os.environ.get(SCHEMA_PATH_ENV, "fresh")
    if path not in {"fresh", "adopted"}:
        pytest.fail(f"unknown {SCHEMA_PATH_ENV}={path}")
    return path


def prepare_schema(url: str) -> None:
    """Build or verify the RAG schema at ``url`` along the selected path."""
    if schema_path() == "adopted":
        migrate(url, MIGRATIONS)
        _ = adopt_legacy(url)
    _ = upgrade_head(url)
