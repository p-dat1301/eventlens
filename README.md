# EventLens

Monorepo foundation for event intelligence built from normalized news articles.

## Architecture

Modular monolith organized by bounded contexts; see `docs/architecture.md` for
layers, dependency rules, ports and LangGraph workflows.

```text
src/backend/shared/contracts   shared frozen Pydantic contracts (+ ArticleBundle)
src/backend/crud               CRUD context, Clean Architecture (crud-api :8000)
src/backend/rag                AI/RAG context, Hexagonal + LangGraph (rag-api :8001, rag-worker)
src/backend/event_pipeline     local deterministic event pipeline (eventlens-events)
src/frontend/web               React presentation with Zod parsing at external boundaries
fixtures/                      canonical contract fixtures + worker-local trace fixtures
```

`fixtures` holds canonical contract fixtures plus worker-local trace fixtures for
local development and tests. Contract fixtures represent shared boundary payloads;
`*.trace.v1.json` files represent worker behavior, not cross-language contracts.

No context imports another context. Domain and application code do not import
FastAPI, Typer, React, PostgreSQL, MinIO, or model SDKs (enforced by
`tests/architecture`). The event pipeline follows this local, deterministic path:

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
with model-generated JSON Schemas, Arrow descriptors for the three Parquet datasets,
and representative JSON fixtures. Generated schemas and descriptors are not checked in.
Cross-record rules remain semantic vectors tested by the Python package; JSON Schema
alone cannot express them.
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
uv run eventlens-events validate fixtures/article_bundle.v1.json
uv run eventlens-events process fixtures/article_bundle.v1.json
```

`process` prints one JSON `ProcessingTrace`. Its in-memory writer resets on every CLI
invocation, so it does not persist events between commands.

Run the four runtimes without Docker (one terminal each):

```bash
make crud-api      # http://127.0.0.1:8000/health
make rag-api       # http://127.0.0.1:8001/health, POST /briefing
make web           # http://localhost:5173 (proxies /briefing to EVENTLENS_RAG_API_URL, default :8001)
make rag-worker    # needs the RAG database: make infra rag-db-create rag-migrate
```

## Containers

```bash
make up
docker compose --profile tools run --rm event-pipeline
docker compose --profile tools run --rm rag-worker
make infra
make down
```

`infra` starts local PostgreSQL (pgvector) and a MinIO placeholder bound to localhost only. The RAG index uses a
separate `eventlens_rag` database on that server (`make rag-db-create rag-migrate rag-load-fixtures rag-rebuild`; Alembic migrations,
see `docs/rag.md`); MinIO is unused. `make up` starts `crud-api`, `rag-api` and `web`. Never reuse development
credentials outside local environments.

The `web` container (Nginx, `src/frontend/web/nginx.conf`) proxies same-origin `/briefing` and `/briefing/*`
(including `/briefing/sources/{source_ref}`) to the Docker service `http://rag-api:8001`, path unchanged; every
other path falls back to the SPA `index.html`. In development the Vite proxy forwards `/briefing` to
`EVENTLENS_RAG_API_URL` instead. A frontend deployed without this Nginx must build with `VITE_API_BASE_URL`
pointing at rag-api and needs matching CORS on rag-api (none is configured today).
