"""PostgreSQL ``SummaryRepository`` (revision ``rag_0004``).

``publish`` is one transaction: lock the job under its claim fence, lock the
current-revision row ``FOR SHARE``, then either insert the summary and mark
the job DONE, or mark it STALE when the revision is no longer current. The
first fenced publication of an identity wins (``ON CONFLICT DO NOTHING``).
"""

# ruff: noqa: S608 - SQL is built from reviewed constant fragments; values are bound.

import json
from datetime import datetime
from typing import LiteralString
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import Engine

from backend.rag.application.ports.summary import PublishResult
from backend.rag.domain.summary import (
    SummaryModelIdentity,
    SummaryResult,
    SummarySource,
    key_points_json,
)
from backend.rag.domain.work_jobs import JobFence
from backend.rag.infra.postgres.engine import execute, first, transaction
from backend.rag.infra.postgres.work_queue import FENCE, fence_params, fenced_update

LOAD_SQL: LiteralString = """
SELECT title, normalized_content, document_json->>'source_name', source_published_at
FROM rag_article_documents
WHERE article_id = %(id)s::uuid AND content_hash = %(hash)s
  AND document_json IS NOT NULL
"""

CURRENT_SQL: LiteralString = """
SELECT 1 FROM rag_article_current
WHERE article_id = %(id)s::uuid AND content_hash = %(hash)s
  AND tombstoned_at IS NULL
"""

INSERT_SQL: LiteralString = """
INSERT INTO rag_article_summaries (summary_id, article_id, content_hash, job_id,
    claim_generation, summary_policy_id, model_id, model_version, prompt_version,
    schema_version, summary_text, key_points, input_chars, usage)
VALUES (%(summary)s::uuid, %(id)s::uuid, %(hash)s, %(job)s::uuid, %(gen)s,
    %(policy)s, %(model_id)s, %(model_version)s, %(prompt)s, %(schema)s,
    %(text)s, %(points)s::jsonb, %(chars)s, %(usage)s::jsonb)
ON CONFLICT (article_id, content_hash, model_id, model_version, prompt_version,
             schema_version) DO NOTHING
"""

_SOURCE_ROW = TypeAdapter(tuple[str, str, str, datetime | None])


class PostgresSummaries:
    """Exact-revision loads and fenced summary publication."""

    def __init__(self, engine: Engine) -> None:
        """Bind the shared engine."""
        self._engine: Engine = engine

    def load(self, article_id: UUID, content_hash: str) -> SummarySource | None:
        """Allowlisted fields of exactly this revision."""
        with transaction(self._engine, read_only=True) as connection:
            row = first(
                execute(connection, LOAD_SQL, {"id": article_id, "hash": content_hash})
            )
        if row is None:
            return None
        title, content, source_name, published = _SOURCE_ROW.validate_python(row)
        return SummarySource(
            article_id, content_hash, title, content, source_name, published
        )

    def is_current(self, article_id: UUID, content_hash: str) -> bool:
        """Current and not tombstoned."""
        with transaction(self._engine, read_only=True) as connection:
            row = first(
                execute(
                    connection, CURRENT_SQL, {"id": article_id, "hash": content_hash}
                )
            )
        return row is not None

    def publish(  # noqa: PLR0913 - mirrors the port
        self,
        *,
        fence: JobFence,
        source: SummarySource,
        identity: SummaryModelIdentity,
        policy_id: str,
        schema_version: str,
        result: SummaryResult,
    ) -> PublishResult:
        """Fenced, version-checked publication in one transaction."""
        revision = {"id": source.article_id, "hash": source.content_hash}
        with transaction(self._engine) as connection:
            owned = first(
                execute(
                    connection,
                    "SELECT 1 FROM rag_work_jobs WHERE " + FENCE + " FOR UPDATE",
                    fence_params(fence),
                )
            )
            if owned is None:
                return "lost"
            current = first(execute(connection, CURRENT_SQL + " FOR SHARE", revision))
            if current is None:
                _ = fenced_update(
                    connection,
                    fence,
                    "status = 'STALE', failure_code = 'revision_superseded'",
                )
                return "stale"
            _ = execute(
                connection,
                INSERT_SQL,
                revision
                | {
                    "summary": uuid4(),
                    "job": fence.job_id,
                    "gen": fence.claim_generation,
                    "policy": policy_id,
                    "model_id": identity.model_id,
                    "model_version": identity.model_version,
                    "prompt": identity.prompt_version,
                    "schema": schema_version,
                    "text": result.text,
                    "points": json.dumps(key_points_json(result.key_points)),
                    "chars": result.input_chars,
                    "usage": None if result.usage is None else json.dumps(result.usage),
                },
            )
            _ = fenced_update(connection, fence, "status = 'DONE'")
        return "published"
