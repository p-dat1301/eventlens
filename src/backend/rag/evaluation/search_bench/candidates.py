"""Turn machine-drafted candidates into a ``search-questions.v1`` set.

Candidates quote exact passages; this module locates each quote in the NFC
content of the cited revision and stores only offsets + excerpt digest. A
quote that is missing or ambiguous (occurs more than once) is a hard error so
no label silently points at the wrong passage. Splits are deterministic:
within each category, questions are ordered by SHA-256 of ``seed:qid`` and the
first ``round(dev_fraction * n)`` go to dev.
"""

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import cast
from unicodedata import normalize
from uuid import UUID

from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.domain.handoff import HandoffArticle
from backend.rag.evaluation.search_bench.labels import (
    GoldArticle,
    GoldSpan,
    QuestionFilters,
    QuestionSet,
    SearchQuestion,
    excerpt_digest,
)


class CandidateError(ValueError):
    """A candidate cannot be resolved against the corpus."""


@dataclass(frozen=True, slots=True)
class Revision:
    """NFC text and RAG hash of one article (offsets refer to this text)."""

    content: str
    content_hash: str
    source_domain: str = ""
    published_at: datetime | None = None


def in_filters(revision: Revision, filters: QuestionFilters) -> bool:
    """Whether a gold article satisfies the question's own scope.

    Same semantics as ``RetrievalScope``: after inclusive, before exclusive,
    unknown publication time excluded once any bound is set. A gold article
    outside the scope can never be retrieved and would cap recall.
    """
    if filters.source_domains and revision.source_domain not in filters.source_domains:
        return False
    published = revision.published_at
    if filters.published_after is None and filters.published_before is None:
        return True
    if published is None:
        return False
    if filters.published_after is not None and published < filters.published_after:
        return False
    return filters.published_before is None or published < filters.published_before


def check_scope(question: SearchQuestion, corpus: Mapping[UUID, Revision]) -> None:
    """Reject gold articles that the question's filters exclude."""
    for gold in question.gold:
        if not in_filters(corpus[gold.article_id], question.filters):
            msg = f"{question.qid}: gold article {gold.article_id} is outside filters"
            raise CandidateError(msg)


def revisions(records: Sequence[HandoffArticle]) -> dict[UUID, Revision]:
    """RAG revisions of the handoff records."""
    out: dict[UUID, Revision] = {}
    for record in records:
        text = normalize("NFC", record.content)
        out[record.article_id] = Revision(
            text,
            normalized_content_hash(text),
            record.source_domain,
            record.published_at,
        )
    return out


def corpus_digest(pairs: Sequence[tuple[UUID, str]]) -> str:
    """SHA-256 over sorted ``article_id:content_hash`` lines."""
    lines = sorted(f"{article_id}:{content_hash}" for article_id, content_hash in pairs)
    return f"sha256:{sha256(chr(10).join(lines).encode()).hexdigest()}"


def _span(revision: Revision, quote: str, qid: str) -> GoldSpan:
    text = normalize("NFC", quote)
    start = revision.content.find(text)
    if start < 0:
        msg = f"{qid}: quote not found in cited revision"
        raise CandidateError(msg)
    if revision.content.find(text, start + 1) >= 0:
        msg = f"{qid}: quote occurs more than once; extend it"
        raise CandidateError(msg)
    return GoldSpan(
        start=start, end=start + len(text), excerpt_sha256=excerpt_digest(text)
    )


def _time(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def question_from_candidate(
    raw: Mapping[str, object], corpus: Mapping[UUID, Revision], split: str
) -> SearchQuestion:
    """Resolve one candidate (``candidate_id``, ``gold[].quotes``) to labels."""
    qid = str(raw["candidate_id"])
    gold: list[GoldArticle] = []
    for item in cast("list[dict[str, object]]", raw.get("gold") or []):
        article_id = UUID(str(item["article_id"]))
        revision = corpus.get(article_id)
        if revision is None:
            msg = f"{qid}: cited article not in corpus"
            raise CandidateError(msg)
        quotes = cast("list[str]", item.get("quotes") or [])
        gold.append(
            GoldArticle(
                article_id=article_id,
                content_hash=revision.content_hash,
                grade=2 if int(cast("int", item["grade"])) == 2 else 1,  # noqa: PLR2004
                spans=tuple(_span(revision, quote, qid) for quote in quotes),
            )
        )
    filters = cast("dict[str, object]", raw.get("filters") or {})
    category = str(raw["category"])
    answerable = bool(raw.get("answerable", category != "no_answer"))
    return SearchQuestion.model_validate(
        {
            "qid": qid,
            "question": normalize("NFC", str(raw["question"])),
            "category": category,
            "split": split,
            "answerable": answerable,
            "filters": QuestionFilters(
                source_domains=tuple(
                    cast("list[str]", filters.get("source_domains") or [])
                ),
                published_after=_time(filters.get("published_after")),
                published_before=_time(filters.get("published_before")),
            ),
            "gold": tuple(gold),
            "reference_answer": raw.get("reference_answer"),
            "label_status": "candidate",
            "origin": "machine_candidate",
            "notes": "machine-drafted candidate; not reviewed",
        }
    )


def assign_splits(
    candidates: Sequence[Mapping[str, object]], seed: str, dev_fraction: float
) -> dict[str, str]:
    """Stratified deterministic dev/test assignment by category."""
    by_category: dict[str, list[str]] = defaultdict(list)
    for raw in candidates:
        by_category[str(raw["category"])].append(str(raw["candidate_id"]))
    splits: dict[str, str] = {}
    for qids in by_category.values():
        ordered = sorted(qids, key=lambda q: sha256(f"{seed}:{q}".encode()).hexdigest())
        cut = round(dev_fraction * len(ordered))
        for index, qid in enumerate(ordered):
            splits[qid] = "dev" if index < cut else "test"
    return splits


def build_question_set(  # noqa: PLR0913 - explicit snapshot identity
    candidates: Sequence[Mapping[str, object]],
    records: Sequence[HandoffArticle],
    *,
    export_id: str,
    chunker_version: str,
    seed: str,
    dev_fraction: float,
) -> QuestionSet:
    """Resolve every candidate; any unresolved quote fails the whole build."""
    corpus = revisions(records)
    splits = assign_splits(candidates, seed, dev_fraction)
    questions = tuple(
        question_from_candidate(raw, corpus, splits[str(raw["candidate_id"])])
        for raw in sorted(candidates, key=lambda r: str(r["candidate_id"]))
    )
    for question in questions:
        check_scope(question, corpus)
    return QuestionSet(
        schema_version="search-questions.v1",
        corpus_export_id=export_id,
        corpus_digest=corpus_digest(
            [(article_id, rev.content_hash) for article_id, rev in corpus.items()]
        ),
        chunker_version=chunker_version,
        split_seed=seed,
        dev_fraction=dev_fraction,
        questions=questions,
    )


def load_candidates(texts: Sequence[str]) -> list[dict[str, object]]:
    """Parse JSONL candidate files."""
    return [
        cast("dict[str, object]", json.loads(line))
        for text in texts
        for line in text.splitlines()
        if line.strip()
    ]
