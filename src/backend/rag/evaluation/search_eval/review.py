"""Review sheet: pooled top-k candidates per question for human relevance labels.

Every configuration contributes its top ``depth`` chunks; the union is written
as CSV (one row per question x chunk) plus a static HTML table. Existing
author labels are shown so a reviewer can confirm or correct them; the
``reviewer_grade`` column (0/1/2) is left blank on purpose.
"""

import csv
import html
import io
from collections.abc import Sequence
from dataclasses import dataclass

from backend.rag.evaluation.search_eval.dataset import EvalQuestion, SearchDataset
from backend.rag.evaluation.search_eval.scoring import RankedPassage
from backend.rag.evaluation.search_eval.systems import SearchSystem

COLUMNS = (
    "question_id",
    "query",
    "intent",
    "answerability",
    "article_id",
    "chunk_id",
    "char_start",
    "char_end",
    "found_by",
    "best_rank",
    "author_grade",
    "reviewer_grade",
    "reviewer_note",
    "excerpt",
)


@dataclass(frozen=True, slots=True)
class ReviewSheet:
    """CSV and HTML renderings plus their row count."""

    csv_text: str
    html_text: str
    rows: int


def _author_grade(question: EvalQuestion, passage: RankedPassage) -> str:
    grades = [
        p.grade
        for p in question.passages
        if p.article_id == passage.article_id
        and passage.char_start < p.char_end
        and p.char_start < passage.char_end
    ]
    if grades:
        return str(max(grades))
    article = question.article_grades.get(passage.article_id)
    return "" if article is None else f"article:{article}"


def build_review_sheet(
    dataset: SearchDataset, systems: Sequence[SearchSystem], depth: int = 10
) -> ReviewSheet:
    """Run each system once and pool its top ``depth`` passages."""
    pooled: dict[tuple[str, str], tuple[RankedPassage, set[str], int]] = {}
    for system in systems:
        _ = system.prepare(dataset.articles)
        try:
            for question in dataset.questions:
                answer = system.search(question)
                for position, passage in enumerate(
                    [p for p in answer.ranked if not p.trap][:depth], 1
                ):
                    key = (question.question_id, str(passage.chunk_id))
                    found = pooled.get(key)
                    if found is None:
                        pooled[key] = (passage, {system.name}, position)
                    else:
                        found[1].add(system.name)
                        pooled[key] = (found[0], found[1], min(found[2], position))
        finally:
            system.close()
    by_id = {q.question_id: q for q in dataset.questions}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    rows: list[list[str]] = []
    for (qid, chunk), (passage, found_by, rank) in sorted(
        pooled.items(), key=lambda item: (item[0][0], item[1][2], item[0][1])
    ):
        question = by_id[qid]
        rows.append(
            [
                qid,
                question.query,
                question.intent,
                question.answerability,
                str(passage.article_id),
                chunk,
                str(passage.char_start),
                str(passage.char_end),
                ";".join(sorted(found_by)),
                str(rank),
                _author_grade(question, passage),
                "",
                "",
                passage.text[:400],
            ]
        )
    writer.writerows(rows)
    header = "".join(f"<th>{html.escape(c)}</th>" for c in COLUMNS)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    page = (
        "<!doctype html><meta charset='utf-8'><title>Search review sheet</title>"
        "<style>body{font:14px system-ui;margin:16px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:4px;vertical-align:top}</style>"
        f"<p>{len(rows)} pooled candidates; fill reviewer_grade 0/1/2.</p>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"
    )
    return ReviewSheet(buffer.getvalue(), page, len(rows))
