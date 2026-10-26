"""Deterministic, token-budgeted evidence context assembly.

The serialized context (one compact JSON object per article, newline
separated) is counted as a whole: IDs, title, source, publication time, span
IDs, offsets and separators all consume budget. Spans are cut only at token
boundaries of the original NFC text, and a cut span gets a new ID and
fingerprint; serialized JSON is never cut.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backend.rag.application.ports.tokens import TokenCounter
from backend.rag.domain.retrieval import (
    EvidenceContext,
    MetadataProvenance,
    OutputPolicy,
    RetrievalMode,
    RetrievedArticle,
    RetrievedSpan,
    build_span,
)
from backend.shared.contracts.evidence import EvidenceSpan


@dataclass(frozen=True, slots=True)
class PreparedSpan:
    """Validated full-chunk span plus ranking provenance."""

    span: EvidenceSpan
    template: RetrievedSpan


@dataclass(frozen=True, slots=True)
class PreparedArticle:
    """Final-checked article with allowlisted fields and ordered spans."""

    article_id: UUID
    content_hash: str
    normalized_content: str
    title: str
    source_name: str
    source_domain: str
    source_published_at: datetime | None
    retrieval_basis: RetrievalMode
    vector_generation: int | None
    metadata: MetadataProvenance | None
    duplicate_article_ids: tuple[UUID, ...]
    spans: tuple[PreparedSpan, ...]


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """Context plus the articles/spans that actually fit."""

    context: EvidenceContext
    articles: tuple[RetrievedArticle, ...]


def _line(rank: int, article: PreparedArticle, spans: list[EvidenceSpan]) -> str:
    published = article.source_published_at
    payload = {
        "rank": rank,
        "article_id": str(article.article_id),
        "content_hash": article.content_hash,
        "title": article.title,
        "source_name": article.source_name,
        "source_domain": article.source_domain,
        "source_published_at": None if published is None else published.isoformat(),
        "spans": [
            {
                "span_id": str(span.span_id),
                "start": span.start_offset,
                "end": span.end_offset,
                "text": span.excerpt,
            }
            for span in spans
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True, slots=True)
class EvidenceAssembler:
    """Fill the budget in rank order; stop at the first span that cannot fit."""

    counter: TokenCounter
    policy: OutputPolicy

    def _count(self, lines: list[str]) -> int:
        return self.counter.count("\n".join(lines))

    def _truncate(  # noqa: PLR0913
        self,
        *,
        lines: list[str],
        rank: int,
        article: PreparedArticle,
        kept: list[EvidenceSpan],
        span: EvidenceSpan,
        budget: int,
    ) -> EvidenceSpan | None:
        """Largest token-boundary prefix of ``span`` that fits, if any."""
        total = self.counter.count(span.excerpt)
        low, high = self.policy.min_truncated_span_tokens, total - 1
        best: EvidenceSpan | None = None
        while low <= high:
            middle = (low + high) // 2
            end = self.counter.prefix_end(span.excerpt, middle)
            if end <= 0 or not span.excerpt[:end].strip():
                low = middle + 1
                continue
            candidate = build_span(
                article.article_id,
                article.content_hash,
                article.normalized_content,
                span.start_offset,
                span.start_offset + end,
            )
            if (
                self._count([*lines, _line(rank, article, [*kept, candidate])])
                <= budget
            ):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def assemble(
        self, articles: tuple[PreparedArticle, ...], budget: int
    ) -> AssembledContext:
        """Serialize articles in order within ``budget`` tokens."""
        lines: list[str] = []
        result: list[RetrievedArticle] = []
        truncated_spans = 0
        exhausted = False
        for article in articles:
            if exhausted:
                break
            rank = len(result) + 1
            kept: list[EvidenceSpan] = []
            templates: list[RetrievedSpan] = []
            for prepared in article.spans:
                attempt = [*lines, _line(rank, article, [*kept, prepared.span])]
                if self._count(attempt) <= budget:
                    kept.append(prepared.span)
                    templates.append(prepared.template)
                    continue
                cut = self._truncate(
                    lines=lines,
                    rank=rank,
                    article=article,
                    kept=kept,
                    span=prepared.span,
                    budget=budget,
                )
                if cut is not None:
                    kept.append(cut)
                    templates.append(prepared.template)
                    truncated_spans += 1
                exhausted = True
                break
            if not kept:
                break
            lines.append(_line(rank, article, kept))
            result.append(
                RetrievedArticle(
                    rank=rank,
                    article_id=article.article_id,
                    content_hash=article.content_hash,
                    title=article.title,
                    source_name=article.source_name,
                    source_domain=article.source_domain,
                    source_published_at=article.source_published_at,
                    retrieval_basis=article.retrieval_basis,
                    vector_generation=article.vector_generation,
                    metadata=article.metadata,
                    duplicate_article_ids=article.duplicate_article_ids,
                    spans=tuple(
                        RetrievedSpan(
                            span=span,
                            chunk_id=template.chunk_id,
                            chunk_index=template.chunk_index,
                            branches=template.branches,
                            lexical_rank=template.lexical_rank,
                            vector_rank=template.vector_rank,
                            fused_score=template.fused_score,
                            rerank_score=template.rerank_score,
                            truncated=span.span_id != template.span.span_id,
                        )
                        for span, template in zip(kept, templates, strict=True)
                    ),
                )
            )
        text = "\n".join(lines)
        identity = self.counter.identity
        return AssembledContext(
            context=EvidenceContext(
                context_format=self.policy.context_format,
                text=text,
                token_count=self.counter.count(text),
                token_budget=budget,
                tokenizer_id=identity.tokenizer_id,
                tokenizer_version=identity.version,
                truncated_spans=truncated_spans,
                omitted_articles=len(articles) - len(result),
            ),
            articles=tuple(result),
        )
