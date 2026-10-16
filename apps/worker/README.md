# EventLens AI Worker

Local deterministic vertical slice for turning one validated article bundle into one
event-processing trace. It demonstrates worker boundaries and policy flow. It is not a
production ingestion, inference, or persistence worker.

## Commands

Run from repository root:

```bash
uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json
uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json
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

## Deferred adapters

Production MinIO and Parquet readers, database writers, model or LLM extractors, MCP
integration, retries, queues, and living-event updates remain intentionally deferred.
