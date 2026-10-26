"""G1 persist-first boundary, exercised directly on ``PersistArticle``.

Replaces the removed PersistenceGraph/BaselineGraphRunner skeleton tests: the
access, persist-first and business assertions are kept; assertions that only
checked the skeleton's LangGraph mechanics were removed with that code.
"""

from pathlib import Path

import pytest

from backend.rag.application.use_cases.persist import AccessDeniedError, PersistArticle
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from scripted_persistence import ScriptedPersistence

FIXTURE = Path(__file__).parents[4] / "fixtures/rag_article_document.v1.json"


def article() -> ArticleDocument:
    return ArticleDocument.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def caller() -> CallerScope:
    return CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )


def test_persists_document_through_injected_port() -> None:
    # Given
    document = article()
    repository = ScriptedPersistence()

    # When
    PersistArticle(repository, FixtureAccessPolicy()).execute(document, caller())

    # Then
    assert repository.calls == [document]


def test_denied_caller_starts_no_persistence_work() -> None:
    # Given
    repository = ScriptedPersistence()
    service = PersistArticle(repository, FixtureAccessPolicy(revoked=True))

    # When / Then
    with pytest.raises(AccessDeniedError):
        service.execute(article(), caller())
    assert repository.calls == []


def test_each_call_checks_its_own_caller() -> None:
    # Given
    document = article()
    repository = ScriptedPersistence()
    service = PersistArticle(repository, FixtureAccessPolicy())
    allowed = caller()
    denied = allowed.model_copy(update={"principal_id": "another-analyst"})

    # When
    service.execute(document, allowed)
    with pytest.raises(AccessDeniedError):
        service.execute(document, denied)

    # Then
    assert repository.calls == [document]
