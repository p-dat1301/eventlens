from pathlib import Path
from typing import NoReturn

import pytest
from typer.testing import CliRunner

from backend.event_pipeline.domain.models import GateVerdict, ProcessingTrace
from backend.event_pipeline.presentation.cli import app
from backend.shared.contracts.bundle import ArticleBundle

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"
GOLDEN_TRACE_PATH = (
    Path(__file__).parents[3] / "fixtures/event_pipeline_accept.trace.v1.json"
)
FILE_READ_ERROR = "error:file_read_failed\n"
VALIDATION_ERROR = "error:bundle_validation_failed\n"


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
    assert result.stderr == VALIDATION_ERROR


def test_validate_command_rejects_invalid_utf8_without_traceback(
    tmp_path: Path,
) -> None:
    # Given
    invalid_fixture = tmp_path / "invalid.json"
    _ = invalid_fixture.write_bytes(b"\xff\xfe")

    # When
    result = CliRunner().invoke(app, ["validate", str(invalid_fixture)])

    # Then
    assert result.exit_code == 2
    assert result.stderr == FILE_READ_ERROR


def test_validate_command_maps_file_read_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    fixture = tmp_path / "unreadable.json"
    _ = fixture.write_text("{}", encoding="utf-8")

    def fail_read_text(_path: Path, *, encoding: str) -> NoReturn:
        assert encoding == "utf-8"
        raise OSError

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    # When
    result = CliRunner().invoke(app, ["validate", str(fixture)])

    # Then
    assert result.exit_code == 2
    assert result.stderr == FILE_READ_ERROR


def test_process_command_matches_golden_trace_across_independent_invocations() -> None:
    # Given
    expected = ProcessingTrace.model_validate_json(
        GOLDEN_TRACE_PATH.read_text(encoding="utf-8")
    )

    # When
    first = CliRunner().invoke(app, ["process", str(FIXTURE_PATH)])
    second = CliRunner().invoke(app, ["process", str(FIXTURE_PATH)])

    # Then
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert ProcessingTrace.model_validate_json(first.stdout) == expected
    assert ProcessingTrace.model_validate_json(second.stdout) == expected
    assert expected.schema_version == "processing_trace.v1"


def test_process_command_rejects_invalid_fixture(tmp_path: Path) -> None:
    # Given
    invalid_fixture = tmp_path / "invalid.json"
    _ = invalid_fixture.write_text("{}", encoding="utf-8")

    # When
    result = CliRunner().invoke(app, ["process", str(invalid_fixture)])

    # Then
    assert result.exit_code == 2
    assert result.stderr == VALIDATION_ERROR


def test_process_command_hides_invalid_field_value(tmp_path: Path) -> None:
    # Given
    raw_field_value = "RAW-SENSITIVE-ARTICLE-CONTENT"
    invalid_fixture = tmp_path / "invalid.json"
    _ = invalid_fixture.write_text(
        f'{{"schema_version":"article_bundle.v1","article":"{raw_field_value}"}}',
        encoding="utf-8",
    )

    # When
    result = CliRunner().invoke(app, ["process", str(invalid_fixture)])

    # Then
    assert result.exit_code == 2
    assert result.stderr == VALIDATION_ERROR
    assert raw_field_value not in result.stderr


def test_process_command_rejects_invalid_utf8_without_traceback(tmp_path: Path) -> None:
    # Given
    invalid_fixture = tmp_path / "invalid.json"
    _ = invalid_fixture.write_bytes(b"\xff\xfe")

    # When
    result = CliRunner().invoke(app, ["process", str(invalid_fixture)])

    # Then
    assert result.exit_code == 2
    assert result.stderr == FILE_READ_ERROR


def test_process_command_treats_reject_as_success(tmp_path: Path) -> None:
    # Given
    bundle = ArticleBundle.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))
    duplicate = bundle.article.duplicate.model_copy(update={"is_duplicate": True})
    rejected = bundle.model_copy(
        update={"article": bundle.article.model_copy(update={"duplicate": duplicate})}
    )
    rejected_fixture = tmp_path / "rejected.json"
    _ = rejected_fixture.write_text(rejected.model_dump_json(), encoding="utf-8")

    # When
    result = CliRunner().invoke(app, ["process", str(rejected_fixture)])

    # Then
    assert result.exit_code == 0
    assert (
        ProcessingTrace.model_validate_json(result.stdout).verdict is GateVerdict.REJECT
    )


def test_process_command_treats_review_as_success(tmp_path: Path) -> None:
    # Given
    bundle = ArticleBundle.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))
    processing = bundle.article.processing.model_copy(
        update={"quality_status": "flagged", "quality_flags": ("short_content",)}
    )
    reviewed = bundle.model_copy(
        update={"article": bundle.article.model_copy(update={"processing": processing})}
    )
    reviewed_fixture = tmp_path / "reviewed.json"
    _ = reviewed_fixture.write_text(reviewed.model_dump_json(), encoding="utf-8")

    # When
    result = CliRunner().invoke(app, ["process", str(reviewed_fixture)])

    # Then
    assert result.exit_code == 0
    assert (
        ProcessingTrace.model_validate_json(result.stdout).verdict is GateVerdict.REVIEW
    )
