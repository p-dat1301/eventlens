"""Local agent and build artifacts must stay out of Docker contexts, Git and lint."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def entries(path: Path) -> set[str]:
    """Non-comment lines of an ignore file, without trailing slashes."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return {
        line.strip().rstrip("/") for line in lines if line.strip()[:1] not in ("", "#")
    }


def test_docker_context_excludes_agent_worktrees() -> None:
    # Given / When
    ignored = entries(ROOT / ".dockerignore")

    # Then: Python Dockerfiles use COPY . . from the repository root
    assert {".claude", ".omo", "dist"} <= ignored


def test_git_ignores_agent_worktrees() -> None:
    # Given / When
    ignored = entries(ROOT / ".gitignore")

    # Then
    assert ".claude" in ignored


def test_ruff_runs_on_explicit_source_paths() -> None:
    # Given
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    # When
    ruff_lines = [
        line.strip() for line in makefile.splitlines() if "uv run ruff" in line
    ]

    # Then
    assert "PY_PATHS := src tests typings" in makefile
    assert ruff_lines
    assert all(line.endswith("$(PY_PATHS)") for line in ruff_lines)
