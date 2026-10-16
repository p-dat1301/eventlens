from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from eventlens_rag.adapters.fixture_access import FixtureAccessPolicy
from eventlens_rag.adapters.scripted_persistence import ScriptedPersistence
from eventlens_rag.application.persist import PersistArticle
from eventlens_rag.domain.access import CallerScope
from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.graph.context import PersistenceContext
from eventlens_rag.graph.graph import PersistenceGraph
from eventlens_rag.graph.routes import persistence_route
from eventlens_rag.graph.runner import BaselineGraphRunner

if TYPE_CHECKING:
    from eventlens_rag.graph.state import PersistenceChannels


def test_concurrent_runs_keep_distinct_state() -> None:
    runner = BaselineGraphRunner()
    messages = ("first", "second", "third")
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = tuple(executor.map(runner.invoke, messages))
    assert tuple(result.message for result in results) == messages
    assert len({result.request_id for result in results}) == 3


def test_persistence_route_skips_completed_state() -> None:
    article = ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )
    state: PersistenceChannels = {"article": article, "persisted": True}

    route = persistence_route(state)

    assert route == "complete"


def test_compiled_persistence_graph_routes_pending_state_to_port() -> None:
    article = ArticleDocument.model_validate_json(
        (Path(__file__).parents[3] / "fixtures/rag_article_document.v1.json").read_text(
            encoding="utf-8"
        )
    )
    repository = ScriptedPersistence()
    graph = PersistenceGraph(PersistArticle(repository, FixtureAccessPolicy()))
    caller = CallerScope(
        schema_version="caller_scope.v1",
        principal_id="fixture-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )

    result = graph.invoke(article, PersistenceContext(caller=caller))

    assert result.persisted is True
    assert repository.calls == [article]
