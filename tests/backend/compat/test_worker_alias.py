import re
from pathlib import Path

import pytest

from backend.compat.worker_alias import WARNING, main

BUNDLE = Path(__file__).parents[3] / "fixtures/article_bundle.v1.json"
# Rich colours Typer help when it detects CI (e.g. GITHUB_ACTIONS=true), which
# splits option names with ANSI escapes; compare the plain text.
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_alias_warns_then_forwards_event_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When
    with pytest.raises(SystemExit) as exit_info:
        main(["validate", str(BUNDLE)])

    # Then
    captured = capsys.readouterr()
    assert exit_info.value.code == 0
    assert captured.err.startswith(WARNING)
    assert "Valid article" in captured.out


def test_alias_forwards_rag_worker_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # When
    with pytest.raises(SystemExit) as exit_info:
        main(["enrich", "--help"])

    # Then
    assert exit_info.value.code == 0
    assert "--max-jobs" in ANSI.sub("", capsys.readouterr().out)


def test_alias_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    # When
    with pytest.raises(SystemExit) as exit_info:
        main(["bogus"])

    # Then
    assert exit_info.value.code == 2
    assert "usage: eventlens-worker" in capsys.readouterr().err
