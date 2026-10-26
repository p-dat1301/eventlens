# EventLens rag-api

FastAPI process of the RAG context (`backend.rag.api_main:app`). Development
binding is loopback only (`make rag-api` / `uv run eventlens-rag-api` →
`127.0.0.1:8001`). CRUD routes are served by the separate crud-api process
(`backend.crud.main:app`, `127.0.0.1:8000`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | rag-api readiness (`{"status":"ok"}`); independent of crud-api |
| `POST` | `/briefing` | Grounded briefing: query → compiled briefing graph → T5 retrieval → T6 synthesis |
| `GET` | `/briefing/sources/{source_ref}` | Internal read-only source viewer for one citation (D37) |

`POST /briefing` takes a strict `briefing_request.v1` body (unknown fields are
rejected). Caller identity and access policy are never read from the request;
the server composition supplies them.

```json
{
  "schema_version": "briefing_request.v1",
  "query": "When did East Pier close and reopen?",
  "published_after": "2026-03-01T00:00:00Z",
  "published_before": "2026-04-01T00:00:00Z",
  "source_domains": ["fixture.example"],
  "topic_filters": [],
  "entity_ids": [],
  "context_budget": 12000,
  "result_limit": 5
}
```

The response (`briefing_response.v1`) has `answer`, `claims`, `citations`
(each with an opaque `source_ref`), `uncertainties`, `retrieved_at`,
`model_version`, `retrieval_mode`, `query_time_basis` (`source_published_at`)
and an `audit` block with policy, index and usage metadata. No prompt, raw
model output, content hash, storage URI or graph state is returned.

## Error codes

Errors are `{"schema_version":"briefing_error.v1","error":{"code","reason"}}`.

| Status | Code |
| --- | --- |
| 400 | `invalid_request`, `invalid_query` |
| 401 | `unauthenticated` (caller resolver could not identify the caller) |
| 403 | `access_denied` |
| 404 | `source_not_found` |
| 408 | `request_cancelled`, `deadline_exceeded` |
| 409 | `evidence_stale` |
| 410 | `source_stale` |
| 422 | `budget_exceeded` (reason: budget failure code) |
| 429 | `capacity_exceeded` |
| 502 | `invalid_grounded_answer` (reason: malformed/grounding code) |
| 503 | `retrieval_unavailable`, `model_unavailable` (reason: provider code) |
| 500 | `configuration_invalid`, `internal_error` |

No evidence is not an error: it is `200` with `outcome="abstained_no_evidence"`.

## Runtime

The default runtime is fixture-only: the in-memory fixture corpus
(`fixtures/rag/articles`, lexical-only until vectors exist), fake embeddings,
the identity reranker and the deterministic fake synthesizer. Activation is
always evaluated in fixture mode with an empty environment, so an API key in the
environment never enables a live provider. The graph and the process-local
concurrency limiter are built once at startup.

### Frontend routing

The web client calls `${VITE_API_BASE_URL}/briefing` (empty base = same origin).

- Development: the Vite dev server proxies `/briefing` to `EVENTLENS_RAG_API_URL`
  (default `http://localhost:8001`).
- Production web container: Nginx proxies same-origin `/briefing` and `/briefing/*`
  (e.g. `/briefing/sources/{source_ref}`) to the Docker service `http://rag-api:8001`,
  path unchanged, with `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`
  and a 30 s read timeout.
- Separate frontend deployment: build with `VITE_API_BASE_URL` set to the rag-api
  origin and configure CORS for that origin; rag-api has no CORS middleware today.

### PostgreSQL opt-in

Set `EVENTLENS_API_RAG_DATABASE_URL` to an already migrated RAG database
(`make rag-migrate`, migrations 001–007) to serve retrieval and source
references from PostgreSQL. The model stays the fixture synthesizer.

### Caller and source references

The caller comes from a server-side `CallerResolver` (default: the fixed
fixture caller). A real authenticator replaces it and raises
`CallerUnauthenticatedError` → `401`. Source references are random tokens; with
PostgreSQL they live in `rag_source_references` (only the token's SHA-256 is
stored, 24 h expiry) and resolve in any API process; the fixture runtime keeps
them in a bounded in-process map.

### Cancellation

Client disconnect cancels the request's `QueryControl`; the worker thread stops
at its next admission check, and an in-flight model call keeps its pinned
concurrency slot until it returns (T6 contract). This is an accepted constraint.
