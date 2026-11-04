"""Regressions for review R1-R4 (codex-review-20261029) on real PostgreSQL.

R1 catalog responses come from one snapshot and keep the selected revision;
R2 a transient storage error never kills a chat worker; R3 an expired job
can never leave its run active; R4 label sync follows publication and trust
changes without manual row deletion.
"""

import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest

import backend.rag.infra.postgres.article_catalog as catalog_sql
import backend.rag.infra.postgres.conversations as conversation_sql
from backend.rag.application.ports.conversations import NewRun
from backend.rag.application.use_cases.catalog import CatalogQueries
from backend.rag.application.use_cases.labels import SyncArticleLabels
from backend.rag.application.use_cases.query_runs import QueryRunExecutor
from backend.rag.composition import (
    FIXTURES,
    build_run_executor,
    build_runtime,
    json_lines_telemetry,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.catalog import CursorCodec, MetadataStatus, catalog_filters
from backend.rag.domain.conversation import (
    ConversationStorageError,
    RunInProgressError,
    ScopeType,
    conversation_scope,
)
from backend.rag.domain.work_jobs import JobKind
from backend.rag.infra.postgres.article_catalog import (
    PostgresArticleCatalog,
    PostgresArticleLabels,
)
from backend.rag.infra.postgres.connection import StorageOperationError
from backend.rag.infra.postgres.conversations import PostgresConversations
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.postgres.work_queue import PostgresWorkQueue
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from backend.rag.worker_pools import PoolWorker
from briefing_support import QUESTION
from news_support import news_document

PAST_DEADLINE = """UPDATE rag_work_jobs SET deadline_at = now() - interval '1 s'
WHERE subject_id = %s"""
SYNCED_PUBLICATION = """SELECT publication_id IS NOT NULL FROM rag_label_syncs
WHERE article_id = %s"""
FIXTURE_ENRICHER = "fake-enricher.v1"  # test model only, never trusted in config


def _catalog(url: str) -> CatalogQueries:
    return CatalogQueries(
        PostgresArticleCatalog(rag_engine(url)), CursorCodec(b"r" * 32)
    )


@contextmanager
def correction_before_cards(
    url: str, corrected: ArticleDocument
) -> Generator[list[bool]]:
    """Commit ``corrected`` from another connection right before cards load."""
    real = cast("Callable[..., object]", getattr(catalog_sql, "execute"))  # noqa: B009
    fired: list[bool] = []

    def execute(connection: object, query: str, params: object = ()) -> object:
        if query == catalog_sql.CARDS_SQL and not fired:
            fired.append(True)
            PostgresRagStore(rag_engine(url)).persist_article(corrected)
        return real(connection, query, params)

    with patch.object(catalog_sql, "execute", execute):
        yield fired


# --- R1 ----------------------------------------------------------------------


def test_detail_never_mixes_content_and_version_of_two_revisions(
    retrieval_url: str,
) -> None:
    old = news_document("Bản gốc: cầu Phú Mỹ cấm xe tải từ 22 giờ.")
    PostgresRagStore(rag_engine(retrieval_url)).persist_article(old)
    new = news_document(
        "Bản sửa: cầu Phú Mỹ cấm xe tải từ 23 giờ.", article_id=old.article_id
    )
    with correction_before_cards(retrieval_url, new) as fired:
        detail = _catalog(retrieval_url).article(old.article_id)

    assert fired
    assert detail.card.article_version_id == old.content_hash
    assert detail.content == old.normalized_content
    fresh = _catalog(retrieval_url).article(old.article_id)  # next request sees B
    assert (fresh.card.article_version_id, fresh.content) == (
        new.content_hash,
        new.normalized_content,
    )


def test_filtered_page_keeps_the_revision_that_matched(retrieval_url: str) -> None:
    old = news_document("Tin có từ khoá bridgeunique trong nội dung.")
    PostgresRagStore(rag_engine(retrieval_url)).persist_article(old)
    new = news_document("Tin đã sửa, không còn từ khoá.", article_id=old.article_id)
    with correction_before_cards(retrieval_url, new) as fired:
        page = _catalog(retrieval_url).list_articles(catalog_filters(q="bridgeunique"))

    assert fired
    assert [c.article_version_id for c in page.items] == [old.content_hash]
    after = _catalog(retrieval_url).list_articles(catalog_filters(q="bridgeunique"))
    assert after.items == ()  # the correction no longer matches


# --- R3 ----------------------------------------------------------------------


def _queued_run(url: str) -> tuple[PostgresConversations, UUID, UUID]:
    store = PostgresConversations(rag_engine(url))
    owner, conversation = uuid4(), uuid4()
    scope = conversation_scope(ScopeType.ALL, None, ())
    _ = store.create(owner, conversation, None, scope.canonical(), scope.digest())
    submitted = store.submit(
        owner,
        conversation,
        NewRun(uuid4(), uuid4(), uuid4(), QUESTION, None, "0" * 64, 3, 600.0),
    )
    return store, owner, submitted.run.run_id


def _states(url: str, run_id: UUID) -> tuple[str, str]:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            """SELECT r.status, j.status FROM rag_query_runs r
            JOIN rag_work_jobs j ON j.subject_id = r.run_id WHERE r.run_id = %s""",
            (run_id,),
        ).fetchone()
    assert row is not None
    return cast("tuple[str, str]", (row[0], row[1]))


def _past_deadline(url: str, run_id: UUID) -> None:
    with psycopg.connect(url) as connection:
        _ = connection.execute(PAST_DEADLINE, (run_id,))


def _resubmit(store: PostgresConversations, owner: UUID, run_id: UUID) -> bool:
    run = store.run(owner, run_id)
    assert run is not None
    result = store.submit(
        owner,
        run.conversation_id,
        NewRun(uuid4(), uuid4(), uuid4(), "câu tiếp", None, "1" * 64, 3, 600.0),
    )
    return result.created


def test_crash_between_job_expiry_and_run_update_is_repaired_on_restart(
    retrieval_url: str,
) -> None:
    store, owner, run_id = _queued_run(retrieval_url)
    _past_deadline(retrieval_url, run_id)
    # Old two-step behaviour, crashing after the first commit.
    _ = PostgresWorkQueue(rag_engine(retrieval_url)).expire(JobKind.QUERY_RUN)
    assert _states(retrieval_url, run_id) == ("queued", "FAILED")
    with pytest.raises(RunInProgressError):
        _ = _resubmit(store, owner, run_id)

    restarted = build_run_executor(retrieval_url, {})
    assert restarted.claim("restarted", 1) == ()

    assert _states(retrieval_url, run_id) == ("failed", "FAILED")
    run = store.run(owner, run_id)
    assert run is not None
    assert run.failure_code == "deadline_exceeded"
    assert _resubmit(store, owner, run_id)


def test_expiry_and_run_update_commit_together_or_not_at_all(
    retrieval_url: str,
) -> None:
    store, owner, run_id = _queued_run(retrieval_url)
    _past_deadline(retrieval_url, run_id)
    real = cast("Callable[..., object]", getattr(conversation_sql, "execute"))  # noqa: B009
    failed: list[bool] = []

    def execute(connection: object, query: str, params: object = ()) -> object:
        if query == conversation_sql.RECONCILE_RUNS_SQL and not failed:
            failed.append(True)
            raise StorageOperationError
        return real(connection, query, params)

    with (
        patch.object(conversation_sql, "execute", execute),
        pytest.raises(ConversationStorageError),
    ):
        _ = store.reconcile_runs()
    assert _states(retrieval_url, run_id) == ("queued", "PENDING")  # rolled back

    assert store.reconcile_runs() == 1
    assert _states(retrieval_url, run_id) == ("failed", "FAILED")
    assert _resubmit(store, owner, run_id)


# --- R2 ----------------------------------------------------------------------


def test_transient_start_failure_is_retried_by_the_same_worker(
    retrieval_url: str,
) -> None:
    PostgresRagStore(rag_engine(retrieval_url)).persist_article(
        load_fixture_articles(FIXTURES)[0]
    )
    store, owner, run_id = _queued_run(retrieval_url)
    executor = build_run_executor(
        retrieval_url, {"EVENTLENS_RAG_ANSWER_PROVIDER": "fixture"}
    )
    real_start = PostgresConversations.start
    calls: list[UUID] = []

    def flaky_start(self: PostgresConversations, run: UUID) -> object:
        calls.append(run)
        if len(calls) == 1:
            raise ConversationStorageError
        return real_start(self, run)

    stop = threading.Event()
    events: list[dict[str, object]] = []

    def step() -> bool:
        claimed = executor.claim("flaky", 1)
        if not claimed:
            return False
        _ = executor.execute(claimed[0])
        return True

    worker = PoolWorker("q0", step, stop, events.append, poll_seconds=0.05)
    with patch.object(PostgresConversations, "start", flaky_start):
        worker.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _states(retrieval_url, run_id)[0] not in {
            "completed",
            "failed",
        }:
            time.sleep(0.1)
        stop.set()
        assert worker.thread is not None
        worker.thread.join(timeout=10)

    assert len(calls) == 2
    assert worker.errors == 1
    assert events[0]["error"] == "ConversationStorageError"
    assert _states(retrieval_url, run_id) == ("completed", "DONE")
    with psycopg.connect(retrieval_url) as connection:
        row = connection.execute(
            "SELECT attempt_count FROM rag_work_jobs WHERE subject_id = %s", (run_id,)
        ).fetchone()
    assert row == (2,)
    assert isinstance(executor, QueryRunExecutor)
    assert store.run(owner, run_id) is not None


# --- R4 ----------------------------------------------------------------------


def _enriched(url: str) -> UUID:
    article = news_document("Bão số 3 gây mưa lớn ở Hà Nội và Hải Phòng.")
    PostgresRagStore(rag_engine(url)).persist_article(article)
    _ = build_runtime(url).runner(json_lines_telemetry(lambda _: None)).run(5)
    return article.article_id


def _state(url: str, article_id: UUID) -> tuple[MetadataStatus, int]:
    card = _catalog(url).article(article_id).card
    return card.metadata_status, len(card.topics)


def test_trusting_then_revoking_a_model_resyncs_without_manual_cleanup(
    retrieval_url: str,
) -> None:
    article_id = _enriched(retrieval_url)
    labels = PostgresArticleLabels(rag_engine(retrieval_url))
    trusted = frozenset({FIXTURE_ENRICHER})

    first = SyncArticleLabels(labels).execute()
    approved = SyncArticleLabels(labels, trusted).execute()
    state_trusted = _state(retrieval_url, article_id)
    stable = SyncArticleLabels(labels, trusted).execute()
    revoked = SyncArticleLabels(labels).execute()
    state_revoked = _state(retrieval_url, article_id)

    assert first.untrusted_model == 1
    assert approved.applied == 1
    assert state_trusted[0] is MetadataStatus.READY
    assert state_trusted[1] > 0
    assert stable.as_dict() == dict.fromkeys(stable.as_dict(), 0)  # idempotent
    assert revoked.untrusted_model == 1
    assert state_revoked == (MetadataStatus.UNAVAILABLE, 0)


def test_a_different_publication_for_the_same_content_is_resynced(
    retrieval_url: str,
) -> None:
    article_id = _enriched(retrieval_url)
    labels = PostgresArticleLabels(rag_engine(retrieval_url))
    trusted = frozenset({FIXTURE_ENRICHER})
    _ = SyncArticleLabels(labels, trusted).execute()
    with psycopg.connect(retrieval_url) as connection:  # sync recorded for another pub
        _ = connection.execute(
            "UPDATE rag_label_syncs SET publication_id = NULL WHERE article_id = %s",
            (article_id,),
        )
    again = SyncArticleLabels(labels, trusted).execute()
    assert again.applied == 1
    with psycopg.connect(retrieval_url) as connection:
        row = connection.execute(
            SYNCED_PUBLICATION,
            (article_id,),
        ).fetchone()
    assert row == (True,)
