"""Derived label-set audit: IDs, status, provenance, NFC offsets, filters, facets."""

import copy
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from backend.rag.evaluation.search_eval.label_audit import audit_label_set

TEXT = unicodedata.normalize(
    "NFC", "Phường Hòa Hưng: lễ viếng lúc 8 giờ. An táng tại nghĩa trang."
)
A, B = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"


@dataclass(frozen=True)
class _Rev:
    normalized_content: str
    content_hash: str
    source_domain: str
    source_published_at: datetime | None


REVISIONS = {
    A: _Rev(TEXT, "sha256:a", "tuoitre.vn", datetime(2026, 10, 15, 11, tzinfo=UTC)),
    B: _Rev(TEXT, "sha256:b", "vietstock.vn", datetime(2026, 10, 16, 1, tzinfo=UTC)),
}
VERSIONS = {A: "va", B: "vb"}


def _span(start: int, end: int) -> dict[str, object]:
    return {
        "start": start,
        "end": end,
        "excerpt_sha256": sha256(TEXT[start:end].encode()).hexdigest(),
    }


def _row(qid: str = "q1", split: str = "dev") -> dict[str, object]:
    start = TEXT.index("lễ viếng")
    return {
        "qid": qid,
        "split": split,
        "answerable": True,
        "filters": {"source_domains": ["tuoitre.vn"]},
        "gold": [
            {
                "article_id": A,
                "content_hash": "sha256:a",
                "article_version_id": "va",
                "grade": 2,
                "spans": [_span(start, start + len("lễ viếng lúc 8 giờ."))],
            }
        ],
        "label_status": "candidate",
        "reviewer": None,
        "reviewed_at": None,
        "eval_status": "eligible",
        "ai_review": {"human_reviewed": False, "applied_by": "AI"},
    }


def _facets(row: dict[str, object]) -> dict[str, object]:
    gold = cast("list[dict[str, object]]", row["gold"])[0]
    span = cast("list[dict[str, object]]", gold["spans"])[0]
    ref = {"article_id": A, **span}
    return {
        "questions": {
            "q1": {
                "facets": [
                    {"facet_id": "q1#F1", "required": True, "alternatives": [[ref]]}
                ]
            },
        }
    }


def _audit(
    row: dict[str, object],
    *,
    source: list[dict[str, object]] | None = None,
    facets: dict[str, object] | None = None,
) -> dict[str, object]:
    return cast(
        "dict[str, object]",
        audit_label_set(
            {"questions": [row]},
            source or [{"qid": "q1", "split": "dev"}],
            REVISIONS,
            VERSIONS,
            facets=facets,
        ),
    )


def _codes(report: dict[str, object]) -> set[str]:
    return {str(i["code"]) for i in cast("list[dict[str, object]]", report["issues"])}


def test_clean_derived_set_passes_with_facets() -> None:
    row = _row()
    report = _audit(row, facets=_facets(row))
    assert report["ok"] is True
    assert report["spans_ok"] == 1
    assert report["required_facets"] == 1


def test_offsets_are_code_points_of_the_nfc_text() -> None:
    row = _row()
    start = TEXT.index("lễ viếng")
    # UTF-8 byte offsets of the same excerpt point elsewhere: rejected.
    byte_start = len(TEXT[:start].encode())
    gold = cast("list[dict[str, object]]", row["gold"])[0]
    good = cast("list[dict[str, object]]", gold["spans"])[0]
    gold["spans"] = [
        {**good, "start": byte_start, "end": byte_start + len("lễ viếng lúc 8 giờ.")}
    ]
    assert "excerpt_hash_mismatch" in _codes(_audit(row))


def test_an_ai_review_is_never_promoted_to_human_gold() -> None:
    cases: list[tuple[str, object, str]] = [
        ("label_status", "human_verified", "label_status_not_candidate"),
        ("reviewer", "someone", "reviewer_filled"),
        ("ai_review", {"human_reviewed": True}, "human_promotion_field"),
        ("qa", {"verified_by_human": "yes"}, "human_promotion_field"),
    ]
    for key, value, code in cases:
        row = _row()
        row[key] = value
        assert code in _codes(_audit(row)), code
    row = _row()
    del row["ai_review"]
    assert "ai_provenance_missing" in _codes(_audit(row))


def test_gold_outside_the_question_filter_is_reported() -> None:
    row = _row()
    row["gold"] = [
        *cast("list[dict[str, object]]", row["gold"]),
        {
            "article_id": B,
            "content_hash": "sha256:b",
            "article_version_id": "vb",
            "grade": 1,
            "spans": [_span(0, 6)],
        },
    ]
    assert "gold_outside_filter" in _codes(_audit(row))
    # Exclusive upper bound: an article published exactly at the bound is out.
    dated = _row()
    dated["filters"] = {
        "published_after": "2026-10-15T00:00:00+00:00",
        "published_before": "2026-10-15T11:00:00+00:00",
    }
    assert "gold_outside_filter" in _codes(_audit(dated))


def test_identity_and_revision_problems_are_listed_not_repaired() -> None:
    row = _row(split="test")
    assert "split_changed" in _codes(_audit(row))
    assert "question_dropped" in _codes(
        _audit(_row(), source=[{"qid": "q1", "split": "dev"}, {"qid": "q2"}])
    )
    stale = _row()
    cast("list[dict[str, object]]", stale["gold"])[0]["article_version_id"] = "old"
    assert "article_version_mismatch" in _codes(_audit(stale))
    no_gold = _row()
    cast("list[dict[str, object]]", no_gold["gold"])[0]["grade"] = 1
    assert "answerable_without_grade2" in _codes(_audit(no_gold))


def test_facets_must_point_at_gold_and_cover_every_grade2_span() -> None:
    row = _row()
    facets = _facets(row)
    broken = copy.deepcopy(facets)
    question = cast("dict[str, dict[str, object]]", broken["questions"])["q1"]
    facet = cast("list[dict[str, object]]", question["facets"])[0]
    ref = cast("list[list[dict[str, object]]]", facet["alternatives"])[0][0]
    ref["end"] = cast("int", ref["end"]) - 1
    codes = _codes(_audit(row, facets=broken))
    assert "facet_span_not_gold" in codes
    assert "grade2_span_without_facet" in codes


def _facet(row: dict[str, object]) -> dict[str, object]:
    facets = _facets(row)
    question = cast("dict[str, dict[str, object]]", facets["questions"])["q1"]
    facet = cast("list[dict[str, object]]", question["facets"])[0]
    facet["facet_id"] = "q1#F1"
    return facets


def test_an_empty_alternative_next_to_a_valid_one_fails_the_audit() -> None:
    row = _row()
    facets = _facet(row)
    question = cast("dict[str, dict[str, object]]", facets["questions"])["q1"]
    facet = cast("list[dict[str, object]]", question["facets"])[0]
    assert _audit(row, facets=facets)["ok"] is True
    cast("list[object]", facet["alternatives"]).append([])
    assert "facet_empty_alternative" in _codes(_audit(row, facets=facets))


def test_malformed_facets_are_rejected() -> None:
    cases: list[tuple[str, object, str]] = [
        ("alternatives", [], "facet_without_alternatives"),
        ("required", "yes", "facet_required_not_bool"),
        ("facet_id", "", "facet_id_missing"),
        ("alternatives", [[{"article_id": A}]], "facet_ref_missing_field"),
    ]
    for key, value, code in cases:
        row = _row()
        facets = _facet(row)
        question = cast("dict[str, dict[str, object]]", facets["questions"])["q1"]
        cast("list[dict[str, object]]", question["facets"])[0][key] = value
        assert code in _codes(_audit(row, facets=facets)), code


def test_duplicate_facet_ids_and_unknown_questions_are_rejected() -> None:
    row = _row()
    facets = _facet(row)
    question = cast("dict[str, dict[str, object]]", facets["questions"])["q1"]
    first = cast("list[dict[str, object]]", question["facets"])[0]
    cast("list[object]", question["facets"]).append(copy.deepcopy(first))
    cast("dict[str, object]", facets["questions"])["q9"] = {"facets": []}
    codes = _codes(_audit(row, facets=facets))
    assert {"facet_id_duplicate", "facets_for_unknown_question"} <= codes
