"""Handoff import, provenance and evaluation vectors on real PostgreSQL/pgvector."""

import json
from collections.abc import Generator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from pydantic import TypeAdapter

from backend.rag.application.handoff_validation import validate_handoff
from backend.rag.application.ports.retrieval import RetrievalScope
from backend.rag.application.ports.source_versions import ProvenanceConflictError
from backend.rag.application.use_cases.import_handoff import (
    HandoffRejectedError,
    ImportHandoff,
)
from backend.rag.application.use_cases.retrieve import bundled_retrieval_policy
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_ingest import IngestClock
from backend.rag.domain.chunking import DEFAULT_POLICY
from backend.rag.domain.handoff import document_from_handoff, provenance_of
from backend.rag.evaluation.search_bench.bench import build_chunks
from backend.rag.evaluation.search_bench.systems import FtsSystem
from backend.rag.infra.access.fixture_access import FixtureAccessPolicy
from backend.rag.infra.embeddings.hashing_embedder import HashingEmbedder
from backend.rag.infra.postgres.alembic_runner import (
    alembic_config,
    head_revision,
    upgrade_head,
)
from backend.rag.infra.postgres.connection import StorageOperationError
from backend.rag.infra.postgres.engine import (
    execute,
    rag_engine,
    scalar,
    transaction,
)
from backend.rag.infra.postgres.eval_vectors import (
    EvalDatabaseRefusedError,
    PostgresEvalVectors,
    require_eval_database,
)
from backend.rag.infra.postgres.source_versions import PostgresSourceVersions
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore

CALLER = CallerScope(
    schema_version="caller_scope.v1",
    principal_id="fixture-analyst",
    scope_id="fixture-internal",
    policy_id="fixture-policy",
    policy_version="v1",
)
NOW = datetime(2026, 10, 26, tzinfo=UTC)
LONG = "\n\n".join(
    f"Đoạn {i}: Tàu hàng liên vận Việt-Trung chở {i * 7} TEU qua ga Đồng Đăng."
    for i in range(60)
)


def handoff_record(content: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "article_id": str(uuid4()),
        "article_version_id": str(uuid4()),
        "canonical_url": f"https://www.example.vn/{uuid4().hex}",
        "title": "Tàu hàng liên vận",
        "content": content,
        "source_id": "11111111-1111-4111-8111-111111111111",
        "source_name": "Nguồn",
        "source_domain": "example.vn",
        "published_at": "2026-10-16T01:00:00Z",
        "updated_at_source": None,
        "fetched_at": "2026-10-16T02:00:00Z",
        "language": "vi",
        "word_count": len(content.split()),
        "content_hash": "c" * 64,
        "normalization_version": "normalize-v4",
        "parse_version": "parser-v2",
        "quality_status": "passed",
        "quality_flags": [],
        "is_current": True,
    }
    base.update(overrides)
    return base


def batch_bytes(records: list[dict[str, object]]) -> tuple[bytes, bytes]:
    articles = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    raw = articles.encode()
    domains: dict[str, int] = {}
    for r in records:
        domains[str(r["source_domain"])] = domains.get(str(r["source_domain"]), 0) + 1
    published = sorted(str(r["published_at"]) for r in records)
    fetched = sorted(str(r["fetched_at"]) for r in records)
    manifest = {
        "schema_version": "eventlens-rag-handoff-v1",
        "export_id": "db-test",
        "exported_at": "2026-10-26T00:00:00Z",
        "articles_object": "x",
        "articles_sha256": sha256(raw).hexdigest(),
        "articles_bytes": len(raw),
        "record_count": len(records),
        "language_counts": {"vi": len(records)},
        "source_counts": domains,
        "parse_version_counts": {"parser-v2": len(records)},
        "quality_status_counts": {"passed": len(records)},
        "missing_published_at_count": 0,
        "published_at_min": published[0],
        "published_at_max": published[-1],
        "fetched_at_min": fetched[0],
        "fetched_at_max": fetched[-1],
        "selection": {
            "current_only": True,
            "quality_status": ["passed"],
            "sampling": "db-test",
            "seed": "db-test",
        },
    }
    return raw, json.dumps(manifest).encode()


@pytest.fixture
def eval_schema(retrieval_url: str) -> Generator[str]:
    schema = "rag_eval_" + uuid4().hex[:16]
    yield schema
    with psycopg.connect(retrieval_url, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


def importer(url: str) -> ImportHandoff:
    return ImportHandoff(
        store=PostgresSourceVersions(rag_engine(url)),
        access=FixtureAccessPolicy(),
        clock=lambda: datetime.now(UTC),
    )


def counts(url: str) -> tuple[int, int, int]:
    with transaction(rag_engine(url), read_only=True) as connection:
        row = execute(
            connection,
            """SELECT (SELECT count(*) FROM rag_article_documents),
                      (SELECT count(*) FROM rag_article_current),
                      (SELECT count(*) FROM rag_source_versions)""",
        ).fetchone()
    assert row is not None
    return TypeAdapter(tuple[int, int, int]).validate_python(tuple(row))


def test_head_includes_source_version_revision(retrieval_url: str) -> None:
    assert head_revision() == "rag_0002_source_versions"
    with transaction(rag_engine(retrieval_url), read_only=True) as connection:
        assert scalar(
            execute(connection, "SELECT version_num FROM alembic_version")
        ) == ("rag_0002_source_versions")


def test_repeated_import_creates_no_duplicate_revision_or_provenance(
    retrieval_url: str,
) -> None:
    records = [handoff_record(LONG), handoff_record("Bài ngắn về giá vàng hôm nay.")]
    report = validate_handoff(*batch_bytes(records))

    first = importer(retrieval_url).execute(report, CALLER)
    second = importer(retrieval_url).execute(report, CALLER)

    assert (first.revisions_inserted, first.provenance_inserted) == (2, 2)
    assert (second.revisions_inserted, second.provenance_inserted) == (0, 0)
    assert second.current == 2
    assert counts(retrieval_url) == (2, 2, 2)
    stored = PostgresSourceVersions(rag_engine(retrieval_url)).provenance_for(
        tuple(UUID(str(r["article_id"])) for r in records)
    )
    assert {p.article_version_id for p in stored} == {
        UUID(str(r["article_version_id"])) for r in records
    }
    assert all(p.upstream_content_hash == "c" * 64 for p in stored)
    assert all(p.rag_content_hash.startswith("sha256:") for p in stored)


def test_invalid_batch_writes_nothing(retrieval_url: str) -> None:
    raw, manifest = batch_bytes([handoff_record(LONG)])
    report = validate_handoff(
        raw, manifest.replace(b'"record_count": 1', b'"record_count": 2')
    )

    with pytest.raises(HandoffRejectedError):
        _ = importer(retrieval_url).execute(report, CALLER)
    assert counts(retrieval_url) == (0, 0, 0)


def test_upstream_version_bound_to_other_revision_is_refused(
    retrieval_url: str,
) -> None:
    original = handoff_record(LONG)
    report = validate_handoff(*batch_bytes([original]))
    _ = importer(retrieval_url).execute(report, CALLER)
    tampered = dict(
        original, content=LONG + " Sửa.", word_count=len((LONG + " Sửa.").split())
    )
    record = validate_handoff(*batch_bytes([tampered])).records[0]
    clock = IngestClock(ingested_at=NOW, processed_at=NOW)
    document = document_from_handoff(record, clock)
    store = PostgresSourceVersions(rag_engine(retrieval_url))

    with pytest.raises(ProvenanceConflictError):
        _ = store.persist_with_provenance(
            document, provenance_of(record, document, "db-test")
        )
    assert counts(retrieval_url) == (1, 1, 1)


def test_update_makes_new_version_current_and_keeps_old_provenance(
    retrieval_url: str,
) -> None:
    article_id = str(uuid4())
    first = handoff_record(LONG, article_id=article_id)
    updated_text = LONG.replace("Đoạn 3:", "Đoạn 3 (cập nhật):")
    second = handoff_record(
        updated_text,
        article_id=article_id,
        canonical_url=first["canonical_url"],
        updated_at_source="2026-10-17T00:00:00Z",
        fetched_at="2026-10-17T01:00:00Z",
    )
    _ = importer(retrieval_url).execute(validate_handoff(*batch_bytes([first])), CALLER)
    receipt = importer(retrieval_url).execute(
        validate_handoff(*batch_bytes([second])), CALLER
    )

    assert receipt.revisions_inserted == 1
    assert counts(retrieval_url) == (2, 1, 2)
    engine = rag_engine(retrieval_url)
    documents = PostgresSourceVersions(engine).current_documents()
    assert [d.normalized_content for d in documents] == [updated_text]
    fts = FtsSystem(
        PostgresRetrievalStore(engine), bundled_retrieval_policy(), DEFAULT_POLICY, NOW
    )
    hits = fts.search("cập nhật Đồng Đăng", RetrievalScope((), None, None, None))
    assert hits
    assert {h.chunk.content_hash for h in hits} == {documents[0].content_hash}


def test_tombstone_hides_article_from_fts_and_eval_vectors(
    retrieval_url: str, eval_schema: str
) -> None:
    kept = handoff_record("Giá vàng thế giới tăng mạnh trong phiên hôm nay.")
    gone = handoff_record("Giá vàng trong nước giảm nhẹ sáng nay.")
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch_bytes([kept, gone])), CALLER
    )
    engine = rag_engine(retrieval_url)
    documents = PostgresSourceVersions(engine).current_documents()
    chunks = build_chunks(documents, DEFAULT_POLICY)
    embedder = HashingEmbedder(24)
    vectors = PostgresEvalVectors(engine, schema=eval_schema)
    vectors.rebuild(
        embedder.identity, chunks, embedder.embed_documents([c.text for c in chunks])
    )
    PostgresRagStore(engine).tombstone_article(UUID(str(gone["article_id"])))
    scope = RetrievalScope((), None, None, None)
    (query,) = embedder.embed_queries(["giá vàng"])

    semantic = vectors.search(embedder.identity, query, scope, 10)
    lexical = FtsSystem(
        PostgresRetrievalStore(engine), bundled_retrieval_policy(), DEFAULT_POLICY, NOW
    ).search("giá vàng", scope)

    assert {h.chunk.article_id for h in semantic} == {UUID(str(kept["article_id"]))}
    assert {h.chunk.article_id for h in lexical} == {UUID(str(kept["article_id"]))}


def test_chunk_offsets_round_trip_to_exact_source_passage(
    retrieval_url: str, eval_schema: str
) -> None:
    text = "Mở đầu\u200b có ký tự ẩn. " + LONG + " Hoàng hôn ở Đồng Đăng."
    record = handoff_record(text)
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch_bytes([record])), CALLER
    )
    engine = rag_engine(retrieval_url)
    (document,) = PostgresSourceVersions(engine).current_documents()
    chunks = build_chunks((document,), DEFAULT_POLICY)
    embedder = HashingEmbedder(32)
    vectors = PostgresEvalVectors(engine, schema=eval_schema)
    vectors.rebuild(
        embedder.identity, chunks, embedder.embed_documents([c.text for c in chunks])
    )
    (query,) = embedder.embed_queries(["Hoàng hôn ở Đồng Đăng"])

    hits = vectors.search(
        embedder.identity, query, RetrievalScope((), None, None, None), 50
    )
    texts = PostgresRetrievalStore(engine).chunk_texts(tuple(h.chunk for h in hits))

    assert document.raw_content == text
    assert len(hits) == len(chunks) > 3
    for item, excerpt in zip(hits, texts, strict=True):
        chunk = item.chunk
        assert excerpt == text[chunk.start_offset : chunk.end_offset]
        assert chunk.content_hash == document.content_hash
    tail = text.index("Hoàng hôn ở Đồng Đăng.")
    assert any(h.chunk.start_offset <= tail < h.chunk.end_offset for h in hits)


def test_eval_vectors_keep_native_dimensions_and_apply_scope(
    retrieval_url: str, eval_schema: str
) -> None:
    a = handoff_record("Thị trường chứng khoán giảm điểm.", source_domain="example.vn")
    b = handoff_record(
        "Thị trường chứng khoán tăng điểm.",
        source_domain="other.vn",
        canonical_url=f"https://other.vn/{uuid4().hex}",
        published_at="2026-10-10T00:00:00Z",
    )
    _ = importer(retrieval_url).execute(validate_handoff(*batch_bytes([a, b])), CALLER)
    engine = rag_engine(retrieval_url)
    chunks = build_chunks(
        PostgresSourceVersions(engine).current_documents(), DEFAULT_POLICY
    )
    vectors = PostgresEvalVectors(engine, schema=eval_schema)
    small, large = HashingEmbedder(8), HashingEmbedder(1024)
    for embedder in (small, large):
        vectors.rebuild(
            embedder.identity,
            chunks,
            embedder.embed_documents([c.text for c in chunks]),
        )
    with transaction(engine, read_only=True) as connection:
        types = dict(
            TypeAdapter(list[tuple[str, str]]).validate_python(
                [
                    tuple(row)
                    for row in execute(
                        connection,
                        """SELECT c.relname, format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND a.attname = 'embedding'""",
                        (eval_schema,),
                    )
                ]
            )
        )
    assert types == {
        "vectors_fake_hashing_8": "vector(8)",
        "vectors_fake_hashing_1024": "vector(1024)",
    }
    (query,) = large.embed_queries(["chứng khoán"])
    domain = vectors.search(
        large.identity, query, RetrievalScope(("other.vn",), None, None, None), 10
    )
    window = vectors.search(
        large.identity,
        query,
        RetrievalScope((), datetime(2026, 10, 15, tzinfo=UTC), None, None),
        10,
    )
    assert {h.chunk.article_id for h in domain} == {UUID(str(b["article_id"]))}
    assert {h.chunk.article_id for h in window} == {UUID(str(a["article_id"]))}
    with pytest.raises(ValueError, match="finite values"):
        _ = vectors.search(
            large.identity, query[:10], RetrievalScope((), None, None, None), 5
        )


def test_eval_guard_refuses_non_eval_database(retrieval_url: str) -> None:
    with pytest.raises(EvalDatabaseRefusedError):
        _ = require_eval_database(rag_engine(retrieval_url))


def _relation_exists(url: str, name: str) -> bool:
    with transaction(rag_engine(url), read_only=True) as connection:
        return bool(
            scalar(
                execute(
                    connection,
                    """SELECT EXISTS (SELECT FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = current_schema() AND c.relname = %s)""",
                    (name,),
                )
            )
        )


def test_source_version_downgrade_only_when_empty(retrieval_url: str) -> None:
    with transaction(rag_engine(retrieval_url)) as connection:
        command.downgrade(alembic_config(connection), "rag_0001_baseline")
    assert not _relation_exists(retrieval_url, "rag_source_versions")
    assert upgrade_head(retrieval_url) == "rag_0002_source_versions"
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch_bytes([handoff_record(LONG)])), CALLER
    )

    with (
        pytest.raises(RuntimeError, match="holds provenance"),
        transaction(rag_engine(retrieval_url)) as connection,
    ):
        command.downgrade(alembic_config(connection), "rag_0001_baseline")
    assert _relation_exists(retrieval_url, "rag_source_versions")
    assert counts(retrieval_url) == (1, 1, 1)


def test_provenance_rows_are_immutable(retrieval_url: str) -> None:
    _ = importer(retrieval_url).execute(
        validate_handoff(*batch_bytes([handoff_record(LONG)])), CALLER
    )
    with (
        pytest.raises(StorageOperationError) as caught,
        transaction(rag_engine(retrieval_url)) as connection,
    ):
        _ = execute(connection, "UPDATE rag_source_versions SET word_count = 0")
    assert "immutable" in str(caught.value.__cause__)
