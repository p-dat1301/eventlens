# EventLens Event Pipeline and RAG Worker CLIs

The event pipeline is its own bounded context (`src/backend/event_pipeline`,
CLI `eventlens-events`). RAG ingest/enrichment belong to the RAG context
(`src/backend/rag/worker_main.py`, CLI `eventlens-rag-worker`). The old
`eventlens-worker` command is a deprecated alias that forwards to both.

Local deterministic vertical slice for turning one validated article bundle into one
event-processing trace. It demonstrates worker boundaries and policy flow. It is not a
production ingestion, inference, or persistence worker.

## Commands

Run from repository root:

```bash
uv run eventlens-events validate fixtures/article_bundle.v1.json
uv run eventlens-events process fixtures/article_bundle.v1.json
```

`validate` parses one `ArticleBundle` and reports article ID plus media count. Invalid
input exits with code 2.

`process` parses one bundle, runs local adapters, then prints one JSON worker-local
`ProcessingTrace`:

```text
ArticleBundle -> FakeEventExtractor -> EventProposal -> deterministic gate
              -> InMemoryEventWriter (ACCEPT only) -> ProcessingTrace
```

Invalid input exits with code 2. Valid `REJECT` and `REVIEW` outcomes still exit with
code 0 because verdict is pipeline output, not CLI failure.

## Local policy

`FakeEventExtractor` derives stable IDs and event fields only from bundle data. For this
synthetic local behavior, it maps article `published_at` directly to event `time_start`
with `time_precision="exact"`; a missing `published_at` maps to no `time_start` and
`time_precision="unknown"`. This mapping demonstrates pipeline flow and does not claim
article publication time is real event start time. Extractor emits fixed confidence
`0.90`. Gate returns:

- `ACCEPT`: proposal is ready; writer inserts event and trace includes write disposition.
- `REJECT`: article ID mismatch, duplicate input, or confidence below `0.50`; no write.
- `REVIEW`: flagged input or missing event time, unless reject reason also applies; no write.

Confidence `0.90`, threshold `0.50`, and gate precedence are local demonstration policy.
They are not canonical EventLens business policy.

## Writer semantics

`InMemoryEventWriter` is insert-only local state:

- First event ID write returns `inserted`.
- Exact replay within same writer instance returns `unchanged`.
- Same event ID with different event data raises conflict and keeps original event.
- Writer resets for every `process` CLI invocation.
- No living-event update or merge behavior exists.
- Writer is not concurrent or durable storage.

Batch processing is ordered and non-atomic. Any port-defined
`EventWriteConflictError`, including conflict on first item, becomes
`BatchAbortedError`. Its `completed` tuple contains traces for prior completed items,
not guarantee those items produced durable side effects; `event_id` identifies
conflicting event. Other adapter exceptions have no wrapping guarantee.

`ProcessingTrace` is worker-local output, not a cross-language contract despite its
worker schema-version marker. Fixture `fixtures/event_pipeline_accept.trace.v1.json`
exercises this worker-local schema; canonical contract fixtures are described in root
README. For `REJECT` and `REVIEW`, `event_id` identifies proposed event even though no
event was persisted; absence of `write_disposition` records that no write occurred.

## RAG ingest and enrichment (T4)

`validate` and `process` above are unchanged and never touch the RAG database. Two
further commands belong to the RAG context; adapters are composed in
`backend/rag/composition.py` (the RAG composition root). Both read
`EVENTLENS_RAG_DATABASE_URL` (or `--database-url`) and first apply the
checksum-verified RAG migrations; use the separate `eventlens_rag` database, never
the legacy Compose `eventlens` database.

```bash
export EVENTLENS_RAG_DATABASE_URL=postgresql://eventlens:eventlens-local@127.0.0.1:5432/eventlens_rag
uv run eventlens-rag-worker ingest fixtures/article_bundle.v1.json
uv run eventlens-rag-worker enrich --max-jobs 5
uv run eventlens-rag-worker enrich --provider fake-timeout --max-jobs 5
```

- `ingest BUNDLE_PATH`: validates the bundle, converts its `Article` to an
  `ArticleDocument`, and commits the document (lexical search available at once) and
  its PENDING enrichment job in one transaction. Prints one JSON line
  `{"event": "article_ingested", "article_id", "content_hash", "job_id",
  "job_created", "current"}`. Re-delivery returns the same `job_id` with
  `job_created: false`.
- `enrich [--max-jobs N] [--provider fake|fake-timeout]`: reconciles current articles
  without a job, then claims and processes at most `N` eligible jobs (1-25, default
  25 = policy `max_jobs_per_pass`). Prints one `enrichment_job` JSON line per job
  (IDs, outcome, failure code, retry delay, index generation; never article text)
  and a final `enrichment_pass` summary. `fake` is the deterministic local enricher;
  `fake-timeout` is fault injection with the same identity whose every call times
  out (retry 30 s, 120 s, then FAILED). No live provider can be selected; an API key
  in the environment changes nothing.

Exit codes: `0` success, including FAILED/RETRY outcomes (they are pipeline output);
`2` invalid bundle or option; `3` `rag_storage_unavailable`; `1`
`rag_migration_failed`. Retry/lease policy and lifecycle are documented in
`docs/rag.md`. The enrichment path does not use `FakeEventExtractor`,
`InMemoryEventWriter` or `event.v1`.

## Deferred adapters

Production MinIO and Parquet readers, event database writers, model or LLM event
extractors, MCP integration, a live enrichment provider transport, and living-event
updates remain intentionally deferred.
