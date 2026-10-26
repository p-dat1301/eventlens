import httpx2
import pytest

from backend.rag.api_main import app


@pytest.mark.anyio
async def test_rag_api_health_returns_ready_status() -> None:
    # Given
    transport = httpx2.ASGITransport(app=app)

    # When
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        timeout=5.0,
    ) as client:
        response = await client.get("/health")

    # Then
    assert response.status_code == 200
    assert response.text == '{"status":"ok"}'
