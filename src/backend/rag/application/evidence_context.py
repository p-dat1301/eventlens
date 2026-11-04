"""Deterministic, token-budgeted evidence context assembly.

The serialized context (one compact JSON object per article, newline
separated) is counted as a whole: IDs, title, source, publication time, span
IDs, offsets and separators all consume budget. Spans are cut only at token
boundaries of the original NFC text, and a cut span gets a new ID and
fingerprint; serialized JSON is never cut.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from backend.rag.domain.retrieval_tuning import Packing
from backend.shared.contracts.evidence import EvidenceSpan


@dataclass(frozen=True, slots=True)
class PreparedSpan:
    """Validated full-chunk span plus ranking provenance."""

    span: EvidenceSpan
    template: RetrievedSpan
    fill: bool = False
    priority: int = 0


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
    indexed: bool = False


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
    """Fill the budget in rank order; stop at the first span that cannot fit.

    ``article_major`` (v3) takes every span of article 1, then article 2, ...
    ``breadth_first`` takes the first span of every article in rank order,
    then the second spans, ... so a tight budget keeps more distinct sources
    instead of spending it on a third span of the top article. Both keep the
    same serialization (one line per article, spans in selection order) and
    the same stop rule; offsets/IDs are never changed except by truncation.
    """

    counter: TokenCounter
    policy: OutputPolicy
    packing: Packing = "article_major"

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
        """Serialize articles in order within ``budget`` tokens.

        Fill spans (``PreparedSpan.fill``) never take part in the base pass;
        they are added afterwards, in candidate order, only where they fit.
        """
        if any(span.fill for article in articles for span in article.spans):
            return self._with_fill(articles, budget)
        if self.packing == "breadth_first":
            return self._breadth_first(articles, budget)
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
            result.append(_retrieved(rank, article, kept, templates))
        return self._result(lines, result, truncated_spans, len(articles), budget)

    def _with_fill(
        self, articles: tuple[PreparedArticle, ...], budget: int
    ) -> AssembledContext:
        """Base spans with the configured packing, then fill spans that fit."""
        base = tuple(
            replace(a, spans=tuple(s for s in a.spans if not s.fill)) for a in articles
        )
        packed = self.assemble(base, budget)
        context = packed.context
        if context.truncated_spans or context.omitted_articles:
            return packed  # the base pass already hit the budget
        by_id = {a.article_id: a for a in articles}
        kept: dict[UUID, list[PreparedSpan]] = {}
        for item in packed.articles:
            prepared = {p.span.span_id: p for p in by_id[item.article_id].spans}
            kept[item.article_id] = [prepared[s.span.span_id] for s in item.spans]
        order = [item.article_id for item in packed.articles]

        def lines() -> list[str]:
            return [
                _line(
                    rank,
                    by_id[article_id],
                    [p.span for p in sorted(kept[article_id], key=_order)],
                )
                for rank, article_id in enumerate(order, start=1)
            ]

        fills = sorted(
            (s for a in articles if a.article_id in kept for s in a.spans if s.fill),
            key=_order,
        )
        for span in fills:
            target = kept[span.span.article_id]
            target.append(span)
            if self._count(lines()) > budget:
                _ = target.pop()
        final_lines = lines()
        result = [
            _retrieved(
                rank,
                by_id[article_id],
                [p.span for p in sorted(kept[article_id], key=_order)],
                [p.template for p in sorted(kept[article_id], key=_order)],
            )
            for rank, article_id in enumerate(order, start=1)
        ]
        return self._result(final_lines, result, 0, len(articles), budget)

    def _result(
        self,
        lines: list[str],
        result: list[RetrievedArticle],
        truncated_spans: int,
        offered: int,
        budget: int,
    ) -> AssembledContext:
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
                omitted_articles=offered - len(result),
            ),
            articles=tuple(result),
        )

    def _breadth_first(  # noqa: C901 - one documented round loop
        self, articles: tuple[PreparedArticle, ...], budget: int
    ) -> AssembledContext:
        """Round r adds the r-th span of every article still in the context."""
        chosen: list[list[EvidenceSpan]] = [[] for _ in articles]
        templates: list[list[RetrievedSpan]] = [[] for _ in articles]
        truncated_spans = 0
        rounds = max((len(a.spans) for a in articles), default=0)

        def lines_for(extra: tuple[int, EvidenceSpan] | None = None) -> list[str]:
            out: list[str] = []
            for index, article in enumerate(articles):
                spans = list(chosen[index])
                if extra is not None and extra[0] == index:
                    spans.append(extra[1])
                if spans:
                    out.append(_line(len(out) + 1, article, spans))
            return out

        exhausted = False
        for round_index in range(rounds):
            for index, article in enumerate(articles):
                if exhausted:
                    break
                if round_index >= len(article.spans):
                    continue
                if round_index > 0 and not chosen[index]:
                    continue  # its first span never fitted; never skip ahead
                prepared = article.spans[round_index]
                if self._count(lines_for((index, prepared.span))) <= budget:
                    chosen[index].append(prepared.span)
                    templates[index].append(prepared.template)
                    continue
                cut = self._fit_prefix(lines_for, index, prepared.span, budget, article)
                if cut is not None:
                    chosen[index].append(cut)
                    templates[index].append(prepared.template)
                    truncated_spans += 1
                exhausted = True
            if exhausted:
                break
        lines: list[str] = []
        result: list[RetrievedArticle] = []
        for index, article in enumerate(articles):
            if not chosen[index]:
                continue
            rank = len(result) + 1
            lines.append(_line(rank, article, chosen[index]))
            result.append(_retrieved(rank, article, chosen[index], templates[index]))
        return self._result(lines, result, truncated_spans, len(articles), budget)

    def _fit_prefix(
        self,
        lines_for: Callable[[tuple[int, EvidenceSpan] | None], list[str]],
        index: int,
        span: EvidenceSpan,
        budget: int,
        article: PreparedArticle,
    ) -> EvidenceSpan | None:
        """Largest token-boundary prefix of ``span`` that fits (breadth-first)."""
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
            if self._count(lines_for((index, candidate))) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best


def _retrieved(
    rank: int,
    article: PreparedArticle,
    kept: list[EvidenceSpan],
    templates: list[RetrievedSpan],
) -> RetrievedArticle:
    return RetrievedArticle(
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
        indexed=article.indexed,
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


def _order(span: PreparedSpan) -> tuple[int, int]:
    """Candidate order; offsets as a stable tie-break."""
    return (span.priority, span.span.start_offset)
