"""Import a validated handoff batch through the persist-first boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from backend.rag.application.handoff_validation import HandoffReport
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.source_versions import SourceVersionStore
from backend.rag.application.use_cases.persist import AccessDeniedError
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_ingest import IngestClock
from backend.rag.domain.handoff import (
    SourceProvenance,
    document_from_handoff,
    provenance_of,
)

if TYPE_CHECKING:
    from backend.rag.domain.article_document import ArticleDocument


class HandoffRejectedError(ValueError):
    """The batch has error-level findings; nothing was written."""

    code: ClassVar[str] = "handoff_rejected"

    def __init__(self, errors: int) -> None:
        """Report only the number of findings; details live in the report."""
        super().__init__(f"handoff batch has {errors} error-level findings")


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    """Counts of what the import changed; repeat imports change nothing."""

    export_id: str
    records: int
    revisions_inserted: int
    provenance_inserted: int
    current: int


@dataclass(frozen=True, slots=True)
class ImportHandoff:
    """Validate-then-authorize, then persist the whole batch atomically.

    The store writes every revision, its lexical pointer, its search outbox
    event (database trigger) and its provenance row in one transaction, so a
    rejected batch or a provenance conflict leaves no partial import. No
    enrichment job is enqueued and no model is called.
    """

    store: SourceVersionStore
    access: AccessPolicy
    clock: Callable[[], datetime]

    def execute(self, report: HandoffReport, caller: CallerScope) -> ImportReceipt:
        """Refuse invalid batches; otherwise persist every record."""
        if not report.ok or report.export_id is None:
            raise HandoffRejectedError(len(report.errors))
        if not self.access.allows(caller):
            raise AccessDeniedError(caller.principal_id)
        export_id = report.export_id
        items: list[tuple[ArticleDocument, SourceProvenance]] = []
        for record in report.records:
            ingested_at = self.clock()
            document = document_from_handoff(
                record, IngestClock(ingested_at=ingested_at, processed_at=self.clock())
            )
            items.append((document, provenance_of(record, document, export_id)))
        outcomes = self.store.persist_batch(items)
        return ImportReceipt(
            export_id=export_id,
            records=len(report.records),
            revisions_inserted=sum(o.revision_inserted for o in outcomes),
            provenance_inserted=sum(o.provenance_inserted for o in outcomes),
            current=sum(o.current for o in outcomes),
        )
