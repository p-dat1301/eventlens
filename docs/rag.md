# EventLens RAG

Reusable T3 foundation plus the T4 enrichment lifecycle, now the RAG bounded
context at `src/backend/rag` (layout and dependency rules: `docs/architecture.md`).
Domain and application (including `application/ports`) are framework-free (no
LangGraph, Typer, psycopg or concrete adapters). LangGraph resides only in
`workflows/`; PostgreSQL and fake adapters reside in `infra/`. Paths below are
relative to `src/backend/rag`.
All embeddings are deterministic local fakes with 1536 dimensions.

## Local database commands

All commands read `EVENTLENS_RAG_DATABASE_URL` and exit `3` with
`rag_storage_unavailable` when PostgreSQL cannot be reached.

- `eventlens-rag migrate` applies checksum-verified migrations in one transaction;
  any failure rolls back schema changes and ledger rows together. Never edit an
  applied migration: add a new numbered file instead.
- The single migration SQL source is `infra/postgres/migrations/` (shipped in the
  wheel and read at runtime via `importlib.resources`). File names and bytes are
  frozen: `test_bundled_sql_preserves_original_migration_bytes` pins the file set
  and the SHA-256 that existing ledgers recorded. Missing SQL fails before opening a database or reporting success.
- Untracked legacy tables (created before the migration ledger, e.g. `rag_chunks`
  without `generation`) stop `migrate` with `rag_migration_failed` and change
  nothing. Existing pre-004 rows cannot be reconstructed from title/content alone:
  migration stops until exact original ArticleDocuments are restored from an
  approved source; never fabricate missing document fields.
- `eventlens-rag load-fixtures` persists `fixtures/rag/articles`; lexical FTS is
  available immediately, before any enrichment.
- `eventlens-rag rebuild` stages fake-embedding generations for current articles
  (reusing complete matching generations) and activates them in one transaction.

## T4 enrichment lifecycle

Flow: `ArticleBundle -> Article -> ArticleDocument -> persist (lexical now) + PENDING
job (same transaction) -> claim PROCESSING -> provider draft -> structural
validation -> stage vectors (EnsureGeneration) -> fenced publish READY`.
The model never runs during ingest; a failed enrichment leaves the document and its
lexical row untouched. Worker commands live in `worker_main.py` (`eventlens-rag-worker
ingest` / `enrich`, see `docs/event-pipeline.md`).

Layers: `domain/{article_ingest,enrichment_lifecycle,enrichment_draft}.py`,
`application/ports/enrichment.py` (`ArticleEnricher`, `EnrichmentRepository`, `TelemetrySink`),
`application/use_cases/{ingest,enrichment}.py`, `workflows/ingestion/` (compiled `StateGraph`
`load -> enrich -> validate -> stage -> publish | fail`, no checkpointer, no retry
loop) and adapters `fake_enricher`, `openai_enricher` (injected transport only),
`postgres_enrichment`, `memory_enrichment` (unit-test fake), `telemetry`.

Article -> ArticleDocument mapping (`domain/article_ingest.py`):

| ArticleDocument field | Source |
|---|---|
| `raw_content` / `normalized_content` | `Article.content` / NFC of it |
| `content_hash`, `content_fingerprint` | RAG `normalized_content_hash` of the NFC text; the upstream `Article.content_hash` is not reused |
| `source_published_at` | `Article.published_at` (publisher time; `None` stays `None`) |
| `ingested_at` | RAG clock when ingest received the article |
| `processed_at` | RAG clock after normalization (never before `ingested_at`) |
| `evidence_spans` | empty; no provenance is invented |

Upstream `raw.fetched_at`, `processing.extracted_at` and `updated_at_source` are not
mapped. Entities/locations never carry canonical IDs (`entity_id`/`location_id` are
`None`) because no ID source exists yet.

Identity: `(article_id, content_hash, enrichment_version, model_version,
prompt_version)`; `enrichment_version` is `article-enrichment.v1`, model/prompt come
from the enricher. A worker claims only jobs of its own identity. Enqueueing a new
identity marks other PENDING/PROCESSING jobs of that article STALE; publishing marks
older READY jobs STALE. Correction (`persist_article` with a new hash) and tombstones
block old publication. Every publication is appended to the immutable
`rag_enrichment_publications` table (migration `006`), so history survives
STALE -> PENDING requeues. Metadata-only revisions reuse the complete embedding
generation (zero embedding calls).

Retry policy `application/policies/enrichment-retry.v1.json`
(`enrichment-retry.v1`): 3 total attempts (each claim counts, including a claim whose
lease expired), 30 s provider deadline per attempt, 120 s lease, backoff 30 s then
120 s (x4, cap 600 s, no jitter) measured on the database clock, at most 25 jobs per
pass. Only `provider_timeout`/`provider_unavailable` retry; malformed output,
taxonomy, evidence/metadata grounding, refusal, rejected requests and invalid
staging go straight to FAILED. A lease that expires at budget becomes FAILED
`lease_expired`. Only the job table owns the budget/schedule; neither graph nor
transport retries.

Fencing: every load/retry/failure/STALE/publish write matches `job_id`,
`claim_generation`, `status = 'PROCESSING'`, `lease_expires_at > clock_timestamp()`,
no recorded cancellation for that generation and a current non-tombstoned revision.
Zero rows means lost ownership: the result is discarded. No transaction stays open
across a provider call. Claims use `FOR UPDATE SKIP LOCKED`.

Cancellation: every model, embedding and DB operation of a run is admitted through
`CancellationScope` (reconcile, claim, load, provider call, generation lookup, each
embedding call, stage write, publish, retry/failure write). `cancel()` takes the same
lock, so after it acknowledges no new operation starts; writes (stage, claim,
reconcile, retry/FAILED) are serialized and commit before a concurrent cancel can
acknowledge. `cancel()` records `cancel_requested_generation` under the job row lock;
the publish transaction locks that row first and re-checks it. Cancel first: no READY, no retry/FAILED write, no further model/DB work; lease
recovery reclaims later. Publish first: READY stands and cancel reports
`ALREADY_PUBLISHED`.

Durability (persist -> enqueue): the document and its job commit in one
transaction; documents persisted by other paths (for example `load-fixtures`) are
picked up by the reconciliation sweep `enqueue_missing` at the start of every
`enrich` pass. No broker is used. Every enqueue first locks the article's
`rag_article_current` row and re-reads the current revision: reconciliation always
enqueues the revision that is current under the lock (never an obsolete candidate),
never revives a STALE job, and supersedes other jobs only after it actually inserted
(or, for explicit ingest, requeued) the current revision's job. Duplicate delivery
never touches attempts, failure code, retry schedule or fence.

Provider boundary: only `article_id`, `title`, `normalized_content`, `source_name`,
`source_domain`, `source_published_at` are sent. No HTTP transport ships; the
OpenAI-compatible adapter is exercised only with a mocked transport and no setting
or API key can select it from the worker CLI. Validation is structural/linkage only;
semantic entailment and model quality remain T9.

## T5 hybrid retrieval and evidence assembly

Use case: `application/use_cases/retrieve.py::RetrieveEvidence.execute(query, caller, control,
options)` returns a typed `RetrievalResult` (`domain/retrieval.py`); raw DB rows never
leave `infra/retrieval/postgres_retrieval.py`.

```text
BriefingQuery + trusted CallerScope + QueryControl (cancel + end-to-end deadline)
  -> fixture access check, query clock read once
  -> metadata eligibility (only for topic/entity filters; valid READY publication of
     the current revision + active generation, never history)
  -> lexical FTS branch (rag_current_lexical; query and chunk terms are PostgreSQL
     'simple' lexemes; T3 chunk windows computed in SQL, bounded scan + pool)
   + exact-cosine vector branch (rag_current_vectors, full EmbeddingIdentity guard,
     complete ACTIVE generations only)
  -> RRF k=60 -> exact/near-duplicate suppression -> rerank (IdentityReranker
     default, <=40 candidates x <=512 tokens, 1.5 s slice) -> article aggregation
  -> access re-check, ONE batched final current/tombstone/scope/metadata read,
     access re-check after the read (and again before return)
  -> token-budgeted evidence context (<=20 articles, <=12k tokens) + metrics
```

T5 independent re-review APPROVE (2026-10-18), fixture/fake scope; R1-R7 closed.
See `.omo/evidence/T5-rereview3-20261018-codex/review.md`. G2 remains OPEN for T6/T7;
ANN quality and live activation remain outside this approval.

- Policy: `policies/retrieval-policy.v3.json` (`retrieval-policy.v3`, fusion
  `rrf-k60.v1`). `retrieval-policy.v1.json` and `.v2.json` are kept byte-identical as
  superseded history. v2 (2026-10-18 review) moved query/chunk terms to PostgreSQL
  'simple' lexemes, bounded the lexical scan and added access re-checks on every
  return path. v3 (re-review 2) freezes the real two-step lexical fetch:
  - SQL metadata candidates: 50 articles x the first 40 chunk windows. Per article
    keep `rank() <= 3` by matched lexemes, with ties at the cut kept.
    `max_metadata_candidates = 2000` is enforced; exceeding it is a typed error.
  - Python applies the frozen tie-break including chunk_id, the 3-per-article cap
    and depth 50.
  - `chunk_texts` fetches text for at most `max_text_chunks = 50` chunks.
  Any change needs a new version and re-run evidence.
- Filters: `published_after` inclusive, `published_before` exclusive on
  `source_published_at`; equal bounds = empty range; NULL publication time excluded
  once any bound is set. Topics any-of, entities any-of, AND across kinds. Missing
  metadata never matches; coverage is reported as `complete|partial|filter_unavailable`.
- Modes: `hybrid|lexical_only|vector_only|none` per query plus per-article
  `retrieval_basis`; lexical fallback is never labelled semantic. Rerank status is
  `applied|skipped|degraded`; only rerank timeout/error/invalid output degrades.
  Cancellation, deadline, access denial and storage failure are typed errors.
- Token counts use `application/ports/tokens.py` with the deterministic `FixtureTokenCounter`;
  they prove budget mechanics only, not real model token usage (T6/D32).
- Known limit: PostgreSQL's `simple` parser treats an unspaced CJK run as one
  lexeme, so CJK substring queries do not match. A passage beyond the first 40
  chunk windows of an article is not scanned; this is reported as
  `metrics.lexical_truncated_articles`, never widened.

Commands (both refuse a database that already holds articles; use an isolated one):

```sh
sh scripts/rag/with-test-db.sh uv run eventlens-rag retrieval-smoke
sh scripts/rag/with-test-db.sh uv run eventlens-rag \
    vector-route-report --output "$TMPDIR/vector-routes"
```

The production vector route is exact; HNSW iterative scan is measured by
`vector-route-report` only and stays disabled in `retrieval-policy.v3`.

## T6 grounded synthesis, citation validation and model budgets

Status (2026-10-24): implementation and independent integration review are
complete in fixture/fake scope. T6 is committed in `5d2ac29`; G2 remains OPEN
until T7 API/graph composition is complete. No live provider was called.
Evidence: `.omo/evidence/T6-rereview-20261024-codex/review.md`,
`.omo/evidence/task-6-eventlens-ai-rag-mvp.md` and the frozen contract
`.omo/evidence/T6/interfaces.md`.

Call order for T7 (process-local; T7 owns API/graph composition):

```text
lease  = ProcessConcurrencyLimiter(30).try_acquire()   # one shared limiter per runtime
control = QueryControl(policy.deadlines.request_deadline_seconds)
ledger  = RequestTokenLedger(counter, policy)          # same counter as RetrieveEvidence
plan    = PrepareSynthesis(counter, policy).execute(query, ledger)
result  = RetrieveEvidence(...).execute(query, caller, control,
            RetrievalOptions(remaining_context_budget=plan.remaining_context_budget))
answer  = SynthesizeAnswer(...).execute(query, result, caller, control, ledger, lease)
lease.release()                                        # pinned in-flight calls keep the slot
```

- `SynthesizeAnswer` returns a `SynthesisResult`, which is one of three
  outcomes: `answered`, `abstained_no_evidence` or `abstained_by_model`. Every
  failure is a typed `SynthesisError` with a stable `code` and `.reason`.
  `answer.source_documents` is internal; T7 must map a public DTO.
- The provider receives only five field groups: article ID, title, the selected
  span texts with their span IDs, source name/domain, and
  `source_published_at`. Hashes, offsets, canonical URLs, storage URIs and
  metadata stay on the server.
- The model returns the strict `synthesis_draft.v1` (claim text, span refs,
  temporal claim, reason codes). Citations, titles, URLs and uncertainty text
  are rendered by the server. Uncertainty text comes from versioned templates.
- Current-evidence boundary: one batched `current_articles` read happens before
  the call and one after it. Access is checked at entry, after each read, and
  at every return. No transaction is held across the model call.
- Policies:
  - `policies/synthesis-policy.v1.json` (label `fixture`): 12,000 cumulative
    input tokens; 1,500 output tokens; 20 articles; 30 concurrent requests; one
    call; zero retries; 20 s request deadline and 15 s per-call timeout.
  - Fixture pricing: 1/4 per token plus 2,000 per call, cap 20,000
    `fixture_micro_usd`. These numbers exist for admission tests only. They are
    not a price.
  - `policies/provider-activation.fixture.v1.json`: fixture mode with an
    `.invalid` endpoint. Live mode always fails closed. T6 ships no trusted
    issuer and no approved live pricing.
- Adapters: `DeterministicFakeSynthesizer` (the default),
  `ScriptedSynthesizer`, and `OpenAICompatibleSynthesizer`, which uses an
  injected transport and refuses a non-live decision. It never retries or
  follows redirects. An API key alone never enables live mode.

Smoke (refuses a non-empty database; fake/scripted model ports only):

```sh
sh scripts/rag/with-test-db.sh uv run eventlens-rag \
    synthesis-smoke --output "$TMPDIR/t6-smoke.json" --evidence "$TMPDIR/t6-evidence.json"
```

The smoke covers seven cases: happy cited answer, no-evidence abstention
(zero calls), lexical-only answer, invalid citation blocked, provider timeout,
budget failure (zero calls) and revoked access. It asserts each case and exits
2 if any expectation fails.

Not measured: real model tokenizer and drift, real cost, live latency (the 8 s
p95 is a target, not an SLO), and semantic support of claims (T9).
Mechanical grounding does not prove that a claim is true.

## Local Compose database

The Compose `eventlens` database may hold untracked pre-ledger RAG tables, so the
Make targets use a separate `eventlens_rag` database on the same server
(override with `RAG_DATABASE_NAME` or `RAG_DATABASE_URL`):

```sh
make infra
make rag-db-create      # idempotent CREATE DATABASE eventlens_rag
make rag-migrate
make rag-load-fixtures
make rag-rebuild        # second run reuses every generation
```

Legacy recovery: leave the `eventlens` database untouched. Its RAG tables have no
ledger and cannot be upgraded in place. To carry legacy content forward, re-ingest
the exact original ArticleDocuments from their approved source into
`eventlens_rag` with the current CLI; dropping or archiving the legacy tables
requires the owner's explicit approval.

## Test harness

- `make rag-db-test` runs `tests/backend/{rag,event_pipeline,crud}` and
  `tests/architecture` (including the T4 rag-worker CLI flows) against an isolated, auto-removed `pgvector/pgvector:0.8.0-pg17`
  container; any skipped test fails the run. T4/T5 DB tests each use a fresh schema
  (T5: `retrieval_url` fixture in `tests/backend/rag/conftest.py`).
- `make gate-g1` runs the G1 technical checks (worker regression, migrate,
  load-fixtures, rebuild twice with generation reuse, DB suite, unavailable-DB
  exit 3) against an isolated database. Exit 1 means a technical failure; exit 2
  means technical PASS with G1 acceptance still OPEN. The script never records
  G1 acceptance: that needs F1-F4 review verdicts, D41 approval evidence (or the
  owner's explicit acceptance of its absence) and explicit user acceptance. The
  presence of the event pipeline package alone establishes nothing. G1 was accepted on
  2026-10-16, scoped to T1-T3 fixture/fake with the separate `eventlens_rag`
  database; see `.omo/evidence/G1/acceptance-decision-2026-10-16.md`. Exit 2 from
  the script still only means technical PASS.
