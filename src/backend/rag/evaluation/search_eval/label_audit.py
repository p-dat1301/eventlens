"""Integrity audit of a derived candidate label set (``search-questions.v1``).

Checks a new label version against the version it was derived from and the
exact RAG revisions (NFC text) it is scored on. Nothing is repaired; every
problem is listed. Text-free output (IDs, offsets, hashes, counts):

* identity: unique question IDs, the same IDs and splits as the source set;
* status: every label stays ``candidate`` with no reviewer, no review date and
  no truthy ``human_*`` / ``*verified*`` field anywhere (an AI review is never
  promoted to human gold); AI provenance is present and says not human;
* gold: article present, ``content_hash`` and ``article_version_id`` equal the
  revision, spans inside the text, ``excerpt_sha256`` of the code-point slice
  of the NFC text, every gold article inside the question filter;
* answerability: answerable questions have a grade-2 article, no-answer
  questions have no gold; grade-0 judgments never overlap gold;
* facets (optional sidecar): every facet span is a gold span of the same
  question with the same hash, every grade-2 span is assigned to a facet.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Final, Protocol, cast

from backend.rag.evaluation.metrics import Json

GOLD_GRADE: Final = 2
EVAL_STATUSES: Final = frozenset({"eligible", "unresolved"})
HUMAN_MARKERS: Final = ("human", "verified")

type Row = Mapping[str, object]


class Revision(Protocol):
    """The RAG revision fields the audit reads."""

    @property
    def normalized_content(self) -> str:
        """NFC RAG text."""
        ...

    @property
    def content_hash(self) -> str:
        """RAG content hash."""
        ...

    @property
    def source_domain(self) -> str:
        """Publisher domain."""
        ...

    @property
    def source_published_at(self) -> datetime | None:
        """Source publication time."""
        ...


def _time(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def inside_filter(filters: Row, revision: Revision) -> bool:
    """Same rule as the benchmark: domain list, inclusive after, exclusive before."""
    domains = cast("list[str]", filters.get("source_domains") or [])
    if domains and revision.source_domain not in domains:
        return False
    after, before = (
        _time(filters.get("published_after")),
        _time(filters.get("published_before")),
    )
    if after is None and before is None:
        return True
    published = revision.source_published_at
    if published is None:
        return False
    if after is not None and published < after:
        return False
    return before is None or published < before


def human_promotions(value: object, path: str = "") -> list[str]:
    """Paths of truthy ``human*``/``*verified*`` fields (recursive)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in cast("Mapping[str, object]", value).items():
            here = f"{path}.{key}" if path else key
            if any(m in key.lower() for m in HUMAN_MARKERS) and item:
                found.append(here)
            found.extend(human_promotions(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            found.extend(human_promotions(item, f"{path}[{index}]"))
    return found


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, Json]] = []

    def add(self, qid: str, code: str, detail: str = "") -> None:
        item: dict[str, Json] = {"question_id": qid, "code": code}
        if detail:
            item["detail"] = detail
        self.items.append(item)


def _span_ok(  # noqa: PLR0913 - one span, its revision and the issue sink
    qid: str,
    *,
    article_id: str,
    span: Row,
    revision: Revision,
    issues: _Issues,
    counts: Counter[str],
) -> None:
    start, end = int(cast("int", span["start"])), int(cast("int", span["end"]))
    text = revision.normalized_content
    counts["spans"] += 1
    if not 0 <= start < end <= len(text):
        issues.add(qid, "span_out_of_range", f"{article_id}:{start}-{end}")
        return
    if sha256(text[start:end].encode()).hexdigest() != span.get("excerpt_sha256"):
        issues.add(qid, "excerpt_hash_mismatch", f"{article_id}:{start}-{end}")
        return
    counts["spans_ok"] += 1


def _gold(  # noqa: PLR0913, C901 - one question against the corpus
    qid: str,
    *,
    row: Row,
    revisions: Mapping[str, Revision],
    versions: Mapping[str, str],
    issues: _Issues,
    counts: Counter[str],
) -> None:
    filters = cast("Row", row.get("filters") or {})
    for entry in cast("list[Row]", row.get("gold") or []):
        article_id = str(entry["article_id"])
        counts["gold_articles"] += 1
        revision = revisions.get(article_id)
        if revision is None:
            issues.add(qid, "gold_article_missing", article_id)
            continue
        if entry.get("content_hash") != revision.content_hash:
            issues.add(qid, "content_hash_mismatch", article_id)
            continue
        version = entry.get("article_version_id")
        if version is None or version != versions.get(article_id):
            issues.add(qid, "article_version_mismatch", article_id)
        if not inside_filter(filters, revision):
            issues.add(qid, "gold_outside_filter", article_id)
        for span in cast("list[Row]", entry.get("spans") or []):
            _span_ok(
                qid,
                article_id=article_id,
                span=span,
                revision=revision,
                issues=issues,
                counts=counts,
            )
    judged = {
        str(j["article_id"])
        for j in cast("list[Row]", row.get("judged_nonrelevant") or [])
    }
    gold_ids = {str(g["article_id"]) for g in cast("list[Row]", row.get("gold") or [])}
    for article_id in sorted(judged & gold_ids):
        issues.add(qid, "grade0_judgment_overlaps_gold", article_id)
    for item in cast("list[Row]", row.get("premise_rebuttal_evidence") or []):
        article_id = str(item["article_id"])
        revision = revisions.get(article_id)
        if revision is None or item.get("content_hash") != revision.content_hash:
            issues.add(qid, "rebuttal_revision_mismatch", article_id)
            continue
        for span in cast("list[Row]", item.get("spans") or []):
            _span_ok(
                qid,
                article_id=article_id,
                span=span,
                revision=revision,
                issues=issues,
                counts=counts,
            )


def _status(qid: str, row: Row, issues: _Issues) -> None:
    if row.get("label_status") != "candidate":
        issues.add(qid, "label_status_not_candidate", str(row.get("label_status")))
    if row.get("reviewer") is not None or row.get("reviewed_at") is not None:
        issues.add(qid, "reviewer_filled")
    for path in human_promotions(row):
        issues.add(qid, "human_promotion_field", path)
    review = row.get("ai_review")
    if not isinstance(review, Mapping):
        issues.add(qid, "ai_provenance_missing")
    elif cast("Row", review).get("human_reviewed") is not False:
        issues.add(qid, "ai_provenance_not_marked_non_human")
    if row.get("eval_status") not in EVAL_STATUSES:
        issues.add(qid, "eval_status_invalid", str(row.get("eval_status")))
    grades = [
        int(cast("int", g["grade"])) for g in cast("list[Row]", row.get("gold") or [])
    ]
    if row.get("answerable") and GOLD_GRADE not in grades:
        issues.add(qid, "answerable_without_grade2")
    if not row.get("answerable") and grades:
        issues.add(qid, "no_answer_with_gold")


REF_KEYS: Final = ("article_id", "start", "end", "excerpt_sha256")


def _ref_errors(ref: object) -> list[tuple[str, str]]:
    if not isinstance(ref, Mapping):
        return [("facet_ref_not_object", "")]
    fields = cast("Row", ref)
    missing = [k for k in REF_KEYS if k not in fields]
    if missing:
        return [("facet_ref_missing_field", ",".join(missing))]
    start, end = fields["start"], fields["end"]
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or isinstance(start, bool)
        or isinstance(end, bool)
        or not 0 <= start < end
    ):
        return [("facet_ref_bad_offsets", f"{start}-{end}")]
    if not isinstance(fields["article_id"], str) or not fields["article_id"]:
        return [("facet_ref_bad_article", "")]
    return []


def facet_shape_errors(entry: object) -> list[tuple[str, str]]:
    """Structural problems of one question's facet entry (no vacuous truth).

    A facet needs a non-empty string ``facet_id``, a boolean ``required`` and a
    non-empty list of alternatives; every alternative is a non-empty list of
    span references. An empty alternative would make ``all([])`` true and
    count missing evidence as present, so it is an error, never a no-op.
    """
    if not isinstance(entry, Mapping):
        return [("facet_entry_not_object", "")]
    facets = cast("Row", entry).get("facets")
    if not isinstance(facets, list):
        return [("facet_list_missing", "")]
    errors: list[tuple[str, str]] = []
    for index, facet in enumerate(cast("list[object]", facets)):
        errors.extend(_facet_errors(index, facet))
    return errors


def _facet_errors(index: int, facet: object) -> list[tuple[str, str]]:
    if not isinstance(facet, Mapping):
        return [("facet_not_object", str(index))]
    fields = cast("Row", facet)
    facet_id = fields.get("facet_id")
    label = facet_id if isinstance(facet_id, str) and facet_id else str(index)
    errors: list[tuple[str, str]] = []
    if not isinstance(facet_id, str) or not facet_id:
        errors.append(("facet_id_missing", label))
    if not isinstance(fields.get("required"), bool):
        errors.append(("facet_required_not_bool", label))
    alternatives = fields.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        return [*errors, ("facet_without_alternatives", label)]
    for alt_index, alternative in enumerate(cast("list[object]", alternatives)):
        if not isinstance(alternative, list) or not alternative:
            errors.append(("facet_empty_alternative", f"{label}[{alt_index}]"))
            continue
        for ref in cast("list[object]", alternative):
            errors.extend(
                (code, f"{label}[{alt_index}] {detail}".strip())
                for code, detail in _ref_errors(ref)
            )
    return errors


def _facets(  # noqa: C901, PLR0912 - one pass: shape, ids, gold links, coverage
    rows: Mapping[str, Row], facets: Mapping[str, object], issues: _Issues
) -> dict[str, Json]:
    raw_questions = facets.get("questions")
    if not isinstance(raw_questions, Mapping):
        issues.add("*", "facet_sidecar_without_questions")
        return {"required_facets": 0, "optional_facets": 0}
    questions = cast("Mapping[str, Row]", raw_questions)
    for qid in sorted(set(questions) - set(rows)):
        issues.add(qid, "facets_for_unknown_question")
    required = optional = 0
    seen_ids: set[str] = set()
    for qid, row in rows.items():
        entry = questions.get(qid)
        if entry is None:
            issues.add(qid, "facets_missing")
            continue
        shape = facet_shape_errors(entry)
        for code, detail in shape:
            issues.add(qid, code, detail)
        if shape:
            continue
        for facet in cast("list[Row]", entry["facets"]):
            facet_id = str(facet["facet_id"])
            if facet_id in seen_ids:
                issues.add(qid, "facet_id_duplicate", facet_id)
            seen_ids.add(facet_id)
        gold = {
            (
                str(g["article_id"]),
                int(cast("int", s["start"])),
                int(cast("int", s["end"])),
            ): (
                int(cast("int", g["grade"])),
                str(s["excerpt_sha256"]),
            )
            for g in cast("list[Row]", row.get("gold") or [])
            for s in cast("list[Row]", g["spans"])
        }
        used: set[tuple[str, int, int]] = set()
        for facet in cast("list[Row]", entry.get("facets") or []):
            if facet.get("required"):
                required += 1
            else:
                optional += 1
            for alternative in cast("list[list[Row]]", facet.get("alternatives") or []):
                for ref in alternative:
                    key = (
                        str(ref["article_id"]),
                        int(cast("int", ref["start"])),
                        int(cast("int", ref["end"])),
                    )
                    if key not in gold or gold[key][1] != ref.get("excerpt_sha256"):
                        issues.add(
                            qid, "facet_span_not_gold", f"{key[0]}:{key[1]}-{key[2]}"
                        )
                    used.add(key)
        eligible = row.get("answerable") and row.get("eval_status") == "eligible"
        if eligible and not any(
            f.get("required") for f in cast("list[Row]", entry.get("facets") or [])
        ):
            issues.add(qid, "eligible_without_required_facet")
        if eligible:
            for key, (grade, _) in sorted(gold.items()):
                if grade >= GOLD_GRADE and key not in used:
                    issues.add(
                        qid, "grade2_span_without_facet", f"{key[0]}:{key[1]}-{key[2]}"
                    )
    return {"required_facets": required, "optional_facets": optional}


def audit_label_set(
    derived: Mapping[str, object],
    source_rows: Sequence[Row],
    revisions: Mapping[str, Revision],
    versions: Mapping[str, str],
    *,
    facets: Mapping[str, object] | None = None,
) -> dict[str, Json]:
    """Audit ``derived`` (parsed labels) against its source rows and the corpus."""
    issues = _Issues()
    counts: Counter[str] = Counter()
    rows_list = cast("list[Row]", derived.get("questions") or [])
    ids = [str(r["qid"]) for r in rows_list]
    for qid, n in Counter(ids).items():
        if n > 1:
            issues.add(qid, "duplicate_question_id")
    rows = {str(r["qid"]): r for r in rows_list}
    source = {str(r["qid"]): r for r in source_rows}
    for qid in sorted(set(source) - set(rows)):
        issues.add(qid, "question_dropped")
    for qid in sorted(set(rows) - set(source)):
        issues.add(qid, "question_added")
    for qid, row in rows.items():
        if qid in source and row.get("split") != source[qid].get("split"):
            issues.add(qid, "split_changed")
        _status(qid, row, issues)
        _gold(
            qid,
            row=row,
            revisions=revisions,
            versions=versions,
            issues=issues,
            counts=counts,
        )
    facet_counts: dict[str, Json] = {}
    if facets is not None:
        facet_counts = _facets(rows, facets, issues)
    statuses = Counter(str(r.get("eval_status")) for r in rows.values())
    report: dict[str, Json] = {
        "questions": len(rows),
        "source_questions": len(source),
        "answerable": sum(1 for r in rows.values() if r.get("answerable")),
        "eval_status": dict(sorted(statuses.items())),
        "label_status": dict(
            sorted(Counter(str(r.get("label_status")) for r in rows.values()).items())
        ),
        "splits": dict(
            sorted(Counter(str(r.get("split")) for r in rows.values()).items())
        ),
        "gold_articles": counts["gold_articles"],
        "spans_checked": counts["spans"],
        "spans_ok": counts["spans_ok"],
        **facet_counts,
        "issues": cast("list[Json]", issues.items),
        "ok": not issues.items,
    }
    return report
