# eventlens-ai-rag-mvp - Work Plan

## TL;DR (For humans)
**What you'll get:** Articles are stored first, enriched by AI with topics, entities, locations, and content metadata, then indexed for analysts to query with grounded claim-level citations and explicit uncertainty.

**Why this approach:** Persist-first enrichment keeps articles searchable when AI fails; versioned metadata and immutable evidence spans prevent taxonomy and citation drift; PostgreSQL + pgvector keeps retrieval inspectable.

**What it will NOT do:** No crawler, event grouping/merge/create, real provider in CI, full RBAC, or direct LLM writes to canonical storage.

**Effort:** Large
**Risk:** High - new retrieval, model, API, and UI path with data-grounding correctness requirements.
**Decisions to sanity-check:** Persist-first async enrichment; controlled taxonomy plus suggestions; Postgres + pgvector; OpenAI-compatible adapter; 1,200/200 Unicode chunking; fail-closed answer errors.

Your next move: run `$start-work eventlens-ai-rag-mvp --worktree <absolute-path>` after reviewing this plan. Full execution detail follows below.

---

> TL;DR (machine): XL/high-risk enrichment-first grounded RAG MVP with 9 implementation tasks and 4 final verification tasks.

## Scope
### Must have
- Approved design scope from `docs/designs/ai-rag-briefing-mvp.md`.
- Shared strict/frozen RAG contracts: `ArticleDocument`, `EvidenceSpan`, `BriefingQuery`, `BriefingAnswer`, `Claim`, `Citation`, `Uncertainty`.
- Versioned `ArticleEnrichment` contract with controlled topics/content type/event relevance, canonical entities/locations, suggested tags, evidence, field confidence, model/prompt versions, and lifecycle state.
- Persist-first async flow: canonical article and lexical index available before enrichment; successful enrichment updates metadata/vector index without blocking raw ingest.
- Versioned local corpus with 30-100 normalized articles and 10 labeled questions.
- PostgreSQL + pgvector storage/index path: full-text search, cosine vector search, hybrid deterministic rank, index metadata, manual rebuild.
- Structured model protocol with OpenAI-compatible HTTP adapter and deterministic fake adapter for CI.
- Claim/evidence-span validator, allowlist/redaction, fail-closed typed errors, stable error codes, budgets and telemetry.
- FastAPI query use case/endpoint and React analyst query/result/citation/uncertainty/error states.
- TDD tests, deterministic eval, API integration, web tests, and one end-to-end analyst flow.
### Must NOT have (guardrails, anti-slop, scope boundaries)
- Do not alter/delete existing dirty AI-worker WIP, `.omo` artifacts, or unrelated files.
- Do not use `FakeEventExtractor`, `InMemoryEventWriter`, or `event.v1` as RAG answer/persistence semantics.
- Do not add crawling, RSS, MinIO/Parquet ingestion, full RBAC/multi-tenancy, living-event automation, event grouping/merge/create/write, Kafka/MCP/queues, fine-tuning, multi-agent orchestration, or production deployment.
- Do not require network/provider secrets in CI; do not return raw model output after validation failure; do not send non-allowlisted fields to provider.
- Do not mutate canonical `Article`; do not write AI tags directly into `article.v1`; do not block article persistence or lexical search on enrichment success.

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: TDD; pytest for Python contracts/retrieval/API, Bun test for web, deterministic eval command, mocked provider transport, no-network CI.
- Evidence: `.omo/evidence/` with one artifact per todo and final verifier; each artifact records exact command, exit code, and relevant assertion output.
- Global verification: `make check`; targeted commands are listed per todo. Every happy path has a failure-path test.
- LLM eval split: enrichment eval measures field precision/recall, taxonomy validity, evidence support, abstention, and hallucination; answer eval remains separate.

## Execution strategy
### Target file map

- Shared contracts: `packages/contracts-python/src/eventlens_contracts/{rag,enrichment}.py`, corresponding `test_rag_*` and `test_enrichment_*` files, exports in `eventlens_contracts/__init__.py`, schemas under `contracts/`.
- Corpus/eval: `fixtures/rag/manifest.v1.json`, `fixtures/rag/articles/*.json`, `fixtures/rag/questions.v1.json`.
- RAG package: `packages/rag-python/pyproject.toml`; `src/eventlens_rag/{ports,errors,chunking,models,telemetry,enrichment,retrieval,synthesis,citations,redaction}.py`; `src/eventlens_rag/adapters/{postgres,openai_compatible,fake}.py`; `src/eventlens_rag/cli.py`; tests mirrored under `packages/rag-python/tests/`.
- Database: `packages/rag-python/migrations/001_enable_vector.sql`, `002_create_rag_index.sql`; update `compose.yaml` to pinned pgvector-enabled PostgreSQL image and healthcheck; add Make targets for migration/rebuild/eval.
- API: keep health paths; add `apps/api/src/eventlens_api/briefing.py` for use case, `briefing_http.py` for DTO/route, wire from `main.py`; tests `test_briefing_use_case.py`, `test_briefing_api.py`.
- Web: `apps/web/src/briefing.ts`, `BriefingView.tsx`, `BriefingView.test.tsx`; update `App.tsx`, `styles.css`; keep components under 250 pure LOC by extracting citation/error views only when needed.
- CI/docs: update `.github/workflows/ci.yml`, `Makefile`, `README.md`, `apps/ai-worker/README.md` only where new commands/scope require it; never overwrite unrelated dirty hunks.

### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

- Wave 0: T1 contracts; T2 corpus/eval after contract shapes are fixed.
- Wave 1: T3 reusable package/storage/index; T4 enrichment lifecycle after T1-T3.
- Wave 2: T5 retrieval and T6 answer synthesis proceed in parallel after enriched SearchDocument boundary exists.
- Wave 3: T7 API, then T8 web; T9 evaluation/CI waits for all runtime paths.
- Final wave: F1-F4 run in parallel after T1-T9; all must pass.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| T1 Shared contracts | Dirty-worktree inventory + existing contracts | T2-T8 | none |
| T2 Corpus and eval fixtures | T1 | T3-T6, T9 | none |
| T3 RAG package + article/index storage | T1, T2 | T4-T7, T9 | none |
| T4 Metadata enrichment | T1-T3 | T5-T7, T9 | none |
| T5 Retrieval | T3, T4 | T7, T9 | T6 |
| T6 Answer synthesis | T3, T4 | T7, T9 | T5 |
| T7 API | T5, T6 | T8, T9 | none |
| T8 Web analyst flow | T7 | T9 | none |
| T9 Evaluation/CI | T1-T8 | F1-F4 | none |

### Data flow and enrichment state

```text
ArticleBundle
  -> validate + persist immutable ArticleDocument
  -> lexical index immediately
  -> create enrichment job [PENDING]
       -> claim job [PROCESSING]
       -> LLM structured metadata + evidence validation
          -> READY  -> persist ArticleEnrichment version -> rebuild SearchDocument metadata/vector
          -> FAILED -> preserve ArticleDocument + lexical index; typed failure + retry policy
  -> hybrid retrieval over available lexical + READY enrichment/vector data
  -> grounded answer synthesis
```

```text
PENDING -> PROCESSING -> READY
   ^          |            |
   |          v            v
 retry <--- FAILED       STALE (article hash or enrichment version changed)
                         |
                         +-> PENDING for new version
```

Idempotency key: `(article_id, content_hash, enrichment_version)`. Only one active job per key. Transport/timeout failures use bounded retry with backoff; schema/evidence failures become `FAILED` without blind retry. A new article hash or enrichment/prompt version supersedes prior output and marks it `STALE`.

## Todos
> Implementation + Test = ONE todo. Never separate.
- [ ] 1. Add shared strict RAG and enrichment contracts
  What to do / Must NOT do: Add frozen `ArticleDocument`, `ArticleEnrichment`, `EnrichmentStatus`, `TopicTag`, `EnrichedEntity`, `EnrichedLocation`, `EvidenceSpan`, `BriefingQuery`, `BriefingAnswer`, `Claim`, `Citation`, and `Uncertainty` models under `packages/contracts-python`. Controlled enums cover `topics`, `content_type`, and `event_relevance`; canonical entity/location structures carry evidence and field confidence; `suggested_tags` captures out-of-taxonomy discovery but is excluded from primary filters. Add model/prompt/enrichment versions, content hash, enriched_at, attempt count, failure code, and lifecycle invariants. Export publicly and add JSON Schema/semantic vectors. Do not mutate `Article`, write enrichment into `article.v1`, or put RAG claims into `event.v1`.
  Parallelization: Wave 0 | Blocked by: existing contract conventions | Blocks: 3, 4, 5, 6, 7
  References (executor has NO interview context - be exhaustive): `packages/contracts-python/src/eventlens_contracts/base.py:1-46`; `packages/contracts-python/src/eventlens_contracts/article.py:61-85`; `packages/contracts-python/src/eventlens_contracts/__init__.py`; `packages/contracts-python/src/eventlens_contracts/schema.py`; `packages/contracts-python/tests/test_models.py`; `packages/contracts-python/tests/test_schema.py`; `packages/contracts-python/tests/test_semantic_vectors.py`; `docs/designs/ai-rag-briefing-mvp.md:45-73,124-136`.
  Acceptance criteria (agent-executable): RAG and enrichment contract tests pass; tests prove valid claim linkage, enrichment lifecycle combinations, controlled taxonomy rejection, suggestions acceptance, evidence/confidence bounds, content-hash/version identity, duplicate IDs, UTC, frozen/extra-forbid behavior.
  QA scenarios (name the exact tool + invocation): happy create valid answer and round-trip JSON Schema; failure `pytest.raises` for every invariant and unknown field; run `uv run basedpyright`. Evidence `.omo/evidence/task-1-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(contracts): add grounded briefing contracts

- [ ] 2. Create versioned corpus and labeled enrichment/RAG evaluation fixtures
  What to do / Must NOT do: Add `fixtures/rag/` with 30-100 normalized ArticleDocument cases, expected ArticleEnrichment outputs, and 10 fixed analyst questions. Label controlled topics, suggested tags, entities, locations, content type, event relevance, evidence spans, confidence, abstention, duplicate/follow-up/conflict/delayed publication/relative date/restricted-field/prompt-injection cases. Record provenance/license and expected IDs. Do not ingest unapproved external content or change existing fixtures.
  Parallelization: Wave 0 | Blocked by: T1 contracts | Blocks: 3, 4, 5, 8
  References (executor has NO interview context - be exhaustive): `docs/designs/ai-rag-briefing-mvp.md:138-147,309-326`; `fixtures/article_bundle.v1.json`; `packages/contracts-python/tests/test_models.py`; `packages/contracts-python/tests/test_semantic_vectors.py`; `.omo/drafts/eventlens-ai-rag-mvp.md:41-47`.
  Acceptance criteria (agent-executable): `uv run pytest packages/contracts-python/tests/test_rag_fixtures.py -q` passes; fixture manifest has fixed version/count, 10 questions, expected source/claim/span IDs, and every required edge; no pre-existing dirty path is modified.
  QA scenarios (name the exact tool + invocation): happy validate complete corpus; failure malformed fixture, missing expected source, or missing provenance/license fails. Evidence `.omo/evidence/task-2-eventlens-ai-rag-mvp.md`.
  Commit: Y | test(fixtures): add labeled RAG corpus and evaluation cases

- [ ] 3. Create reusable RAG package, article/enrichment storage, and PostgreSQL + pgvector index
  What to do / Must NOT do: Add `packages/rag-python` as uv workspace member with ports/services/adapters/CLI. Add PostgreSQL tables for immutable article documents, enrichment jobs/versions, chunks, vectors, and index metadata. Persist ArticleDocument first and build lexical FTS immediately. Add pgvector `vector(1536)` + HNSW cosine index, manual rebuild, and transaction-safe job claim/idempotency constraints. Store `(article_id, content_hash, enrichment_version)` uniqueness; chunk 1,200 Unicode code points with 200 overlap. Correction/deletion means rebuild. CI uses fake embeddings. Do not require MinIO/Parquet or block article persistence on enrichment.
  Parallelization: Wave 1 | Blocked by: T1/T2 | Blocks: 4, 5, 6, 8
  References (executor has NO interview context - be exhaustive): `compose.yaml:1-59`; `README.md:86-96`; `docs/designs/ai-rag-briefing-mvp.md:75-108,254-270,294-307`; pgvector hybrid/SQL guidance from official pgvector docs; `apps/api/pyproject.toml:7-14`; `pyproject.toml:9-16`.
  Acceptance criteria (agent-executable): `uv sync --all-packages --group dev` includes `eventlens-rag`; API package can import `eventlens_rag` while no app imports another app; local `docker compose --profile infra up -d postgres` plus migration/rebuild command creates FTS and pgvector structures; indexed rows expose corpus/index versions, indexed_at, content hash and offsets; deterministic fake embeddings need no network/provider secret.
  QA scenarios (name the exact tool + invocation): happy run `make infra`, rebuild command, indexed fixture query; failure stop DB/index and assert typed unavailable error, change content hash and assert stale/rebuild requirement, duplicate document and assert suppression. Evidence `.omo/evidence/task-3-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(search): add versioned pgvector corpus index

- [ ] 4. Implement asynchronous metadata enrichment lifecycle
  What to do / Must NOT do: Inside `packages/rag-python`, add `ArticleEnricher` and `EnrichmentRepository` ports, fake and OpenAI-compatible structured enrichment adapters, worker/CLI orchestration, allowlisted prompt context, controlled taxonomy + suggestions, evidence/confidence validation, state transitions, bounded retry/backoff, idempotency, stale/supersede handling, telemetry, and SearchDocument rebuild after READY. Transport/timeout retry; schema/evidence failure becomes FAILED. Keep ArticleDocument and lexical index available on failure. Do not silently reuse enrichment across content hash/model/prompt/version changes.
  Parallelization: Wave 1 | Blocked by: T1/T2/T3 | Blocks: T5/T6/T7/T9
  References (executor has NO interview context - be exhaustive): enrichment contracts from T1; `packages/rag-python` storage from T3; `apps/ai-worker/src/eventlens_worker/ports.py:23-38`; `apps/ai-worker/src/eventlens_worker/runner.py:60-116`; user-approved persist-first async and controlled+suggestions decisions.
  Acceptance criteria (agent-executable): tests cover every legal/illegal state transition, duplicate delivery, concurrent claim, retry exhaustion, stale article hash, changed prompt/model/version, malformed output, unsupported taxonomy, ungrounded metadata, prompt injection, redaction, FAILED lexical fallback, READY metadata/vector reindex, and telemetry without article body.
  QA scenarios (name the exact tool + invocation): happy persist article -> lexical search -> READY enrichment -> metadata/vector search; failure model timeout -> retry/FAILED while lexical result remains; stale version creates new PENDING without overwriting history. Evidence `.omo/evidence/task-4-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(ai): add versioned article metadata enrichment

- [ ] 5. Implement hybrid retrieval and deterministic evidence assembly
  What to do / Must NOT do: Inside `packages/rag-python`, implement repository/index ports and services for PostgreSQL lexical FTS + pgvector cosine retrieval, reciprocal-rank fusion with fixed `k=60`, publication-time filters, source/entity metadata filters, freshness, dedup, deterministic tie-break `(fused_score DESC, source_published_at DESC NULLS LAST, article_id ASC)`, max 20 articles, and max 12,000 context tokens. Return evidence spans with index version and query time basis `source_published_at`; measure lexical, semantic, combined Recall@k independently. Do not call model or create/update events here.
  Parallelization: Wave 2 | Blocked by: T1-T4 | Blocks: T7, T9 | Can parallelize with: T6
  References (executor has NO interview context - be exhaustive): `apps/ai-worker/src/eventlens_worker/ports.py:23-38`; `apps/ai-worker/src/eventlens_worker/runner.py:60-116`; `packages/contracts-python/src/eventlens_contracts/article.py:61-85`; `docs/designs/ai-rag-briefing-mvp.md:75-108,149-157,309-326`.
  Acceptance criteria (agent-executable): `uv run pytest packages/rag-python/tests/test_retrieval.py packages/rag-python/tests/test_evidence_context.py -q` proves lexical/semantic/hybrid ranking, exact fusion/tie order, publication boundary rules, null publication handling, dedup, zero hits, stale index, max article/token budgets, and stage metrics; retrieval never imports worker/event modules or emits event writes.
  QA scenarios (name the exact tool + invocation): happy query known topic and filter by READY enrichment; failure enrichment pending/failed/stale falls back to lexical without pretending metadata exists; other retrieval errors remain typed. Evidence `.omo/evidence/task-5-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(search): add deterministic hybrid retrieval

- [ ] 6. Add structured OpenAI-compatible answer synthesis, redaction, and grounding validator
  What to do / Must NOT do: Inside `packages/rag-python`, add model and embedding protocols, deterministic fake adapters, mocked OpenAI-compatible HTTP adapters configured only by `EVENTLENS_OPENAI_BASE_URL`, `EVENTLENS_OPENAI_API_KEY`, `EVENTLENS_CHAT_MODEL`, and `EVENTLENS_EMBEDDING_MODEL`; allowlisted context builder; structured output parser; claim/span citation validator; abstention; typed errors; telemetry. Allowed provider fields: article ID, title, normalized content spans, source name/domain, source publication time. Explicitly prohibit raw storage URI, internal processing metadata, credentials, and non-allowlisted fields. Enforce max 1,500 output tokens, p95 instrumentation, 30 concurrency guard, and fail closed for timeout/provider error/malformed output/unsupported claim/unknown span/fingerprint mismatch. Do not return raw model output after validation failure.
  Parallelization: Wave 2 | Blocked by: T1-T4; uses fake CI model | Blocks: T7, T9 | Can parallelize with: T5
  References (executor has NO interview context - be exhaustive): `apps/ai-worker/src/eventlens_worker/models.py:57-165`; `apps/ai-worker/src/eventlens_worker/ports.py:23-38`; `docs/designs/ai-rag-briefing-mvp.md:92-108,159-167,223-234,282-326`; `packages/contracts-python/src/eventlens_contracts/base.py:41-46`.
  Acceptance criteria (agent-executable): `uv run pytest packages/rag-python/tests/test_synthesis.py packages/rag-python/tests/test_citations.py packages/rag-python/tests/test_redaction.py packages/rag-python/tests/test_openai_adapter.py -q` proves valid answer, abstention, claim/span binding, offset/fingerprint rejection, prompt injection containment, provider timeout/error, malformed JSON, budget/concurrency failure, telemetry without restricted content, no raw fallback, and exact HTTP mapping.
  QA scenarios (name the exact tool + invocation): happy fake model answer uses content plus READY metadata; failure answer validator remains independent from enrichment failure and never trusts suggested tags as facts. Evidence `.omo/evidence/task-6-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(ai): add grounded synthesis and citation validation

- [ ] 7. Expose briefing query use case and FastAPI endpoint
  What to do / Must NOT do: Add `eventlens-rag` as API workspace dependency. Extend framework-free API application layer with dependency-injected repository/retriever/synthesizer, add strict query/answer HTTP DTOs, stable error mapping, internal-only allowlist boundary, publication-time basis metadata, and composition-root wiring. Keep route thin. Add query size/budget/concurrency validation. Do not put SQL, provider calls, or event mutation in `http.py`; do not import `apps/ai-worker`; do not require auth beyond explicit internal-only boundary marker for MVP.
  Parallelization: Wave 3 | Blocked by: T5/T6 | Blocks: T8/T9
  References (executor has NO interview context - be exhaustive): `apps/api/src/eventlens_api/application.py:1-16`; `apps/api/src/eventlens_api/http.py:1-25`; `apps/api/src/eventlens_api/main.py:1-21`; `apps/api/tests/test_health.py`; `apps/api/tests/test_health_use_case.py`; `apps/api/tests/conftest.py`; `docs/designs/ai-rag-briefing-mvp.md:169-177,282-307`.
  Acceptance criteria (agent-executable): `uv run pytest apps/api/tests/test_briefing_api.py apps/api/tests/test_briefing_use_case.py -q` proves valid query, empty/oversized query, no evidence, retrieval unavailable/stale, model unavailable, invalid answer, budget exceeded, stable error payloads, source-published time basis, and health regression; `httpx2` ASGI integration exercises full fake path.
  QA scenarios (name the exact tool + invocation): happy API returns cited answer and enrichment freshness metadata; failure pending/failed enrichment still answers via lexical content when evidence suffices. Evidence `.omo/evidence/task-7-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(api): add grounded briefing query endpoint

- [ ] 8. Build analyst web query, citation, uncertainty, and failure-state flow
  What to do / Must NOT do: Replace static disconnected foundation state with a focused analyst query/result experience using existing editorial tokens. Add Zod strict response parsing, query client, answer/claims/citations/uncertainty rendering, source inspection, time-basis label, loading, empty, no-evidence, partial, model error, validation error, and retry states. Do not add dashboard, event browser, auth UI, or unrelated redesign.
  Parallelization: Wave 3 | Blocked by: T7 API contract | Blocks: T9
  References (executor has NO interview context - be exhaustive): `apps/web/src/App.tsx:1-81`; `apps/web/src/foundation.ts:1-15`; `apps/web/src/foundation.test.ts:1-38`; `apps/web/src/tokens.css`; `apps/web/src/styles.css`; `apps/web/package.json:6-25`; `apps/web/DESIGN.md:9-98`.
  Acceptance criteria (agent-executable): `cd apps/web && bun test && bun run check && bun run typecheck && bun run build` passes; tests prove strict response parsing, cited claim rendering, source opening/inspection, uncertainty/time-basis display, empty/no-evidence/loading/partial/error/retry states at 320px and desktop widths; API client never renders unsupported answer as valid.
  QA scenarios (name the exact tool + invocation): happy inspect citations plus enrichment freshness; failure stale/pending metadata displays honest degraded state, not fabricated tags. Evidence `.omo/evidence/task-8-eventlens-ai-rag-mvp.md`.
  Commit: Y | feat(web): add analyst briefing experience

- [ ] 9. Add deterministic enrichment/RAG eval, end-to-end verification, telemetry gates, and CI wiring
  What to do / Must NOT do: Add separate enrichment and answer eval targets. Enrichment report measures controlled-topic/entity/location/content-type/event-relevance precision/recall, taxonomy validity, evidence support, confidence calibration, abstention, hallucination, stale/retry behavior, latency/token/cost. RAG report keeps retrieval/citation/claim/abstention/conflict/temporal/security metrics. Add persist -> lexical -> enrich -> reindex -> query E2E with fake adapters, no network/provider secrets.
  Parallelization: Wave 3 | Blocked by: T1-T8 | Blocks: F1-F4
  References (executor has NO interview context - be exhaustive): `Makefile:1-35`; `.github/workflows/ci.yml:19-62`; `docs/designs/ai-rag-briefing-mvp.md:206-234,309-326`; `pyproject.toml:54-62`; `apps/web/package.json:7-11`.
  Acceptance criteria (agent-executable): `make check`, `make enrichment-eval`, and `make rag-eval` pass with no network; reports include corpus, index/model/prompt/enrichment versions, enrichment field metrics, evidence support, Recall@k, citation metrics, abstention/conflict/security results, p95 latency, token counts, cost estimate, concurrency and freshness; CI runs the same commands from clean frozen install.
  QA scenarios (name the exact tool + invocation): happy clean install + `make check && make enrichment-eval && make rag-eval`; failure poisoned metadata, unsupported tag, missing evidence, stale enrichment, DB/model outage each fail correct gate without losing lexical availability. Evidence `.omo/evidence/task-9-eventlens-ai-rag-mvp.md`.
  Commit: Y | test(ci): add deterministic RAG evaluation gates

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit - inspect all changed files against every Must/Never clause; run `git diff --check`, verify no dirty pre-existing path was overwritten, and attach `.omo/evidence/final-plan-compliance.md`.
- [ ] F2. Code quality review - run `uv run ruff check .`, `uv run basedpyright`, `cd apps/web && bun run check && bun run typecheck`; inspect architecture dependency direction and typed errors; attach `.omo/evidence/final-code-quality.md`.
- [ ] F3. Real manual QA - start local Postgres/index/API/web, execute analyst query, inspect citation source and each loading/empty/error/retry state; attach screenshots/logs in `.omo/evidence/final-manual-qa.md`.
- [ ] F4. Scope fidelity - compare final diff to this plan and spec; verify no crawler, event write/grouping, raw fallback, provider secret in CI, or unrelated worker-WIP changes; attach `.omo/evidence/final-scope-fidelity.md`.

## Commit strategy

Create focused commits in dependency order, never commit `.omo` session artifacts or unrelated pre-existing WIP:

1. `feat(contracts): add grounded briefing contracts`
2. `test(fixtures): add labeled RAG corpus and evaluation cases`
3. `feat(search): add versioned pgvector corpus index`
4. `feat(ai): add versioned article metadata enrichment`
5. `feat(search): add deterministic hybrid retrieval`
6. `feat(ai): add grounded synthesis and citation validation`
7. `feat(api): add grounded briefing query endpoint`
8. `feat(web): add analyst briefing experience`
9. `test(ci): add deterministic enrichment and RAG evaluation gates`

Pair implementation and tests in each commit. Before commit, inspect `git status`, `git diff`,
and `git diff --check`; stage only files belonging to the current task.

## Success criteria

- A versioned 30-100 article corpus and 10-question evaluation set exists.
- Canonical articles persist and become lexically searchable before enrichment; LLM failure never removes or blocks them.
- ArticleEnrichment is immutable/versioned, evidence-backed, idempotent by article hash + enrichment version, and follows tested PENDING/PROCESSING/READY/FAILED/STALE transitions.
- Controlled taxonomy drives filters; suggested tags remain discoverable but never silently become canonical filter values.
- Analyst query returns structured answer with every material claim bound to valid article/span evidence, or explicit abstention/error.
- Publication-time filter basis is visible; event occurrence is never inferred from publication time.
- Hybrid retrieval is deterministic, bounded, deduplicated, and reports index metadata.
- Postgres + pgvector local path works; CI fake path needs no network/provider secret.
- Runtime budgets enforce 20 articles, 12k input, 1.5k output, 30 concurrency; p95 8s is reported as initial target.
- API and web expose recoverable stable states for all failure matrix cases.
- `make check`, `make enrichment-eval`, and `make rag-eval` pass from clean frozen install.

## Failure modes and test coverage

```text
ArticleBundle
  -> ArticleDocument validation
       ├─ invalid canonical input ............ unit: reject, clear validation error
       └─ valid -> persist + lexical index ... integration: searchable immediately
  -> Enrichment job
       ├─ duplicate/concurrent delivery ...... integration: one active idempotency key
       ├─ provider timeout/5xx ............... unit+integration: bounded retry -> FAILED
       ├─ malformed/ungrounded metadata ...... unit+[→EVAL]: FAILED, no metadata reindex
       ├─ prompt injection/restricted fields . [→EVAL]: ignored/redacted
       ├─ article/version changed ............ unit: old READY -> STALE; new PENDING
       └─ READY .............................. integration: metadata/vector reindex
  -> Hybrid retrieval
       ├─ enrichment pending/failed/stale .... integration: lexical degradation, explicit freshness
       ├─ vector/index unavailable ........... integration: typed degraded/unavailable result
       └─ READY .............................. [→EVAL]: lexical+metadata+vector ranking
  -> Answer synthesis
       ├─ no evidence ........................ unit/E2E: abstention
       ├─ invalid citation/span .............. unit: INVALID_ANSWER, no raw fallback
       └─ valid .............................. [→E2E]: answer -> citation inspection
```

No known silent critical gap remains in plan. Every failure has a test requirement, typed handling, and visible/degraded behavior.

## What already exists

- Strict frozen Article/Event contracts, JSON Schema, semantic vectors, fixtures, pytest conventions: reused.
- Worker ports, typed models, deterministic fake adapters, runner/trace patterns: reused as patterns only; event semantics not reused.
- FastAPI application/HTTP split and React/Zod foundation: extended rather than rebuilt.
- PostgreSQL/MinIO Compose placeholders: PostgreSQL replaced/pinned to pgvector-capable image; MinIO remains unused.

## NOT in scope

- Crawling/source acquisition: normalized ArticleBundle remains input.
- Full RBAC/multi-tenancy: MVP internal-only allowlist/redaction.
- Event extraction/grouping/merge/living-event writes: enrichment classifies article metadata only.
- Human-reviewed taxonomy management UI: controlled taxonomy lives in versioned contract/config; suggestions are output only.
- Production queue/Kafka/retry service: DB-backed enrichment job lifecycle + CLI/worker loop only.
- Fine-tuning, multi-agent enrichment, automatic truth arbitration, production deploy.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| Eng Review | `/plan-eng-review` | Architecture & tests | 2 | CLEAR | Enrichment isolated as persist-first versioned phase; lifecycle, tests, and degradation paths folded |
| Outside Voice | team review | Independent architecture/test challenge | 2 | COMPLETE | State/idempotency/retry/eval/performance risks addressed |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | NOT RUN | Analyst UI exists in scope; review recommended before implementation |

**VERDICT:** ENG CLEARED — updated plan ready to implement; design review optional but recommended for analyst UI.

NO UNRESOLVED DECISIONS
