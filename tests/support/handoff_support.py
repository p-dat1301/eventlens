"""Synthetic ``eventlens-rag-handoff-v1`` batches for tests (no publisher text)."""

import json
from collections import Counter
from hashlib import sha256
from uuid import uuid4

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
