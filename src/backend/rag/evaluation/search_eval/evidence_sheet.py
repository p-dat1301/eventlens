"""Private review sheet: question, labels, gold quotes and retrieved chunks.

Holds publisher text, so it is written only where the operator points it
(``.private/...``, gitignored) and never into a report. Per question:
question text, label status (verbatim), AI prescreen verdict (separate from
the label), filters, the reference answer marked as a machine summary, every
gold quote cut verbatim from the RAG text by its offsets, and the top
retrieved chunks of every configuration with source/date metadata, gold
coverage, a link to a full-article page and the original URL. Reviewer
columns in ``review.csv`` are blank on purpose.
"""

import csv
import html
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from backend.rag.domain.article_document import ArticleDocument
from backend.rag.evaluation.search_eval.dataset import (
    EvalQuestion,
    PassageLabel,
    SearchDataset,
)
from backend.rag.evaluation.search_eval.scoring import RankedPassage
from backend.rag.evaluation.search_eval.systems import SystemAnswer

DEPTH: Final = 5
REFERENCE_NOTE: Final = (
    "Đáp án tham chiếu là BẢN TỔNG HỢP do máy soạn khi tạo nhãn, không phải "
    "trích dẫn nguyên văn và chưa được người duyệt."
)
CSV_COLUMNS: Final = (
    "question_id",
    "split",
    "category",
    "label_status",
    "ai_prescreen",
    "question_ok",
    "gold_ok",
    "missing_relevant_article_ids",
    "reviewer",
    "note",
)
_CSS: Final = (
    "body{font:14px/1.5 system-ui,sans-serif;margin:16px;max-width:1100px}"
    "section{border-top:2px solid #888;margin-top:24px;padding-top:8px}"
    ".meta{color:#555;font-size:12px}.q{font-size:16px;font-weight:600}"
    ".ref{background:#fff6d6;padding:6px;border-left:4px solid #e0b000}"
    ".gold{background:#e3f6e3;padding:6px;border-left:4px solid #2a8a2a;margin:4px 0}"
    ".hit{padding:6px;border:1px solid #ccc;margin:4px 0}"
    ".contains{border-color:#2a8a2a}.overlaps{border-color:#d08a00}"
    "mark{background:#b9f0b9}pre{white-space:pre-wrap;font:inherit}"
    "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:3px 6px}"
)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _meta(document: ArticleDocument) -> str:
    published = (
        "không có published_at"
        if document.source_published_at is None
        else document.source_published_at.isoformat()
    )
    return (
        f"{_e(document.source_domain)} · {_e(document.source_name)} · "
        f"đăng {_e(published)} · revision {_e(document.content_hash[:19])}…"
    )


def _coverage(passage: RankedPassage, gold: tuple[PassageLabel, ...]) -> str:
    same = [g for g in gold if g.article_id == passage.article_id]
    if not same:
        return "other-article"
    if any(
        passage.char_start <= g.char_start and g.char_end <= passage.char_end
        for g in same
    ):
        return "contains"
    if any(
        passage.char_start < g.char_end and g.char_start < passage.char_end
        for g in same
    ):
        return "overlaps"
    return "gold-article-other-passage"


def _highlight(text: str, start: int, gold: list[PassageLabel]) -> str:
    """Escape ``text`` (which begins at ``start``) and mark gold sub-spans."""
    cuts = sorted(
        (max(g.char_start - start, 0), min(g.char_end - start, len(text)))
        for g in gold
        if g.char_start - start < len(text) and g.char_end - start > 0
    )
    out: list[str] = []
    cursor = 0
    for raw_left, right in cuts:
        left = max(raw_left, cursor)
        if right <= left:
            continue
        out.extend((_e(text[cursor:left]), "<mark>", _e(text[left:right]), "</mark>"))
        cursor = right
    out.append(_e(text[cursor:]))
    return "".join(out)


@dataclass(slots=True)
class EvidenceSheet:
    """Collects first-repeat answers, then writes the private sheet."""

    dataset: SearchDataset
    answers: dict[str, dict[str, SystemAnswer]] = field(
        default_factory=dict[str, dict[str, SystemAnswer]]
    )

    def add(self, config: str, question: EvalQuestion, answer: SystemAnswer) -> None:
        """Runner sink (first repeat only)."""
        self.answers.setdefault(question.question_id, {})[config] = answer

    def _documents(self) -> dict[UUID, ArticleDocument]:
        return {a.article_id: a for a in self.dataset.articles}

    def _links(self, document: ArticleDocument, anchor: str) -> str:
        page = f"articles/{document.article_id}.html#{anchor}"
        url = _e(document.canonical_url)
        return (
            f"<a href='{page}'>xem toàn bài</a> · "
            f"<a href='{url}' rel='noreferrer'>URL gốc</a>"
        )

    def _header(self, question: EvalQuestion) -> list[str]:
        filters = question.filters
        meta = " · ".join(
            (
                _e(question.question_id),
                f"split {_e(question.split)}",
                _e(question.category),
                (
                    f"label_status <b>{_e(question.label_status)}</b> "
                    f"({_e(question.origin)})"
                ),
                (
                    f"AI prescreen: {_e(question.ai_prescreen or 'none')} "
                    "(không phải người duyệt)"
                ),
            )
        )
        answerable = (
            "Có đáp án" if question.answerable else "KHÔNG có đáp án / sai tiền đề"
        )
        parts = [
            f"<section id='{_e(question.question_id)}'>",
            f"<div class='meta'>{meta}</div>",
            f"<p class='q'>{_e(question.query)}</p>",
            f"<p class='meta'>{answerable}</p>",
        ]
        if not filters.empty:
            block = (
                "<p class='meta'>Bộ lọc: nguồn "
                f"{_e(list(filters.source_domains))}, "
                f"từ {_e(filters.published_after)} "
                f"đến trước {_e(filters.published_before)}</p>"
            )
            parts.append(block)
        if question.reference_answer:
            block = (
                f"<div class='ref'><b>{_e(REFERENCE_NOTE)}</b><br>"
                f"{_e(question.reference_answer)}</div>"
            )
            parts.append(block)
        return parts

    def _question(
        self, question: EvalQuestion, docs: dict[UUID, ArticleDocument]
    ) -> str:
        parts = self._header(question)
        for label in question.passages:
            document = docs.get(label.article_id)
            if document is None:
                continue
            quote = document.normalized_content[label.char_start : label.char_end]
            span = f"offset {label.char_start}-{label.char_end}"
            links = self._links(document, f"g{label.char_start}")
            block = (
                f"<div class='gold'><b>Gold grade {label.grade}</b> · "
                f"{_e(document.title)}<div class='meta'>{_meta(document)} · "
                f"{span} · {links}</div><pre>{_e(quote)}</pre></div>"
            )
            parts.append(block)
        gold = question.passages
        answers = self.answers.get(question.question_id, {})
        for config, answer in sorted(answers.items()):
            parts.append(f"<h4>{_e(config)} — top {DEPTH}</h4>")
            hits = [p for p in answer.ranked if not p.trap][:DEPTH]
            if not hits:
                parts.append("<p class='meta'>(không có kết quả)</p>")
            for rank, passage in enumerate(hits, 1):
                document = docs.get(passage.article_id)
                if document is None:
                    continue
                kind = _coverage(passage, gold)
                same = [g for g in gold if g.article_id == passage.article_id]
                chunk = (
                    f"chunk {_e(passage.chunk_version)} "
                    f"{passage.char_start}-{passage.char_end}"
                )
                links = self._links(document, f"c{passage.char_start}")
                text = _highlight(passage.text, passage.char_start, same)
                block = (
                    f"<div class='hit {kind}'><b>#{rank}</b> [{_e(kind)}] "
                    f"{_e(document.title)}<div class='meta'>{_meta(document)} · "
                    f"{chunk} · {links}</div><pre>{text}</pre></div>"
                )
                parts.append(block)
        parts.append("</section>")
        return "".join(parts)

    def _article(self, document: ArticleDocument) -> str:
        gold = [
            p
            for q in self.dataset.questions
            for p in q.passages
            if p.article_id == document.article_id
        ]
        text = document.normalized_content
        chunk_starts = {
            p.char_start
            for per in self.answers.values()
            for answer in per.values()
            for p in answer.ranked[:DEPTH]
            if p.article_id == document.article_id
        }
        anchors = sorted(
            {(g.char_start, "g") for g in gold} | {(c, "c") for c in chunk_starts}
        )
        body: list[str] = []
        cursor = 0
        for anchor, kind in anchors:
            body.append(_highlight(text[cursor:anchor], cursor, gold))
            body.append(f"<a id='{kind}{anchor}'></a>")
            cursor = anchor
        body.append(_highlight(text[cursor:], cursor, gold))
        facts = self.dataset.facts.get(document.article_id)
        parser = (
            ""
            if facts is None
            else f"{facts.parse_version}/{facts.normalization_version}"
        )
        return (
            f"<!doctype html><meta charset='utf-8'><title>{_e(document.title)}</title>"
            f"<style>{_CSS}</style><p><a href='../index.html'>← review</a></p>"
            f"<h2>{_e(document.title)}</h2><p class='meta'>{_meta(document)} · parser "
            f"{_e(parser)} · article {_e(document.article_id)} · "
            f"<a href='{_e(document.canonical_url)}' rel='noreferrer'>URL gốc</a></p>"
            "<p class='meta'>Văn bản RAG (NFC). Offset tính theo code point; vùng "
            "tô xanh là gold span của nhãn candidate.</p>"
            f"<pre>{''.join(body)}</pre>"
        )

    def write(self, directory: Path) -> int:
        """Write index.html, review.csv and one page per article; file count."""
        docs = self._documents()
        (directory / "articles").mkdir(parents=True, exist_ok=True)
        sections = "".join(self._question(q, docs) for q in self.dataset.questions)
        configs = sorted({c for per in self.answers.values() for c in per})
        page = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>Search evidence review</title>"
            f"<style>{_CSS}</style><h1>Review: {_e(self.dataset.export_id)}</h1>"
            f"<p>{len(self.dataset.questions)} câu · "
            f"cấu hình: {_e(', '.join(configs))}."
            " Nhãn đều là <b>candidate</b> do máy soạn; AI prescreen không phải người "
            "duyệt. Điền quyết định vào review.csv.</p>"
            "<p class='meta'>[contains] chunk chứa trọn gold span · [overlaps] chạm "
            "một phần · [gold-article-other-passage] đúng bài, sai đoạn · "
            "[other-article] bài khác (có thể là gold bị thiếu — hãy kiểm).</p>"
            f"{sections}"
        )
        _ = (directory / "index.html").write_text(page, encoding="utf-8")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for q in self.dataset.questions:
            writer.writerow(
                [
                    q.question_id,
                    q.split,
                    q.category,
                    q.label_status,
                    q.ai_prescreen or "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        _ = (directory / "review.csv").write_text(buffer.getvalue(), encoding="utf-8")
        for document in self.dataset.articles:
            _ = (directory / "articles" / f"{document.article_id}.html").write_text(
                self._article(document), encoding="utf-8"
            )
        return 2 + len(self.dataset.articles)
