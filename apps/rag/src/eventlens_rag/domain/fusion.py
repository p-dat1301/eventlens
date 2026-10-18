"""Pure branch ranking, RRF fusion, dedup, rerank ordering and aggregation.

Freshness only changes each branch's score before its ranks are assigned; the
RRF formula and the fused/reranked tie-breaks are exactly the frozen policy.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast
from uuid import UUID

from eventlens_rag.domain.retrieval import (
    Branch,
    BranchCandidate,
    ChunkRef,
    DedupPolicy,
    FreshnessPolicy,
    FusedCandidate,
    LexicalPage,
    LexicalPolicy,
    RankedCandidate,
    VectorHit,
    VectorPolicy,
    dedup_fingerprint,
    text_terms,
)

SECONDS_PER_DAY = 86_400.0

type PublishedKey = tuple[int, float]


def published_key(value: datetime | None) -> PublishedKey:
    """Sort key for ``source_published_at DESC NULLS LAST``."""
    if value is None:
        return (1, 0.0)
    return (0, -value.timestamp())


def freshness_bonus(
    published: datetime | None, now: datetime, policy: FreshnessPolicy
) -> float:
    """Additive exponential decay bonus; missing or future timestamps get 0."""
    if published is None or published > now:
        return 0.0
    age_days = (now - published).total_seconds() / SECONDS_PER_DAY
    return policy.weight * math.pow(0.5, age_days / policy.half_life_days)


@dataclass(frozen=True, slots=True)
class _Scored:
    chunk: ChunkRef
    score: float
    secondary: float
    published: datetime | None
    text: str
    generation: int | None

    def key(self) -> tuple[float, float, PublishedKey, str, str]:
        return (
            -self.score,
            -self.secondary,
            published_key(self.published),
            str(self.chunk.article_id),
            str(self.chunk.chunk_id),
        )


def _assign_ranks(
    scored: Iterable[_Scored], branch: Branch, depth: int, per_article: int
) -> tuple[BranchCandidate, ...]:
    """Sort, cap per article, cut to depth and number ranks from 1."""
    taken: dict[UUID, int] = {}
    seen: set[UUID] = set()
    result: list[BranchCandidate] = []
    for item in sorted(scored, key=_Scored.key):
        chunk_id = item.chunk.chunk_id
        if chunk_id in seen:
            continue
        count = taken.get(item.chunk.article_id, 0)
        if count >= per_article:
            continue
        seen.add(chunk_id)
        taken[item.chunk.article_id] = count + 1
        result.append(
            BranchCandidate(
                chunk=item.chunk,
                branch=branch,
                rank=len(result) + 1,
                score=item.score,
                source_published_at=item.published,
                text=item.text,
                vector_generation=item.generation,
            )
        )
        if len(result) >= depth:
            break
    return tuple(result)


def rank_lexical(
    page: LexicalPage,
    now: datetime,
    policy: LexicalPolicy,
    freshness: FreshnessPolicy,
    chunker_version: str,
) -> tuple[BranchCandidate, ...]:
    """Rank the bounded storage chunk pool by lexeme coverage + freshness.

    Matching uses PostgreSQL ``simple`` lexemes for both query and chunk, so no
    Python casefold is involved and offsets stay on the original NFC text.
    """
    if page.term_count == 0:
        return ()
    scored = [
        _Scored(
            chunk=ChunkRef(
                article_id=chunk.article_id,
                content_hash=chunk.content_hash,
                chunker_version=chunker_version,
                chunk_index=chunk.chunk_index,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
            ),
            score=chunk.matched_terms / page.term_count
            + freshness_bonus(chunk.source_published_at, now, freshness),
            secondary=chunk.document_rank,
            published=chunk.source_published_at,
            text="",  # filled by the caller for the selected chunks only
            generation=None,
        )
        for chunk in page.chunks
        if chunk.matched_terms > 0
    ]
    return _assign_ranks(
        scored, Branch.LEXICAL, policy.depth, policy.max_chunks_per_article
    )


def rank_vector(
    hits: Sequence[VectorHit],
    now: datetime,
    policy: VectorPolicy,
    freshness: FreshnessPolicy,
    chunker_version: str,
) -> tuple[BranchCandidate, ...]:
    """Rank exact cosine hits; similarity is ``1 - cosine distance``."""
    scored = [
        _Scored(
            chunk=ChunkRef(
                article_id=hit.article_id,
                content_hash=hit.content_hash,
                chunker_version=chunker_version,
                chunk_index=hit.chunk_index,
                start_offset=hit.start_offset,
                end_offset=hit.end_offset,
            ),
            score=(1.0 - hit.distance)
            + freshness_bonus(hit.source_published_at, now, freshness),
            secondary=0.0,
            published=hit.source_published_at,
            text=hit.content,
            generation=hit.generation,
        )
        for hit in hits
    ]
    return _assign_ranks(
        scored, Branch.VECTOR, policy.depth, policy.max_chunks_per_article
    )


def fused_key(candidate: FusedCandidate) -> tuple[float, PublishedKey, str, str]:
    """``fused_score DESC, source_published_at DESC NULLS LAST, article, chunk``."""
    return (
        -candidate.fused_score,
        published_key(candidate.source_published_at),
        str(candidate.chunk.article_id),
        str(candidate.chunk.chunk_id),
    )


def fuse(
    lexical: Sequence[BranchCandidate], vector: Sequence[BranchCandidate], k: int
) -> tuple[FusedCandidate, ...]:
    """Reciprocal rank fusion; a branch without the chunk contributes 0."""
    lexical_rank: dict[UUID, BranchCandidate] = {}
    vector_rank: dict[UUID, BranchCandidate] = {}
    for target, items in ((lexical_rank, lexical), (vector_rank, vector)):
        for item in items:
            existing = target.get(item.chunk.chunk_id)
            if existing is None or item.rank < existing.rank:
                target[item.chunk.chunk_id] = item
    fused: list[FusedCandidate] = []
    for chunk_id in lexical_rank.keys() | vector_rank.keys():
        lex = lexical_rank.get(chunk_id)
        vec = vector_rank.get(chunk_id)
        base = lex if lex is not None else vec
        if base is None:  # pragma: no cover - union of both key sets
            continue
        score = sum(1.0 / (k + item.rank) for item in (lex, vec) if item is not None)
        fused.append(
            FusedCandidate(
                chunk=base.chunk,
                fused_score=score,
                own_fused_score=score,
                lexical_rank=None if lex is None else lex.rank,
                vector_rank=None if vec is None else vec.rank,
                source_published_at=base.source_published_at,
                text=base.text,
                vector_generation=None if vec is None else vec.vector_generation,
            )
        )
    return tuple(sorted(fused, key=fused_key))


def _shingles(text: str, size: int) -> frozenset[tuple[str, ...]]:
    words = text_terms(text)
    width = min(size, len(words))
    if width == 0:
        return frozenset()
    return frozenset(
        tuple(words[index : index + width]) for index in range(len(words) - width + 1)
    )


def jaccard(
    left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]
) -> float:
    """Jaccard similarity; empty sets are never near-duplicates."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _representative_key(candidate: FusedCandidate) -> tuple[int, float, str, str]:
    published = candidate.source_published_at
    return (
        1 if published is None else 0,
        0.0 if published is None else published.timestamp(),
        str(candidate.chunk.article_id),
        str(candidate.chunk.chunk_id),
    )


def suppress_duplicates(
    fused: Sequence[FusedCandidate], policy: DedupPolicy
) -> tuple[tuple[FusedCandidate, ...], int]:
    """Cluster exact/near duplicates; keep one stable representative each.

    Exact fingerprint matches cluster regardless of article; shingle Jaccard
    matches cluster only across articles. The representative is the earliest
    published member (nulls last, then IDs) and carries the cluster's best
    fused score plus the suppressed chunk/article IDs.
    """
    clusters: list[list[tuple[FusedCandidate, str, frozenset[tuple[str, ...]]]]] = []
    for candidate in fused:
        fingerprint = dedup_fingerprint(candidate.text)
        shingles = _shingles(candidate.text, policy.shingle_size)
        target: list[tuple[FusedCandidate, str, frozenset[tuple[str, ...]]]] | None
        target = None
        for cluster in clusters:
            for member, member_fp, member_shingles in cluster:
                same = member_fp == fingerprint
                near = (
                    member.chunk.article_id != candidate.chunk.article_id
                    and jaccard(member_shingles, shingles)
                    >= policy.near_duplicate_threshold
                )
                if same or near:
                    target = cluster
                    break
            if target is not None:
                break
        if target is None:
            clusters.append([(candidate, fingerprint, shingles)])
        else:
            target.append((candidate, fingerprint, shingles))
    kept: list[FusedCandidate] = []
    suppressed = 0
    for cluster in clusters:
        members = [member for member, _, _ in cluster]
        representative = min(members, key=_representative_key)
        others = [m for m in members if m is not representative]
        suppressed += len(others)
        kept.append(
            replace(
                representative,
                fused_score=max(m.fused_score for m in members),
                duplicate_chunk_ids=tuple(
                    sorted((m.chunk.chunk_id for m in others), key=str)
                ),
                duplicate_article_ids=tuple(
                    sorted(
                        {
                            m.chunk.article_id
                            for m in others
                            if m.chunk.article_id != representative.chunk.article_id
                        },
                        key=str,
                    )
                ),
            )
        )
    return tuple(sorted(kept, key=fused_key)), suppressed


def finite_score(value: object) -> float | None:
    """Real finite number as float, else ``None`` (bool, overflow, hostile)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def validate_rerank_scores(  # noqa: PLR0911 - one early exit per invalid shape
    expected: Sequence[UUID], returned: object
) -> dict[UUID, float] | None:
    """Return scores only if they cover exactly the expected IDs once each.

    ``returned`` is untrusted: it must be a tuple/list of ``(UUID, number)``
    pairs. Any other shape, unknown/duplicate/missing ID or non-finite,
    non-numeric or unconvertible score makes the whole output invalid
    (``None``); the caller keeps fused order.
    """
    if not isinstance(returned, tuple | list):
        return None
    wanted = set(expected)
    scores: dict[UUID, float] = {}
    for row in cast("Sequence[object]", returned):
        if not isinstance(row, tuple | list):
            return None
        pair = cast("Sequence[object]", row)
        if len(pair) != 2:  # noqa: PLR2004 - (id, score)
            return None
        candidate_id, raw_score = pair[0], pair[1]
        if not isinstance(candidate_id, UUID) or candidate_id not in wanted:
            return None
        score = finite_score(raw_score)
        if candidate_id in scores or score is None:
            return None
        scores[candidate_id] = score
    if scores.keys() != wanted:
        return None
    return scores


def order_after_rerank(
    window: Sequence[FusedCandidate],
    tail: Sequence[FusedCandidate],
    scores: Mapping[UUID, float] | None,
) -> tuple[RankedCandidate, ...]:
    """Reranked window (or fused order) followed by the un-reranked tail."""
    if scores is None:
        ordered = list(window)
    else:
        ordered = sorted(
            window,
            key=lambda item: (-scores[item.chunk.chunk_id], *fused_key(item)),
        )
    ranked = [
        RankedCandidate(
            candidate=item,
            rerank_score=None if scores is None else scores[item.chunk.chunk_id],
            position=index + 1,
        )
        for index, item in enumerate(ordered)
    ]
    ranked.extend(
        RankedCandidate(candidate=item, rerank_score=None, position=len(ranked) + i)
        for i, item in enumerate(tail, start=1)
    )
    return tuple(ranked)


def aggregate_articles(
    ranked: Sequence[RankedCandidate], max_spans_per_article: int
) -> tuple[tuple[UUID, tuple[RankedCandidate, ...]], ...]:
    """Group by article in best-chunk order; keep at most N spans each."""
    groups: dict[UUID, list[RankedCandidate]] = {}
    for item in ranked:
        bucket = groups.setdefault(item.candidate.chunk.article_id, [])
        if len(bucket) < max_spans_per_article:
            bucket.append(item)
    return tuple((article_id, tuple(items)) for article_id, items in groups.items())
