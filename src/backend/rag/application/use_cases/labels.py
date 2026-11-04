"""Project enrichment publications into catalog topic/location labels.

Re-running is the invalidation path: a new publication, a newly trusted
model or a revoked model makes the revision pending again.

Only publications whose ``model_version`` is explicitly trusted become
labels; fake/fixture enrichers stay ``unavailable`` so no fake metadata ever
looks like real data. The write is fenced on the revision still being
current, and manual labels are never touched.
"""

from dataclasses import dataclass, field

from backend.rag.application.ports.catalog import ArticleLabelStore
from backend.rag.domain.taxonomy import derive_labels

DEFAULT_BATCH = 500


@dataclass(slots=True)
class LabelSyncReport:
    """Counts of one sync pass."""

    applied: int = 0
    untrusted_model: int = 0
    invalid_metadata: int = 0
    superseded: int = 0
    unmatched_locations: int = 0

    def as_dict(self) -> dict[str, int]:
        """JSON-friendly counts."""
        return {
            "applied": self.applied,
            "untrusted_model": self.untrusted_model,
            "invalid_metadata": self.invalid_metadata,
            "superseded": self.superseded,
            "unmatched_locations": self.unmatched_locations,
        }


@dataclass(frozen=True, slots=True)
class SyncArticleLabels:
    """Idempotent label projection; safe to run from any worker or CLI."""

    store: ArticleLabelStore
    trusted_models: frozenset[str] = field(default_factory=frozenset)

    def execute(self, limit: int = DEFAULT_BATCH) -> LabelSyncReport:
        """Sync up to ``limit`` new, re-published or trust-changed revisions."""
        report = LabelSyncReport()
        pending = self.store.pending(limit, self.trusted_models)
        if not pending:
            return report
        aliases = self.store.alias_index()
        for source in pending:
            metadata = source.metadata
            if source.metadata_invalid or metadata is None:
                outcome, labels = "invalid_metadata", None
            elif metadata.model_version not in self.trusted_models:
                outcome, labels = "untrusted_model", None
            else:
                outcome, labels = "applied", derive_labels(metadata, aliases)
            if not self.store.apply(source, labels, outcome):
                report.superseded += 1
                continue
            match outcome:
                case "applied":
                    report.applied += 1
                    report.unmatched_locations += (
                        0 if labels is None else labels.unmatched_locations
                    )
                case "untrusted_model":
                    report.untrusted_model += 1
                case _:
                    report.invalid_metadata += 1
        return report
