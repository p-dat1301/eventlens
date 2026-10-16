from pathlib import Path

import pytest

from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.adapters.scripted_persistence import ScriptedPersistence
from eventlens_rag.application.persist import AccessDeniedError, PersistArticle
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.graph import PersistenceGraph
from eventlens_rag.graph.runner import BaselineGraphRunner


def test_compiled_state_graph_validates_input_and_isolates_invocations() -> None:
    # Given
    runner = BaselineGraphRunner()

    # When
    first = runner.invoke("first request")
    second = runner.invoke("second request")

    # Then
    assert first.request_id != second.request_id
    assert first.message == "first request"
    assert second.message == "second request"


def test_compiled_state_graph_rejects_blank_input() -> None:
    # Given
    runner = BaselineGraphRunner()

    # When / Then
    with pytest.raises(ValueError, match="message"):
        _ = runner.invoke(" ")


def test_persistence_graph_isolates_caller_context_across_invocations() -> None:
    article = ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )
    repository = ScriptedPersistence()
    graph = PersistenceGraph(PersistArticle(repository, FixtureAccessPolicy()))
    allowed = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )
    denied = allowed.model_copy(update={"principal_id": "another-analyst"})

    result = graph.invoke(article, PersistenceContext(caller=allowed))
    with pytest.raises(AccessDeniedError):
        _ = graph.invoke(article, PersistenceContext(caller=denied))

    assert result.persisted is True
    assert repository.calls == [article]
