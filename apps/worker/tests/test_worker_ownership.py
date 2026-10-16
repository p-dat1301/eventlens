from pathlib import Path

from eventlens_worker.bundle import ArticleBundle


def test_bundle_owned_by_worker() -> None:
    # Given / When
    owner = Path(__file__).parents[1] / "src/eventlens_worker/bundle.py"

    # Then
    assert owner.is_file()
    assert ArticleBundle.__module__ == "eventlens_worker.bundle"
