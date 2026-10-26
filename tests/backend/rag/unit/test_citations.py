"""T6 grounding: ID/span/offset/fingerprint attacks, temporal and Unicode."""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from unicodedata import normalize
from uuid import UUID, uuid4

import pytest

from backend.rag.application.citations import (
    build_registry,
    select_evidence,
    validate_draft,
)
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import RetrievalResult
from backend.rag.domain.synthesis import (
    SynthesisOutcome,
    TrustedSource,
    TrustedSourceRegistry,
)
from backend.rag.domain.synthesis_draft import (
    DraftClaim,
    DraftUncertainty,
    SynthesisDraft,
)
from backend.rag.domain.synthesis_errors import (
    GroundingFailureCode,
    MalformedOutputReason,
    SynthesisGroundingError,
    SynthesisMalformedOutputError,
)
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from backend.shared.contracts.evidence import EvidenceSpan
from synthesis_support import (
    IDENTITY,
    SYNTH_POLICY,
    MemoryStore,
    article,
    briefing,
    claim,
    draft,
    respond,
    retrieve,
    run,
    sequential_ids,
    store_with,
)

BRIDGE = "Harbor bridge closed on 2026-03-14 after the storm."
FERRY = "Harbor ferries resumed service after the closure."


def _setup() -> tuple[MemoryStore, BriefingQuery, RetrievalResult]:
    store = store_with(
        article(BRIDGE, title="Bridge report"),
        article(FERRY, title="Ferry update"),
    )
    query = briefing("harbor")
    return store, query, retrieve(store, query)


def _span(retrieval: RetrievalResult, index: int = 0) -> UUID:
    return retrieval.articles[index].spans[0].span.span_id


type ContentFor = Callable[[RetrievalResult], str]
type SpanMutation = Callable[[EvidenceSpan], dict[str, object]]
type DraftBuilder = Callable[[UUID], SynthesisDraft]


def _grounding(content_for: ContentFor) -> GroundingFailureCode:
    store, query, retrieval = _setup()
    content = content_for(retrieval)
    with pytest.raises(SynthesisGroundingError) as raised:
        _ = run(
            ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
        )
    return raised.value.reason


def _registry(
    retrieval: RetrievalResult,
    store: MemoryStore,
    query: BriefingQuery,
    sent: frozenset[UUID] | None = None,
) -> TrustedSourceRegistry:
    selected = select_evidence(retrieval, 20)
    ids = tuple(entry.article.article_id for entry in selected)
    sent_ids = sent or frozenset(
        item.span.span_id for entry in selected for item in entry.spans
    )
    return build_registry(selected, sent_ids, store.current_articles(ids), query)


def _one_claim(span_id: UUID) -> SynthesisDraft:
    return SynthesisDraft.model_validate_json(
        draft([claim("c1", "Bridge closed.", [span_id])])
    )


def test_unknown_span_id_fails_closed() -> None:
    reason = _grounding(lambda _: draft([claim("c1", "x", [uuid4()])]))
    assert reason is GroundingFailureCode.UNKNOWN_SPAN


def test_valid_but_not_sent_span_is_rejected() -> None:
    store, query, retrieval = _setup()
    sent = frozenset({_span(retrieval, 0)})
    registry = _registry(retrieval, store, query, sent)

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = validate_draft(
            _one_claim(_span(retrieval, 1)), registry, SYNTH_POLICY, sequential_ids()
        )

    assert raised.value.reason is GroundingFailureCode.SPAN_NOT_SENT


def test_span_of_other_article_attached_to_source_is_wrong_article() -> None:
    store, query, retrieval = _setup()
    registry = _registry(retrieval, store, query)
    first, second = (retrieval.articles[i].article_id for i in (0, 1))
    foreign = registry.sources[second].sent_spans[0]
    tampered = TrustedSourceRegistry(
        sources={
            first: TrustedSource(
                document=registry.sources[first].document, sent_spans=(foreign,)
            )
        },
        span_owner={foreign.span_id: first},
    )

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = validate_draft(
            _one_claim(foreign.span_id), tampered, SYNTH_POLICY, sequential_ids()
        )
    assert raised.value.reason is GroundingFailureCode.WRONG_ARTICLE


def _tampered(
    span: EvidenceSpan, registry: TrustedSourceRegistry
) -> TrustedSourceRegistry:
    source = registry.sources[span.article_id]
    return TrustedSourceRegistry(
        sources={
            span.article_id: TrustedSource(document=source.document, sent_spans=(span,))
        },
        span_owner={span.span_id: span.article_id},
    )


def _fingerprint(text: str) -> str:
    return f"sha256:{sha256(text.encode()).hexdigest()}"


MUTATIONS: list[tuple[SpanMutation, GroundingFailureCode]] = [
    (
        lambda _: {"content_hash": "sha256:" + "0" * 64},
        GroundingFailureCode.CONTENT_HASH_MISMATCH,
    ),
    (
        lambda s: {"end_offset": s.end_offset + 500},
        GroundingFailureCode.OFFSET_MISMATCH,
    ),
    (
        lambda s: {
            "start_offset": s.start_offset + 1,
            "end_offset": s.end_offset,
        },
        GroundingFailureCode.EXCERPT_MISMATCH,
    ),
    (
        lambda _: {"span_fingerprint": "sha256:" + "1" * 64},
        GroundingFailureCode.FINGERPRINT_MISMATCH,
    ),
]


@pytest.mark.parametrize(
    ("mutate", "code"),
    MUTATIONS,
)
def test_tampered_span_identity_fails_closed(
    mutate: SpanMutation, code: GroundingFailureCode
) -> None:
    store, query, retrieval = _setup()
    registry = _registry(retrieval, store, query)
    original = retrieval.articles[0].spans[0].span
    changes = mutate(original)
    bad = EvidenceSpan.model_construct(None, **{**dict(original), **changes})

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = validate_draft(
            _one_claim(bad.span_id),
            _tampered(bad, registry),
            SYNTH_POLICY,
            sequential_ids(),
        )
    assert raised.value.reason is code


def test_old_revision_span_after_correction_is_stale_not_cited() -> None:
    store, query, retrieval = _setup()
    target = retrieval.articles[0].article_id
    store.add(article(BRIDGE + " Corrected.", article_id=target))

    from backend.rag.domain.synthesis_errors import (  # noqa: PLC0415
        SynthesisEvidenceStaleError,
    )

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = _registry(retrieval, store, query)


SHAPE_ATTACKS: list[ContentFor] = [
    lambda r: draft([claim("c1", "a", [_span(r)]), claim("c1", "b", [_span(r, 1)])]),
    lambda r: draft([claim("c1", "a", [_span(r), _span(r)])]),
    lambda r: draft(
        [claim("c1", "a", [_span(r)])],
        uncertainties=[{"reason_code": "conflicting_sources", "claim_ref": "c9"}],
    ),
    lambda _: draft([claim("c1", "a", [])]),
    lambda _: draft([]),
    lambda r: draft([claim("c1", "a", [_span(r)])], answer="Uncited prose."),
    lambda r: draft(
        [{**claim("c1", "a", [_span(r)]), "source_title": "Fake"}],
    ),
    lambda r: draft(
        [{**claim("c1", "a", [_span(r)]), "source_reference": "https://evil.test"}],
    ),
    lambda r: draft(
        [claim("c1", "a", [_span(r)])],
        uncertainties=[
            {
                "reason_code": "conflicting_sources",
                "claim_ref": None,
                "explanation": "Free prose sneaks in.",
            }
        ],
    ),
    lambda r: draft([claim("c1", "a", [_span(r)])], caller_scope={"x": 1}),
    lambda r: draft([claim("c1", "a", [_span(r)])], model_version="evil"),
    lambda r: draft(
        [claim("c1", "a", [_span(r)])],
        status="insufficient",
        uncertainties=[{"reason_code": "insufficient_evidence", "claim_ref": None}],
    ),
    lambda r: draft(
        [claim("c1", "a", [_span(r)])],
        uncertainties=[{"reason_code": "no_matches", "claim_ref": None}],
    ),
    lambda r: draft([claim("c1", "a", [str(_span(r))[:-1] + "Z"])]),  # pyright: ignore[reportArgumentType]
]


@pytest.mark.parametrize(
    "content_for",
    SHAPE_ATTACKS,
    ids=[
        "duplicate-claim-ref",
        "duplicate-span-ref",
        "dangling-claim-ref",
        "claim-without-citation",
        "sufficient-without-claims",
        "free-answer",
        "fake-title",
        "fake-url",
        "uncertainty-prose",
        "caller-scope",
        "self-provenance",
        "insufficient-with-claims",
        "server-only-reason",
        "malformed-uuid",
    ],
)
def test_shape_attacks_fail_closed_before_any_answer(
    content_for: ContentFor,
) -> None:
    store, query, retrieval = _setup()
    content = content_for(retrieval)

    with pytest.raises(SynthesisMalformedOutputError) as raised:
        _ = run(
            ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
        )
    assert raised.value.reason is MalformedOutputReason.SCHEMA_VIOLATION


def _constructed(**fields: object) -> SynthesisDraft:
    return SynthesisDraft.model_construct(
        schema_version="synthesis_draft.v1",
        evidence_status=fields.get("status", "sufficient"),
        claims=fields.get("claims", ()),
        uncertainties=fields.get("uncertainties", ()),
    )


def _draft_claim(ref: str, spans: tuple[UUID, ...]) -> DraftClaim:
    return DraftClaim.model_construct(
        claim_ref=ref, text="t", evidence_span_ids=spans, temporal_claim=None
    )


SHAPE_RECHECKS: list[tuple[DraftBuilder, GroundingFailureCode]] = [
    (
        lambda s: _constructed(claims=(_draft_claim("c1", (s,)),) * 2),
        GroundingFailureCode.DUPLICATE_CLAIM_REF,
    ),
    (
        lambda s: _constructed(claims=(_draft_claim("c1", (s, s)),)),
        GroundingFailureCode.DUPLICATE_SPAN_REF,
    ),
    (
        lambda _: _constructed(claims=(_draft_claim("c1", ()),)),
        GroundingFailureCode.CLAIM_WITHOUT_CITATION,
    ),
    (
        lambda s: _constructed(
            claims=(_draft_claim("c1", (s,)),),
            uncertainties=(
                DraftUncertainty.model_construct(
                    reason_code="conflicting_sources", claim_ref="c2"
                ),
            ),
        ),
        GroundingFailureCode.DANGLING_CLAIM_REF,
    ),
    (lambda _: _constructed(), GroundingFailureCode.STATUS_MISMATCH),
]


@pytest.mark.parametrize(
    ("build", "code"),
    SHAPE_RECHECKS,
)
def test_validator_rechecks_shape_for_unparsed_drafts(
    build: DraftBuilder, code: GroundingFailureCode
) -> None:
    store, query, retrieval = _setup()
    registry = _registry(retrieval, store, query)
    built = build(_span(retrieval))

    with pytest.raises(SynthesisGroundingError) as raised:
        _ = validate_draft(built, registry, SYNTH_POLICY, sequential_ids())
    assert raised.value.reason is code


def _temporal(spans: list[UUID], **fields: object) -> dict[str, object]:
    body: dict[str, object] = {
        "value_state": "known",
        "start_at": "2026-03-14T00:00:00Z",
        "end_at": None,
        "precision": "day",
        "confidence": 0.9,
        "evidence_span_ids": [str(span) for span in spans],
        "relative_expression": None,
        "reference_at": None,
        "timezone_basis": None,
    }
    body.update(fields)
    return body


def test_known_occurrence_time_comes_from_cited_span_not_publication() -> None:
    published = datetime(2026, 3, 20, tzinfo=UTC)
    store = store_with(article(BRIDGE, published=published))
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    span = _span(retrieval)
    content = draft([claim("c1", "Bridge closed.", [span], _temporal([span]))])

    result = run(
        ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
    )

    temporal = result.answer.claims[0].temporal_claim
    assert temporal is not None
    assert temporal.start_at == datetime(2026, 3, 14, tzinfo=UTC)
    assert temporal.start_at != published


def test_unknown_time_stays_unknown() -> None:
    store, query, retrieval = _setup()
    span = _span(retrieval)
    unknown = _temporal(
        [span], value_state="unknown", start_at=None, precision="unknown"
    )
    content = draft([claim("c1", "Bridge closed.", [span], unknown)])

    result = run(
        ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
    )

    temporal = result.answer.claims[0].temporal_claim
    assert temporal is not None
    assert temporal.value_state == "unknown"
    assert temporal.start_at is None


def test_relative_time_with_utc_basis_is_kept() -> None:
    store, query, retrieval = _setup()
    span = _span(retrieval)
    relative = _temporal(
        [span],
        relative_expression="yesterday",
        reference_at="2026-03-15T00:00:00Z",
        timezone_basis="UTC",
    )
    content = draft([claim("c1", "Bridge closed.", [span], relative)])

    result = run(
        ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
    )

    temporal = result.answer.claims[0].temporal_claim
    assert temporal is not None
    assert temporal.relative_expression == "yesterday"


@pytest.mark.parametrize(
    "fields",
    [
        {"relative_expression": "yesterday"},
        {"start_at": "2026-03-14T00:00:00+07:00"},
        {"value_state": "unknown"},
    ],
    ids=["relative-without-basis", "non-utc", "unknown-with-timestamp"],
)
def test_invalid_temporal_basis_fails_closed(fields: dict[str, object]) -> None:
    store, query, retrieval = _setup()
    span = _span(retrieval)
    content = draft([claim("c1", "x", [span], _temporal([span], **fields))])

    with pytest.raises(SynthesisMalformedOutputError):
        _ = run(
            ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
        )


def test_temporal_evidence_must_be_cited_by_its_claim() -> None:
    reason = _grounding(
        lambda r: draft([claim("c1", "x", [_span(r)], _temporal([_span(r, 1)]))])
    )
    assert reason is GroundingFailureCode.TEMPORAL_EVIDENCE_NOT_CITED


@pytest.mark.parametrize(
    "content",
    [
        normalize("NFD", "Harbor café reopened near Straße 5 in İzmir."),
        "Harbor Ελλάδα λιμάνι έκλεισε. Straße İSTANBUL ß.",
        "Harbor 港口 关闭 today, 항구 reopened.",
    ],
    ids=["nfd-input", "greek-sharp-s-dotted-i", "cjk-hangul"],
)
def test_unicode_offsets_are_nfc_code_points(content: str) -> None:
    store = store_with(article(content))
    query = briefing("harbor")
    retrieval = retrieve(store, query)

    result = run(DeterministicFakeSynthesizer(IDENTITY), store, query, retrieval)

    assert result.outcome is SynthesisOutcome.ANSWERED
    document = result.answer.source_documents[0]
    assert document.normalized_content == normalize("NFC", content)
    for span in document.evidence_spans:
        assert document.normalized_content[span.start_offset : span.end_offset] == (
            span.excerpt
        )
        assert span.span_fingerprint == _fingerprint(span.excerpt)
        assert span.offset_basis == "unicode_codepoint"


def test_truncated_span_keeps_post_truncation_identity() -> None:
    long = "Harbor " + " ".join(f"word{i}" for i in range(400))
    store = store_with(article(long, article_id=UUID(int=7)))
    query = briefing("harbor")
    retrieval = retrieve(store, query, budget=200)
    truncated = [
        item for entry in retrieval.articles for item in entry.spans if item.truncated
    ]
    assert len(truncated) == 1
    cut = truncated[0].span

    content = draft([claim("c1", "Long piece.", [cut.span_id])])
    model = ScriptedSynthesizer(IDENTITY, [respond(content)])
    result = run(model, store, query, retrieval)

    sent = model.requests[0].serialized
    assert cut.excerpt in sent.messages[1].content
    citation = result.answer.citations[0]
    assert citation.evidence_span_id == cut.span_id
    doc = next(
        d for d in result.answer.source_documents if d.article_id == cut.article_id
    )
    assert doc.evidence_spans == (cut,)
    assert len(cut.excerpt) < len(long)


def test_rendered_answer_contains_only_claims_and_templates() -> None:
    store, query, retrieval = _setup()
    content = draft(
        [claim("c1", "Bridge closed.", [_span(retrieval)])],
        uncertainties=[{"reason_code": "conflicting_sources", "claim_ref": "c1"}],
    )

    result = run(
        ScriptedSynthesizer(IDENTITY, [respond(content)]), store, query, retrieval
    )

    expected = "Bridge closed.\n\n" + SYNTH_POLICY.template("conflicting_sources").text
    assert result.answer.answer == expected
    assert result.answer.uncertainties[0].claim_id == result.answer.claims[0].claim_id
    assert [b.kind for b in result.answer.display_blocks] == ["claim", "uncertainty"]
