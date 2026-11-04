"""crud-api -> rag-api catalog over real HTTP and real PostgreSQL.

A real uvicorn rag-api (in a thread, loopback, ephemeral port) serves the
internal catalog over a per-test migrated schema; the crud-api HTTP adapter
calls it with the service token. The full flow: handoff import -> catalog
(via crud) -> summary job -> conversation run -> source viewer.
"""

import os
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.crud.application.ports.article_catalog import (
    CatalogUnavailableError,
    CatalogUpstreamError,
)
from backend.crud.composition import compose
from backend.crud.domain.catalog import ArticleListParams, CatalogFilterParams
from backend.crud.infra.rag_catalog_http import RagCatalogHttp
from backend.crud.main import create_app as create_crud
from backend.rag.api_main import create_app as create_rag
from backend.rag.application.handoff_validation import validate_handoff
from backend.rag.application.use_cases.import_handoff import ImportHandoff
from backend.rag.composition import (
    FIXTURE_CALLER,
    build_run_executor,
    build_summary_runner,
    news_runtime,
    runtime_from_environment,
)
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.postgres.engine import dispose_engines, rag_engine
from backend.rag.infra.postgres.source_versions import PostgresSourceVersions
from backend.rag.infra.postgres.store import PostgresRagStore
from handoff_support import batch, record
from schema_support import prepare_schema

pytestmark = pytest.mark.anyio
TOKEN = "integration-service-token-0123456789"  # noqa: S105 - test value
Json = dict[str, object]


@pytest.fixture
def rag_url() -> Generator[str]:
    base = os.environ.get("EVENTLENS_RAG_DATABASE_URL")
    if base is None:
        pytest.skip("EVENTLENS_RAG_DATABASE_URL required for real PostgreSQL test")
    schema = "crud_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    url = make_conninfo(base, options=f"-c search_path={schema},public")
    prepare_schema(url)
    yield url
    dispose_engines()
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
        )


def _env(url: str) -> dict[str, str]:
    return {
        "EVENTLENS_API_RAG_DATABASE_URL": url,
        "EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN": TOKEN,
        "EVENTLENS_RAG_ANSWER_PROVIDER": "fixture",
        "EVENTLENS_RAG_SUMMARY_PROVIDER": "fixture",
    }


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return cast("int", probe.getsockname()[1])


@pytest.fixture
def rag_api(rag_url: str) -> Generator[str]:
    """Real rag-api over HTTP; yields its base URL."""
    env = _env(rag_url)
    runtime = runtime_from_environment(env)
    app = create_rag(runtime, news_runtime(env, runtime))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            pytest.fail("rag-api did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _import(url: str, *records: dict[str, object]) -> None:
    report = validate_handoff(*batch(list(records)))
    _ = ImportHandoff(
        PostgresSourceVersions(rag_engine(url)),
        FixtureAccessPolicy(),
        lambda: datetime(2026, 10, 29, tzinfo=UTC),
    ).execute(report, FIXTURE_CALLER)


TEXT = (
    "Sở Giao thông vận tải TP.HCM thông báo cấm xe tải trên cầu Phú Mỹ từ 22 giờ. "
    "Người dân được khuyến cáo đi đường vòng qua cầu Sài Gòn trong thời gian thi công."
)


def test_http_adapter_reads_the_real_catalog(rag_url: str, rag_api: str) -> None:
    first = record(
        content=TEXT, word_count=len(TEXT.split()), title="Cấm xe tải cầu Phú Mỹ"
    )
    second = record(published_at="2026-10-21T08:00:00Z")
    _import(rag_url, first, second)
    adapter = RagCatalogHttp(rag_api, TOKEN)

    page = adapter.list_articles(ArticleListParams(CatalogFilterParams(), limit=1))
    rest = adapter.list_articles(
        ArticleListParams(CatalogFilterParams(), cursor=page.next_cursor, limit=1)
    )
    found = adapter.list_articles(ArticleListParams(CatalogFilterParams(q="Phú Mỹ")))
    detail = adapter.get_article(UUID(str(first["article_id"])), None)
    facets = adapter.facets(CatalogFilterParams())

    assert page.has_more
    assert {page.items[0].article_id, rest.items[0].article_id} == {
        UUID(str(first["article_id"])),
        UUID(str(second["article_id"])),
    }
    assert [card.title for card in found.items] == ["Cấm xe tải cầu Phú Mỹ"]
    assert detail.version.upstream_version_id == UUID(str(first["article_version_id"]))
    assert detail.content == TEXT
    assert facets.total == 2
    assert adapter.topics().items
    with pytest.raises(CatalogUpstreamError) as missing:
        _ = adapter.get_article(uuid4(), None)
    assert (missing.value.status, missing.value.code) == (404, "article_not_found")
    PostgresRagStore(rag_engine(rag_url)).tombstone_article(
        UUID(str(second["article_id"]))
    )
    with pytest.raises(CatalogUpstreamError) as gone:
        _ = adapter.get_article(UUID(str(second["article_id"])), None)
    assert gone.value.status == 410


def test_wrong_token_or_dead_catalog_is_unavailable(rag_api: str) -> None:
    with pytest.raises(CatalogUnavailableError) as wrong:
        _ = RagCatalogHttp(rag_api, "wrong-token").topics()
    assert wrong.value.reason == "service_auth_failed"
    with pytest.raises(CatalogUnavailableError) as dead:
        _ = RagCatalogHttp(f"http://127.0.0.1:{_free_port()}", TOKEN, 1.0).topics()
    assert dead.value.reason == "catalog_unreachable"


async def test_import_catalog_summary_run_and_source_viewer(
    rag_url: str, rag_api: str
) -> None:
    article = record(
        content=TEXT, word_count=len(TEXT.split()), title="Cấm xe tải cầu Phú Mỹ"
    )
    _import(rag_url, article, record())
    article_id = str(article["article_id"])
    crud = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(
            app=create_crud(compose(RagCatalogHttp(rag_api, TOKEN)))
        ),
        base_url="http://crud",
    )
    rag = httpx2.AsyncClient(base_url=rag_api, timeout=10)
    async with crud, rag:
        listed = cast(
            "Json", (await crud.get("/api/v1/articles", params={"q": "Phú Mỹ"})).json()
        )
        pending = cast(
            "Json", (await crud.get(f"/api/v1/articles/{article_id}/summary")).json()
        )
        report = build_summary_runner(rag_url, _env(rag_url)).run(5, "it")
        ready = cast(
            "Json", (await crud.get(f"/api/v1/articles/{article_id}/summary")).json()
        )

        token = cast(
            "str",
            cast("Json", (await rag.post("/api/v1/sessions")).json())["session_token"],
        )
        owner = {"Authorization": f"Bearer {token}"}
        created = await rag.post(
            "/api/v1/conversations",
            json={
                "scope": {"type": "articles", "articles": [{"article_id": article_id}]}
            },
            headers=owner,
        )
        conversation = cast("str", cast("Json", created.json())["conversation_id"])
        submitted = await rag.post(
            f"/api/v1/conversations/{conversation}/runs",
            json={"question": "Xe tải bị cấm trên cầu nào?"},
            headers=owner | {"Idempotency-Key": "flow-1"},
        )
        run_id = cast("str", cast("Json", submitted.json())["run_id"])
        executor = build_run_executor(rag_url, _env(rag_url))
        while claimed := executor.claim("it", 1):
            _ = executor.execute(claimed[0])
        run = cast(
            "Json", (await rag.get(f"/api/v1/runs/{run_id}", headers=owner)).json()
        )
        citations = cast("list[Json]", run["citations"])
        ref = await rag.post(str(citations[0]["source_ref_url"]), headers=owner)
        viewer = await rag.get(str(cast("Json", ref.json())["viewer_url"]))

    items = cast("list[Json]", listed["items"])
    assert [i["article_id"] for i in items] == [article_id]
    assert pending["status"] == "pending"
    assert {o.status for o in report.outcomes} == {"published"}
    assert ready["status"] == "ready"
    assert ready["key_points"]
    assert submitted.status_code == 202
    assert run["status"] == "completed", run
    assert {str(c["article_id"]) for c in citations} == {article_id}
    assert viewer.status_code == 200
    span = cast("Json", cast("Json", viewer.json())["span"])
    assert span["excerpt"] == cast("Json", citations[0]["span"])["excerpt"]
