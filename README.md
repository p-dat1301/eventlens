# EventLens

Monorepo foundation for event intelligence built from normalized news articles.

## Architecture

Deployable units live under `apps/`; reusable boundary code lives under `packages/`.
Dependencies point inward:

```text
presentation / adapters -> application -> domain contracts
```

- `apps/api`: FastAPI HTTP adapter and framework-free use cases.
- `apps/ai-worker`: Typer adapter and framework-free validation pipeline.
- `apps/web`: React presentation with Zod parsing at external boundaries.
- `packages/contracts-python`: frozen Pydantic models shared by Python processes.
- `contracts`: checked-in JSON Schema and Arrow descriptor artifacts for language-neutral integration.
- `fixtures`: representative contract payloads for local development and tests.

No app imports another app. Domain and application code do not import FastAPI, Typer,
React, PostgreSQL, MinIO, or model SDKs. Infrastructure adapters will implement ports
when ingestion and persistence are added.

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
Production MinIO access, Parquet ingestion, database persistence, model inference,
Kafka, and final product UI remain intentionally out of scope.

## Local Development

Requirements: Python 3.12, uv, Bun 1.4+, and optional Docker.

```bash
make sync
make check
make validate-fixture
```

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
