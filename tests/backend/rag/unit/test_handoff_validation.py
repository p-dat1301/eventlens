import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from backend.rag.application.handoff_validation import Finding, validate_handoff
from backend.rag.domain.article_ingest import IngestClock
from backend.rag.domain.handoff import document_from_handoff, provenance_of

EXPORTED = "2026-10-26T14:26:14Z"
BODY = (
    "Tuyến phà Riverside tạm dừng lúc 09:00 UTC do thời tiết xấu.\n\n"
    "Tuyến hoạt động trở lại lúc 14:00 UTC cùng ngày."
)


def record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "article_id": str(uuid4()),
        "article_version_id": str(uuid4()),
        "canonical_url": f"https://www.example.vn/bai-{uuid4().hex[:8]}",
        "title": "Thông báo điều chỉnh lịch phà",
        "content": BODY,
        "source_id": "11111111-1111-4111-8111-111111111111",
        "source_name": "Nguồn minh họa",
        "source_domain": "example.vn",
        "published_at": "2026-10-20T08:00:00Z",
        "updated_at_source": None,
        "fetched_at": "2026-10-20T08:05:00Z",
        "language": "vi",
        "word_count": len(BODY.split()),
        "content_hash": "a" * 64,
        "normalization_version": "normalize-v4",
        "parse_version": "parser-v2",
        "quality_status": "passed",
        "quality_flags": [],
        "is_current": True,
    }
    base.update(overrides)
    return base


def batch(
    records: list[dict[str, object]], **manifest_overrides: object
) -> tuple[bytes, bytes]:
    articles = "".join(
        json.dumps(item, ensure_ascii=False) + "\n" for item in records
    ).encode()
    published = sorted(str(r["published_at"]) for r in records if r["published_at"])
    fetched = sorted(str(r["fetched_at"]) for r in records)
    manifest: dict[str, object] = {
        "schema_version": "eventlens-rag-handoff-v1",
        "export_id": "unit-batch",
        "exported_at": EXPORTED,
        "articles_object": "exports/unit/articles.jsonl",
        "articles_sha256": sha256(articles).hexdigest(),
        "articles_bytes": len(articles),
        "record_count": len(records),
        "language_counts": dict(Counter(str(r["language"]) for r in records)),
        "source_counts": dict(Counter(str(r["source_domain"]) for r in records)),
        "parse_version_counts": dict(Counter(str(r["parse_version"]) for r in records)),
        "quality_status_counts": dict(
            Counter(str(r["quality_status"]) for r in records)
        ),
        "missing_published_at_count": sum(1 for r in records if not r["published_at"]),
        "published_at_min": published[0] if published else None,
        "published_at_max": published[-1] if published else None,
        "fetched_at_min": fetched[0],
        "fetched_at_max": fetched[-1],
        "selection": {
            "current_only": True,
            "quality_status": ["passed"],
            "sampling": "unit",
            "seed": "unit",
        },
    }
    manifest.update(manifest_overrides)
    return articles, json.dumps(manifest).encode()


def codes(findings: Sequence[Finding]) -> list[str]:
    return [item.code for item in findings]


def test_valid_batch_passes_and_maps_hashes_without_text() -> None:
    articles, manifest = batch([record(), record()])

    report = validate_handoff(articles, manifest)

    assert report.ok, report.errors
    assert report.valid_records == 2
    assert report.hash_mapping == {
        "records": 2,
        "upstream_equals_rag_nfc_sha256": 0,
        "upstream_differs_from_rag_nfc_sha256": 2,
    }
    rendered = json.dumps(report.to_json(), ensure_ascii=False, default=str)
    assert "Riverside" not in rendered
    assert "phà" not in rendered


def test_checksum_size_and_count_mismatch_are_errors() -> None:
    articles, manifest = batch(
        [record()], articles_sha256="0" * 64, articles_bytes=1, record_count=9
    )

    report = validate_handoff(articles, manifest)

    assert not report.ok
    assert {
        "articles_sha256_mismatch",
        "articles_bytes_mismatch",
        "record_count_mismatch",
    } <= set(codes(report.errors))


def test_duplicate_identities_and_foreign_url_host_are_errors() -> None:
    first = record()
    second = record(
        article_id=first["article_id"],
        article_version_id=first["article_version_id"],
        canonical_url=first["canonical_url"],
    )
    third = record(canonical_url="https://evil.test/x")
    articles, manifest = batch([first, second, third])

    report = validate_handoff(articles, manifest)

    assert {
        "duplicate_article_id",
        "duplicate_article_version_id",
        "duplicate_url",
        "url_host_outside_source_domain",
    } <= set(codes(report.errors))


def test_schema_errors_report_field_and_type_only() -> None:
    bad = record(content="   ", published_at="2026-10-20T08:00:00+07:00")
    articles, manifest = batch([record()])
    articles += (json.dumps(bad, ensure_ascii=False) + "\n").encode()

    report = validate_handoff(articles, manifest)

    schema = [f for f in report.errors if f.code == "schema"]
    assert len(schema) == 1
    assert schema[0].line == 2
    assert "content" in schema[0].detail
    assert "published_at" in schema[0].detail
    assert "Riverside" not in schema[0].detail


def test_manifest_counts_and_ranges_are_rederived() -> None:
    articles, manifest = batch(
        [record()],
        source_counts={"other.vn": 1},
        missing_published_at_count=3,
        fetched_at_max="2026-10-21T00:00:00Z",
    )

    report = validate_handoff(articles, manifest)

    found = [(f.code, f.detail) for f in report.errors]
    assert ("manifest_count_mismatch", "source_counts") in found
    assert ("manifest_count_mismatch", "missing_published_at_count") in found
    assert ("manifest_range_mismatch", "fetched_at") in found


def test_timestamps_after_export_and_non_current_records_are_errors() -> None:
    articles, manifest = batch(
        [record(fetched_at="2026-10-27T00:00:00Z", is_current=False)]
    )

    report = validate_handoff(articles, manifest)

    assert "timestamp_after_export" in codes(report.errors)
    assert "not_current_in_current_only_batch" in codes(report.errors)


def test_text_hygiene_is_reported_as_warnings() -> None:
    content = "Giá\u200b vàng\u00a0tăng &quot;mạnh&quot; <b>hôm nay</b>"
    articles, manifest = batch(
        [record(content=content, word_count=99, title="Giá &amp; vàng")]
    )

    report = validate_handoff(articles, manifest)

    assert report.ok
    warnings = codes(report.warnings)
    assert "invisible_or_format_characters" in warnings
    assert "html_entity" in warnings
    assert "html_tag" in warnings
    assert "word_count_mismatch" in warnings
    detail = next(
        f.detail for f in report.warnings if f.code == "invisible_or_format_characters"
    )
    assert "U+200B" in detail
    assert "U+00A0" in detail


def test_near_duplicates_are_paired_by_id() -> None:
    shared = " ".join(f"từ{i}" for i in range(80))
    a = record(content=shared + " kết thúc A", word_count=82)
    b = record(content=shared + " kết thúc B", word_count=82)
    c = record()
    articles, manifest = batch([a, b, c])

    report = validate_handoff(articles, manifest)

    assert len(report.near_duplicates) == 1
    pair = report.near_duplicates[0]
    assert {pair.article_id, pair.other_article_id} == {
        str(a["article_id"]),
        str(b["article_id"]),
    }
    assert pair.jaccard > 0.9


def test_invalid_utf8_and_missing_manifest_are_errors() -> None:
    articles, _ = batch([record()])

    report = validate_handoff(articles + b"\xff\xfe\n", None)

    assert {"manifest_missing", "invalid_utf8"} <= set(codes(report.errors))


def test_document_mapping_keeps_offsets_and_null_publication() -> None:
    decomposed = "Hoàng hôn"  # "Hoàng" with a combining grave accent
    articles, manifest = batch(
        [
            record(
                content=decomposed,
                word_count=2,
                published_at=None,
            )
        ],
        published_at_min=None,
        published_at_max=None,
        missing_published_at_count=1,
    )
    report = validate_handoff(articles, manifest)
    assert report.ok
    assert "not_nfc" in codes(report.warnings)
    item = report.records[0]
    clock = IngestClock(
        ingested_at=datetime(2026, 10, 27, tzinfo=UTC),
        processed_at=datetime(2026, 10, 27, tzinfo=UTC),
    )

    document = document_from_handoff(item, clock)
    provenance = provenance_of(item, document, "unit-batch")

    assert document.raw_content == decomposed
    assert document.normalized_content == "Hoàng hôn"
    assert document.source_published_at is None
    assert document.ingested_at == clock.ingested_at
    assert provenance.article_version_id == item.article_version_id
    assert provenance.upstream_content_hash == "a" * 64
    assert provenance.rag_content_hash == document.content_hash
    assert provenance.rag_content_hash != f"sha256:{'a' * 64}"


@pytest.mark.parametrize("status", ["warning"])
def test_quality_status_outside_selection_is_rejected(status: str) -> None:
    articles, manifest = batch([record(quality_status=status)])

    report = validate_handoff(articles, manifest)

    assert "quality_status_outside_selection" in codes(report.errors)
    assert isinstance(report.records[0].article_id, UUID)
