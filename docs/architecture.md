# EventLens Architecture

**Modular Monolith organized by Bounded Contexts.** One Python distribution
(`eventlens`, import root `backend`, source under `src/backend`) and one web app
(`src/frontend/web`). Each bounded context owns its layers; contexts never import
each other and meet only through `backend.shared.contracts`.

```text
src/
  backend/
    shared/contracts/      shared Pydantic contracts (+ ArticleBundle, semantic vectors)
    crud/                  CRUD context — Clean Architecture
      domain/ application/{use_cases,ports}/ presentation/ infra/
      main.py              crud-api entry point (:8000)
    rag/                   AI/RAG context — Hexagonal + LangGraph workflows
      domain/
      application/{use_cases,ports,policies}/  (+ shared services at the root)
      workflows/{ingestion,query}/
      presentation/        FastAPI routers, DTOs, error mapping, JSON Schemas
      infra/{postgres,repositories,retrieval,embeddings,llm,access,telemetry,diagnostics}/
      composition.py       composition root (the only place adapters are built)
      api_main.py          rag-api entry point (:8001)
      worker_main.py       rag-worker entry point (ingest | enrich)
      cli_main.py          eventlens-rag maintenance entry point
    event_pipeline/        legacy deterministic event pipeline (validate | process)
      domain/ application/{ports.py,use_cases}/ infra/ presentation/cli.py composition.py
    compat/worker_alias.py deprecated eventlens-worker alias (forwarding only)
  frontend/web/            React + Vite analyst UI
tests/
  architecture/            import-boundary tests (this document, enforced)
  backend/{shared,crud,rag,event_pipeline}/
  support/                 shared test doubles (on pytest pythonpath)
scripts/rag/               isolated-database harness and G1 gate
docker/                    one Dockerfile per runtime
typings/                   local LangGraph stubs
```

## Bounded contexts and ownership

| Context | Owns | Does not own |
| --- | --- | --- |
| `crud` | Transactional CRUD resources and the crud-api process. Today only `GET /health`; `domain/`, `application/ports/`, `infra/` are intentionally empty (no placeholder entities). | Anything RAG: no retrieval, briefing, LLM or RAG tables. |
| `rag` | ArticleDocument storage and its PostgreSQL schema (migrations 001–007), enrichment lifecycle, index generations, retrieval, grounded synthesis, briefing API, source viewer, the rag-api and rag-worker processes. | CRUD resources; event extraction. |
| `event_pipeline` | Local ArticleBundle → event proposal → gate → in-memory write vertical slice. | RAG storage or workflows. |
| `shared.contracts` | Frozen cross-boundary models (`article.v1`, `event.v1`, `article_enrichment.v1`, …), `ArticleBundle`, semantic vectors. | Behavior of any context. |

Rule: when CRUD needs RAG data (or the reverse) it goes through an HTTP call or a
shared contract, never a Python import. `tests/architecture` fails on any
cross-context import, and the rag-api process is checked not to load
`backend.crud` or `backend.event_pipeline`.

## CRUD: Clean Architecture

```text
presentation (FastAPI routers)  ->  application (use cases, ports)  ->  domain
                                           ^
                         infra (adapters implementing application ports)
```

- `domain/`: entities and rules; no framework imports.
- `application/use_cases/`: one module per use case (`health.py`); depends on
  domain and its own `ports/` only.
- `presentation/`: HTTP schemas and routes; calls use cases; never imports
  `infra/` or a database driver.
- `infra/`: implements `application/ports`; wired in the entry point.

## RAG: Hexagonal Architecture with LangGraph state machine workflows

```text
           driving adapters                               driven adapters
  presentation/briefing_http  ─┐                     ┌─  infra/postgres, repositories
  worker_main / cli_main       ├─> application ports ─┤   infra/retrieval, embeddings
                               │   (use cases)        │   infra/llm, access, telemetry
  workflows/{ingestion,query} ─┘        │             └─  (implement application/ports)
                                        v
                                     domain
              composition.py instantiates every adapter and injects it
```

- `domain/`: value objects, policies, typed errors. Pydantic is allowed; FastAPI,
  Typer, LangGraph, psycopg and SDKs are not.
- `application/ports/`: the service ports (below). `application/use_cases/`:
  `ingest`, `persist`, `index`, `enrichment`, `retrieve`, `synthesize`,
  `activation`, `briefing`, `inspect_source`. Root modules `citations`,
  `concurrency`, `evidence_context`, `ledger`, `rendering`, `synthesis_policies`
  are services shared by several use cases. `application/policies/*.json` are the
  versioned policy files loaded through `importlib.resources`.
- `workflows/`: LangGraph orchestration only (see below).
- `presentation/`: `briefing_http.py` (DTOs, `/briefing`, `/briefing/sources/{ref}`,
  error handlers, `CallerResolver`), `health.py` (rag-api `/health`),
  `schemas.py` (JSON Schema export of RAG contracts).
- `infra/`: adapters grouped by capability. `postgres/` holds the connection
  helper, migration ledger, storage, staging and publication; the single
  migration source is `infra/postgres/migrations/`.
- `composition.py`: builds the query runtime (`compose_briefing`,
  `runtime_from_environment`) and the ingestion runtime (`build_runtime`), and
  exposes `prepare_storage` / `json_lines_telemetry` so entry points never
  construct adapters themselves.

### Service ports (`backend.rag.application.ports`)

| Module | Ports |
| --- | --- |
| `access` | `AccessPolicy` |
| `persistence` | `ArticlePersistence` |
| `enrichment` | `ArticleEnricher`, `EnrichmentRepository`, `TelemetrySink` |
| `staging` | `GenerationStaging` |
| `embeddings` | `EmbeddingPort` |
| `retrieval` | `RetrievalStore` |
| `reranker` | `RerankerPort` |
| `tokens` | `TokenCounter` |
| `synthesis` | `SynthesisModelPort`, `TokenLedger`, `ConcurrencyLimiter`, `CurrentArticleReader`, `SynthesisTelemetry`, `TrustedIssuerRegistry` |
| `source_references` | `SourceReferenceRegistry` |
| `briefing` | `BriefingSteps` (called by query nodes), `BriefingWorkflow` (implemented by the query workflow) |
| `telemetry` | `BriefingTelemetry` |

Event pipeline ports: `EventExtractor`, `EventWriter` (`event_pipeline/application/ports.py`).

### LangGraph workflows

Each workflow is a package with its own `state.py` (TypedDict channels),
`context.py` (invocation-local runtime context), `nodes.py`, `routes.py`,
`graph.py` and `workflow.py`:

- `graph.py` only adds nodes/edges and compiles (`build_ingestion_graph`,
  `build_query_graph`). `.compile()` appears nowhere else (tested).
- `nodes.py` reads state, calls one application service method
  (`EnrichmentSteps` / `BriefingSteps`), returns a state patch.
- `routes.py` holds pure routing functions only.
- `workflow.py` is the facade: compiles once in its constructor, then invokes
  per claim / per request (`EnrichmentGraph`, `EnrichmentRunner`, `BriefingGraph`).
- State never carries a DB session, ORM model, LLM client or adapter; services
  are bound into nodes at construction and the per-call scope travels in the
  runtime context. No checkpointer.
- Graphs are compiled once at startup by the composition root (rag-api) or once
  per worker pass (rag-worker), never per request.

Node names are unchanged from T4/T7. The conceptual flows map onto them as:

**Ingestion** `load → normalize → persist → enrich → validate → embed/index → publish`

| Step | Where |
| --- | --- |
| load, normalize, persist | `IngestArticle` (rag-worker `ingest`): bundle → `ArticleDocument`, lexical row + PENDING job in one transaction, before the graph |
| (claim) | `EnrichmentRunner` claims one job; graph node `load` reloads the claimed revision |
| enrich | node `enrich` |
| validate | node `validate` |
| embed/index | node `stage` (`EnsureGeneration`) |
| publish | node `publish`; any typed failure → node `fail` |

**Query** `admit → retrieve → assemble → synthesize → validate → finalize`

| Step | Where |
| --- | --- |
| admit | `BriefingUseCase` (`_admit`, concurrency lease) before the graph; node `prepare` plans the budget |
| retrieve | node `retrieve` |
| assemble | inside `RetrieveEvidence` (`evidence_context`) |
| synthesize | node `synthesize`, or `abstain` on empty evidence |
| validate | inside `SynthesizeAnswer` (`citations`) |
| finalize | nodes `answered` / `abstained`, or one of six failure terminals (`retrieval_failed`, `synthesis_failed`, `timed_out`, `cancelled`, `budget_exceeded`, `access_denied`) |

## Dependency rules (enforced by `tests/architecture`)

1. `domain` imports no framework and no outer layer of its context.
2. `application` (incl. `ports`) never imports `infra`, `presentation`,
   `workflows`, `composition` or FastAPI/Typer/LangGraph/psycopg.
3. `presentation` never imports `infra`, `workflows` or a database driver.
4. `workflows` import application, domain and LangGraph only; never `infra`,
   `presentation` or `composition`. `routes.py` and `state.py` stay pure.
5. `infra` never imports delivery code, workflows or the composition root.
6. Outside `infra`, only `composition.py`, `worker_main.py` and `cli_main.py`
   import `backend.rag.infra` (the two CLIs only to map typed storage errors to
   exit codes, and `cli_main` to drive maintenance/diagnostics). `api_main.py`
   imports no adapter.
7. Bounded contexts never import each other; `shared` imports no context.
8. One migration source; no `apps/`, `contracts/` or `eventlens_*` packages.

## Runtimes

| Runtime | Command | Address |
| --- | --- | --- |
| web | `make web` (`cd src/frontend/web && bun run dev`) | `http://localhost:5173` |
| crud-api | `make crud-api` (`uv run eventlens-crud-api`) | `http://127.0.0.1:8000/health` |
| rag-api | `make rag-api` (`uv run eventlens-rag-api`) | `http://127.0.0.1:8001/health`, `POST /briefing`, `GET /briefing/sources/{source_ref}` |
| rag-worker | `make rag-worker` (`uv run eventlens-rag-worker enrich --max-jobs 5`; `ingest BUNDLE`) | needs `EVENTLENS_RAG_DATABASE_URL` |

The Vite dev server proxies `/briefing` to `EVENTLENS_RAG_API_URL`
(default `http://localhost:8001`). rag-api is fixture-only by default;
`EVENTLENS_API_RAG_DATABASE_URL` opts retrieval and source references into an
already migrated PostgreSQL database (variable name unchanged).

In the `web` container, Nginx (`src/frontend/web/nginx.conf`) proxies same-origin
`location = /briefing` and `location ^~ /briefing/` to `http://rag-api:8001`
without rewriting the path; `/` keeps the SPA `index.html` fallback and crud-api
`/health` is not proxied. A frontend served outside this Nginx must set
`VITE_API_BASE_URL` to the rag-api origin at build time and rag-api must allow
that origin via CORS (not configured today).

Containers: `docker compose up --build crud-api rag-api web`;
`docker compose --profile tools run --rm rag-worker` (needs `make infra rag-db-create`)
and `docker compose --profile tools run --rm event-pipeline`.

### CLIs

| Command | Module | Subcommands |
| --- | --- | --- |
| `eventlens-rag` | `backend.rag.cli_main:app` | `migrate`, `load-fixtures`, `rebuild`, `retrieval-smoke`, `synthesis-smoke`, `vector-route-report`, `db-test` |
| `eventlens-rag-worker` | `backend.rag.worker_main:app` | `ingest`, `enrich` |
| `eventlens-events` | `backend.event_pipeline.presentation.cli:app` | `validate`, `process` |
| `eventlens-crud-api` / `eventlens-rag-api` | `backend.crud.main:run` / `backend.rag.api_main:run` | — |
| `eventlens-worker` (deprecated) | `backend.compat.worker_alias:main` | forwards `validate|process` and `ingest|enrich`; prints a deprecation warning; removed in the next PR |

## Migrations

`src/backend/rag/infra/postgres/migrations/001…007_*.sql` is the only copy; names
and bytes are unchanged from before the refactor, so the ledger
(`rag_schema_migrations`, SHA-256 of each script) sees no checksum change. The
loader uses `importlib.resources.files("backend.rag.infra.postgres")
.joinpath("migrations")`, which also works from the built wheel.

## Related documents

- `docs/rag.md` — RAG lifecycle, retrieval, synthesis and database runbook.
- `docs/rag-api.md` — rag-api endpoints and error codes.
- `docs/event-pipeline.md` — event pipeline and rag-worker CLI behavior.
- `docs/contracts.md` — shared contracts and semantic vectors.
