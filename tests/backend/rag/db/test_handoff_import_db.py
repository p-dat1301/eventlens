"""Handoff import on real PostgreSQL: atomic batch, idempotency, provenance.

handoff JSONL -> validation -> ArticleDocument + provenance -> PostgreSQL
(revision, current pointer, outbox event via trigger, provenance) in one
transaction per batch.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import cast
from unicodedata import normalize
from uuid import uuid4

import psycopg
import pytest

from backend.rag.application.handoff_validation import validate_handoff
from backend.rag.application.ports.source_versions import ProvenanceConflictError
from backend.rag.application.use_cases.import_handoff import (
    HandoffRejectedError,
    ImportHandoff,
)
from backend.rag.composition import FIXTURE_CALLER
from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.source_versions import PostgresSourceVersions
from handoff_support import BODY, batch, record

TABLES = (
    "rag_article_documents",
    "rag_article_current",
    "rag_source_versions",
    "rag_search_outbox",
    "rag_enrichment_jobs",
    "rag_chunks",
)
# Zero-width space and a literal HTML entity must survive byte for byte.
DIRTY = "Giá vàng &quot;SJC&quot; tăng\u200b mạnh.\n\nNgân hàng điều chỉnh lãi suất."


def importer(url: str) -> ImportHandoff:
    return ImportHandoff(
        PostgresSourceVersions(rag_engine(url)),
        FixtureAccessPolicy(),
        lambda: datetime(2026, 10, 28, tzinfo=UTC),
    )


def counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as connection:
        return {
            table: cast(
                "tuple[int]",
                connection.execute(f"SELECT count(*) FROM {table}").fetchone(),  # noqa: S608 - constant
            )[0]
            for table in TABLES
        }


def outbox_events(url: str) -> list[str]:
    with psycopg.connect(url) as connection:
        rows = cast(
            "list[tuple[str]]",
            connection.execute(
                "SELECT event_type FROM rag_search_outbox ORDER BY event_id"
            ).fetchall(),
        )
    return [row[0] for row in rows]


def test_repeat_import_adds_no_revision_provenance_outbox_or_job(
    retrieval_url: str,
) -> None:
    records = [record(), record(content=DIRTY, word_count=len(DIRTY.split()))]
    report = validate_handoff(*batch(records))

    first = importer(retrieval_url).execute(report, FIXTURE_CALLER)
    after_first = counts(retrieval_url)
    second = importer(retrieval_url).execute(report, FIXTURE_CALLER)

    assert (first.revisions_inserted, first.provenance_inserted, first.current) == (
        2,
        2,
        2,
    )
    assert (second.revisions_inserted, second.provenance_inserted) == (0, 0)
    assert second.current == 2
    assert counts(retrieval_url) == after_first
    assert after_first["rag_search_outbox"] == 2
    assert after_first["rag_enrichment_jobs"] == 0  # import enqueues no model work
    assert after_first["rag_chunks"] == 0
    assert outbox_events(retrieval_url) == ["article_revision_created"] * 2


def test_conflicting_upstream_version_rolls_back_the_whole_batch(
    retrieval_url: str,
) -> None:
    original = record()
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch([original])), FIXTURE_CALLER
    )
    before = counts(retrieval_url)
    newcomer = record()
    # Same article_version_id, different content: provenance contradiction.
    tampered = {**original, "content": BODY + " Bổ sung.", "word_count": 0}
    tampered["word_count"] = len(str(tampered["content"]).split())
    report = validate_handoff(*batch([newcomer, tampered]))
    assert report.ok

    with pytest.raises(ProvenanceConflictError):
        _ = importer(retrieval_url).execute(report, FIXTURE_CALLER)

    assert counts(retrieval_url) == before  # newcomer was not written either


def test_same_version_with_other_parse_version_is_refused(retrieval_url: str) -> None:
    original = record()
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch([original])), FIXTURE_CALLER
    )
    relabeled = {**original, "parse_version": "parser-v9"}

    with pytest.raises(ProvenanceConflictError):
        _ = importer(retrieval_url).execute(
            validate_handoff(*batch([relabeled])), FIXTURE_CALLER
        )


def test_invalid_batch_writes_nothing(retrieval_url: str) -> None:
    articles, manifest = batch([record(), record()], articles_sha256="0" * 64)
    report = validate_handoff(articles, manifest)

    with pytest.raises(HandoffRejectedError):
        _ = importer(retrieval_url).execute(report, FIXTURE_CALLER)

    assert set(counts(retrieval_url).values()) == {0}


def test_provenance_hashes_text_and_null_publication_round_trip(
    retrieval_url: str,
) -> None:
    item = record(
        content=DIRTY,
        word_count=len(DIRTY.split()),
        published_at=None,
        content_hash="b" * 64,
        parse_version="parser-v1",
        normalization_version="normalize-v1",
    )
    articles, manifest = batch(
        [item], parse_version_counts={"parser-v1": 1}, missing_published_at_count=1
    )
    report = validate_handoff(articles, manifest)
    assert report.ok, report.errors
    _ = importer(retrieval_url).execute(report, FIXTURE_CALLER)
    store = PostgresSourceVersions(rag_engine(retrieval_url))

    (document,) = store.current_documents()
    (provenance,) = store.provenance_for((document.article_id,))

    assert document.raw_content == DIRTY
    assert document.normalized_content == normalize("NFC", DIRTY)
    assert "\u200b" in document.normalized_content
    assert "&quot;" in document.normalized_content
    assert document.content_hash == normalized_content_hash(normalize("NFC", DIRTY))
    assert document.source_published_at is None  # never replaced by fetched_at
    assert provenance.upstream_content_hash == "b" * 64
    assert provenance.rag_content_hash == document.content_hash
    assert (provenance.parse_version, provenance.upstream_normalization_version) == (
        "parser-v1",
        "normalize-v1",
    )
    assert provenance.export_id == "unit-batch"
    assert str(provenance.article_version_id) == item["article_version_id"]
    start = document.normalized_content.index("SJC")
    assert document.normalized_content[start : start + 3] == "SJC"


def test_read_back_is_ordered_and_complete(retrieval_url: str) -> None:
    records = [record(article_id=str(uuid4())) for _ in range(3)]
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch(records)), FIXTURE_CALLER
    )

    stored = PostgresSourceVersions(rag_engine(retrieval_url)).current_documents()

    assert sorted(str(d.article_id) for d in stored) == sorted(
        str(r["article_id"]) for r in records
    )


def test_old_replay_does_not_promote_or_enqueue(retrieval_url: str) -> None:
    old = record()
    new = {
        **old,
        "article_version_id": str(uuid4()),
        "content": BODY + " Tin mới.",
        "word_count": len((BODY + " Tin mới.").split()),
        "fetched_at": "2026-10-21T09:00:00Z",
        "updated_at_source": "2026-10-21T08:00:00Z",
    }
    service = importer(retrieval_url)
    for row in (old, new):
        _ = service.execute(validate_handoff(*batch([row])), FIXTURE_CALLER)
    store = PostgresSourceVersions(rag_engine(retrieval_url))
    current = store.current_documents()
    before = counts(retrieval_url)
    receipt = service.execute(validate_handoff(*batch([old])), FIXTURE_CALLER)
    assert receipt.current == 0
    assert counts(retrieval_url) == before
    assert store.current_documents() == current


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Tiêu đề sửa"),
        ("published_at", "2026-10-20T08:01:00Z"),
        ("canonical_url", "https://www.example.vn/url-khac"),
        ("source_name", "Nguồn khác"),
    ],
)
@pytest.mark.parametrize("new_version", [False, True])
def test_metadata_conflict_never_silently_succeeds(
    retrieval_url: str,
    field: str,
    value: str,
    *,
    new_version: bool,
) -> None:
    old = record()
    service = importer(retrieval_url)
    _ = service.execute(validate_handoff(*batch([old])), FIXTURE_CALLER)
    changed = {**old, field: value}
    if new_version:
        changed["article_version_id"] = str(uuid4())
    before = counts(retrieval_url)
    with pytest.raises(ProvenanceConflictError):
        _ = service.execute(validate_handoff(*batch([changed])), FIXTURE_CALLER)
    assert counts(retrieval_url) == before


@pytest.mark.parametrize("fetched", ["2026-10-19T08:05:00Z", "2026-10-20T08:05:00Z"])
def test_unseen_older_or_ambiguous_revision_is_refused(
    retrieval_url: str, fetched: str
) -> None:
    old = record()
    service = importer(retrieval_url)
    _ = service.execute(validate_handoff(*batch([old])), FIXTURE_CALLER)
    changed = {
        **old,
        "article_version_id": str(uuid4()),
        "content": BODY + " Khác.",
        "word_count": len((BODY + " Khác.").split()),
        "fetched_at": fetched,
    }
    before = counts(retrieval_url)
    with pytest.raises(ProvenanceConflictError):
        _ = service.execute(validate_handoff(*batch([changed])), FIXTURE_CALLER)
    assert counts(retrieval_url) == before


def test_concurrent_replays_leave_current_and_outbox_unchanged(
    retrieval_url: str,
) -> None:
    old = record()
    service = importer(retrieval_url)
    _ = service.execute(validate_handoff(*batch([old])), FIXTURE_CALLER)
    before = counts(retrieval_url)
    barrier = Barrier(2)

    def replay() -> int:
        _ = barrier.wait(timeout=10)
        return service.execute(
            validate_handoff(*batch([old])), FIXTURE_CALLER
        ).revisions_inserted

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(replay) for _ in range(2)]
        assert [f.result(timeout=20) for f in futures] == [0, 0]
    assert counts(retrieval_url) == before
