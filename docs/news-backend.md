# EventLens news backend — catalog, summaries, scoped chat

Backend for the news web (feed, topic/location filters, article page,
per-article AI summary, scoped conversations). Design rationale:
`docs/designs/news-web-and-chat-architecture.md` (main checkout). The web
client, its same-origin proxy and the persistent demo are documented in
`src/frontend/web/README.md`.

OpenAPI: `docs/openapi/crud-api.openapi.json`, `docs/openapi/rag-api.openapi.json`
(`make news-openapi` regenerates them).

## 1. Topology and ownership

```text
browser ──> reverse proxy (same origin)
              ├─ /api/v1/articles*, /api/v1/article-facets,
              │  /api/v1/topics, /api/v1/locations, /api/v1/sources ──> crud-api :8000
              └─ /api/v1/sessions*, /api/v1/conversations*, /api/v1/runs*,
                 /briefing*                                            ──> rag-api  :8001
crud-api ── ArticleCatalogPort (HTTP adapter, service token) ──> rag-api /internal/v1/catalog/*
rag-worker serve ── claims rag_work_jobs (query_run pool | summary pool) ──> PostgreSQL
```

| Module | Owns | Notes |
| --- | --- | --- |
| `rag` | articles/revisions, provenance, labels, gazetteer, summaries, jobs, sessions, conversations, runs, citations (PostgreSQL, Alembic `rag_0004_news_backend`) | single implementation of catalog filter semantics (`domain/catalog.py` + `infra/postgres/article_catalog.py`) |
| `crud` | public catalog routes (Clean Architecture) | no article tables, no DB connection; `application/ports/article_catalog.py` + `infra/rag_catalog_http.py` |
| `shared.contracts.catalog` | `catalog.v1` wire DTOs | produced by rag-api, validated strictly by crud-api, served unchanged |

`/internal/*` must never be routed by the public proxy. Catalog and
conversation CRUD are plain use cases; LangGraph runs only the summarization
workflow (`workflows/summarization/`) and the existing query workflow.

## 2. Catalog API (crud-api)

| Method | Path | Body |
| --- | --- | --- |
| GET | `/api/v1/articles` | `article_list.v1` |
| GET | `/api/v1/articles/{article_id}` | `article_detail.v1` (`?article_version_id=` → 409 when stale) |
| GET | `/api/v1/articles/{article_id}/summary` | `article_summary.v1` |
| GET | `/api/v1/article-facets` | `article_facets.v1` |
| GET | `/api/v1/topics` | `topic_list.v1` |
| GET | `/api/v1/locations` | `location_list.v1` (`parent_id`, `q` alias prefix, `limit` ≤ 100) |
| GET | `/api/v1/sources` | `source_list.v1` |

### Filters (`/articles`, `/article-facets`)

| Parameter | Type | Rule |
| --- | --- | --- |
| `q` | string ≤ 200 | PostgreSQL `simple` `websearch_to_tsquery` on title + text (AND of words, `"phrase"`, `-not`); `search_mode="lexical"` |
| `topic_ids` | repeated, ≤ 20 | topic codes from `/topics`; OR within the kind |
| `location_ids` | repeated, ≤ 20 | gazetteer IDs from `/locations`; OR within the kind |
| `source_ids` | repeated UUID, ≤ 20 | from `/sources`; OR within the kind |
| `published_after` | ISO-8601 **with offset** | inclusive, compared in UTC |
| `published_before` | ISO-8601 **with offset** | exclusive; `after == before` → empty |
| `include_descendants` | bool, default `true` | a location also matches its gazetteer subtree (former provinces roll up to the current unit) |
| `sort` | `latest` \| `oldest` \| `relevance` | default `relevance` with `q`, else `latest`; `relevance` without `q` → 400 |
| `cursor` | opaque ≤ 1024 | from `next_cursor`; bound to filters + sort |
| `limit` | 1–50, default 20 | |

* Repeated parameters: `?topic_ids=economy&topic_ids=health` (OpenAPI `form`,
  `explode: true`). Kinds are AND-ed.
* Dates: the client converts a calendar day in `Asia/Ho_Chi_Minh` to UTC
  bounds, e.g. 29/10/2026 → `published_after=2026-10-28T17:00:00Z&published_before=2026-10-29T17:00:00Z`.
  A timestamp without offset → `400 timezone_required`. Articles without a
  publisher time stay in the unfiltered feed (sorted last) and never match a
  time bound; `fetched_at` is never used.
* Sort keys: `latest` = `published_at DESC NULLS LAST, article_id`;
  `oldest` = `published_at ASC NULLS LAST, article_id`; `relevance` =
  `rank DESC, published_at DESC NULLS LAST, article_id`.
* Cursor: HMAC-signed keyset position plus the SHA-256 of the canonical
  filters and the sort. Reusing it with other filters → `400
  cursor_mismatch`; garbage/foreign → `400 invalid_cursor`. Without
  `EVENTLENS_RAG_CURSOR_SECRET` (≥ 32 bytes) each rag-api process uses a
  random key, so cursors do not survive a restart. Pages are not a snapshot
  *across* requests: edits/new articles between pages may shift items (no DB
  transaction is held across requests). *Within* one response every
  statement reads one `REPEATABLE READ` snapshot and cards are built from
  the exact revision the page/detail query selected, so a concurrent
  correction never mixes the text of one revision with the title/version of
  another, and a listed item always matched the filters.
* One item = one current article (`article_id`), never a chunk; tombstoned
  articles are excluded. Lists never include full text.

### Labels, gazetteer, statuses

* Topics: frozen `TopicTag` codes with Vietnamese names (`topic-taxonomy.v1`).
* Locations: `vn-admin-2025.v1` — `vn`, 6 `municipality`, 28 `province`
  (after the 2025-07-01 reorganisation) and 29 `former_province` children
  with `valid_until=2025-07-01` (e.g. `vn-former-binh-duong` → parent
  `vn-ho-chi-minh`). Aliases include unaccented and English forms.
* A label is about the article content; the newsroom location is never
  used. Labels come from (a) enrichment publications whose `model_version`
  is listed in `EVENTLENS_RAG_TRUSTED_ENRICHMENT_MODELS` (location names must
  match exactly one alias key) or (b) manual rows; a manual `rejected` row
  suppresses the same model label. Nothing is guessed: unmatched locations
  stay unknown and are counted. Every label-sync pass (worker `serve` every
  30 s, or `eventlens-rag-worker sync-labels`) re-processes revisions whose
  publication changed or whose model was added to / removed from the
  trusted list: trusting a model applies its labels, revoking it removes
  them (`metadata="unavailable"`); manual labels are never touched.
* `processing_status.metadata`: `ready` (labels synced from a trusted
  publication), `pending` (enrichment/sync not done), `failed`,
  `unavailable` (publication from an untrusted model, e.g. the fake
  enricher). `processing_status.summary`: `ready`, `pending` (job queued),
  `failed`, `unavailable` (no job).
* Facets (`semantics="all_filters_applied"`): every selected filter is
  applied, including the facet's own kind (not self-excluding). Counts are
  distinct current `article_id`s; location counts include descendants.
  `topics_unclassified` / `locations_unknown` count articles without labels.

### Detail and summary

`article_detail.v1` carries `article` (card), `content` (normalized plain
text; `EVENTLENS_RAG_CATALOG_CONTENT=withheld` returns `null` +
`content_policy="withheld"`), `ingested_at`, `updated_at_source`, `language`,
`version` (`article_version_id` = RAG revision key `sha256:…`,
`upstream_version_id` = data-team version, `revision_count`) and `labels`
with provenance. Unknown → `404 article_not_found`; tombstoned → `410
article_gone`.

`article_summary.v1` is read-only (no model call on GET): `status`,
`source="ai"`, `text`, `key_points[].supporting_spans[]` (code-point offsets
into the NFC content of `article_version_id`, excerpt, `span_fingerprint`),
`generator` (model/prompt/schema/policy identity), `failure_code`,
`publisher_summary` (always `null`: handoff records carry no sapo). Span
offsets/hashes prove the excerpt exists in that revision, not that it
semantically supports the point.

### Summary lifecycle

```text
revision becomes current (trigger, same transaction)
  -> rag_work_jobs(kind=summary, revision_key=content_hash) PENDING; older PENDING -> STALE
worker claim (SKIP LOCKED, attempt+1, lease 180 s)
  -> load exact revision -> prepare (> 24 000 chars => FAILED content_too_long, never truncated)
  -> summarize (one call, 60 s) -> validate (strict draft, every quote must be an exact substring)
  -> verify current -> publish (fenced: job + current row locked) -> DONE | STALE
transient provider error -> PENDING with backoff 30 s / 120 s (3 attempts) ; other errors -> FAILED
```

The provider response is schema-validated end to end (`choices[0].message
.content`, `finish_reason="stop"`); any other shape is `malformed_output`
(FAILED, no retry) and the worker continues with the next job. A storage
error mid-job releases the claim with backoff instead of waiting for the
lease.

Admin retry: `eventlens-rag-worker summary-requeue --article-id <id>`
(`requeued|scheduled|already_pending|not_current`). A summary already
published for the same model/prompt/schema identity is kept; regenerate by
changing that identity.

## 3. Chat API (rag-api)

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/v1/sessions` | 201 `session.v1`; sets `eventlens_session` cookie (HttpOnly, SameSite=Lax, `Secure` with `EVENTLENS_RAG_SESSION_COOKIE_SECURE=1`) and returns the token once |
| DELETE | `/api/v1/sessions/current` | revoke |
| POST | `/api/v1/conversations` | 201 `conversation.v1`, body `{scope, title?}` |
| GET | `/api/v1/conversations` | `conversation_list.v1`, `limit`, `cursor` |
| GET | `/api/v1/conversations/{id}/messages` | `message_list.v1`, `after_sequence`, `limit` |
| POST | `/api/v1/conversations/{id}/runs` | 202 `run_accepted.v1`, `Idempotency-Key` header, `Location` = `status_url` |
| GET | `/api/v1/runs/{run_id}` | `query_run.v1` (poll) |
| POST | `/api/v1/runs/{run_id}/cancel` | 202 `query_run.v1` with the real state |
| DELETE | `/api/v1/conversations/{id}` | 204; queued runs cancelled, running runs flagged |
| POST | `/api/v1/runs/{run_id}/citations/{citation_id}/source-ref` | 201 `source_ref.v1` → `GET /briefing/sources/{ref}` |
| POST | `/briefing`, GET `/briefing/sources/{ref}` | legacy, unchanged |

### Identity and ownership

* Identity = server-side session only (`Authorization: Bearer <token>` or
  the cookie). `X-User-ID`, owner fields or caller IDs from clients are never
  read. Only the token's SHA-256 is stored; sessions last 7 days.
* Every conversation/message/run/cancel/delete/citation statement matches
  the session owner in SQL; a foreign resource answers `404` like a missing
  one. No session → `401 unauthenticated`.
* Demo boundary: sessions are anonymous owners (one per browser). They
  isolate conversations; they are **not** user accounts and **not** an ACL
  for a private corpus. The corpus caller stays the fixture scope
  (`fixture-policy v1`); a public deployment needs real login (OIDC/session)
  and per-resource corpus ACL before private articles exist.
* Internal catalog: `Authorization: Bearer $EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN`
  (constant-time compare). Unset → `503 internal_auth_not_configured`.

### Scopes

```json
{"scope": {"type": "all"}}
{"scope": {"type": "filtered", "filters": {"topic_ids": ["transportation"],
  "location_ids": ["vn-ho-chi-minh"], "published_after": "2026-10-28T17:00:00Z",
  "published_before": "2026-10-29T17:00:00Z"}}}
{"scope": {"type": "articles", "articles": [{"article_id": "…",
  "article_version_id": "sha256:…"}]}}
```

* The conversation stores the scope **definition** (`scope`,
  `scope_sha256`). Each run re-resolves it against current revisions with the
  catalog semantics (`filtered` = the whole matching set, ≤ 5 000 articles,
  else `scope_too_large`; never a page), re-checks access, and records
  `effective_scope`, `as_of` and `source_versions` (article → revision used
  as evidence).
* The restriction is applied **before** retrieval: intersected into
  `allowed_article_ids` for the PostgreSQL SQL and the Elasticsearch `terms`
  filter, and re-checked by the final current-revision check (article and
  revision must match). The model never receives out-of-scope text.
* `articles` + `article_version_id`: a newer revision fails the run with
  `scope_stale` (`reason=pinned_version_superseded`) instead of silently
  answering from another version; creating such a conversation → `400
  invalid_scope` reason `scope_stale`.
* History: a short follow-up (≤ 12 words) is retrieved together with the
  previous user **question** (`previous-question-concat.v1`); earlier answers
  are never evidence.

### Runs

```text
POST runs ─> queued ─(worker claim)─> running ─> completed (outcome answered|abstained)
                │                         ├────> failed (error.code)
                └─ cancel ─> cancelled    └─ cancel flag ─> cancelled (cooperative)
```

* `POST …/runs` inserts message + run + job in one transaction under a lock
  of the conversation row. Same `Idempotency-Key` (per owner + conversation)
  and same body → the original run (`replayed=true`, header
  `Idempotent-Replayed: true`); same key, different body → `409
  idempotency_key_reused`; a second active run → `409 run_in_progress`.
* Closing the HTTP connection never affects a run. A worker crash leaves the
  job leased; after the lease (60 s) another worker re-claims it (3 attempts),
  queue deadline 10 min (`deadline_exceeded`). A storage error inside a run
  releases the claim with a 2 s backoff (one attempt) instead of waiting for
  the lease. Every claim first runs one transaction that expires exhausted
  run jobs **and** settles every queued/running run whose job is terminal or
  missing (`failed` with the job's code, or `cancelled`), so a crash at any
  point can never leave a conversation blocked by `run_in_progress`.
* `rag-worker serve` loops log any step error by type/code, back off
  exponentially (≤ 30 s) and continue; if a worker thread still dies the
  process exits 1 so its supervisor (Compose/systemd) restarts it.
* `completed` + `abstained` only when there is no evidence or the model
  abstained; provider/storage errors are `failed`:
  `model_unavailable` (`provider_not_configured`, provider codes),
  `retrieval_unavailable`, `deadline_exceeded`, `evidence_stale`,
  `invalid_grounded_answer`, `budget_exceeded`, `capacity_exceeded`,
  `access_denied`, `scope_stale`, `scope_article_unavailable`,
  `scope_too_large`, `invalid_scope`, `configuration_invalid`,
  `internal_error`.
* Cancel: queued → `cancelled` at once; running → `cancel_requested=true`,
  the worker polls every 0.5 s and stops at the next admission check. An
  in-flight model call is not interrupted (provider billing may continue).
* Citations are stored as identity/version/span (`article_id`,
  `article_version_id`, offsets, excerpt, fingerprint). `source-ref`
  re-checks ownership, access and that the cited revision is still current,
  then issues a fresh 24 h ref: changed/removed → `410 source_stale`,
  revoked access → `403 access_denied`, unknown → `404 citation_not_found`.
  A stored citation is never re-pointed to newer text.

## 4. Errors

All `/api/v1` and `/internal/v1` errors:

```json
{"schema_version": "api_error.v1", "error": {"code": "cursor_mismatch", "reason": null, "request_id": "…"}}
```

`X-Request-ID` is echoed (or generated). crud-api: catalog unreachable,
timeout (3 s), wrong token or open circuit (5 failures, 10 s) → `503
catalog_unavailable` (+ `Retry-After: 5`, `reason`); malformed upstream body →
`502 catalog_contract_mismatch`. Never an empty page.

| Status | Codes |
| --- | --- |
| 400 | `invalid_request`, `timezone_required`, `invalid_publication_range`, `query_too_long`, `too_many_*_ids`, `invalid_topic_id`, `invalid_location_id`, `unknown_topic_id`, `unknown_location_id`, `invalid_sort`, `relevance_requires_query`, `invalid_limit`, `invalid_cursor`, `cursor_mismatch`, `invalid_scope` (+reason), `invalid_question` |
| 401 | `unauthenticated`, `service_unauthenticated` (internal) |
| 403 | `access_denied` |
| 404 | `article_not_found`, `conversation_not_found`, `run_not_found`, `citation_not_found`, `not_found` |
| 409 | `article_version_stale` (reason = current version), `idempotency_key_reused`, `run_in_progress` |
| 410 | `article_gone`, `source_stale` |
| 502 | `catalog_contract_mismatch` |
| 503 | `catalog_unavailable`, `conversation_storage_unavailable`, `retrieval_unavailable`, `model_unavailable`, `internal_auth_not_configured` |

## 5. Configuration

| Variable | Process | Default / meaning |
| --- | --- | --- |
| `EVENTLENS_API_RAG_DATABASE_URL` | rag-api | unset: fixture runtime, catalog/chat `503` |
| `EVENTLENS_RAG_DATABASE_URL` | rag-worker, CLIs | required |
| `EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN` | rag-api | unset: internal catalog `503` |
| `EVENTLENS_CRUD_RAG_SERVICE_TOKEN` | crud-api | same value as above |
| `EVENTLENS_CRUD_RAG_CATALOG_URL` | crud-api | `http://127.0.0.1:8001` |
| `EVENTLENS_CRUD_RAG_TIMEOUT_SECONDS` | crud-api | `3` |
| `EVENTLENS_RAG_CURSOR_SECRET` | rag-api | random per process (≥ 32 bytes when set) |
| `EVENTLENS_RAG_ANSWER_PROVIDER` | rag-api, rag-worker | `none` (runs → 503 / failed `model_unavailable`); `fixture` (explicit demo: deterministic fake synthesizer, answers carry `mode="fixture"`); `live` (placeholder: live synthesis not implemented, runs fail `model_unavailable`) |
| `EVENTLENS_RAG_SUMMARY_PROVIDER` | rag-worker | `none` (jobs stay pending); `fixture` (explicit demo, identity `fixture-summarizer`); `openai-compatible` (+ `EVENTLENS_RAG_SUMMARY_BASE_URL`, `_MODEL`, `_MODEL_VERSION`, optional `_API_KEY`) |
| `EVENTLENS_RAG_TRUSTED_ENRICHMENT_MODELS` | rag-worker | comma list of enrichment `model_version`s allowed to become labels; empty = none |
| `EVENTLENS_RAG_SESSION_COOKIE_SECURE` | rag-api | `0` (set `1` behind HTTPS) |
| `EVENTLENS_RAG_CATALOG_CONTENT` | rag-api | `full` \| `withheld` |

Retrieval defaults are unchanged (PostgreSQL branches, hybrid opt-in via
`EVENTLENS_RAG_SEARCH_BACKEND=elasticsearch`, RRF k=60 1:1, 3 spans/article,
freshness/reranker/multi-hop off, tuning `baseline`).

## 6. Run locally

```sh
make infra rag-db-create rag-migrate          # Compose PostgreSQL, eventlens_rag, Alembic head
export EVENTLENS_RAG_DATABASE_URL=postgresql://eventlens:eventlens-local@127.0.0.1:5432/eventlens_rag
export EVENTLENS_API_RAG_DATABASE_URL=$EVENTLENS_RAG_DATABASE_URL
export EVENTLENS_INTERNAL_SERVICE_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
export EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN=$EVENTLENS_INTERNAL_SERVICE_TOKEN
export EVENTLENS_CRUD_RAG_SERVICE_TOKEN=$EVENTLENS_INTERNAL_SERVICE_TOKEN
# demo only, explicit: deterministic fake answers/summaries (never "real" output)
export EVENTLENS_RAG_ANSWER_PROVIDER=fixture EVENTLENS_RAG_SUMMARY_PROVIDER=fixture
make rag-load-fixtures                        # or: data-team batch, see below
make rag-api    # :8001
make crud-api   # :8000
make rag-worker-serve
make news-smoke # 7-step HTTP smoke, report in .omo/evidence/news-backend/final-gates/
```

Real corpus: the existing importer `eventlens-rag handoff-import --articles
… --manifest …` only writes to databases whose name ends in `_eval` (guard
of the search benchmark); use a dedicated database such as
`eventlens_news_eval` for a local demo, or extend the guard in a reviewed
change. Labels need a trusted enrichment provider; without one every article
shows `metadata="unavailable"`/`pending` and topic/location filters match
nothing (correct, not an error).

### curl (IDs come from earlier responses)

```sh
C=http://127.0.0.1:8000 R=http://127.0.0.1:8001
curl -s "$C/api/v1/articles?limit=5"
TOPIC=$(curl -s "$C/api/v1/topics" | jq -r '.items[0].id')
LOC=$(curl -s "$C/api/v1/locations?q=ho%20chi%20minh" | jq -r '.items[0].id')
curl -s "$C/api/v1/articles?topic_ids=$TOPIC&location_ids=$LOC&published_after=2026-10-28T17:00:00Z&published_before=2026-10-29T17:00:00Z"
curl -s "$C/api/v1/article-facets?location_ids=$LOC"
ID=$(curl -s "$C/api/v1/articles?limit=1" | jq -r '.items[0].article_id')
curl -s "$C/api/v1/articles/$ID" ; curl -s "$C/api/v1/articles/$ID/summary"
TOKEN=$(curl -s -X POST "$R/api/v1/sessions" | jq -r .session_token)
CONV=$(curl -s -X POST "$R/api/v1/conversations" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"scope\":{\"type\":\"articles\",\"articles\":[{\"article_id\":\"$ID\"}]}}" | jq -r .conversation_id)
RUN=$(curl -s -X POST "$R/api/v1/conversations/$CONV/runs" -H "Authorization: Bearer $TOKEN" \
  -H 'Idempotency-Key: demo-1' -H 'Content-Type: application/json' \
  -d '{"question":"Bài này nói gì về giao thông?"}' | jq -r .run_id)
curl -s "$R/api/v1/runs/$RUN" -H "Authorization: Bearer $TOKEN"     # poll until terminal
REF=$(curl -s "$R/api/v1/runs/$RUN" -H "Authorization: Bearer $TOKEN" | jq -r '.citations[0].source_ref_url')
VIEW=$(curl -s -X POST "$R$REF" -H "Authorization: Bearer $TOKEN" | jq -r .viewer_url)
curl -s "$R$VIEW"
```

## 7. Reverse proxy

Route by prefix on the same origin: crud-api for `/api/v1/articles`,
`/api/v1/article-facets`, `/api/v1/topics`, `/api/v1/locations`,
`/api/v1/sources`; rag-api for `/api/v1/sessions`, `/api/v1/conversations`,
`/api/v1/runs`, `/briefing`. Deny `/internal/`. Forward cookies and
`X-Request-ID`; polling needs no SSE settings. Implemented in
`src/frontend/web/nginx.conf` (production) and `vite.config.ts` (development);
route table in `src/frontend/web/README.md`.

## 8. Known limits

* No live model has been called. Live chat is **not implemented**, not just
  unconfigured: `EVENTLENS_RAG_ANSWER_PROVIDER=live` only evaluates the
  activation (which refuses without a trusted issuer) and has no branch that
  builds a live synthesizer/transport; runs fail `model_unavailable`. The
  OpenAI-compatible summary adapter and its transport are wired but no
  provider endpoint is configured or approved.
* Enrichment providers are still `fake`/`fake-timeout`; their output is never
  turned into labels unless explicitly trusted (tests only).
* Lexical `q` uses PostgreSQL `simple` (no Vietnamese word segmentation or
  diacritic folding); ES catalog projection is not implemented (PG only).
* No SSE; polling only. Deleted conversations are soft-deleted (content kept
  until a retention job exists).
* Cursors are not snapshots; facets are not self-excluding.
