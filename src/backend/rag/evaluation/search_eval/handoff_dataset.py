"""Benchmark dataset from an external handoff batch plus candidate labels.

Inputs are bytes the CLI read from operator-supplied paths (never a path baked
into code): ``articles.jsonl`` + ``manifest.json`` (``eventlens-rag-handoff-v1``)
and a ``search-questions.v1`` labels file. Nothing is cleaned or rewritten:

* the batch must pass ``validate_handoff`` (checksum, size, count, schema,
  identity, time, manifest counts); warnings and near-duplicates are kept;
* articles are the RAG revisions (``document_from_handoff``; NFC, RAG hash) --
  read back from PostgreSQL when the caller imported them first;
* labels must name this export and this exact corpus digest; gold passages are
  code-point spans on the RAG text, checked against ``excerpt_sha256``, never
  chunk IDs, so any chunker is scored from offsets;
* ``label_status`` / ``origin`` are copied verbatim; an AI prescreen verdict is
  a separate field and never upgrades a label;
* label problems (missing article, hash/offset mismatch, gold outside the
  question filter) are recorded as issues, not repaired.
"""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final, cast
from uuid import UUID

from backend.rag.application.handoff_validation import HandoffReport, validate_handoff
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.article_ingest import IngestClock
from backend.rag.domain.handoff import document_from_handoff
from backend.rag.evaluation.search_eval.dataset import (
    ANSWERABLE,
    ArticleFacts,
    EvalQuestion,
    PassageLabel,
    QuestionFilters,
    SearchDataset,
    corpus_digest_of,
)

QUESTIONS_SCHEMA: Final = "search-questions.v1"
HANDOFF_KIND: Final = "handoff"
SCOPE: Final = "fixture-internal"  # visibility "internal" (see VISIBILITY_SCOPES)
GOLD_GRADE: Final = 2
PRESCREEN_VERDICTS: Final = frozenset(
    {"no_obvious_issue", "needs_fix", "no_answer_needs_confirmation"}
)


class HandoffDatasetError(ValueError):
    """Batch invalid, labels for another corpus, or revisions do not match."""


@dataclass(frozen=True, slots=True)
class HandoffDataset:
    """Dataset plus the text-free validation report it was built from."""

    dataset: SearchDataset
    validation: HandoffReport


def _time(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


def _filters(raw: Mapping[str, object]) -> QuestionFilters:
    domains = cast("list[str]", raw.get("source_domains") or [])
    return QuestionFilters(
        source_domains=tuple(domains),
        published_after=_time(raw.get("published_after")),
        published_before=_time(raw.get("published_before")),
    )


def _inside(filters: QuestionFilters, document: ArticleDocument) -> bool:
    if filters.source_domains and document.source_domain not in filters.source_domains:
        return False
    published = document.source_published_at
    if filters.published_after is None and filters.published_before is None:
        return True
    if published is None:
        return False
    if filters.published_after is not None and published < filters.published_after:
        return False
    return not (
        filters.published_before is not None and published >= filters.published_before
    )


def _passages(
    qid: str,
    gold: list[dict[str, object]],
    by_id: Mapping[UUID, ArticleDocument],
    filters: QuestionFilters,
    issues: list[dict[str, str]],
) -> tuple[dict[UUID, int], list[PassageLabel]]:
    grades: dict[UUID, int] = {}
    passages: list[PassageLabel] = []
    for entry in gold:
        article_id = UUID(str(entry["article_id"]))
        grade = int(cast("int", entry["grade"]))
        grades[article_id] = grade
        document = by_id.get(article_id)
        if document is None:
            issues.append({"question_id": qid, "code": "gold_article_missing"})
            continue
        if entry["content_hash"] != document.content_hash:
            issues.append({"question_id": qid, "code": "gold_revision_mismatch"})
            continue
        if not _inside(filters, document):
            issues.append(
                {
                    "question_id": qid,
                    "code": "gold_outside_question_filter",
                    "detail": f"grade {grade} {document.source_domain}",
                }
            )
        text = document.normalized_content
        for span in cast("list[dict[str, object]]", entry["spans"]):
            start, end = int(cast("int", span["start"])), int(cast("int", span["end"]))
            if not 0 <= start < end <= len(text):
                issues.append({"question_id": qid, "code": "gold_span_out_of_range"})
                continue
            digest = sha256(text[start:end].encode()).hexdigest()
            if digest != span["excerpt_sha256"]:
                issues.append({"question_id": qid, "code": "gold_excerpt_mismatch"})
                continue
            passages.append(
                PassageLabel(
                    article_id=article_id,
                    content_hash=document.content_hash,
                    char_start=start,
                    char_end=end,
                    grade=grade,
                    temporal_correct=True,
                )
            )
    return grades, passages


def _questions(
    payload: Mapping[str, object],
    by_id: Mapping[UUID, ArticleDocument],
    prescreen: Mapping[str, str],
) -> tuple[tuple[EvalQuestion, ...], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    out: list[EvalQuestion] = []
    seen: set[str] = set()
    for row in cast("list[dict[str, object]]", payload["questions"]):
        qid = str(row["qid"])
        if qid in seen:
            msg = f"duplicate question_id {qid}"
            raise HandoffDatasetError(msg)
        seen.add(qid)
        filters = _filters(cast("dict[str, object]", row.get("filters") or {}))
        gold = cast("list[dict[str, object]]", row.get("gold") or [])
        grades, passages = _passages(qid, gold, by_id, filters, issues)
        answerable = bool(row["answerable"])
        if answerable and not any(g >= GOLD_GRADE for g in grades.values()):
            issues.append({"question_id": qid, "code": "answerable_without_gold"})
        if not answerable and gold:
            issues.append({"question_id": qid, "code": "no_answer_with_gold"})
        out.append(
            EvalQuestion(
                question_id=qid,
                query=str(row["question"]),
                # Labels carry a category, not a query intent.
                intent="unlabeled",
                answerability=ANSWERABLE if answerable else "no_answer",
                required_article_ids=tuple(
                    a for a, g in grades.items() if g >= GOLD_GRADE
                ),
                required_chunk_ids=(),
                required_claims=(),
                temporal_requirement=None,
                allowed_scope=SCOPE,
                article_grades=grades,
                passages=tuple(passages),
                split=str(row["split"]),
                category=str(row["category"]),
                label_status=str(row["label_status"]),
                origin=None if row.get("origin") is None else str(row["origin"]),
                ai_prescreen=prescreen.get(qid),
                filters=filters,
                reference_answer=None
                if row.get("reference_answer") is None
                else str(row["reference_answer"]),
            )
        )
    unknown = sorted(set(prescreen) - seen)
    if unknown:
        msg = f"prescreen names unknown questions: {unknown[:3]}"
        raise HandoffDatasetError(msg)
    return tuple(out), issues


def _documents(
    validation: HandoffReport, exported_at: datetime
) -> tuple[ArticleDocument, ...]:
    clock = IngestClock(ingested_at=exported_at, processed_at=exported_at)
    return tuple(
        sorted(
            (document_from_handoff(r, clock) for r in validation.records),
            key=lambda d: str(d.article_id),
        )
    )


def load_handoff_dataset(  # noqa: PLR0913 - explicit inputs, no defaults hidden
    articles: bytes,
    manifest: bytes,
    questions: bytes,
    *,
    prescreen: bytes | None = None,
    stored: tuple[ArticleDocument, ...] | None = None,
    expected_export_id: str | None = None,
) -> HandoffDataset:
    """Validate the batch, bind labels to its exact revisions, build the dataset.

    ``stored`` are the revisions read back from PostgreSQL after import; they
    must be exactly the batch's RAG revisions (same IDs and hashes) or the
    run stops, so a report can never silently score another corpus.
    """
    validation = validate_handoff(articles, manifest)
    if not validation.ok or validation.export_id is None:
        codes = sorted({f.code for f in validation.errors})
        msg = f"handoff batch rejected: {codes}"
        raise HandoffDatasetError(msg)
    if expected_export_id is not None and validation.export_id != expected_export_id:
        msg = f"export {validation.export_id!r} != expected {expected_export_id!r}"
        raise HandoffDatasetError(msg)
    raw_manifest = cast("dict[str, object]", json.loads(manifest))
    exported_at = datetime.fromisoformat(str(raw_manifest["exported_at"]))
    mapped = _documents(validation, exported_at)
    documents = mapped
    if stored is not None:
        documents = tuple(sorted(stored, key=lambda d: str(d.article_id)))
        if [(d.article_id, d.content_hash) for d in documents] != [
            (d.article_id, d.content_hash) for d in mapped
        ]:
            msg = "stored revisions differ from the handoff batch"
            raise HandoffDatasetError(msg)
    digest = corpus_digest_of(documents)
    payload = cast("dict[str, object]", json.loads(questions))
    if payload.get("schema_version") != QUESTIONS_SCHEMA:
        msg = f"labels schema must be {QUESTIONS_SCHEMA}"
        raise HandoffDatasetError(msg)
    if payload.get("corpus_export_id") != validation.export_id:
        msg = "labels were written for another export"
        raise HandoffDatasetError(msg)
    if payload.get("corpus_digest") != digest:
        msg = "labels were written for another corpus digest"
        raise HandoffDatasetError(msg)
    verdicts = (
        {}
        if prescreen is None
        else cast("dict[str, str]", json.loads(prescreen)["verdicts"])
    )
    if set(verdicts.values()) - PRESCREEN_VERDICTS:
        msg = "unknown prescreen verdict"
        raise HandoffDatasetError(msg)
    by_id = {d.article_id: d for d in documents}
    eval_questions, issues = _questions(payload, by_id, verdicts)
    records = {r.article_id: r for r in validation.records}
    facts = {
        d.article_id: ArticleFacts(
            source_domain=d.source_domain,
            parse_version=records[d.article_id].parse_version,
            normalization_version=records[d.article_id].normalization_version,
            upstream_content_hash=records[d.article_id].content_hash,
            chars=len(d.normalized_content),
        )
        for d in documents
    }
    statuses = Counter(q.label_status for q in eval_questions)
    label_digest = f"sha256:{sha256(questions).hexdigest()}"
    dataset = SearchDataset(
        articles=documents,
        questions=eval_questions,
        manifest={
            "version": f"{validation.export_id}/{QUESTIONS_SCHEMA}",
            "labels": ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            + "; machine-drafted unless label_status says otherwise",
            "split_seed": payload.get("split_seed"),
            "label_chunker_version": payload.get("chunker_version"),
        },
        fixture_digest=label_digest,
        corpus_digest=digest,
        kind=HANDOFF_KIND,
        as_of=exported_at,
        export_id=validation.export_id,
        articles_sha256=validation.articles_sha256,
        label_digest=label_digest,
        label_issues=tuple(issues),
        facts=facts,
    )
    return HandoffDataset(dataset=dataset, validation=validation)
