"""The DB suite must only create databases on an explicitly owned test server."""

import runpy
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rag/path_database.py"


def test_path_database_refuses_unowned_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVENTLENS_RAG_TEST_SERVER_OWNED", raising=False)
    monkeypatch.setenv(
        "EVENTLENS_RAG_DATABASE_URL", "postgresql://example:example@127.0.0.1:1/example"
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "fresh"])
    with pytest.raises(SystemExit, match="refusing database creation outside"):
        runpy.run_path(str(SCRIPT))["main"]()


def test_path_database_rejects_invalid_name_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTLENS_RAG_TEST_SERVER_OWNED", "1")
    monkeypatch.setenv(
        "EVENTLENS_RAG_DATABASE_URL", "postgresql://example:example@127.0.0.1:1/example"
    )
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "../production"])
    with pytest.raises(SystemExit, match="invalid database suffix"):
        runpy.run_path(str(SCRIPT))["main"]()
