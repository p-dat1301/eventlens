from pathlib import Path

from backend.shared.contracts.bundle import ArticleBundle


def test_bundle_owned_by_shared_contracts() -> None:
    # Given / When: ArticleBundle feeds both RAG ingest and the event pipeline,
    # so it lives in shared contracts and neither context imports the other.
    owner = Path(__file__).parents[3] / "src/backend/shared/contracts/bundle.py"

    # Then
    assert owner.is_file()
    assert ArticleBundle.__module__ == "backend.shared.contracts.bundle"
