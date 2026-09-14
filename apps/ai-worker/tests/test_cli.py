from pathlib import Path

from typer.testing import CliRunner

from eventlens_worker.cli import app

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"


def test_validate_command_accepts_valid_fixture() -> None:
    # Given
    runner = CliRunner()

    # When
    result = runner.invoke(app, ["validate", str(FIXTURE_PATH)])

    # Then
    assert result.exit_code == 0
    assert result.stdout == (
        "Valid article 550e8400-e29b-41d4-a716-446655440000 with 2 media items.\n"
    )


def test_validate_command_rejects_invalid_fixture(tmp_path: Path) -> None:
    # Given
    invalid_fixture = tmp_path / "invalid.json"
    _ = invalid_fixture.write_text("{}", encoding="utf-8")
    runner = CliRunner()

    # When
    result = runner.invoke(app, ["validate", str(invalid_fixture)])

    # Then
    assert result.exit_code == 2
