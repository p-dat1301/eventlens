"""Catalog, labels and per-revision summaries on real PostgreSQL (rag_0004).

Each test gets its own migrated schema (``retrieval_url``, fresh or adopted
path). Labels are written through the label projection or as manual rows;
summaries use the deterministic fixture/scripted summarizers (no provider).
"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from typer.testing import CliRunner

from backend.rag.application.ports.summary import ArticleSummaryModelPort
from backend.rag.application.use_cases.catalog import CatalogQueries
from backend.rag.application.use_cases.labels import SyncArticleLabels
from backend.rag.application.use_cases.summarize import SummarySteps
from backend.rag.cli_main import app as cli
from backend.rag.composition import build_runtime, json_lines_telemetry
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.catalog import (
    ArticleGoneError,
    ArticleVersionStaleError,
    CatalogFilters,
    CatalogRequestError,
    CatalogSort,
    CatalogStorageError,
    CursorCodec,
    MetadataStatus,
    SummaryStatus,
    catalog_filters,
)
from backend.rag.domain.work_jobs import JobKind
from backend.rag.infra.llm.summarizers import FixtureSummarizer
from backend.rag.infra.postgres.article_catalog import (
    PostgresArticleCatalog,
    PostgresArticleLabels,
)
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.postgres.summaries import PostgresSummaries
from backend.rag.infra.postgres.work_queue import PostgresWorkQueue, requeue_summary
from backend.rag.workflows.summarization.workflow import SummaryGraph, SummaryRunner
from news_support import ScriptedSummarizer, draft_quoting

EXPIRE_LEASES = "UPDATE rag_work_jobs SET lease_expires_at = now() - interval '1 s'"
T0 = datetime(2026, 10, 28, 17, tzinfo=UTC)  # 2026-10-29 00:00 Asia/Ho_Chi_Minh
SOURCE_A = UUID("11111111-1111-4111-8111-111111111111")
SOURCE_B = UUID("22222222-2222-4222-8222-222222222222")


def doc(
    content: str,
    *,
    published: datetime | None = T0,
    source: UUID = SOURCE_A,
    article_id: UUID | None = None,
    title: str = "Tin thử nghiệm",
) -> ArticleDocument:
    digest = f"sha256:{sha256(content.encode()).hexdigest()}"
    return ArticleDocument.model_validate_json(
        json.dumps(
            {
                "schema_version": "article_document.v1",
                "article_id": str(article_id or uuid4()),
                "source_id": str(source),
                "title": title,
                "raw_content": content,
                "normalization_version": "nfc.v1",
                "normalized_content": content,
                "canonical_url": f"https://news.example/{uuid4().hex[:8]}",
                "source_name": "Báo A" if source == SOURCE_A else "Báo B",
                "source_domain": "a.example" if source == SOURCE_A else "b.example",
                "source_published_at": None
                if published is None
                else published.isoformat(),
                "ingested_at": "2026-10-29T00:00:00+00:00",
                "processed_at": "2026-10-29T00:00:00+00:00",
                "content_hash": digest,
                "content_fingerprint": digest,
                "visibility": "internal",
                "evidence_spans": [],
            }
        )
    )


def persist(url: str, *documents: ArticleDocument) -> None:
    store = PostgresRagStore(rag_engine(url))
    for document in documents:
        store.persist_article(document)


def label(
    url: str, document: ArticleDocument, kind: str, value: str, review: str = "approved"
) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(
            """INSERT INTO rag_article_labels (article_id, content_hash, kind,
                topic_code, location_id, provenance, match_method, review_status)
            VALUES (%s, %s, %s, %s, %s, 'manual', 'manual', %s)""",
            (
                document.article_id,
                document.content_hash,
                kind,
                value if kind == "topic" else None,
                value if kind == "location" else None,
                review,
            ),
        )


def queries(url: str) -> CatalogQueries:
    return CatalogQueries(
        PostgresArticleCatalog(rag_engine(url)), CursorCodec(b"c" * 32)
    )


def ids(url: str, filters: CatalogFilters, sort: str | None = None) -> list[UUID]:
    found: list[UUID] = []
    cursor: str | None = None
    while True:
        page = queries(url).list_articles(filters, limit=2, cursor=cursor, sort=sort)
        found += [card.article_id for card in page.items]
        if not page.has_more:
            return found
        cursor = page.next_cursor


def test_one_article_with_many_chunks_is_listed_and_counted_once(
    retrieval_url: str,
) -> None:
    long_text = " ".join(f"Đoạn {i} nói về giao thông TP.HCM." for i in range(400))
    articles = [doc(long_text), doc("Tin ngắn về kinh tế."), doc("Tin thứ ba.")]
    persist(retrieval_url, *articles)
    result = CliRunner().invoke(cli, ["rebuild", "--database-url", retrieval_url])
    assert result.exit_code == 0, result.output
    with psycopg.connect(retrieval_url) as connection:
        row = connection.execute(
            "SELECT count(*) FROM rag_chunks WHERE article_id = %s",
            (articles[0].article_id,),
        ).fetchone()
    assert row is not None
    assert row[0] > 3

    listed = ids(retrieval_url, catalog_filters())
    facets = queries(retrieval_url).facets(catalog_filters())
    search = ids(retrieval_url, catalog_filters(q="giao thông"))

    assert sorted(listed, key=str) == sorted((a.article_id for a in articles), key=str)
    assert facets.total == 3
    assert sum(f.count for f in facets.sources) == 3
    assert search == [articles[0].article_id]


def test_or_within_a_kind_and_across_kinds(retrieval_url: str) -> None:
    traffic_hcm, economy_hn, traffic_hn = doc("a"), doc("b"), doc("c")
    persist(retrieval_url, traffic_hcm, economy_hn, traffic_hn)
    for document, topic, location in (
        (traffic_hcm, "transportation", "vn-ho-chi-minh"),
        (economy_hn, "economy", "vn-ha-noi"),
        (traffic_hn, "transportation", "vn-ha-noi"),
    ):
        label(retrieval_url, document, "topic", topic)
        label(retrieval_url, document, "location", location)

    either = set(
        ids(retrieval_url, catalog_filters(topic_ids=("transportation", "economy")))
    )
    both = ids(
        retrieval_url,
        catalog_filters(topic_ids=("transportation",), location_ids=("vn-ha-noi",)),
    )
    facets = queries(retrieval_url).facets(catalog_filters(location_ids=("vn-ha-noi",)))

    assert either == {
        traffic_hcm.article_id,
        economy_hn.article_id,
        traffic_hn.article_id,
    }
    assert both == [traffic_hn.article_id]
    assert facets.total == 2
    assert {(f.facet_id, f.count) for f in facets.topics} == {
        ("transportation", 1),
        ("economy", 1),
    }
    assert facets.topics_unclassified == 0


def test_former_province_rolls_up_only_with_descendants(retrieval_url: str) -> None:
    binh_duong = doc("Khu công nghiệp ở Bình Dương mở rộng.")
    unlabelled = doc("Không rõ địa phương.")
    persist(retrieval_url, binh_duong, unlabelled)
    label(retrieval_url, binh_duong, "location", "vn-former-binh-duong")

    hcm = ("vn-ho-chi-minh",)
    with_children = ids(retrieval_url, catalog_filters(location_ids=hcm))
    exact = ids(
        retrieval_url, catalog_filters(location_ids=hcm, include_descendants=False)
    )
    facets = queries(retrieval_url).facets(catalog_filters())

    assert with_children == [binh_duong.article_id]
    assert exact == []
    counts = {f.facet_id: f.count for f in facets.locations}
    assert (
        counts["vn-former-binh-duong"] == counts["vn-ho-chi-minh"] == counts["vn"] == 1
    )
    assert facets.locations_unknown == 1


def test_half_open_time_window_in_utc_and_null_publication(retrieval_url: str) -> None:
    at_start = doc("start", published=T0)
    last_micro = doc("end", published=T0 + timedelta(days=1, microseconds=-1))
    at_end = doc("next day", published=T0 + timedelta(days=1))
    undated = doc("undated", published=None)
    persist(retrieval_url, at_start, last_micro, at_end, undated)

    window = catalog_filters(
        published_after=T0, published_before=T0 + timedelta(days=1)
    )
    latest = ids(retrieval_url, catalog_filters())

    assert set(ids(retrieval_url, window)) == {
        at_start.article_id,
        last_micro.article_id,
    }
    assert latest[-1] == undated.article_id  # NULLS LAST, still in the feed
    assert latest[0] == at_end.article_id
    oldest = ids(retrieval_url, catalog_filters(), sort="oldest")
    assert oldest[0] == at_start.article_id
    assert oldest[-1] == undated.article_id


def test_keyset_pages_never_repeat_and_cursor_is_bound_to_filters(
    retrieval_url: str,
) -> None:
    same_time = [doc(f"tin {i}", published=T0) for i in range(5)]
    persist(retrieval_url, *same_time, doc("nguồn B", source=SOURCE_B))

    listed = ids(retrieval_url, catalog_filters())
    first = queries(retrieval_url).list_articles(catalog_filters(), limit=2)

    assert len(listed) == len(set(listed)) == 6
    assert first.next_cursor is not None
    with pytest.raises(CatalogRequestError) as caught:
        _ = queries(retrieval_url).list_articles(
            catalog_filters(source_ids=(SOURCE_B,)), limit=2, cursor=first.next_cursor
        )
    assert caught.value.code == "cursor_mismatch"


def test_unknown_taxonomy_ids_are_rejected(retrieval_url: str) -> None:
    for filters in (
        catalog_filters(topic_ids=("not_a_topic",)),
        catalog_filters(location_ids=("vn-atlantis",)),
    ):
        with pytest.raises(CatalogRequestError):
            _ = queries(retrieval_url).list_articles(filters)


def test_tombstone_and_correction_are_reflected_at_once(retrieval_url: str) -> None:
    kept, removed = doc("bài giữ lại"), doc("bài bị gỡ")
    persist(retrieval_url, kept, removed)
    label(retrieval_url, kept, "topic", "economy")
    PostgresRagStore(rag_engine(retrieval_url)).tombstone_article(removed.article_id)
    corrected = doc("bài giữ lại, đã sửa", article_id=kept.article_id)
    persist(retrieval_url, corrected)

    catalog = queries(retrieval_url)
    assert ids(retrieval_url, catalog_filters()) == [kept.article_id]
    with pytest.raises(ArticleGoneError):
        _ = catalog.article(removed.article_id)
    detail = catalog.article(kept.article_id)
    assert detail.card.article_version_id == corrected.content_hash
    assert detail.card.topics == ()  # labels belonged to the old revision
    assert detail.revision_count == 2
    with pytest.raises(ArticleVersionStaleError) as stale:
        _ = catalog.article(kept.article_id, kept.content_hash)
    assert stale.value.current_version_id == corrected.content_hash
    with psycopg.connect(retrieval_url) as connection:
        jobs = dict(
            connection.execute(
                """SELECT revision_key, status FROM rag_work_jobs
                WHERE kind = 'summary' AND subject_id = %s""",
                (kept.article_id,),
            ).fetchall()
        )
    assert jobs == {kept.content_hash: "STALE", corrected.content_hash: "PENDING"}


def test_storage_failure_is_an_error_not_an_empty_feed() -> None:
    broken = PostgresArticleCatalog(
        rag_engine("postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    )
    with pytest.raises(CatalogStorageError):
        _ = broken.list_page(catalog_filters(), CatalogSort.LATEST, None, 5)


def test_only_trusted_enrichment_models_become_labels(retrieval_url: str) -> None:
    article = doc("Tin về thời tiết.")
    persist(retrieval_url, article)
    _ = build_runtime(retrieval_url).runner(json_lines_telemetry(lambda _: None)).run(5)
    labels = PostgresArticleLabels(rag_engine(retrieval_url))

    untrusted = SyncArticleLabels(labels).execute()
    card = queries(retrieval_url).article(article.article_id).card

    assert untrusted.untrusted_model == 1
    assert card.metadata_status is MetadataStatus.UNAVAILABLE
    assert card.topics == ()

    trusted = SyncArticleLabels(labels, frozenset({"fake-enricher.v1"})).execute()
    card = queries(retrieval_url).article(article.article_id).card
    assert trusted.applied == 1
    assert card.metadata_status is MetadataStatus.READY
    assert card.topics
    first_topic = card.topics[0].topic_id
    label(retrieval_url, article, "topic", first_topic, review="rejected")
    card = queries(retrieval_url).article(article.article_id).card
    assert first_topic not in {t.topic_id for t in card.topics}  # manual override wins


# --- summaries ----------------------------------------------------------------


def _runner(url: str, model: ArticleSummaryModelPort | None = None) -> SummaryRunner:
    engine = rag_engine(url)
    return SummaryRunner(
        SummarySteps(
            PostgresSummaries(engine),
            model or FixtureSummarizer(),
            PostgresWorkQueue(engine),
        )
    )


TEXT = (
    "Cầu Phú Mỹ tạm đóng một làn từ 22 giờ ngày 28/10. "
    "Sở Giao thông TP.HCM khuyến cáo người dân đi đường vòng qua cầu Sài Gòn."
)


def test_summary_is_scheduled_cached_per_revision_and_read_only(
    retrieval_url: str,
) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    catalog = queries(retrieval_url)
    assert catalog.summary(article.article_id).status is SummaryStatus.PENDING

    first = _runner(retrieval_url).run(5, "w")
    again = _runner(retrieval_url).run(5, "w")
    view = catalog.summary(article.article_id)

    assert [o.status for o in first.outcomes] == ["published"]
    assert again.outcomes == ()
    assert view.status is SummaryStatus.READY
    assert view.article_version_id == article.content_hash
    assert view.generator is not None
    assert view.generator.model_id == "fixture-summarizer"
    for point in view.key_points:
        for span in point.spans:
            assert TEXT[span.start_offset : span.end_offset] == span.excerpt
            assert (
                span.span_fingerprint
                == f"sha256:{sha256(span.excerpt.encode()).hexdigest()}"
            )
    assert catalog.list_articles(catalog_filters()).items[0].summary_preview


def test_job_claimed_for_an_old_revision_never_publishes(retrieval_url: str) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    engine = rag_engine(retrieval_url)
    steps = SummarySteps(
        PostgresSummaries(engine), FixtureSummarizer(), PostgresWorkQueue(engine)
    )
    claimed = steps.claim("w", 1)[0]
    persist(retrieval_url, doc(TEXT + " Đã cập nhật.", article_id=article.article_id))

    outcome = SummaryGraph(steps).invoke(claimed, "w")

    assert outcome.status == "stale"
    view = queries(retrieval_url).summary(article.article_id)
    assert view.status is SummaryStatus.PENDING  # new revision's own job
    assert view.article_version_id != article.content_hash


def test_expired_lease_is_recovered_by_another_worker(retrieval_url: str) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    queue = PostgresWorkQueue(rag_engine(retrieval_url))
    crashed = queue.claim(JobKind.SUMMARY, "crashed", 60, 1)[0]
    with psycopg.connect(retrieval_url) as connection:
        _ = connection.execute(EXPIRE_LEASES)

    report = _runner(retrieval_url).run(1, "survivor")

    assert [o.status for o in report.outcomes] == ["published"]
    assert not queue.complete(crashed.fence)  # the crashed claim lost its fence
    with psycopg.connect(retrieval_url) as connection:
        row = connection.execute("SELECT attempt_count FROM rag_work_jobs").fetchone()
    assert row == (2,)


def test_malformed_output_is_a_visible_failure(retrieval_url: str) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    _ = _runner(retrieval_url, ScriptedSummarizer(["{bad"])).run(1, "w")
    view = queries(retrieval_url).summary(article.article_id)
    assert view.status is SummaryStatus.FAILED
    assert view.failure_code == "malformed_output"
    assert view.text is None


def test_admin_requeue_retries_a_failed_summary(retrieval_url: str) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    engine = rag_engine(retrieval_url)
    _ = _runner(retrieval_url, ScriptedSummarizer(["{bad"])).run(1, "w")

    assert requeue_summary(engine, article.article_id) == "requeued"
    assert requeue_summary(engine, article.article_id) == "already_pending"
    assert requeue_summary(engine, uuid4()) == "not_current"
    _ = _runner(retrieval_url).run(1, "w")
    assert (
        queries(retrieval_url).summary(article.article_id).status is SummaryStatus.READY
    )


def test_scripted_quotes_map_to_offsets_of_the_stored_revision(
    retrieval_url: str,
) -> None:
    article = doc(TEXT)
    persist(retrieval_url, article)
    quote = "Sở Giao thông TP.HCM khuyến cáo"
    _ = _runner(retrieval_url, ScriptedSummarizer([draft_quoting(quote)])).run(1, "w")
    span = queries(retrieval_url).summary(article.article_id).key_points[0].spans[0]
    assert (span.start_offset, span.end_offset) == (
        TEXT.index(quote),
        TEXT.index(quote) + len(quote),
    )
