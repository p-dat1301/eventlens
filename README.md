# EventLens

Monorepo foundation for event intelligence built from normalized news articles.

## Architecture

Application units live under `apps/`; shared boundary code lives under `contracts/`.
Dependencies point inward:

```text
presentation / adapters -> application -> domain contracts
```

- `apps/api`: FastAPI HTTP adapter and framework-free use cases.
- `apps/ai-worker`: Typer adapter, local ArticleBundle validation, and framework-free event-processing pipeline.
- `apps/rag`: RAG CLI, app-owned document/briefing models, graph, and persistence adapters.
- `apps/web`: React presentation with Zod parsing at external boundaries.
- `contracts/python`: frozen Pydantic models shared by Python processes.
- `contracts`: checked-in JSON Schema and Arrow descriptor artifacts for language-neutral integration.
- `fixtures`: canonical contract fixtures plus worker-local trace fixtures for local
  development and tests. Contract fixtures represent shared boundary payloads;
  `*.trace.v1.json` files represent worker behavior, not cross-language contracts.

No app imports another app. Domain and application code do not import FastAPI, Typer,
React, PostgreSQL, MinIO, or model SDKs. Worker processing currently follows this local,
deterministic path:

```text
ArticleBundle -> FakeEventExtractor -> EventProposal -> gate
              -> InMemoryEventWriter (ACCEPT only) -> worker-local ProcessingTrace
```

`ACCEPT`, `REJECT`, and `REVIEW` are observable gate verdicts. `REJECT` and `REVIEW`
skip writes. Current fake extraction confidence and gate thresholds are hard-coded local
demonstration policy, not canonical business policy.

## Data Boundary

Data team publishes Silver data independently. Current frozen inputs:

- `article.v1`
- `article_media.v1`
- `article_rejected.v1`
- `article_batch_manifest.v1`

This foundation implements four frozen Data input contracts plus downstream `event.v1`,
with JSON Schemas, Arrow descriptors for the three Parquet datasets, and representative
fixtures. Cross-record rules remain semantic vectors tested by the Python package; JSON
Schema alone cannot express them.
Production MinIO and Parquet readers, database event writers, model or LLM extractors,
MCP integration, retries, Kafka, living-event updates, and final product UI remain
intentionally out of scope. Current worker adapters are local fakes, not production-ready
substitutes.

## Local Development

Requirements: Python 3.12, uv, Bun 1.4+, and optional Docker.

```bash
make sync
make check
make validate-fixture
```

Validate or process one local fixture directly:

```bash
uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json
uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json
```

`process` prints one JSON `ProcessingTrace`. Its in-memory writer resets on every CLI
invocation, so it does not persist events between commands.

Run services without Docker:

```bash
uv run --package eventlens-api eventlens-api
cd apps/web && bun run dev
```

Endpoints:

- API health: `http://localhost:8000/health`
- Web: `http://localhost:5173`

## Containers

```bash
make up
docker compose --profile tools run --rm ai-worker
make infra
make down
```

`infra` starts local PostgreSQL and MinIO placeholders bound to localhost only. Application code does not use
them yet. Never reuse development credentials outside local environments.
