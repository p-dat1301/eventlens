from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from backend.event_pipeline.application.ports import EventExtractor
from backend.event_pipeline.infra.fake_extractor import FakeEventExtractor
from backend.shared.contracts.bundle import ArticleBundle

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"


def load_bundle() -> ArticleBundle:
    return ArticleBundle.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_extract_builds_scheduled_event_from_published_article() -> None:
    # Given
    bundle = load_bundle()
    article = bundle.article
    extractor = FakeEventExtractor()

    # When
    proposal = extractor.extract(bundle)

    # Then
    expected_event_id = uuid5(NAMESPACE_URL, f"eventlens:event:{article.article_id}")
    assert proposal.proposal_id == uuid5(
        NAMESPACE_URL,
        f"eventlens:proposal:{article.article_id}:fake.v1",
    )
    assert proposal.article_id == article.article_id
    assert proposal.extractor_version == "fake.v1"
    assert proposal.event.confidence == 0.90
    assert proposal.event.id == expected_event_id
    assert proposal.event.event_type == "SCHEDULED_EVENT"
    assert proposal.event.canonical_title == article.title
    assert proposal.event.status == "detected"
    assert proposal.event.severity == "info"
    assert proposal.event.time_start == article.published_at
    assert proposal.event.time_end is None
    assert proposal.event.time_precision == "exact"
    assert proposal.event.first_seen_at == article.published_at
    assert proposal.event.last_seen_at == article.published_at
    assert proposal.event.last_changed_at == article.published_at
    assert proposal.event.source_count == 1
    assert proposal.event.article_count == 1
    assert proposal.event.confidence == 0.90
    assert proposal.event.attributes is None
    assert proposal.event.schema_version == "event.v1"
    assert proposal.event.created_at == article.published_at
    assert proposal.event.updated_at == article.published_at
    assert proposal.event.entities == ()


def test_extract_uses_extraction_time_when_publication_time_unknown() -> None:
    # Given
    bundle = load_bundle()
    article = bundle.article.model_copy(update={"published_at": None})
    undated_bundle = bundle.model_copy(update={"article": article})

    # When
    proposal = FakeEventExtractor().extract(undated_bundle)

    # Then
    extracted_at = article.processing.extracted_at
    assert proposal.event.time_start is None
    assert proposal.event.time_precision == "unknown"
    assert proposal.event.first_seen_at == extracted_at
    assert proposal.event.last_seen_at == extracted_at
    assert proposal.event.last_changed_at == extracted_at
    assert proposal.event.created_at == extracted_at
    assert proposal.event.updated_at == extracted_at


def test_extract_is_deterministic_and_satisfies_port() -> None:
    # Given
    bundle = load_bundle()
    extractor = FakeEventExtractor()

    # When
    first = extractor.extract(bundle)
    second = extractor.extract(bundle)

    # Then
    assert isinstance(extractor, EventExtractor)
    assert first == second
