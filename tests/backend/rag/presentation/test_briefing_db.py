"""T7 HTTP path on real PostgreSQL: retrieval + shared source references.

Needs ``EVENTLENS_RAG_DATABASE_URL`` (``make rag-db-test``); each test gets its
own schema. The model stays the deterministic fake; no provider is touched.
"""

import os
from collections.abc import Generator
from uuid import uuid4

import httpx2
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import TypeAdapter

from backend.rag.api_main import create_app
from backend.rag.composition import (
    DATABASE_URL_ENV,
    FIXTURES,
    BriefingRuntime,
    compose_briefing,
    runtime_from_environment,
)
from backend.rag.domain.retrieval import RetrievalStorageError
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.repositories.postgres_source_references import (
    PostgresSourceReferences,
)
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from briefing_support import (
    answer_of,
    body,
    error_of,
    revise,
    source_of,
)
from schema_support import prepare_schema

pytestmark = pytest.mark.anyio
EXPIRE_ALL = (
    "UPDATE rag_source_references SET expires_at = created_at + interval '1 us'"
)
_ROWS = TypeAdapter(list[tuple[object, ...]])


@pytest.fixture
def rag_url() -> Generator[str]:
    """Fresh migrated schema loaded with the 30 fixture articles."""
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    schema = "t7_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    url = make_conninfo(base, options=f"-c search_path={schema},public")
    prepare_schema(url)
    store = PostgresRagStore(rag_engine(url))
    for document in load_fixture_articles(FIXTURES):
        store.persist_article(document)
    yield url
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def _runtime(url: str) -> BriefingRuntime:
    return compose_briefing(
        PostgresRetrievalStore(rag_engine(url)),
        references=PostgresSourceReferences(rag_engine(url)),
    )


def _client(runtime: BriefingRuntime) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(runtime)),
        base_url="http://testserver",
        timeout=10.0,
    )


async def _cited_ref(runtime: BriefingRuntime) -> tuple[str, str]:
    async with _client(runtime) as client:
        response = await client.post("/briefing", json=body())
    assert response.status_code == 200, response.text
    citation = answer_of(response).citations[0]
    return citation.source_ref, citation.source_url


async def test_briefing_answers_from_postgres(rag_url: str) -> None:
    # When
    async with _client(_runtime(rag_url)) as client:
        response = await client.post("/briefing", json=body())

    # Then
    assert response.status_code == 200
    answer = answer_of(response)
    assert answer.outcome == "answered"
    assert answer.retrieval_mode == "lexical_only"
    assert answer.citations
    assert answer.query_time_basis == "source_published_at"


async def test_reference_from_one_process_resolves_in_another(rag_url: str) -> None:
    # Given: two independently composed runtimes (stand-ins for two processes).
    ref, url = await _cited_ref(_runtime(rag_url))

    # When
    async with _client(_runtime(rag_url)) as client:
        view = await client.get(f"/briefing/sources/{ref}")

    # Then
    assert view.status_code == 200
    source = source_of(view)
    assert source.source_url == url
    span = source.span
    assert source.normalized_content[span.start_offset : span.end_offset] == (
        span.excerpt
    )


async def test_only_the_token_digest_is_stored(rag_url: str) -> None:
    # Given
    ref, _ = await _cited_ref(_runtime(rag_url))

    # When
    with psycopg.connect(rag_url) as connection:
        raw = connection.execute("SELECT * FROM rag_source_references").fetchall()
    rows = [tuple(str(value) for value in row) for row in _ROWS.validate_python(raw)]

    # Then
    assert rows
    assert all(ref not in values for values in rows)
    assert all(len(values[0]) == 64 for values in rows)


async def test_tombstoned_source_is_stale(rag_url: str) -> None:
    # Given
    runtime = _runtime(rag_url)
    ref, url = await _cited_ref(runtime)
    article = next(
        d for d in load_fixture_articles(FIXTURES) if str(d.canonical_url) == url
    )
    PostgresRagStore(rag_engine(rag_url)).tombstone_article(article.article_id)

    # When
    async with _client(runtime) as client:
        view = await client.get(f"/briefing/sources/{ref}")

    # Then
    assert view.status_code == 410
    assert error_of(view).code == "source_stale"


async def test_new_revision_makes_reference_stale(rag_url: str) -> None:
    # Given
    runtime = _runtime(rag_url)
    ref, url = await _cited_ref(runtime)
    article = next(
        d for d in load_fixture_articles(FIXTURES) if str(d.canonical_url) == url
    )
    PostgresRagStore(rag_engine(rag_url)).persist_article(revise(article))

    # When
    async with _client(runtime) as client:
        view = await client.get(f"/briefing/sources/{ref}")

    # Then
    assert view.status_code == 410


async def test_expired_reference_is_not_found(rag_url: str) -> None:
    # Given
    runtime = _runtime(rag_url)
    ref, _ = await _cited_ref(runtime)
    with psycopg.connect(rag_url) as connection:
        _ = connection.execute(EXPIRE_ALL)

    # When
    async with _client(runtime) as client:
        view = await client.get(f"/briefing/sources/{ref}")

    # Then
    assert view.status_code == 404
    assert error_of(view).code == "source_not_found"


def test_unreachable_reference_store_is_typed() -> None:
    # Given: a closed local port (no network beyond loopback).
    references = PostgresSourceReferences(
        rag_engine("postgresql://nobody:none@127.0.0.1:1/none")
    )

    # When / Then
    with pytest.raises(RetrievalStorageError):
        _ = references.resolve("anything")


def test_environment_opt_in_selects_postgres(rag_url: str) -> None:
    # When
    runtime = runtime_from_environment({DATABASE_URL_ENV: rag_url})
    fixture = runtime_from_environment({})

    # Then
    assert isinstance(runtime.sources.references, PostgresSourceReferences)
    assert isinstance(runtime.sources.sources, PostgresRetrievalStore)
    assert not isinstance(fixture.sources.sources, PostgresRetrievalStore)
