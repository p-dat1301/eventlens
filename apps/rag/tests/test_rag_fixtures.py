import shutil
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypedDict

from eventlens_contracts import ArticleEnrichment
from eventlens_rag.domain.article_document import ArticleDocument

RAG_FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "rag"
DUPLICATE_ARTICLE_DOCUMENT_ID: Final = "duplicate ArticleDocument article_id"
DUPLICATE_ARTICLE_ENRICHMENT_ID: Final = "duplicate ArticleEnrichment article_id"
REQUIRED_COVERAGE = frozenset(
    {
        "duplicate",
        "follow_up",
        "conflicting_claims",
        "delayed_publication",
        "date_only",
        "instant",
        "range",
        "unknown_date",
        "relative_date",
        "utc_timezone",
        "dst_boundary",
        "unknown_metadata",
        "restricted_scope",
        "prompt_injection",
        "uncited_prose",
        "malformed_evidence",
        "vietnamese_cjk",
        "combining_mark",
        "non_bmp_emoji",
        "controlled_topics_separate_suggestions",
    }
)


class ExpectedArticle(TypedDict):
    article_id: str
    span_id: str
    claim_id: str
    purpose: str


class ScopeCase(TypedDict):
    restricted_article_id: str
    allowed_article_ids: list[str]


class ExpectedReference(TypedDict):
    article_id: str
    span_id: str
    claim_id: str


class Question(TypedDict):
    question_id: str
    question: str
    answer_mode: Literal["grounded", "abstain", "compare"]
    uncertainties: list[str]
    time_basis: str
    allowed_article_ids: list[str]
    expected_references: list[ExpectedReference]


class QuestionsFixture(TypedDict):
    schema_version: Literal["rag_questions.v1"]
    questions: list[Question]


class ManifestFixture(TypedDict):
    schema_version: Literal["rag_corpus_manifest.v1"]
    corpus_version: str
    corpus_count: int
    provenance: str
    source_type: str
    license: str
    limitations: str
    normalization_version: str
    enrichment_version: str
    model_version: str
    prompt_version: str
    embedding_version: str
    coverage: list[str]
    expected_articles: list[ExpectedArticle]
    scope_cases: list[ScopeCase]


ManifestAdapter = TypeAdapter(ManifestFixture)
QuestionsAdapter = TypeAdapter(QuestionsFixture)
Mutation = Callable[[ManifestFixture, QuestionsFixture], None]


def index_articles(
    records: Sequence[ArticleDocument],
) -> dict[UUID, ArticleDocument]:
    if len({record.article_id for record in records}) != len(records):
        raise AssertionError(DUPLICATE_ARTICLE_DOCUMENT_ID)
    return {record.article_id: record for record in records}


def index_enrichments(
    records: Sequence[ArticleEnrichment],
) -> dict[UUID, ArticleEnrichment]:
    if len({record.article_id for record in records}) != len(records):
        raise AssertionError(DUPLICATE_ARTICLE_ENRICHMENT_ID)
    return {record.article_id: record for record in records}


def fixture_graph() -> tuple[
    ManifestFixture,
    dict[UUID, ArticleDocument],
    dict[UUID, ArticleEnrichment],
    QuestionsFixture,
]:
    manifest = ManifestAdapter.validate_json(
        (RAG_FIXTURE_PATH / "manifest.v1.json").read_text(encoding="utf-8")
    )
    questions = QuestionsAdapter.validate_json(
        (RAG_FIXTURE_PATH / "questions.v1.json").read_text(encoding="utf-8")
    )
    article_records = tuple(
        ArticleDocument.model_validate_json(path.read_text("utf-8"))
        for path in sorted((RAG_FIXTURE_PATH / "articles").glob("*.json"))
    )
    enrichment_records = tuple(
        ArticleEnrichment.model_validate_json(path.read_text("utf-8"))
        for path in sorted((RAG_FIXTURE_PATH / "enrichments").glob("*.json"))
    )
    articles = index_articles(article_records)
    enrichments = index_enrichments(enrichment_records)
    assert len(articles) == manifest["corpus_count"], "missing source article"
    return manifest, articles, enrichments, questions


def assert_fixture_graph(
    manifest: ManifestFixture,
    articles: dict[UUID, ArticleDocument],
    enrichments: dict[UUID, ArticleEnrichment],
    questions: QuestionsFixture,
) -> None:
    assert manifest["corpus_count"] == 30
    assert manifest["provenance"] == "synthetic-authored"
    assert manifest["source_type"] == "internal fixture"
    assert manifest["license"] == "synthetic-authored"
    assert "not live model-quality proof" in manifest["limitations"]
    assert manifest["normalization_version"] == "nfc.v1"
    assert manifest["enrichment_version"] == "rag-fixture-enrichment.v1"
    assert manifest["model_version"] == "manual-ground-truth.v1"
    assert manifest["prompt_version"] == "rag-fixture-prompt.v1"
    assert manifest["embedding_version"] == "fixture-embedding.v1"
    assert set(manifest["coverage"]) == REQUIRED_COVERAGE
    assert len(articles) == len(enrichments) == manifest["corpus_count"]
    assert set(articles) == set(enrichments)
    for article_id, enrichment in enrichments.items():
        article = articles[article_id]
        assert enrichment.content_hash == article.content_hash
        source_spans = {span.span_id: span for span in article.evidence_spans}
        assert all(
            source_spans.get(span.span_id) == span for span in enrichment.evidence_spans
        )
    for expected in manifest["expected_articles"]:
        article = articles[UUID(expected["article_id"])]
        assert UUID(expected["span_id"]) in {
            span.span_id for span in article.evidence_spans
        }
        assert expected["purpose"]
    assert len(questions["questions"]) == 10
    for question in questions["questions"]:
        assert question["answer_mode"] in {"grounded", "abstain", "compare"}
        assert question["uncertainties"]
        assert question["time_basis"]
        allowed_article_ids = {UUID(value) for value in question["allowed_article_ids"]}
        for reference in question["expected_references"]:
            article_id = UUID(reference["article_id"])
            assert article_id in articles
            assert article_id in allowed_article_ids
            assert UUID(reference["span_id"]) in {
                span.span_id for span in articles[article_id].evidence_spans
            }
    for scope_case in manifest["scope_cases"]:
        assert UUID(scope_case["restricted_article_id"]) not in {
            UUID(value) for value in scope_case["allowed_article_ids"]
        }
    assert enrichments[
        UUID("00000000-0000-4000-8000-000000000015")
    ].event_relevance == ("no_actionable_event")
    assert enrichments[UUID("00000000-0000-4000-8000-000000000021")].topics == (
        "energy",
    )
    article_twenty_one = articles[UUID("00000000-0000-4000-8000-000000000021")]
    enrichment_twenty_one = enrichments[UUID("00000000-0000-4000-8000-000000000021")]
    assert enrichment_twenty_one.entities
    assert enrichment_twenty_one.locations
    assert enrichment_twenty_one.evidence_spans == article_twenty_one.evidence_spans
    assert all(
        extracted.evidence_span_ids
        for extracted in (
            *enrichment_twenty_one.entities,
            *enrichment_twenty_one.locations,
        )
    )
    assert {
        span_id
        for extracted in (
            *enrichment_twenty_one.entities,
            *enrichment_twenty_one.locations,
        )
        for span_id in extracted.evidence_span_ids
    } <= {span.span_id for span in article_twenty_one.evidence_spans}
    assert enrichments[UUID("00000000-0000-4000-8000-000000000021")].suggested_tags == (
        "solar-storage",
        "community-workshop",
    )
    assert (
        "线路5"
        in articles[UUID("00000000-0000-4000-8000-000000000018")].normalized_content
    )
    combining_mark_article = articles[UUID("00000000-0000-4000-8000-000000000019")]
    assert (
        combining_mark_article.raw_content != combining_mark_article.normalized_content
    )
    assert (
        "🚨"
        in articles[UUID("00000000-0000-4000-8000-000000000020")].normalized_content
    )


def test_rag_fixture_graph_when_loaded_then_all_contract_and_label_links_resolve() -> (
    None
):
    # Given / When
    manifest, articles, enrichments, questions = fixture_graph()

    # Then
    assert_fixture_graph(manifest, articles, enrichments, questions)


def test_fixture_graph_when_source_article_file_missing_then_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    fixture_copy = tmp_path / "rag"
    _ = shutil.copytree(RAG_FIXTURE_PATH, fixture_copy)
    (fixture_copy / "articles" / "23.json").unlink()
    monkeypatch.setattr(sys.modules[__name__], "RAG_FIXTURE_PATH", fixture_copy)

    # When / Then
    with pytest.raises(AssertionError, match="missing source article"):
        _ = fixture_graph()


def test_article_index_when_duplicate_document_ids_then_rejects() -> None:
    # Given
    article = ArticleDocument.model_validate_json(
        (RAG_FIXTURE_PATH / "articles" / "21.json").read_text("utf-8")
    )

    # When / Then
    with pytest.raises(AssertionError, match=DUPLICATE_ARTICLE_DOCUMENT_ID):
        _ = index_articles((article, article))


def test_enrichment_index_when_duplicate_enrichment_ids_then_rejects() -> None:
    # Given
    enrichment = ArticleEnrichment.model_validate_json(
        (RAG_FIXTURE_PATH / "enrichments" / "21.json").read_text("utf-8")
    )

    # When / Then
    with pytest.raises(AssertionError, match=DUPLICATE_ARTICLE_ENRICHMENT_ID):
        _ = index_enrichments((enrichment, enrichment))


def test_rag_fixture_article_when_evidence_is_malformed_then_t1_rejects_it() -> None:
    # Given
    source = next((RAG_FIXTURE_PATH / "articles").glob("*.json")).read_text("utf-8")
    payload = source.replace('"start_offset":0', '"start_offset":1', 1)
    assert payload != source

    # When / Then
    with pytest.raises(ValidationError, match="evidence_excerpt_mismatch"):
        _ = ArticleDocument.model_validate_json(payload)


def test_rag_fixture_enrichment_with_forged_source_span_rejects() -> None:
    # Given
    manifest, articles, enrichments, questions = fixture_graph()
    article_id = next(iter(enrichments))
    source_span = articles[article_id].evidence_spans[0]
    forged_span = source_span.model_copy(
        update={
            "excerpt": "FORGED EVIDENCE",
            "end_offset": source_span.start_offset + len("FORGED EVIDENCE"),
            "span_fingerprint": (
                "sha256:557b899b942e086f0c9b23a9104d4a7f77777324c8c91eb677f6130d64c8a7bd"
            ),
        }
    )
    enrichments[article_id] = enrichments[article_id].model_copy(
        update={"evidence_spans": (forged_span,)}
    )

    # When / Then
    with pytest.raises(AssertionError):
        assert_fixture_graph(manifest, articles, enrichments, questions)


def remove_provenance(manifest: ManifestFixture, _questions: QuestionsFixture) -> None:
    manifest["provenance"] = ""


def remove_license(manifest: ManifestFixture, _questions: QuestionsFixture) -> None:
    manifest["license"] = ""


def reduce_count(manifest: ManifestFixture, _questions: QuestionsFixture) -> None:
    manifest["corpus_count"] = 29


def remove_coverage(manifest: ManifestFixture, _questions: QuestionsFixture) -> None:
    manifest["coverage"] = ["duplicate"]


def remove_expected_source(
    _manifest: ManifestFixture, questions: QuestionsFixture
) -> None:
    first_question = next(iter(questions["questions"]))
    first_reference = next(iter(first_question["expected_references"]))
    first_reference["article_id"] = "00000000-0000-4000-8000-000000000000"


def leak_scope(manifest: ManifestFixture, _questions: QuestionsFixture) -> None:
    manifest["scope_cases"][0]["allowed_article_ids"].append(
        manifest["scope_cases"][0]["restricted_article_id"]
    )


@pytest.mark.parametrize(
    "mutation",
    [
        remove_provenance,
        remove_license,
        reduce_count,
        remove_coverage,
        remove_expected_source,
        leak_scope,
    ],
)
def test_rag_fixture_labels_when_required_link_or_metadata_is_missing_then_reject(
    mutation: Mutation,
) -> None:
    # Given
    manifest, articles, enrichments, questions = fixture_graph()
    invalid_manifest = deepcopy(manifest)
    invalid_questions = deepcopy(questions)

    # When
    mutation(invalid_manifest, invalid_questions)

    # Then
    with pytest.raises(AssertionError):
        assert_fixture_graph(invalid_manifest, articles, enrichments, invalid_questions)
