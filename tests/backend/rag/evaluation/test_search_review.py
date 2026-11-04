from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.evaluation.search_bench.candidates import CandidateError, Revision
from backend.rag.evaluation.search_bench.labels import (
    GoldArticle,
    GoldSpan,
    QuestionFilters,
    QuestionSet,
    SearchQuestion,
    excerpt_digest,
)
from backend.rag.evaluation.search_bench.review import (
    ArticleDecision,
    ArticleView,
    QuestionDecision,
    ReviewDecisions,
    apply_review,
    build_review_sheet,
    pool_from_report,
)

A = UUID("00000000-0000-4000-8000-0000000000a1")
B = UUID("00000000-0000-4000-8000-0000000000b2")
TEXT_A = "Mở đầu. Giá vàng tăng 2% hôm qua. Kết thúc."
TEXT_B = "Bài khác: vàng tăng 2% theo báo cáo."
REVISIONS = {
    A: Revision(TEXT_A, normalized_content_hash(TEXT_A)),
    B: Revision(TEXT_B, normalized_content_hash(TEXT_B)),
}
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def question_set() -> QuestionSet:
    start = TEXT_A.index("Giá vàng")
    end = start + len("Giá vàng tăng 2% hôm qua.")
    q = SearchQuestion(
        qid="q1",
        question="Giá vàng tăng bao nhiêu?",
        category="datetime",
        split="test",
        answerable=True,
        filters=QuestionFilters(),
        gold=(
            GoldArticle(
                article_id=A,
                content_hash=REVISIONS[A].content_hash,
                grade=2,
                spans=(
                    GoldSpan(
                        start=start,
                        end=end,
                        excerpt_sha256=excerpt_digest(TEXT_A[start:end]),
                    ),
                ),
            ),
        ),
        reference_answer="2%",
        label_status="candidate",
        origin="machine_candidate",
    )
    return QuestionSet(
        schema_version="search-questions.v1",
        corpus_export_id="e",
        corpus_digest=normalized_content_hash("digest"),
        chunker_version="c",
        split_seed="s",
        dev_fraction=0.4,
        questions=(q, q.model_copy(update={"qid": "q2"})),
    )


def test_pool_is_union_of_top_articles_across_systems() -> None:
    report: dict[str, object] = {
        "per_question": {
            "A": [{"qid": "q1", "top_articles": [str(A), str(B)]}],
            "B": [{"qid": "q1", "top_articles": [str(B)]}],
        }
    }

    assert pool_from_report(report, depth=1) == {"q1": [A, B]}


def _views() -> dict[UUID, ArticleView]:
    return {
        A: ArticleView(
            title="<script>x()</script>",
            source_domain="a.vn",
            published_at=None,
            revision_hash=REVISIONS[A].content_hash,
        ),
        B: ArticleView(
            title="Bài B",
            source_domain="b.vn",
            published_at="2026-10-01",
            revision_hash=REVISIONS[B].content_hash,
        ),
    }


def _embedded(sheet: str) -> dict[str, object]:
    start = sheet.index('<script id="data" type="application/json">') + len(
        '<script id="data" type="application/json">'
    )
    end = sheet.index("</script>", start)
    return TypeAdapter(dict[str, object]).validate_json(sheet[start:end])


def test_sheet_embeds_escaped_data_with_full_text_and_pool() -> None:
    sheet = build_review_sheet(question_set(), REVISIONS, _views(), {"q1": [B]})

    assert "<script>x()</script>" not in sheet
    data = _embedded(sheet)
    articles = cast("dict[str, dict[str, object]]", data["articles"])
    assert articles[str(A)]["title"] == "<script>x()</script>"
    assert articles[str(A)]["content"] == TEXT_A
    questions = cast("list[dict[str, object]]", data["questions"])
    first = cast("list[dict[str, object]]", questions[0]["articles"])
    assert [a["origin"] for a in first] == ["gold", "pool"]
    assert first[0]["quotes"] == ["Giá vàng tăng 2% hôm qua."]
    assert data["corpus_digest"] == question_set().corpus_digest


def test_apply_review_accept_edit_reject_and_new_gold() -> None:
    decisions = ReviewDecisions(
        schema_version="review-decisions.v1",
        corpus_digest=question_set().corpus_digest,
        decisions=(
            QuestionDecision(
                qid="q1",
                status="accept",
                articles=(
                    ArticleDecision(
                        article_id=A, grade=2, quotes=("Giá vàng tăng 2% hôm qua.",)
                    ),
                    ArticleDecision(article_id=B, grade=2, quotes=("vàng tăng 2%",)),
                ),
            ),
            QuestionDecision(qid="q2", status="reject", notes="mơ hồ"),
        ),
    )

    merged = apply_review(
        question_set(), decisions, REVISIONS, reviewer="SẾP", reviewed_at=NOW
    )

    first, second = merged.questions
    assert first.label_status == "reviewed_edited"  # B was added
    assert {g.article_id for g in first.gold} == {A, B}
    span = next(g for g in first.gold if g.article_id == B).spans[0]
    assert TEXT_B[span.start : span.end] == "vàng tăng 2%"
    assert first.reviewer == "SẾP"
    assert second.label_status == "reviewed_rejected"


def test_unchanged_accept_is_reviewed_accepted() -> None:
    decisions = ReviewDecisions(
        schema_version="review-decisions.v1",
        corpus_digest=question_set().corpus_digest,
        decisions=(
            QuestionDecision(
                qid="q1",
                status="accept",
                articles=(
                    ArticleDecision(
                        article_id=A, grade=2, quotes=("Giá vàng tăng 2% hôm qua.",)
                    ),
                ),
            ),
        ),
    )

    merged = apply_review(
        question_set(), decisions, REVISIONS, reviewer="r", reviewed_at=NOW
    )

    assert [q.label_status for q in merged.questions] == [
        "reviewed_accepted",
        "candidate",
    ]


def test_review_rejects_bad_quotes_and_foreign_corpus() -> None:
    bad_quote = ReviewDecisions(
        schema_version="review-decisions.v1",
        corpus_digest=question_set().corpus_digest,
        decisions=(
            QuestionDecision(
                qid="q1",
                status="edit",
                articles=(
                    ArticleDecision(article_id=A, grade=2, quotes=("không có",)),
                ),
            ),
        ),
    )
    with pytest.raises(CandidateError, match="missing or ambiguous"):
        _ = apply_review(
            question_set(), bad_quote, REVISIONS, reviewer="r", reviewed_at=NOW
        )
    foreign = bad_quote.model_copy(
        update={"corpus_digest": normalized_content_hash("other")}
    )
    with pytest.raises(CandidateError, match="different corpus"):
        _ = apply_review(
            question_set(), foreign, REVISIONS, reviewer="r", reviewed_at=NOW
        )


def test_sheet_starts_unreviewed_and_exports_only_decided_questions() -> None:
    sheet = build_review_sheet(question_set(), REVISIONS, _views(), {})

    assert 'status: ""' in sheet
    assert "if (!s.status) continue;" in sheet
    assert "localStorage" in sheet


def test_reviewed_gold_outside_question_filters_is_rejected() -> None:
    scoped = question_set().model_copy(
        update={
            "questions": (
                question_set()
                .questions[0]
                .model_copy(
                    update={"filters": QuestionFilters(source_domains=("a.vn",))}
                ),
            )
        }
    )
    corpus = {
        A: Revision(TEXT_A, REVISIONS[A].content_hash, "a.vn", None),
        B: Revision(TEXT_B, REVISIONS[B].content_hash, "b.vn", None),
    }
    decisions = ReviewDecisions(
        schema_version="review-decisions.v1",
        corpus_digest=scoped.corpus_digest,
        decisions=(
            QuestionDecision(
                qid="q1",
                status="accept",
                articles=(
                    ArticleDecision(
                        article_id=A, grade=2, quotes=("Giá vàng tăng 2% hôm qua.",)
                    ),
                    ArticleDecision(article_id=B, grade=1),
                ),
            ),
        ),
    )

    with pytest.raises(CandidateError, match="outside filters"):
        _ = apply_review(scoped, decisions, corpus, reviewer="r", reviewed_at=NOW)
