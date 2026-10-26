"""Import boundaries of the modular monolith (bounded contexts + layers)."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "src/backend"
RAG = BACKEND / "rag"
FRAMEWORKS = ("fastapi", "starlette", "uvicorn", "typer", "langgraph", "psycopg")


def imports_of(path: Path) -> set[str]:
    """Absolute module names imported by one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def violations(area: Path, forbidden: tuple[str, ...]) -> list[tuple[str, str]]:
    """(file, import) pairs where a module under ``area`` imports a forbidden one."""
    return sorted(
        (str(path.relative_to(ROOT)), name)
        for path in area.rglob("*.py")
        for name in imports_of(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    )


def test_framework_imports_stay_inside_graph_and_adapters() -> None:
    # Given
    restricted = RAG

    # When
    found = [
        path
        for area in ("domain", "application")
        for path in (restricted / area).rglob("*.py")
        if "langgraph" in path.read_text(encoding="utf-8")
        or "psycopg" in path.read_text(encoding="utf-8")
    ]

    # Then
    assert found == []


def test_importing_domain_does_not_load_langgraph() -> None:
    # Given: a clean interpreter without cached graph modules.
    code = (
        "import sys; "
        "from backend.rag.domain.query import BriefingQuery; "
        "assert 'langgraph' not in sys.modules; "
        "assert 'backend.rag.workflows' not in sys.modules"
    )

    # When: the domain model is imported in a fresh process.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal test code
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    # Then: domain import stays framework-free.
    assert result.returncode == 0, result.stderr


def test_core_layers_never_import_typer_graph_or_concrete_adapters() -> None:
    # Given: framework-free domain/application (ports live in application).
    forbidden = ("import typer", "backend.rag.infra", "backend.rag.workflows")

    # When
    found = [
        (path.name, token)
        for area in ("domain", "application")
        for path in (RAG / area).rglob("*.py")
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]

    # Then
    assert found == []


def test_t5_retrieval_core_never_imports_frameworks_worker_or_event_writes() -> None:
    # Given: T5 domain/application/ports retrieval modules.
    modules = [
        RAG / "domain/retrieval.py",
        RAG / "domain/fusion.py",
        RAG / "application/use_cases/retrieve.py",
        RAG / "application/evidence_context.py",
        RAG / "application/ports/retrieval.py",
        RAG / "application/ports/reranker.py",
        RAG / "application/ports/tokens.py",
    ]
    forbidden = (
        "langgraph",
        "fastapi",
        "psycopg",
        "backend.rag.infra",
        "backend.rag.workflows",
        "backend.event_pipeline",
        "backend.shared.contracts.event",
    )

    # When
    found = [
        (path.name, token)
        for path in modules
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]

    # Then
    assert found == []


@pytest.mark.parametrize("context", ["crud", "rag", "event_pipeline"])
def test_domain_never_imports_frameworks_or_outer_layers(context: str) -> None:
    # Given
    base = f"backend.{context}"
    outer = tuple(
        f"{base}.{layer}"
        for layer in ("application", "workflows", "presentation", "infra")
    )

    # When / Then
    assert violations(BACKEND / context / "domain", FRAMEWORKS + outer) == []


@pytest.mark.parametrize("context", ["crud", "rag", "event_pipeline"])
def test_application_never_imports_adapters_or_delivery(context: str) -> None:
    # Given
    base = f"backend.{context}"
    forbidden = (
        *FRAMEWORKS,
        f"{base}.infra",
        f"{base}.presentation",
        f"{base}.workflows",
        f"{base}.composition",
    )

    # When / Then
    assert violations(BACKEND / context / "application", forbidden) == []


@pytest.mark.parametrize("context", ["crud", "rag", "event_pipeline"])
def test_presentation_never_touches_infra_or_database(context: str) -> None:
    # Given: delivery adapters reach storage only through application ports.
    base = f"backend.{context}"
    forbidden = ("psycopg", "langgraph", f"{base}.infra", f"{base}.workflows")

    # When / Then
    assert violations(BACKEND / context / "presentation", forbidden) == []


def test_workflows_call_application_never_concrete_adapters() -> None:
    # Given
    forbidden = (
        "psycopg",
        "fastapi",
        "typer",
        "backend.rag.infra",
        "backend.rag.presentation",
        "backend.rag.composition",
    )

    # When / Then
    assert violations(RAG / "workflows", forbidden) == []


def test_infra_never_imports_delivery_or_composition() -> None:
    # Given
    forbidden = (
        "fastapi",
        "typer",
        "langgraph",
        "backend.rag.presentation",
        "backend.rag.workflows",
        "backend.rag.composition",
        "backend.rag.api_main",
        "backend.rag.worker_main",
        "backend.rag.cli_main",
    )

    # When / Then
    assert violations(RAG / "infra", forbidden) == []


def test_only_composition_and_entry_points_reach_rag_adapters() -> None:
    # Given: outside infra, only the composition root wires adapters; worker and
    # maintenance entry points also map typed storage errors to exit codes. The
    # T9 evaluation package is an outer-ring harness like cli_main.
    allowed = {"composition.py", "worker_main.py", "cli_main.py"}

    # When
    importers = sorted(
        str(path.relative_to(RAG))
        for path in RAG.rglob("*.py")
        if not {"infra", "evaluation"} & set(path.relative_to(RAG).parts)
        and any(name.startswith("backend.rag.infra") for name in imports_of(path))
    )

    # Then
    assert set(importers) <= allowed
    assert "api_main.py" not in importers


def test_only_cli_reaches_the_evaluation_harness() -> None:
    # Given: evaluation wires fakes into graphs; no runtime layer may import it.
    importers = sorted(
        str(path.relative_to(RAG))
        for path in RAG.rglob("*.py")
        if "evaluation" not in path.relative_to(RAG).parts
        and any(name.startswith("backend.rag.evaluation") for name in imports_of(path))
    )

    # Then
    assert importers == ["cli_main.py"]


def test_evaluation_never_imports_live_provider_adapters() -> None:
    # Given
    forbidden = (
        "openai",
        "httpx",
        "backend.rag.infra.llm.openai_synthesis",
        "backend.rag.infra.llm.openai_enricher",
        "backend.rag.infra.embeddings.openai_embeddings",
        "backend.rag.infra.postgres",
    )

    # When / Then
    assert violations(RAG / "evaluation", forbidden) == []


def test_workflow_state_carries_no_session_client_or_adapter() -> None:
    # Given
    forbidden = (
        "psycopg",
        "sqlalchemy",
        "openai",
        "httpx",
        "backend.rag.infra",
        "backend.rag.application.use_cases",
    )

    # When
    found = [
        item
        for workflow in ("ingestion", "query")
        for item in violations(RAG / "workflows" / workflow / "state.py", forbidden)
    ]

    # Then
    assert found == []


def test_graphs_compile_only_in_graph_modules() -> None:
    # Given / When
    compiling = sorted(
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if ".compile()" in path.read_text(encoding="utf-8")
    )

    # Then
    assert compiling == [
        "rag/workflows/ingestion/graph.py",
        "rag/workflows/query/graph.py",
    ]


def test_routes_modules_stay_pure() -> None:
    # Given
    forbidden = (
        "langgraph",
        "psycopg",
        "backend.rag.infra",
        "backend.rag.application.use_cases",
    )

    # When
    found = [
        item
        for workflow in ("ingestion", "query")
        for item in violations(RAG / "workflows" / workflow / "routes.py", forbidden)
    ]

    # Then
    assert found == []


@pytest.mark.parametrize(
    ("context", "others"),
    [
        ("crud", ("backend.rag", "backend.event_pipeline")),
        ("rag", ("backend.crud", "backend.event_pipeline")),
        ("event_pipeline", ("backend.crud", "backend.rag")),
        ("shared", ("backend.crud", "backend.rag", "backend.event_pipeline")),
    ],
)
def test_bounded_contexts_never_import_each_other(
    context: str, others: tuple[str, ...]
) -> None:
    # Given / When / Then: contexts meet only through backend.shared.contracts.
    assert violations(BACKEND / context, others) == []


def test_rag_api_process_never_loads_crud_or_event_pipeline() -> None:
    # Given
    code = (
        "import sys; import backend.rag.api_main; "
        "assert not any(m.startswith(('backend.crud', 'backend.event_pipeline')) "
        "for m in sys.modules)"
    )

    # When
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal test code
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )

    # Then
    assert result.returncode == 0, result.stderr


def test_single_migration_source_and_no_legacy_packages() -> None:
    # Given / When
    sql_dirs = {path.parent for path in ROOT.glob("src/**/*.sql")}

    # Then: one migration source; old distributions are gone.
    assert sql_dirs == {RAG / "infra/postgres/migrations"}
    assert not (ROOT / "apps").exists()
    assert not (ROOT / "contracts").exists()
    assert not list(BACKEND.rglob("eventlens_*"))
