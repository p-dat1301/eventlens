from pathlib import Path

from eventlens_worker.application import BundleValidation, validate_bundle_json

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"


def test_validation_use_case_summarizes_valid_bundle() -> None:
    # Given
    fixture_json = FIXTURE_PATH.read_text(encoding="utf-8")

    # When
    result = validate_bundle_json(fixture_json)

    # Then
    assert result == BundleValidation(
        article_id="550e8400-e29b-41d4-a716-446655440000",
        media_count=2,
    )
