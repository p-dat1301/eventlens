"""D37 source viewer: opaque refs re-checked against the current revision."""

from collections.abc import Callable
from uuid import UUID

import httpx2
import pytest

from backend.rag.api_main import create_app
from backend.rag.application.use_cases.inspect_source import (
    InspectSource,
    SourceNotFoundError,
)
from backend.rag.composition import FIXTURE_CALLER
from backend.rag.domain.access import CallerScope
from backend.rag.infra.repositories.memory_source_references import (
    InMemorySourceReferences,
)
from backend.rag.presentation.briefing_http import PublicCitation
from briefing_support import (
    Harness,
    answer_of,
    body,
    error_of,
    harness,
    json_keys,
    query,
    revise,
    source_of,
)

pytestmark = pytest.mark.anyio


async def _briefing_then_view(
    h: Harness, mutate: Callable[[Harness, PublicCitation], object] | None = None
) -> tuple[PublicCitation, httpx2.Response]:
    transport = httpx2.ASGITransport(app=create_app(h.runtime))
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=10.0
    ) as client:
        answer = answer_of(await client.post("/briefing", json=body()))
        citation = answer.citations[0]
        if mutate is not None:
            _ = mutate(h, citation)
        view = await client.get(f"/briefing/sources/{citation.source_ref}")
    return citation, view


def _article_id(h: Harness, citation: PublicCitation) -> UUID:
    return next(
        d.article_id
        for d in h.store.documents.values()
        if str(d.canonical_url) == citation.source_url
    )


async def test_viewer_returns_authorized_current_source_and_span() -> None:
    # Given
    h = harness()

    # When
    citation, view = await _briefing_then_view(h)

    # Then
    assert view.status_code == 200
    source = source_of(view)
    assert source.title == citation.source_title
    assert source.source_url == citation.source_url
    assert source.span == citation.span
    span = source.span
    content = source.normalized_content
    assert content[span.start_offset : span.end_offset] == span.excerpt
    assert json_keys(view.text) == {
        "schema_version",
        "title",
        "source_name",
        "source_domain",
        "source_url",
        "source_published_at",
        "normalized_content",
        "span",
        "start_offset",
        "end_offset",
        "excerpt",
    }


async def test_viewer_rejects_tombstoned_source() -> None:
    # When
    _, view = await _briefing_then_view(
        harness(), lambda h, c: h.store.tombstone(_article_id(h, c))
    )

    # Then
    assert view.status_code == 410
    assert error_of(view).code == "source_stale"
    assert "Harbor" not in view.text


async def test_viewer_rejects_edited_revision() -> None:
    # When
    _, view = await _briefing_then_view(
        harness(),
        lambda h, c: h.store.add(revise(h.store.documents[_article_id(h, c)])),
    )

    # Then
    assert view.status_code == 410
    assert error_of(view).code == "source_stale"


async def test_viewer_rejects_revoked_access() -> None:
    # When
    _, view = await _briefing_then_view(
        harness(), lambda h, _: setattr(h.access, "revoked", True)
    )

    # Then
    assert view.status_code == 403
    assert error_of(view).code == "access_denied"


@pytest.mark.parametrize("ref", ["unknown-ref", "x" * 64])
async def test_viewer_unknown_reference_is_not_found(ref: str) -> None:
    # Given
    transport = httpx2.ASGITransport(app=create_app(harness().runtime))

    # When
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        view = await client.get(f"/briefing/sources/{ref}")

    # Then
    assert view.status_code == 404
    assert error_of(view).code == "source_not_found"


async def test_viewer_rejects_overlong_reference() -> None:
    # Given
    transport = httpx2.ASGITransport(app=create_app(harness().runtime))

    # When
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        view = await client.get(f"/briefing/sources/{'x' * 65}")

    # Then
    assert view.status_code == 400


async def test_viewer_storage_outage_is_typed() -> None:
    # When
    _, view = await _briefing_then_view(
        harness(), lambda h, _: setattr(h.store, "available", False)
    )

    # Then
    assert view.status_code == 503
    assert error_of(view).code == "retrieval_unavailable"


def test_reference_bound_to_another_caller_is_not_found() -> None:
    # Given: a reference issued to a different principal (hypothetical scope).
    h = harness()
    outcome = h.runtime.use_case.execute(query())
    references = InMemorySourceReferences()
    span = outcome.cited_sources[0].span
    other = CallerScope.model_construct(
        schema_version="caller_scope.v1",
        principal_id="other-analyst",
        scope_id="fixture-internal",
        policy_id="fixture-policy",
        policy_version="v1",
    )
    token = references.issue(other, span)
    viewer = InspectSource(references, h.store, h.access)
    reads = h.store.reads

    # When / Then
    with pytest.raises(SourceNotFoundError):
        _ = viewer.execute(token, FIXTURE_CALLER)
    assert h.store.reads == reads  # rejected before any storage read


def test_reference_registry_is_bounded() -> None:
    # Given
    h = harness()
    outcome = h.runtime.use_case.execute(query())
    span = outcome.cited_sources[0].span
    references = InMemorySourceReferences(capacity=2)

    # When
    tokens = [references.issue(FIXTURE_CALLER, span) for _ in range(3)]

    # Then
    assert references.resolve(tokens[0]) is None
    assert references.resolve(tokens[2]) is not None
