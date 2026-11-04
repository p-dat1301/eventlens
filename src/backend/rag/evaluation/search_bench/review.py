"""Human review of candidate labels: private HTML sheet + decision merge.

The sheet shows each question with its candidate gold passages in context and
a pool of articles retrieved by any benchmarked system (TREC-style pooling),
so a reviewer can grade articles the drafter missed. It contains article text
and must stay in private storage. The reviewer exports ``review-decisions.json``
(``review-decisions.v1``); ``apply_review`` merges it into the question set and
re-resolves every quote to offsets, so reviewed labels obey the same exactness
rules as candidates.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from importlib.resources import files
from typing import Literal, cast
from uuid import UUID

from pydantic import Field

from backend.rag.evaluation.search_bench.candidates import (
    CandidateError,
    Revision,
    check_scope,
)
from backend.rag.evaluation.search_bench.labels import (
    GoldArticle,
    GoldSpan,
    QuestionSet,
    SearchQuestion,
    excerpt_digest,
)
from backend.shared.contracts.base import ContractModel

POOL_DEPTH = 5


class ArticleView(ContractModel):
    """What the reviewer sees about one article."""

    title: str
    source_domain: str
    published_at: str | None
    revision_hash: str
    url: str | None = None


class ArticleDecision(ContractModel):
    """Grade for one article; grade-2 needs at least one exact quote."""

    article_id: UUID
    grade: Literal[0, 1, 2]
    quotes: tuple[str, ...] = ()


class QuestionDecision(ContractModel):
    """Reviewer verdict for one question."""

    qid: str
    status: Literal["accept", "edit", "reject"]
    question: str | None = None
    answerable: bool | None = None
    articles: tuple[ArticleDecision, ...] = ()
    notes: str = ""


class ReviewDecisions(ContractModel):
    """Exported by the review sheet."""

    schema_version: Literal["review-decisions.v1"]
    corpus_digest: str
    decisions: tuple[QuestionDecision, ...] = Field(default_factory=tuple)


def pool_from_report(
    report: Mapping[str, object], depth: int = POOL_DEPTH
) -> dict[str, list[UUID]]:
    """Union of each system's top-``depth`` articles per question."""
    pools: dict[str, list[UUID]] = {}
    per_question = cast(
        "Mapping[str, Sequence[Mapping[str, object]]]", report["per_question"]
    )
    for rows in per_question.values():
        for row in rows:
            bucket = pools.setdefault(str(row["qid"]), [])
            for article in cast("Sequence[str]", row["top_articles"])[:depth]:
                article_id = UUID(article)
                if article_id not in bucket:
                    bucket.append(article_id)
    return pools


def _payload(
    question_set: QuestionSet,
    revisions: Mapping[UUID, Revision],
    views: Mapping[UUID, ArticleView],
    pools: Mapping[str, Sequence[UUID]],
) -> dict[str, object]:
    """Data the sheet renders: each article's text once, questions by ID."""
    used: set[UUID] = set()
    questions: list[dict[str, object]] = []
    for q in question_set.questions:
        gold = {g.article_id: g for g in q.gold}
        order = [*gold, *(a for a in pools.get(q.qid, ()) if a not in gold)]
        articles: list[dict[str, object]] = []
        for article_id in order:
            if article_id not in revisions or article_id not in views:
                continue
            used.add(article_id)
            item = gold.get(article_id)
            text = revisions[article_id].content
            spans = [] if item is None else [[s.start, s.end] for s in item.spans]
            articles.append(
                {
                    "id": str(article_id),
                    "origin": "gold" if item is not None else "pool",
                    "grade": 0 if item is None else item.grade,
                    "spans": spans,
                    "quotes": [text[start:end] for start, end in spans],
                }
            )
        questions.append(
            {
                "qid": q.qid,
                "question": q.question,
                "category": q.category,
                "split": q.split,
                "answerable": q.answerable,
                "reference_answer": q.reference_answer,
                "filters": json.loads(q.filters.model_dump_json()),
                "articles": articles,
            }
        )
    return {
        "corpus_digest": question_set.corpus_digest,
        "sheet_id": sha256(
            "\n".join(q.qid for q in question_set.questions).encode()
        ).hexdigest()[:12],
        "questions": questions,
        "articles": {
            str(article_id): {
                "title": views[article_id].title,
                "source_domain": views[article_id].source_domain,
                "published_at": views[article_id].published_at,
                "url": views[article_id].url,
                "content": revisions[article_id].content,
            }
            for article_id in sorted(used, key=str)
        },
    }


def build_review_sheet(
    question_set: QuestionSet,
    revisions: Mapping[UUID, Revision],
    views: Mapping[UUID, ArticleView],
    pools: Mapping[str, Sequence[UUID]],
) -> str:
    """Self-contained private HTML (template ``review_sheet.html``).

    Contains full article text. Data is embedded as JSON and rendered with
    ``textContent``; ``<`` is escaped so text can never close the script tag.
    Progress is kept in the viewer's ``localStorage`` (per sheet) and only
    questions with an explicit verdict are exported.
    """
    data = json.dumps(
        _payload(question_set, revisions, views, pools),
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    template = (
        files("backend.rag.evaluation.search_bench")
        .joinpath("review_sheet.html")
        .read_text(encoding="utf-8")
    )
    return template.replace("__DATA__", data)


def _span(revision: Revision, quote: str, qid: str) -> GoldSpan:
    start = revision.content.find(quote)
    if start < 0 or revision.content.find(quote, start + 1) >= 0:
        msg = f"{qid}: reviewed quote missing or ambiguous"
        raise CandidateError(msg)
    return GoldSpan(
        start=start, end=start + len(quote), excerpt_sha256=excerpt_digest(quote)
    )


def apply_review(
    question_set: QuestionSet,
    decisions: ReviewDecisions,
    revisions: Mapping[UUID, Revision],
    *,
    reviewer: str,
    reviewed_at: datetime,
) -> QuestionSet:
    """Merge decisions; undecided questions stay ``candidate``."""
    if decisions.corpus_digest != question_set.corpus_digest:
        msg = "decisions were made on a different corpus snapshot"
        raise CandidateError(msg)
    by_qid = {d.qid: d for d in decisions.decisions}
    merged: list[SearchQuestion] = []
    for q in question_set.questions:
        decision = by_qid.get(q.qid)
        if decision is None:
            merged.append(q)
            continue
        if decision.status == "reject":
            merged.append(
                q.model_copy(
                    update={
                        "label_status": "reviewed_rejected",
                        "reviewer": reviewer,
                        "reviewed_at": reviewed_at,
                        "notes": decision.notes,
                    }
                )
            )
            continue
        gold = tuple(
            GoldArticle(
                article_id=item.article_id,
                content_hash=revisions[item.article_id].content_hash,
                grade=2 if item.grade == 2 else 1,  # noqa: PLR2004
                spans=tuple(
                    _span(revisions[item.article_id], quote, q.qid)
                    for quote in item.quotes
                ),
            )
            for item in decision.articles
            if item.grade > 0
        )
        answerable = (
            q.answerable if decision.answerable is None else decision.answerable
        )
        question = decision.question or q.question
        edited = (
            decision.status == "edit"
            or question != q.question
            or answerable != q.answerable
            or gold != q.gold
        )
        merged.append(
            SearchQuestion.model_validate(
                {
                    **q.model_dump(),
                    "question": question,
                    "answerable": answerable,
                    "gold": gold if answerable else (),
                    "label_status": "reviewed_edited"
                    if edited
                    else "reviewed_accepted",
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "notes": decision.notes,
                }
            )
        )
        check_scope(merged[-1], revisions)
    return question_set.model_copy(update={"questions": tuple(merged)})
