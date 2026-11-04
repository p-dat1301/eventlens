"""rag-api news routes without a database: auth and fail-closed behaviour."""

import httpx2
import pytest

from backend.rag.api_main import create_app
from backend.rag.composition import compose_briefing, fixture_store, news_runtime

pytestmark = pytest.mark.anyio
TOKEN = "internal-test-token-0123456789abcdef"  # noqa: S105 - test value


def _client(environ: dict[str, str]) -> httpx2.AsyncClient:
    runtime = compose_briefing(fixture_store())
    app = create_app(runtime, news_runtime(environ, runtime))
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    )


async def test_internal_catalog_requires_a_configured_service_token() -> None:
    async with _client({}) as client:
        unconfigured = await client.get("/internal/v1/catalog/topics")
    async with _client({"EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN": TOKEN}) as client:
        missing = await client.get("/internal/v1/catalog/topics")
        wrong = await client.get(
            "/internal/v1/catalog/topics", headers={"Authorization": "Bearer nope"}
        )
        forged_user = await client.get(
            "/internal/v1/catalog/topics", headers={"X-User-ID": "admin"}
        )
    assert unconfigured.status_code == 503
    assert unconfigured.json()["error"]["code"] == "internal_auth_not_configured"
    assert missing.status_code == wrong.status_code == forged_user.status_code == 401


async def test_catalog_without_storage_is_503_not_an_empty_page() -> None:
    async with _client({"EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN": TOKEN}) as client:
        response = await client.get(
            "/internal/v1/catalog/articles",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "catalog_unavailable"


async def test_chat_without_storage_fails_closed() -> None:
    async with _client({}) as client:
        session = await client.post("/api/v1/sessions")
        runs = await client.get("/api/v1/conversations")
    assert session.status_code == runs.status_code == 503


async def test_legacy_briefing_errors_keep_their_envelope() -> None:
    async with _client({}) as client:
        response = await client.post("/briefing", json={"query": "x"})
    assert response.status_code == 400
    assert response.json()["schema_version"] == "briefing_error.v1"
