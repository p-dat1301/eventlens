"""Import a validated handoff batch through the persist-first boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from backend.rag.application.handoff_validation import HandoffReport
from backend.rag.application.ports.access import AccessPolicy
from backend.rag.application.ports.source_versions import SourceVersionStore
from backend.rag.application.use_cases.persist import AccessDeniedError
from backend.rag.domain.access import CallerScope
from backend.rag.domain.article_ingest import IngestClock
from backend.rag.domain.handoff import document_from_handoff, provenance_of


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
    """Authorize once, then persist each record with its provenance.

    No enrichment job is enqueued here; the worker's reconciliation sweep picks
    the revisions up when enrichment runs. No model is called.
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
        inserted = provenance = current = 0
        for record in report.records:
            ingested_at = self.clock()
            document = document_from_handoff(
                record, IngestClock(ingested_at=ingested_at, processed_at=self.clock())
            )
            outcome = self.store.persist_with_provenance(
                document, provenance_of(record, document, report.export_id)
            )
            inserted += outcome.revision_inserted
            provenance += outcome.provenance_inserted
            current += outcome.current
        return ImportReceipt(
            export_id=report.export_id,
            records=len(report.records),
            revisions_inserted=inserted,
            provenance_inserted=provenance,
            current=current,
        )
