from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.rag.domain.answer import (
    BriefingAnswer,
    Citation,
    Claim,
    ClaimDisplayBlock,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.temporal import TemporalClaim


def test_temporal_evidence_when_cited_only_by_other_claim_then_reject() -> None:
    # Given
    article_path = Path(__file__).parents[4] / "fixtures" / "rag" / "articles"
    documents = tuple(
        ArticleDocument.model_validate_json((article_path / name).read_text("utf-8"))
        for name in ("01.json", "02.json")
    )
    second = documents[1]
    first_claim_id = UUID("30000000-0000-4000-8000-000000000001")
    second_claim_id = UUID("30000000-0000-4000-8000-000000000002")
    temporal = TemporalClaim(
        value_state="known",
        start_at=datetime(2026, 3, 1, 8, tzinfo=UTC),
        end_at=None,
        precision="minute",
        confidence=0.8,
        evidence_span_ids=(second.evidence_spans[0].span_id,),
        relative_expression=None,
        reference_at=None,
        timezone_basis=None,
    )
    claims = (
        Claim(claim_id=first_claim_id, text="Gate closed.", temporal_claim=temporal),
        Claim(claim_id=second_claim_id, text="Gate reopened.", temporal_claim=None),
    )
    citations = tuple(
        Citation(
            citation_id=UUID(f"90000000-0000-4000-8000-00000000000{index}"),
            claim_id=claim.claim_id,
            article_id=document.article_id,
            evidence_span_id=document.evidence_spans[0].span_id,
            source_title=document.title,
            source_reference=document.canonical_url,
        )
        for index, (claim, document) in enumerate(
            zip(claims, documents, strict=True), 1
        )
    )

    # When / Then
    with pytest.raises(ValidationError, match="unknown_temporal_evidence"):
        _ = BriefingAnswer(
            schema_version="briefing_answer.v1",
            claims=claims,
            citations=citations,
            uncertainties=(),
            display_blocks=tuple(
                ClaimDisplayBlock(kind="claim", claim_id=claim.claim_id)
                for claim in claims
            ),
            answer="Gate closed.\n\nGate reopened.",
            source_documents=documents,
            retrieved_at=datetime(2026, 9, 23, 10, tzinfo=UTC),
            model_version="model-1",
            index_version="index-1",
        )
