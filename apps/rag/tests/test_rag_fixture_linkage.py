from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from pydantic import TypeAdapter
from typing_extensions import TypedDict

from eventlens_rag.domain.article_document import ArticleDocument

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "rag"


class Reference(TypedDict):
    claim_id: UUID
    article_id: UUID
    span_id: UUID


class RegisteredClaim(Reference):
    label: str


class ClaimsFixture(TypedDict):
    schema_version: Literal["rag_claims.v1"]
    claims: list[RegisteredClaim]


class ExpectedArticle(Reference):
    purpose: str


class ManifestFixture(TypedDict):
    coverage: list[str]
    expected_articles: list[ExpectedArticle]


class Question(TypedDict):
    question_id: UUID
    expected_references: list[Reference]


class QuestionsFixture(TypedDict):
    questions: list[Question]


class CoverageCase(TypedDict):
    purpose: str
    question_ids: list[UUID]
    fixture_only_reason: str | None


class CoverageFixture(TypedDict):
    schema_version: Literal["rag_coverage.v1"]
    cases: list[CoverageCase]


def assert_linkage(manifest: ManifestFixture, questions: QuestionsFixture) -> None:
    registry = TypeAdapter(ClaimsFixture).validate_json(
        (FIXTURE_PATH / "claims.v1.json").read_text("utf-8")
    )
    links = {
        claim["claim_id"]: (claim["article_id"], claim["span_id"])
        for claim in registry["claims"]
    }
    assert len(links) == len(registry["claims"])
    articles = {
        article.article_id: article
        for path in (FIXTURE_PATH / "articles").glob("*.json")
        for article in (ArticleDocument.model_validate_json(path.read_text("utf-8")),)
    }
    for claim in registry["claims"]:
        assert claim["label"].strip()
        article = articles[claim["article_id"]]
        assert claim["span_id"] in {span.span_id for span in article.evidence_spans}
    references: list[Reference] = [
        {
            "claim_id": entry["claim_id"],
            "article_id": entry["article_id"],
            "span_id": entry["span_id"],
        }
        for entry in manifest["expected_articles"]
    ]
    references.extend(
        reference
        for question in questions["questions"]
        for reference in question["expected_references"]
    )
    for reference in references:
        assert links.get(reference["claim_id"]) == (
            reference["article_id"],
            reference["span_id"],
        )
    coverage = TypeAdapter(CoverageFixture).validate_json(
        (FIXTURE_PATH / "coverage.v1.json").read_text("utf-8")
    )
    expected_by_purpose = {
        entry["purpose"]: entry for entry in manifest["expected_articles"]
    }
    assert set(expected_by_purpose) == set(manifest["coverage"])
    assert {case["purpose"] for case in coverage["cases"]} == set(manifest["coverage"])
    assert len(coverage["cases"]) == len(manifest["coverage"])
    question_by_id = {
        question["question_id"]: question for question in questions["questions"]
    }
    assert len(question_by_id) == len(questions["questions"])
    for case in coverage["cases"]:
        assert bool(case["question_ids"]) != bool(case["fixture_only_reason"])
        assert set(case["question_ids"]) <= question_by_id.keys()
        expected = expected_by_purpose[case["purpose"]]
        triple: Reference = {
            "claim_id": expected["claim_id"],
            "article_id": expected["article_id"],
            "span_id": expected["span_id"],
        }
        for question_id in case["question_ids"]:
            assert triple in question_by_id[question_id]["expected_references"]


def fixture_labels() -> tuple[ManifestFixture, QuestionsFixture]:
    return (
        TypeAdapter(ManifestFixture).validate_json(
            (FIXTURE_PATH / "manifest.v1.json").read_text("utf-8")
        ),
        TypeAdapter(QuestionsFixture).validate_json(
            (FIXTURE_PATH / "questions.v1.json").read_text("utf-8")
        ),
    )


def test_fixture_linkage_when_loaded_then_exact_triples_and_purposes_resolve() -> None:
    # Given / When
    manifest, questions = fixture_labels()
    # Then
    assert_linkage(manifest, questions)


@pytest.mark.parametrize("question_reference", [False, True])
@pytest.mark.parametrize("claim_suffix", ["002", "099"])
def test_fixture_linkage_when_claim_wrong_or_unknown_then_reject(
    *, question_reference: bool, claim_suffix: str
) -> None:
    # Given
    manifest, questions = fixture_labels()
    # When
    claim_id = UUID(f"30000000-0000-4000-8000-000000000{claim_suffix}")
    if question_reference:
        questions["questions"][0]["expected_references"][0]["claim_id"] = claim_id
    else:
        manifest["expected_articles"][0]["claim_id"] = claim_id
    # Then
    with pytest.raises(AssertionError):
        assert_linkage(manifest, questions)


@pytest.mark.parametrize("purpose", ["undeclared", "follow_up"])
def test_fixture_linkage_when_purpose_unknown_or_missing_then_reject(
    purpose: str,
) -> None:
    # Given
    manifest, questions = fixture_labels()
    # When
    manifest["expected_articles"][0]["purpose"] = purpose
    # Then
    with pytest.raises(AssertionError):
        assert_linkage(manifest, questions)


@pytest.mark.parametrize("field", ["article_id", "span_id"])
@pytest.mark.parametrize("question_reference", [False, True])
def test_fixture_linkage_when_article_or_span_belongs_to_other_claim_then_reject(
    *, field: Literal["article_id", "span_id"], question_reference: bool
) -> None:
    # Given
    manifest, questions = fixture_labels()
    other = manifest["expected_articles"][1]
    # When
    if question_reference:
        questions["questions"][0]["expected_references"][0][field] = other[field]
    else:
        manifest["expected_articles"][0][field] = other[field]
    # Then
    with pytest.raises(AssertionError):
        assert_linkage(manifest, questions)


def test_fixture_linkage_when_declared_purposes_swap_question_links_then_reject() -> (
    None
):
    # Given
    manifest, questions = fixture_labels()
    first, second = manifest["expected_articles"][:2]
    # When
    first["purpose"], second["purpose"] = second["purpose"], first["purpose"]
    # Then
    with pytest.raises(AssertionError):
        assert_linkage(manifest, questions)
