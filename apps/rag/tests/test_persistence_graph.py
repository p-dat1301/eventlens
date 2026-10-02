from pathlib import Path

import pytest

from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.adapters.scripted_persistence import ScriptedPersistence
from eventlens_rag.application.persist import AccessDeniedError, PersistArticle
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.graph import PersistenceGraph


def test_graph_persists_document_through_injected_port() -> None:
    article = ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )
    caller = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )
    repository = ScriptedPersistence()
    context = PersistenceContext(caller=caller)
    graph = PersistenceGraph(PersistArticle(repository, FixtureAccessPolicy()))
    result = graph.invoke(article, context)
    assert repository.calls == [article]
    assert result.persisted
    assert result.article == article


def test_denied_graph_starts_no_persistence_work() -> None:
    article = ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )
    caller = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )
    repository = ScriptedPersistence()
    graph = PersistenceGraph(
        PersistArticle(repository, FixtureAccessPolicy(revoked=True))
    )
    with pytest.raises(AccessDeniedError):
        _ = graph.invoke(article, PersistenceContext(caller=caller))
    assert repository.calls == []
