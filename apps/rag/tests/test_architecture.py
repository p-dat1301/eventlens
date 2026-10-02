import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_framework_imports_stay_inside_graph_and_adapters() -> None:
    # Given
    restricted = ROOT / "apps/rag/src/eventlens_rag"

    # When
    violations = [
        path
        for area in ("domain", "application", "ports")
        for path in (restricted / area).rglob("*.py")
        if "langgraph" in path.read_text(encoding="utf-8")
        or "psycopg" in path.read_text(encoding="utf-8")
    ]

    # Then
    assert violations == []


def test_importing_domain_does_not_load_langgraph() -> None:
    # Given: a clean interpreter without cached graph modules.
    code = (
        "import sys; "
        "from eventlens_rag.domain.query import BriefingQuery; "
        "assert 'langgraph' not in sys.modules; "
        "assert 'eventlens_rag.graph.runner' not in sys.modules"
    )

    # When: the domain model is imported in a fresh process.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal test code
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    # Then: domain import stays framework-free.
    assert result.returncode == 0, result.stderr
