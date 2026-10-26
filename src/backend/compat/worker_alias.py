"""Deprecated ``eventlens-worker`` alias; remove once nothing calls it.

Forwards ``validate|process`` to ``eventlens-events`` and ``ingest|enrich`` to
``eventlens-rag-worker`` after one deprecation warning on stderr. No command
logic lives here.
"""

import sys
from collections.abc import Sequence

from backend.event_pipeline.presentation.cli import app as events_app
from backend.rag.worker_main import app as rag_worker_app

EVENT_COMMANDS = frozenset({"validate", "process"})
RAG_COMMANDS = frozenset({"ingest", "enrich"})
WARNING = (
    "DeprecationWarning: eventlens-worker is deprecated; use "
    "eventlens-events validate|process or eventlens-rag-worker ingest|enrich"
)
USAGE_EXIT = 2


def main(argv: Sequence[str] | None = None) -> None:
    """Warn once, then dispatch to the owning CLI unchanged."""
    args = list(sys.argv[1:] if argv is None else argv)
    print(WARNING, file=sys.stderr)  # noqa: T201 - CLI warning channel
    command = args[0] if args else ""
    if command in EVENT_COMMANDS:
        events_app(args=args, prog_name="eventlens-worker")
    elif command in RAG_COMMANDS:
        rag_worker_app(args=args, prog_name="eventlens-worker")
    else:
        print(  # noqa: T201 - CLI usage channel
            "usage: eventlens-worker {validate,process,ingest,enrich} ...",
            file=sys.stderr,
        )
        raise SystemExit(USAGE_EXIT)
