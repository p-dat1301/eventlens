"""PostgreSQL article catalog (revision ``rag_0004``).

The only SQL of the catalog filter semantics in ``backend.rag.domain.catalog``.

Every statement reads current, non-tombstoned revisions only and counts
``article_id`` (never chunks). Each read is one short read-only
``REPEATABLE READ`` transaction, so every statement of one response sees the
same snapshot; cards are loaded for the exact ``(article_id, content_hash)``
the page/detail statement selected, never re-resolved to a newer revision.
"""

# ruff: noqa: S608, EM101 - SQL is built from reviewed constant fragments; values are bound.

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final, Literal, LiteralString, cast
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine

from backend.rag.application.ports.catalog import LabelSource, ScopeSnapshot
from backend.rag.domain.catalog import (
    SUMMARY_PREVIEW_CHARS,
    ArticleCard,
    ArticleDetail,
    ArticleGoneError,
    ArticleNotFoundError,
    ArticleSummaryView,
    CatalogFacets,
    CatalogFilters,
    CatalogRequestError,
    CatalogRow,
    CatalogSort,
    CatalogStorageError,
    CursorPosition,
    FacetCount,
    LabelProvenance,
    LocationEntry,
    LocationRef,
    MetadataStatus,
    SourceEntry,
    SourceRef,
    SummaryGenerator,
    SummaryKeyPoint,
    SummaryStatus,
    SupportingSpan,
    TopicEntry,
    TopicRef,
)
from backend.rag.domain.taxonomy import DerivedLabels, location_alias_key
from backend.rag.infra.postgres.connection import (
    StorageOperationError,
    StorageUnavailableError,
)
from backend.rag.infra.postgres.engine import execute, first, rows, transaction
from backend.rag.infra.retrieval.postgres_retrieval import VALID_METADATA
from backend.shared.contracts import ArticleEnrichment

CONTENT_POLICY: Final = "full"

# Shared filter CTEs: the gazetteer subtree of the selected locations and the
# filtered current revisions with their optional lexical rank.
FILTERED: LiteralString = """
WITH RECURSIVE wanted(location_id) AS (
    SELECT location_id FROM rag_locations
    WHERE location_id = ANY(%(locations)s::text[])
  UNION
    SELECT child.location_id FROM rag_locations child
    JOIN wanted ON child.parent_id = wanted.location_id
    WHERE %(descendants)s
), query AS MATERIALIZED (
    SELECT CASE WHEN %(q)s::text IS NULL THEN NULL
           ELSE websearch_to_tsquery('simple', %(q)s::text) END AS tsq
), base AS MATERIALIZED (
    SELECT d.article_id, d.content_hash, d.source_id, d.source_published_at,
           CASE WHEN query.tsq IS NULL THEN NULL
           ELSE ts_rank_cd(d.search_vector, query.tsq)::float8 END AS rank
    FROM rag_current_lexical d, query
    WHERE d.document_json IS NOT NULL
      AND (%(q)s::text IS NULL OR d.search_vector @@ query.tsq)
      AND (cardinality(%(sources)s::uuid[]) = 0
           OR d.source_id = ANY(%(sources)s::uuid[]))
      AND (%(after)s::timestamptz IS NULL
           OR d.source_published_at >= %(after)s::timestamptz)
      AND (%(before)s::timestamptz IS NULL
           OR d.source_published_at < %(before)s::timestamptz)
      AND (cardinality(%(topics)s::text[]) = 0 OR EXISTS (
           SELECT 1 FROM rag_current_labels l
           WHERE l.article_id = d.article_id AND l.content_hash = d.content_hash
             AND l.kind = 'topic' AND l.topic_code = ANY(%(topics)s::text[])))
      AND (cardinality(%(locations)s::text[]) = 0 OR EXISTS (
           SELECT 1 FROM rag_current_labels l JOIN wanted USING (location_id)
           WHERE l.article_id = d.article_id AND l.content_hash = d.content_hash
             AND l.kind = 'location'))
)"""

_NEWER: LiteralString = """(
    CASE WHEN %(c_null)s THEN b.source_published_at IS NULL
                              AND b.article_id > %(c_id)s::uuid
    ELSE b.source_published_at IS NULL
         OR b.source_published_at < %(c_pub)s::timestamptz
         OR (b.source_published_at = %(c_pub)s::timestamptz
             AND b.article_id > %(c_id)s::uuid) END)"""
_OLDER: LiteralString = """(
    CASE WHEN %(c_null)s THEN b.source_published_at IS NULL
                              AND b.article_id > %(c_id)s::uuid
    ELSE b.source_published_at IS NULL
         OR b.source_published_at > %(c_pub)s::timestamptz
         OR (b.source_published_at = %(c_pub)s::timestamptz
             AND b.article_id > %(c_id)s::uuid) END)"""
_RANKED: LiteralString = (
    "(b.rank < %(c_rank)s::float8 OR (b.rank = %(c_rank)s::float8 AND " + _NEWER + "))"
)
_LATEST_ORDER: LiteralString = (
    "(b.source_published_at IS NULL), b.source_published_at DESC, b.article_id"
)
_OLDEST_ORDER: LiteralString = (
    "(b.source_published_at IS NULL), b.source_published_at ASC, b.article_id"
)


def _page_sql(after: LiteralString, order: LiteralString) -> LiteralString:
    return (
        FILTERED
        + """
SELECT b.article_id, b.content_hash, b.rank FROM base b
WHERE NOT %(cursor)s OR """
        + after
        + """
ORDER BY """
        + order
        + """
LIMIT %(limit)s"""
    )


PAGE_SQL: Final[Mapping[CatalogSort, LiteralString]] = {
    CatalogSort.LATEST: _page_sql(_NEWER, _LATEST_ORDER),
    CatalogSort.OLDEST: _page_sql(_OLDER, _OLDEST_ORDER),
    CatalogSort.RELEVANCE: _page_sql(_RANKED, "b.rank DESC, " + _LATEST_ORDER),
}

# Card fields of selected current revisions: allowlisted document fields,
# label sync / enrichment / summary state and the upstream version id.
CARDS_SQL: LiteralString = """
SELECT cur.article_id, cur.content_hash, d.title,
       d.document_json->>'canonical_url', d.source_id,
       d.document_json->>'source_name', d.document_json->>'source_domain',
       d.source_published_at,
       sync.outcome,
       (SELECT j.status FROM rag_enrichment_jobs j
        WHERE j.article_id = cur.article_id AND j.content_hash = cur.content_hash
        ORDER BY j.updated_at DESC, j.job_id LIMIT 1),
       work.status, work.failure_code,
       summary.summary_text,
       (SELECT v.article_version_id FROM rag_source_versions v
        WHERE v.article_id = cur.article_id AND v.content_hash = cur.content_hash
        ORDER BY v.recorded_at DESC, v.article_version_id LIMIT 1)
FROM rag_article_current cur
JOIN rag_article_documents d
    ON d.article_id = cur.article_id AND d.content_hash = cur.content_hash
LEFT JOIN rag_label_syncs sync
    ON sync.article_id = cur.article_id AND sync.content_hash = cur.content_hash
LEFT JOIN rag_work_jobs work
    ON work.kind = 'summary' AND work.subject_id = cur.article_id
    AND work.revision_key = cur.content_hash
LEFT JOIN LATERAL (
    SELECT s.summary_text FROM rag_article_summaries s
    WHERE s.article_id = cur.article_id AND s.content_hash = cur.content_hash
    ORDER BY s.created_at DESC, s.summary_id LIMIT 1) summary ON true
WHERE (cur.article_id, cur.content_hash) IN (
        SELECT * FROM unnest(%(ids)s::uuid[], %(hashes)s::text[]))
  AND cur.tombstoned_at IS NULL AND d.document_json IS NOT NULL
"""

LABELS_SQL: LiteralString = """
SELECT DISTINCT l.article_id, l.kind, coalesce(l.topic_code, l.location_id),
       coalesce(t.name_vi, loc.canonical_name), loc.location_type, t.sort_order
FROM rag_current_labels l
LEFT JOIN rag_topics t ON t.topic_code = l.topic_code
LEFT JOIN rag_locations loc ON loc.location_id = l.location_id
WHERE (l.article_id, l.content_hash) IN (
        SELECT * FROM unnest(%(ids)s::uuid[], %(hashes)s::text[]))
ORDER BY l.article_id, l.kind, t.sort_order, 3
"""

UNKNOWN_SQL: LiteralString = """
SELECT
  ARRAY(SELECT t FROM unnest(%(topics)s::text[]) t
        WHERE t NOT IN (SELECT topic_code FROM rag_topics)),
  ARRAY(SELECT l FROM unnest(%(locations)s::text[]) l
        WHERE l NOT IN (SELECT location_id FROM rag_locations))
"""

FACETS_SQL: LiteralString = (
    FILTERED
    + """, topic_labels AS (
    SELECT DISTINCT b.article_id, l.topic_code FROM base b
    JOIN rag_current_labels l ON l.article_id = b.article_id
        AND l.content_hash = b.content_hash AND l.kind = 'topic'
), location_labels AS (
    SELECT DISTINCT b.article_id, l.location_id FROM base b
    JOIN rag_current_labels l ON l.article_id = b.article_id
        AND l.content_hash = b.content_hash AND l.kind = 'location'
), ancestors(article_id, location_id) AS (
    SELECT article_id, location_id FROM location_labels
  UNION
    SELECT a.article_id, loc.parent_id FROM ancestors a
    JOIN rag_locations loc ON loc.location_id = a.location_id
    WHERE loc.parent_id IS NOT NULL
)
SELECT 'total', NULL::text, count(*), NULL::text, NULL::text FROM base
UNION ALL
SELECT 'topic', tl.topic_code, count(DISTINCT tl.article_id), t.name_vi, NULL
FROM topic_labels tl JOIN rag_topics t USING (topic_code)
GROUP BY tl.topic_code, t.name_vi, t.sort_order
UNION ALL
SELECT 'topic_unclassified', NULL, count(*), NULL, NULL FROM base b
WHERE NOT EXISTS (SELECT 1 FROM topic_labels tl WHERE tl.article_id = b.article_id)
UNION ALL
SELECT 'location', a.location_id, count(DISTINCT a.article_id),
       loc.canonical_name, loc.location_type
FROM ancestors a JOIN rag_locations loc USING (location_id)
GROUP BY a.location_id, loc.canonical_name, loc.location_type
UNION ALL
SELECT 'location_unknown', NULL, count(*), NULL, NULL FROM base b
WHERE NOT EXISTS (
    SELECT 1 FROM location_labels ll WHERE ll.article_id = b.article_id)
UNION ALL
SELECT 'source', b.source_id::text, count(*),
       min(d.document_json->>'source_name'), min(d.document_json->>'source_domain')
FROM base b JOIN rag_article_documents d
    ON d.article_id = b.article_id AND d.content_hash = b.content_hash
GROUP BY b.source_id
"""
)

SCOPE_SQL: LiteralString = (
    FILTERED
    + """
SELECT b.article_id, b.content_hash, count(*) OVER () FROM base b
ORDER BY b.article_id LIMIT %(limit)s"""
)

DETAIL_SQL: LiteralString = """
SELECT cur.tombstoned_at, cur.content_hash, d.normalized_content,
       d.document_json->>'ingested_at',
       (SELECT count(*) FROM rag_article_documents r
        WHERE r.article_id = cur.article_id),
       (SELECT v.updated_at_source FROM rag_source_versions v
        WHERE v.article_id = cur.article_id AND v.content_hash = cur.content_hash
        ORDER BY v.recorded_at DESC LIMIT 1),
       (SELECT v.language FROM rag_source_versions v
        WHERE v.article_id = cur.article_id AND v.content_hash = cur.content_hash
        ORDER BY v.recorded_at DESC LIMIT 1)
FROM rag_article_current cur
JOIN rag_article_documents d
    ON d.article_id = cur.article_id AND d.content_hash = cur.content_hash
WHERE cur.article_id = %(id)s::uuid
"""

PROVENANCE_SQL: LiteralString = """
SELECT l.kind, coalesce(l.topic_code, l.location_id), l.provenance,
       l.match_method, l.model_version, l.confidence, l.review_status
FROM rag_article_labels l
JOIN rag_article_current cur
    ON cur.article_id = l.article_id AND cur.content_hash = l.content_hash
WHERE l.article_id = %(id)s::uuid
ORDER BY l.kind, 2, l.provenance
"""

SUMMARY_SQL: LiteralString = """
SELECT cur.content_hash, cur.tombstoned_at, work.status, work.failure_code,
       s.summary_text, s.key_points::text, s.model_id, s.model_version,
       s.prompt_version, s.schema_version, s.summary_policy_id, s.created_at
FROM rag_article_current cur
LEFT JOIN rag_work_jobs work
    ON work.kind = 'summary' AND work.subject_id = cur.article_id
    AND work.revision_key = cur.content_hash
LEFT JOIN LATERAL (
    SELECT * FROM rag_article_summaries x
    WHERE x.article_id = cur.article_id AND x.content_hash = cur.content_hash
    ORDER BY x.created_at DESC, x.summary_id LIMIT 1) s ON true
WHERE cur.article_id = %(id)s::uuid
"""

LOCATIONS_SQL: LiteralString = """
SELECT loc.location_id, loc.canonical_name, loc.location_type, loc.parent_id,
       loc.valid_until::text, loc.gazetteer_version,
       ARRAY(SELECT a.alias FROM rag_location_aliases a
             WHERE a.location_id = loc.location_id ORDER BY a.alias)
FROM rag_locations loc
WHERE (%(parent)s::text IS NULL OR loc.parent_id = %(parent)s::text)
  AND (%(prefix)s::text IS NULL OR EXISTS (
       SELECT 1 FROM rag_location_aliases a
       WHERE a.location_id = loc.location_id
         AND (a.alias_key = %(prefix)s::text
              OR a.alias_key LIKE %(prefix)s::text || '%%'
              OR a.alias_key LIKE '%% ' || %(prefix)s::text || '%%')))
ORDER BY (loc.location_type = 'former_province'), loc.canonical_name,
         loc.location_id
LIMIT %(limit)s
"""

SOURCES_SQL: LiteralString = """
SELECT d.source_id, min(d.document_json->>'source_name'),
       min(d.document_json->>'source_domain'), count(*)
FROM rag_current_lexical d
WHERE d.document_json IS NOT NULL
GROUP BY d.source_id ORDER BY 2, 1
"""

VERSIONS_SQL: LiteralString = """
SELECT article_id, content_hash FROM rag_article_current
WHERE article_id = ANY(%(ids)s::uuid[]) AND tombstoned_at IS NULL
"""

_PAGE_ROWS = TypeAdapter(list[tuple[UUID, str, float | None]])
_CARD_ROWS = TypeAdapter(
    list[
        tuple[
            UUID,
            str,
            str,
            str,
            UUID,
            str,
            str,
            datetime | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            UUID | None,
        ]
    ]
)
_LABEL_ROWS = TypeAdapter(list[tuple[UUID, str, str, str, str | None, int | None]])
_FACET_ROWS = TypeAdapter(list[tuple[str, str | None, int, str | None, str | None]])
_SCOPE_ROWS = TypeAdapter(list[tuple[UUID, str, int]])
_DETAIL_ROW = TypeAdapter(
    tuple[datetime | None, str, str, str | None, int, datetime | None, str | None]
)
_PROVENANCE_ROWS = TypeAdapter(
    list[tuple[str, str, str, str, str | None, float | None, str]]
)
_SUMMARY_ROW = TypeAdapter(
    tuple[
        str,
        datetime | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        datetime | None,
    ]
)
_KEY_POINTS = TypeAdapter(list[dict[str, object]])
_TOPIC_ROWS = TypeAdapter(list[tuple[str, str, str, str]])
_LOCATION_ROWS = TypeAdapter(
    list[tuple[str, str, str, str | None, str | None, str, list[str]]]
)
_SOURCE_ROWS = TypeAdapter(list[tuple[UUID, str, str, int]])
_VERSION_ROWS = TypeAdapter(list[tuple[UUID, str]])
_NOW = TypeAdapter(datetime)


def filter_params(filters: CatalogFilters) -> dict[str, object]:
    """Bind the shared filter CTE."""
    return {
        "q": filters.q,
        "topics": list(filters.topic_ids),
        "locations": list(filters.location_ids),
        "descendants": filters.include_descendants,
        "sources": list(filters.source_ids),
        "after": filters.published_after,
        "before": filters.published_before,
    }


def metadata_status(sync: str | None, job: str | None) -> MetadataStatus:
    """Label state: sync outcome first, then the enrichment job state."""
    if sync == "applied":
        return MetadataStatus.READY
    if sync == "untrusted_model":
        return MetadataStatus.UNAVAILABLE
    if sync == "invalid_metadata" or job == "FAILED":
        return MetadataStatus.FAILED
    return MetadataStatus.PENDING


def summary_status(
    has_summary: bool,  # noqa: FBT001
    job: str | None,
) -> SummaryStatus:
    """AI summary state of the current revision."""
    if has_summary:
        return SummaryStatus.READY
    if job in {"PENDING", "PROCESSING"}:
        return SummaryStatus.PENDING
    if job == "FAILED":
        return SummaryStatus.FAILED
    return SummaryStatus.UNAVAILABLE


def _preview(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= SUMMARY_PREVIEW_CHARS:
        return text
    return text[: SUMMARY_PREVIEW_CHARS - 1].rstrip() + "…"


class PostgresArticleCatalog:
    """``ArticleCatalogStore`` over the RAG schema."""

    def __init__(self, engine: Engine, content_policy: str = CONTENT_POLICY) -> None:
        """Bind the shared engine."""
        self._engine: Engine = engine
        # Every statement of one response sees the same snapshot, so a
        # concurrent correction can never mix two revisions in one body.
        self._snapshot: Engine = engine.execution_options(
            isolation_level="REPEATABLE READ"
        )
        self._content_full: bool = content_policy == "full"

    def _read[T](self, work: Callable[[Connection], T]) -> T:
        """One read-only REPEATABLE READ transaction: one snapshot per response."""
        try:
            with transaction(self._snapshot, read_only=True) as connection:
                return work(connection)
        except (StorageUnavailableError, StorageOperationError, ValidationError):
            raise CatalogStorageError from None

    @staticmethod
    def _check_known(connection: Connection, filters: CatalogFilters) -> None:
        if not filters.topic_ids and not filters.location_ids:
            return
        row = first(
            execute(
                connection,
                UNKNOWN_SQL,
                {
                    "topics": list(filters.topic_ids),
                    "locations": list(filters.location_ids),
                },
            )
        )
        unknown_topics, unknown_locations = TypeAdapter(
            tuple[list[str], list[str]]
        ).validate_python(row)
        if unknown_topics:
            raise CatalogRequestError("unknown_topic_id")
        if unknown_locations:
            raise CatalogRequestError("unknown_location_id")

    @staticmethod
    def _now(connection: Connection) -> datetime:
        row = first(execute(connection, "SELECT now()"))
        return _NOW.validate_python(None if row is None else row[0])

    def list_page(
        self,
        filters: CatalogFilters,
        sort: CatalogSort,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[tuple[CatalogRow, ...], datetime]:
        """Keyset page of current articles plus the snapshot time."""

        def work(connection: Connection) -> tuple[tuple[CatalogRow, ...], datetime]:
            self._check_known(connection, filters)
            as_of = self._now(connection)
            if filters.empty_range:
                return (), as_of
            params = filter_params(filters) | {
                "cursor": after is not None,
                "c_null": after is not None and after.published_at is None,
                "c_pub": None if after is None else after.published_at,
                "c_id": None if after is None else after.article_id,
                "c_rank": None if after is None else after.rank,
                "limit": limit,
            }
            page = _PAGE_ROWS.validate_python(
                rows(execute(connection, PAGE_SQL[sort], params))
            )
            cards = self._cards(connection, tuple((a, h) for a, h, _ in page))
            return (
                tuple(
                    CatalogRow(cards[article_id], rank)
                    for article_id, _, rank in page
                    if article_id in cards
                ),
                as_of,
            )

        return self._read(work)

    def _cards(
        self, connection: Connection, revisions: tuple[tuple[UUID, str], ...]
    ) -> dict[UUID, ArticleCard]:
        """Cards of exactly the selected revisions (same snapshot)."""
        if not revisions:
            return {}
        params = {
            "ids": [a for a, _ in revisions],
            "hashes": [h for _, h in revisions],
        }
        found = _CARD_ROWS.validate_python(rows(execute(connection, CARDS_SQL, params)))
        labels = _LABEL_ROWS.validate_python(
            rows(execute(connection, LABELS_SQL, params))
        )
        topics: dict[UUID, list[TopicRef]] = {}
        locations: dict[UUID, list[LocationRef]] = {}
        for article_id, kind, label_id, name, location_type, _ in labels:
            if kind == "topic":
                topics.setdefault(article_id, []).append(TopicRef(label_id, name))
            else:
                locations.setdefault(article_id, []).append(
                    LocationRef(label_id, name, location_type or "unknown")
                )
        cards: dict[UUID, ArticleCard] = {}
        for (
            article_id,
            content_hash,
            title,
            url,
            source_id,
            source_name,
            source_domain,
            published,
            sync,
            enrichment_job,
            summary_job,
            _summary_failure,
            summary_text,
            upstream,
        ) in found:
            cards[article_id] = ArticleCard(
                article_id=article_id,
                article_version_id=content_hash,
                upstream_version_id=upstream,
                title=title,
                source=SourceRef(source_id, source_name, source_domain),
                canonical_url=url,
                published_at=published,
                topics=tuple(topics.get(article_id, ())),
                locations=tuple(locations.get(article_id, ())),
                summary_preview=_preview(summary_text),
                metadata_status=metadata_status(sync, enrichment_job),
                summary_status=summary_status(summary_text is not None, summary_job),
            )
        return cards

    def article(self, article_id: UUID) -> ArticleDetail:
        """Current revision detail; tombstones and unknown IDs are errors."""

        def work(connection: Connection) -> ArticleDetail:
            row = first(execute(connection, DETAIL_SQL, {"id": article_id}))
            if row is None:
                raise ArticleNotFoundError
            tombstoned, revision, content, ingested, revisions, updated, language = (
                _DETAIL_ROW.validate_python(row)
            )
            if tombstoned is not None:
                raise ArticleGoneError
            card = self._cards(connection, ((article_id, revision),)).get(article_id)
            if card is None:
                raise ArticleNotFoundError
            provenance = _PROVENANCE_ROWS.validate_python(
                rows(execute(connection, PROVENANCE_SQL, {"id": article_id}))
            )
            return ArticleDetail(
                card=card,
                content=content if self._content_full else None,
                content_policy="full" if self._content_full else "withheld",
                ingested_at=None
                if ingested is None
                else datetime.fromisoformat(ingested),
                updated_at_source=updated,
                language=language,
                revision_count=revisions,
                labels=tuple(
                    LabelProvenance(
                        kind=cast("Literal['topic', 'location']", kind),
                        label_id=label_id,
                        provenance=source,
                        match_method=method,
                        model_version=model,
                        confidence=confidence,
                        review_status=review,
                    )
                    for kind, label_id, source, method, model, confidence, review in (
                        provenance
                    )
                ),
            )

        return self._read(work)

    def summary(self, article_id: UUID) -> ArticleSummaryView:
        """Latest published summary of the current revision, or its job state."""

        def work(connection: Connection) -> ArticleSummaryView:
            row = first(execute(connection, SUMMARY_SQL, {"id": article_id}))
            if row is None:
                raise ArticleNotFoundError
            (
                content_hash,
                tombstoned,
                job,
                failure,
                text,
                points,
                model_id,
                model_version,
                prompt_version,
                schema_version,
                policy_id,
                created,
            ) = _SUMMARY_ROW.validate_python(row)
            if tombstoned is not None:
                raise ArticleGoneError
            status = summary_status(text is not None, job)
            ready = status is SummaryStatus.READY
            return ArticleSummaryView(
                article_id=article_id,
                article_version_id=content_hash,
                status=status,
                source="ai",
                text=text,
                key_points=_key_points(points) if ready else (),
                generator=SummaryGenerator(
                    model_id or "",
                    model_version or "",
                    prompt_version or "",
                    schema_version or "",
                    policy_id or "",
                )
                if ready
                else None,
                failure_code=failure if status is SummaryStatus.FAILED else None,
                created_at=created if ready else None,
                publisher_summary=None,
            )

        return self._read(work)

    def facets(self, filters: CatalogFilters) -> CatalogFacets:
        """One-statement facet counts under every filter."""

        def work(connection: Connection) -> CatalogFacets:
            self._check_known(connection, filters)
            as_of = self._now(connection)
            if filters.empty_range:
                return CatalogFacets(0, (), 0, (), 0, (), as_of)
            found = _FACET_ROWS.validate_python(
                rows(execute(connection, FACETS_SQL, filter_params(filters)))
            )
            groups: dict[str, list[FacetCount]] = {}
            totals: dict[str, int] = {}
            for kind, facet_id, count, name, extra in found:
                if facet_id is None:
                    totals[kind] = count
                    continue
                groups.setdefault(kind, []).append(
                    FacetCount(
                        facet_id,
                        name or facet_id,
                        count,
                        location_type=extra if kind == "location" else None,
                        domain=extra if kind == "source" else None,
                    )
                )

            def ordered(kind: str) -> tuple[FacetCount, ...]:
                return tuple(
                    sorted(groups.get(kind, ()), key=lambda f: (-f.count, f.facet_id))
                )

            return CatalogFacets(
                total=totals.get("total", 0),
                topics=ordered("topic"),
                topics_unclassified=totals.get("topic_unclassified", 0),
                locations=ordered("location"),
                locations_unknown=totals.get("location_unknown", 0),
                sources=ordered("source"),
                as_of=as_of,
            )

        return self._read(work)

    def topics(self) -> tuple[TopicEntry, ...]:
        """Topics in display order."""

        def work(connection: Connection) -> tuple[TopicEntry, ...]:
            found = _TOPIC_ROWS.validate_python(
                rows(
                    execute(
                        connection,
                        """SELECT topic_code, name_vi, name_en, taxonomy_version
                        FROM rag_topics ORDER BY sort_order""",
                    )
                )
            )
            return tuple(TopicEntry(*row) for row in found)

        return self._read(work)

    def locations(
        self, parent_id: str | None, query: str | None, limit: int
    ) -> tuple[LocationEntry, ...]:
        """Gazetteer units by parent and/or alias-word prefix."""
        prefix = None if query is None else location_alias_key(query) or None

        def work(connection: Connection) -> tuple[LocationEntry, ...]:
            found = _LOCATION_ROWS.validate_python(
                rows(
                    execute(
                        connection,
                        LOCATIONS_SQL,
                        {"parent": parent_id, "prefix": prefix, "limit": limit},
                    )
                )
            )
            return tuple(
                LocationEntry(lid, name, kind, parent, until, tuple(aliases), version)
                for lid, name, kind, parent, until, version, aliases in found
            )

        return self._read(work)

    def sources(self) -> tuple[SourceEntry, ...]:
        """Publishers of current articles."""

        def work(connection: Connection) -> tuple[SourceEntry, ...]:
            found = _SOURCE_ROWS.validate_python(rows(execute(connection, SOURCES_SQL)))
            return tuple(SourceEntry(*row) for row in found)

        return self._read(work)

    def resolve_scope(self, filters: CatalogFilters, cap: int) -> ScopeSnapshot:
        """Every matching current revision (``cap`` + 1 detects overflow)."""

        def work(connection: Connection) -> ScopeSnapshot:
            self._check_known(connection, filters)
            as_of = self._now(connection)
            if filters.empty_range:
                return ScopeSnapshot({}, 0, as_of)
            params = filter_params(filters) | {"limit": cap + 1}
            found = _SCOPE_ROWS.validate_python(
                rows(execute(connection, SCOPE_SQL, params))
            )
            return ScopeSnapshot(
                {a: h for a, h, _ in found}, found[0][2] if found else 0, as_of
            )

        return self._read(work)

    def current_versions(self, article_ids: tuple[UUID, ...]) -> dict[UUID, str]:
        """Current revision hash of each non-tombstoned article."""

        def work(connection: Connection) -> dict[UUID, str]:
            found = _VERSION_ROWS.validate_python(
                rows(execute(connection, VERSIONS_SQL, {"ids": list(article_ids)}))
            )
            return dict(found)

        return self._read(work)


def _key_points(text: str | None) -> tuple[SummaryKeyPoint, ...]:
    if text is None:
        return ()
    points: list[SummaryKeyPoint] = []
    for item in _KEY_POINTS.validate_json(text):
        spans = TypeAdapter(list[dict[str, object]]).validate_python(item["spans"])
        points.append(
            SummaryKeyPoint(
                text=str(item["text"]),
                spans=tuple(
                    SupportingSpan(
                        start_offset=int(str(s["start_offset"])),
                        end_offset=int(str(s["end_offset"])),
                        excerpt=str(s["excerpt"]),
                        span_fingerprint=str(s["span_fingerprint"]),
                    )
                    for s in spans
                ),
            )
        )
    return tuple(points)


# --- label projection ------------------------------------------------------

PENDING_LABELS_SQL: LiteralString = (
    """
WITH valid AS (
"""
    + VALID_METADATA
    + """
), single AS (
    SELECT v.article_id, count(*) AS publications,
           (array_agg(v.metadata::text))[1] AS metadata
    FROM valid v GROUP BY v.article_id
), candidates AS (
    SELECT cur.article_id, cur.content_hash, single.publications, single.metadata,
           (SELECT p.publication_id FROM rag_enrichment_publications p
            JOIN rag_enrichment_jobs job ON job.job_id = p.job_id
                AND job.claim_generation = p.claim_generation
                AND job.status = 'READY'
            WHERE p.article_id = cur.article_id
              AND p.content_hash = cur.content_hash
              AND p.index_generation = cur.active_index_generation
            ORDER BY p.publication_id DESC LIMIT 1) AS publication_id
    FROM rag_article_current cur
    JOIN single ON single.article_id = cur.article_id
    WHERE cur.tombstoned_at IS NULL
)
-- (Re)sync when never synced, when the publication changed, or when the
-- trust decision recorded for its model no longer matches the policy.
SELECT c.article_id, c.content_hash, c.publications, c.metadata, c.publication_id
FROM candidates c
LEFT JOIN rag_label_syncs s
    ON s.article_id = c.article_id AND s.content_hash = c.content_hash
WHERE s.article_id IS NULL
   OR s.publication_id IS DISTINCT FROM c.publication_id
   OR (s.outcome = 'untrusted_model'
       AND s.model_version = ANY(%(trusted)s::text[]))
   OR (s.outcome = 'applied'
       AND NOT coalesce(s.model_version = ANY(%(trusted)s::text[]), false))
ORDER BY c.article_id
LIMIT %(limit)s
"""
)
_PENDING_ROWS = TypeAdapter(list[tuple[UUID, str, int, str | None, int | None]])

LOCK_CURRENT_SQL: LiteralString = """
SELECT 1 FROM rag_article_current
WHERE article_id = %(id)s::uuid AND content_hash = %(hash)s
  AND tombstoned_at IS NULL
FOR SHARE
"""


class PostgresArticleLabels:
    """``ArticleLabelStore``: replaces model labels of a still-current revision."""

    def __init__(self, engine: Engine) -> None:
        """Bind the shared engine."""
        self._engine: Engine = engine

    def pending(self, limit: int, trusted: frozenset[str]) -> tuple[LabelSource, ...]:
        """Revisions never synced or whose publication/trust decision changed."""
        params = {"limit": limit, "trusted": sorted(trusted)}
        with transaction(self._engine, read_only=True) as connection:
            found = _PENDING_ROWS.validate_python(
                rows(execute(connection, PENDING_LABELS_SQL, params))
            )
        sources: list[LabelSource] = []
        for article_id, content_hash, publications, text, publication_id in found:
            metadata: ArticleEnrichment | None = None
            invalid = publications != 1 or text is None or publication_id is None
            if not invalid and text is not None:
                try:
                    metadata = ArticleEnrichment.model_validate_json(text)
                except ValidationError:
                    invalid = True
            if metadata is not None and (
                metadata.article_id != article_id
                or metadata.content_hash != content_hash
            ):
                metadata, invalid = None, True
            sources.append(
                LabelSource(article_id, content_hash, publication_id, metadata, invalid)
            )
        return tuple(sources)

    def alias_index(self) -> dict[str, str]:
        """Alias key -> location ID."""
        with transaction(self._engine, read_only=True) as connection:
            found = TypeAdapter(list[tuple[str, str]]).validate_python(
                rows(
                    execute(
                        connection,
                        "SELECT alias_key, location_id FROM rag_location_aliases",
                    )
                )
            )
        return dict(found)

    def apply(
        self,
        source: LabelSource,
        labels: DerivedLabels | None,
        outcome: str,
    ) -> bool:
        """Fenced replace: only while the revision is current; manual rows stay."""
        with transaction(self._engine) as connection:
            locked = first(
                execute(
                    connection,
                    LOCK_CURRENT_SQL,
                    {"id": source.article_id, "hash": source.content_hash},
                )
            )
            if locked is None:
                return False
            _ = execute(
                connection,
                """DELETE FROM rag_article_labels
                WHERE article_id = %(id)s::uuid AND content_hash = %(hash)s
                  AND provenance = 'enrichment'""",
                {"id": source.article_id, "hash": source.content_hash},
            )
            items = () if labels is None else labels.labels
            model = None if source.metadata is None else source.metadata.model_version
            for label in items:
                _ = execute(
                    connection,
                    """INSERT INTO rag_article_labels (article_id, content_hash,
                        kind, topic_code, location_id, provenance, match_method,
                        publication_id, model_version, confidence,
                        evidence_span_ids)
                    VALUES (%(id)s::uuid, %(hash)s, %(kind)s, %(topic)s,
                        %(location)s, 'enrichment', %(method)s, %(publication)s,
                        %(model)s, %(confidence)s, %(spans)s::uuid[])""",
                    {
                        "id": source.article_id,
                        "hash": source.content_hash,
                        "kind": label.kind,
                        "topic": label.label_id if label.kind == "topic" else None,
                        "location": label.label_id
                        if label.kind == "location"
                        else None,
                        "method": label.match_method,
                        "publication": source.publication_id,
                        "model": model,
                        "confidence": label.confidence,
                        "spans": list(label.evidence_span_ids),
                    },
                )
            _ = execute(
                connection,
                """INSERT INTO rag_label_syncs (article_id, content_hash, outcome,
                    publication_id, model_version, topic_labels, location_labels,
                    unmatched_locations)
                VALUES (%(id)s::uuid, %(hash)s, %(outcome)s, %(publication)s,
                    %(model)s, %(topics)s, %(locations)s, %(unmatched)s)
                ON CONFLICT (article_id, content_hash) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    publication_id = EXCLUDED.publication_id,
                    model_version = EXCLUDED.model_version,
                    topic_labels = EXCLUDED.topic_labels,
                    location_labels = EXCLUDED.location_labels,
                    unmatched_locations = EXCLUDED.unmatched_locations,
                    synced_at = clock_timestamp()""",
                {
                    "id": source.article_id,
                    "hash": source.content_hash,
                    "outcome": outcome,
                    "publication": source.publication_id,
                    "model": model,
                    "topics": sum(1 for x in items if x.kind == "topic"),
                    "locations": sum(1 for x in items if x.kind == "location"),
                    "unmatched": 0 if labels is None else labels.unmatched_locations,
                },
            )
        return True
