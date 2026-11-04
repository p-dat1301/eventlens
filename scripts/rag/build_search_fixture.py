"""Build ``fixtures/rag/search-selection`` from the authored synthetic corpus.

Usage: ``uv run python scripts/rag/build_search_fixture.py [--check]``.
Deterministic: IDs are UUIDv5 of the authored keys, offsets are computed from
the exact NFC text, ``required_chunk_ids`` use ``paragraph-900-1.v1``. With
``--check`` nothing is written and a difference exits 1.
"""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from unicodedata import normalize
from uuid import UUID, uuid5

from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.domain.paragraph_chunking import (
    DEFAULT_PARAGRAPH_POLICY,
    chunk_paragraphs,
)
from backend.rag.domain.search_index import search_chunk_id

sys.path.insert(0, str(Path(__file__).parent))
from search_fixture_corpus import ARTICLES, QUESTIONS  # noqa: E402

ROOT = Path(__file__).parents[2]
OUT = ROOT / "fixtures/rag/search-selection"
NAMESPACE = UUID("8a3b1c2d-4e5f-4a6b-9c7d-0e1f2a3b4c5d")
INGESTED = "2026-04-10T00:00:00Z"
SCOPE = "fixture-internal"


def article_id(key: str) -> UUID:
    return uuid5(NAMESPACE, f"article:{key}")


DEFAULT_META: dict[str, object] = {
    "dataset_id": "eventlens-search-selection",
    "version": "search-selection.v1",
    "labels": "author-labeled (single annotator); not independently reviewed",
}


def build(  # noqa: C901, PLR0912 - one pass over the authored corpus
    authored_articles: tuple[tuple[object, ...], ...] = ARTICLES,
    authored_questions: tuple[tuple[object, ...], ...] = QUESTIONS,
    meta: dict[str, object] | None = None,
) -> dict[str, str]:
    """Fixture files; ``meta`` overrides dataset id/version/labels (+query clock)."""
    meta = {**DEFAULT_META, **(meta or {})}
    articles: list[dict[str, object]] = []
    texts: dict[str, tuple[UUID, str, str]] = {}
    for row in authored_articles:
        key, domain, source, published, title, paragraphs = row  # pyright: ignore[reportAssignmentType]
        text = normalize("NFC", "\n\n".join(paragraphs))
        digest = normalized_content_hash(text)
        aid = article_id(key)
        texts[key] = (aid, text, digest)
        articles.append(
            {
                "schema_version": "article_document.v1",
                "article_id": str(aid),
                "source_id": str(uuid5(NAMESPACE, f"source:{domain}")),
                "title": normalize("NFC", title),
                "raw_content": text,
                "normalization_version": "nfc.v1",
                "normalized_content": text,
                "canonical_url": f"https://{domain}/synthetic/{key}",
                "source_name": source,
                "source_domain": domain,
                "source_published_at": published,
                "ingested_at": INGESTED,
                "processed_at": INGESTED,
                "content_hash": digest,
                "content_fingerprint": digest,
                "visibility": "internal",
                "evidence_spans": [],
            }
        )
    questions: list[dict[str, object]] = []
    qrels: list[dict[str, object]] = []
    for row in authored_questions:
        qid, query, intent, answerability, evidence, claims, temporal = row[:7]  # pyright: ignore[reportAssignmentType]
        extras = row[7] if len(row) > 7 else {}  # noqa: PLR2004 - optional extras
        required_articles: list[str] = []
        required_chunks: list[str] = []
        graded: dict[str, int] = {}
        for key, quote, grade in evidence:
            aid, text, digest = texts[key]
            quote_nfc = normalize("NFC", quote)
            start = text.index(quote_nfc)
            end = start + len(quote_nfc)
            graded[str(aid)] = max(graded.get(str(aid), 0), grade)
            temporal_ok = temporal != "latest" or grade == 2
            qrels.append(
                {
                    "question_id": qid,
                    "level": "passage",
                    "article_id": str(aid),
                    "content_hash": digest,
                    "char_start": start,
                    "char_end": end,
                    "quote": quote_nfc,
                    "grade": grade,
                    "answerability": answerability,
                    "temporal_correct": temporal_ok,
                    "citation_support": "exact" if grade == 2 else "partial",
                }
            )
            if grade == 2 and answerability == "answerable":
                if str(aid) not in required_articles:
                    required_articles.append(str(aid))
                for chunk in chunk_paragraphs(aid, digest, text):
                    if chunk.char_start <= start and end <= chunk.char_end:
                        cid = str(
                            search_chunk_id(
                                aid, digest, chunk.chunker_version, chunk.index
                            )
                        )
                        if cid not in required_chunks:
                            required_chunks.append(cid)
        for aid, grade in graded.items():
            qrels.append(
                {
                    "question_id": qid,
                    "level": "article",
                    "article_id": aid,
                    "grade": grade,
                    "answerability": answerability,
                    "temporal_correct": temporal != "latest" or grade == 2,
                }
            )
        extra = {
            k: v for k, v in extras.items() if k in {"category", "split", "filters"}
        }  # pyright: ignore[reportAttributeAccessIssue]
        questions.append(
            {
                **extra,
                "question_id": qid,
                "query": normalize("NFC", query),
                "intent": intent,
                "answerability": answerability,
                "required_article_ids": required_articles,
                "required_chunk_ids": required_chunks,
                "required_claims": [normalize("NFC", c) for c in claims],
                "temporal_requirement": temporal,
                "allowed_scope": SCOPE,
            }
        )
    files = {
        "articles.jsonl": "".join(
            json.dumps(a, ensure_ascii=False, sort_keys=True) + "\n" for a in articles
        ),
        "questions.jsonl": "".join(
            json.dumps(q, ensure_ascii=False, sort_keys=True) + "\n" for q in questions
        ),
        "qrels.jsonl": "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in qrels
        ),
    }
    digests = {
        name: "sha256:" + sha256(body.encode()).hexdigest()
        for name, body in files.items()
    }
    manifest = {
        **meta,
        "synthetic": True,
        "license": "authored for EventLens tests; no publisher text",
        "chunk_ids_for": DEFAULT_PARAGRAPH_POLICY.version,
        "counts": {
            "articles": len(articles),
            "questions": len(questions),
            "qrels": len(qrels),
        },
        "files": digests,
        "corpus_digest": "sha256:"
        + sha256(
            "".join(sorted(str(a["content_hash"]) for a in articles)).encode()
        ).hexdigest(),
    }
    files["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = build()
    if args.check:
        stale = [
            name
            for name, body in files.items()
            if not (OUT / name).is_file()
            or (OUT / name).read_text(encoding="utf-8") != body
        ]
        if stale:
            print(f"search-selection fixture out of date: {stale}", file=sys.stderr)  # noqa: T201
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        _ = (OUT / name).write_text(body, encoding="utf-8")
    print(f"wrote {sorted(files)} to {OUT}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
