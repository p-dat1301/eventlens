"""Normalized catalog snapshot of the RAG schema in ``current_schema()``.

Used by the adoption preflight (live schema must equal the packaged
``expected_schema.json``) and by tests that compare a legacy-runner database
with an Alembic-built one. Object definitions come from PostgreSQL's own
deparsers (``pg_get_*def``), so equal snapshots mean equal behavior-relevant
DDL: columns and their order, defaults, generated expressions, identities,
constraints, indexes (GIN/HNSW), views, triggers and trigger functions.
Ownership, privileges, OIDs and extension versions are deliberately excluded.
"""

import json
from importlib.resources import files

from pydantic import TypeAdapter
from sqlalchemy import Connection

from backend.rag.infra.postgres.engine import execute, rows, scalar

RAG_TABLES = (
    "rag_article_current",
    "rag_article_documents",
    "rag_chunks",
    "rag_enrichment_jobs",
    "rag_enrichment_publications",
    "rag_enrichment_versions",
    "rag_index_generations",
    "rag_schema_migrations",
    "rag_source_references",
)
RAG_VIEWS = ("rag_current_lexical", "rag_current_vectors")
RAG_FUNCTIONS = ("rag_immutable_revision",)

type Snapshot = dict[str, list[list[str]]]

_SNAPSHOT = TypeAdapter(dict[str, list[list[str]]])
_TEXT_ROWS = TypeAdapter(list[list[str]])

# Every query is scoped to current_schema() and the fixed RAG object names;
# Alembic's own alembic_version table is excluded on purpose.
_QUERIES: dict[str, str] = {
    "columns": """
SELECT c.relname, a.attnum::text, a.attname, format_type(a.atttypid, a.atttypmod),
       a.attnotnull::text, coalesce(pg_get_expr(d.adbin, d.adrelid), ''),
       a.attgenerated::text, a.attidentity::text
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE n.nspname = current_schema() AND a.attnum > 0 AND NOT a.attisdropped
  AND c.relname = ANY(%(relations)s)
ORDER BY c.relname, a.attnum""",
    "constraints": """
SELECT c.relname, k.conname, k.contype::text, pg_get_constraintdef(k.oid, true)
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relname = ANY(%(tables)s)
ORDER BY c.relname, k.conname""",
    "indexes": """
SELECT t.relname, i.relname, pg_get_indexdef(i.oid)
FROM pg_index x
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = current_schema() AND t.relname = ANY(%(tables)s)
ORDER BY t.relname, i.relname""",
    "views": """
SELECT c.relname, pg_get_viewdef(c.oid, true)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relkind = 'v'
  AND c.relname = ANY(%(views)s)
ORDER BY c.relname""",
    "triggers": """
SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid, true)
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND NOT t.tgisinternal
  AND c.relname = ANY(%(tables)s)
ORDER BY c.relname, t.tgname""",
    "functions": """
SELECT p.proname, l.lanname, pg_get_function_result(p.oid),
       pg_get_function_identity_arguments(p.oid), p.prosrc
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = current_schema() AND p.proname = ANY(%(functions)s)
ORDER BY p.proname""",
    "relations": """
SELECT c.relname, c.relkind::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relname LIKE 'rag\\_%%'
  AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
ORDER BY c.relname""",
}


def snapshot(connection: Connection) -> Snapshot:
    """Read the normalized RAG catalog of ``current_schema()``."""
    schema = TypeAdapter(str).validate_python(
        scalar(execute(connection, "SELECT current_schema()"))
    )
    params = {
        "relations": [*RAG_TABLES, *RAG_VIEWS],
        "tables": list(RAG_TABLES),
        "views": list(RAG_VIEWS),
        "functions": list(RAG_FUNCTIONS),
    }
    result: Snapshot = {}
    for name, query in _QUERIES.items():
        found = _TEXT_ROWS.validate_python(
            [list(row) for row in rows(execute(connection, query, params))]
        )
        result[name] = [[_unqualify(cell, schema) for cell in row] for row in found]
    has_vector = scalar(
        execute(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')",
        )
    )
    result["extensions"] = [["vector"]] if has_vector else []
    return result


def _unqualify(text: str, schema: str) -> str:
    """Drop the schema qualifier so copies in scoped test schemas compare."""
    return text.replace(f'"{schema}".', "").replace(f"{schema}.", "")


def expected_snapshot() -> Snapshot:
    """Packaged catalog of the schema state after historical migration 007."""
    resource = files("backend.rag.infra.postgres").joinpath("expected_schema.json")
    return _SNAPSHOT.validate_python(json.loads(resource.read_text(encoding="utf-8")))


def differences(actual: Snapshot, expected: Snapshot) -> list[str]:
    """Human-readable catalog drift, empty when the snapshots match."""
    problems: list[str] = []
    for section in sorted(expected.keys() | actual.keys()):
        want = {tuple(row) for row in expected.get(section, [])}
        have = {tuple(row) for row in actual.get(section, [])}
        problems.extend(
            f"{section}: missing {list(row)}" for row in sorted(want - have)
        )
        problems.extend(
            f"{section}: unexpected {list(row)}" for row in sorted(have - want)
        )
    return problems
