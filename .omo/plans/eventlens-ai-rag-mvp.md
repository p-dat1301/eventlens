# eventlens-ai-rag-mvp - Work Plan

## TL;DR (For humans)
**What you'll get:** Persist-first article enrichment and grounded RAG, followed by noncanonical article/story grouping and topic discovery, durable multi-turn conversations, and bounded read-only agentic retrieval. Every answer keeps primary article/span citations and explicit uncertainty.

**Why this approach:** Persist-first enrichment keeps articles searchable when AI fails; versioned metadata and immutable evidence spans prevent taxonomy and citation drift; PostgreSQL + pgvector keeps retrieval inspectable.

**What it will NOT do:** No crawler, canonical event grouping/merge/create/write, multi-agent orchestration, real provider in CI, full RBAC, or direct LLM writes to canonical storage. G1-G3 exclude grouping, conversations, and automatic tools; G4-G6 add those extensions behind separate gates.

**Effort:** Large
**Risk:** High - new retrieval, model, API, and UI path with data-grounding correctness requirements.
**Decisions to sanity-check:** Persist-first async enrichment; controlled taxonomy plus suggestions; Postgres + pgvector; OpenAI-compatible adapter; 1,200/200 Unicode chunking; fail-closed answer errors.

Readiness caveat: document completion is not ENG CLEARED. Resolve independent review and gate-specific policy decisions below before executing affected work. Then run `$start-work eventlens-ai-rag-mvp --worktree <absolute-path>` with an inventoried worktree. This request authorizes expanded planning, not implementation, commit/push, live provider activation, shared access, or deployment.

---

> TL;DR (machine): XL/high-risk roadmap: G1-G3 fixture/fake LangGraph baseline T1-T9; G4 grouping/topics T10-T11; G5 conversations T12-T13; G6 bounded tools/eval T14-T15. Repeat F1-F4 at G3, G4, G5, G6 with scoped evidence. No task is accepted by this document.

## Scope
### Relationship to approved MVP design
`docs/designs/ai-rag-briefing-mvp.md` remains the source for the first briefing workflow, source/time semantics and evidence boundary through G1-G3. This roadmap explicitly supersedes only its optional post-briefing phase order: design Phase 4 `What Changed` evidence/answer snapshots and Phase 5 existing-event candidate grouping are deferred to separate proposals, not fulfilled by G4. G4 noncanonical story/topic grouping, G5 conversations and G6 bounded tools are separately reviewed roadmap extensions, not claims of having implemented the older event-grouping phase. No canonical event write or automatic merge is authorized. This plan does not edit or silently reinterpret the approved design document.

### Must have
- Approved design scope from `docs/designs/ai-rag-briefing-mvp.md`.
- Shared strict/frozen RAG contracts: `ArticleDocument`, `EvidenceSpan`, `BriefingQuery`, `BriefingAnswer`, `Claim`, `Citation`, `Uncertainty`.
- Span text contract (D33): T1 freezes versioned NFC normalization of searchable text, Unicode code-point offsets with half-open `[start, end)` bounds, and fingerprints computed over that normalized text. Preserve original source separately; never apply normalized offsets to raw text. T8 converts code-point positions to JavaScript UTF-16 boundaries before highlighting. Shared fixtures test non-BMP emoji, combining marks, Vietnamese/CJK text, normalization changes and exact substring/fingerprint agreement across Python and web.
- Versioned `ArticleEnrichment` contract with controlled topics/content type/event relevance, canonical entities/locations, suggested tags, evidence, field confidence, model/prompt versions, and lifecycle state.
- Persist-first async flow: canonical article and lexical index available before enrichment; successful enrichment updates metadata/vector index without blocking raw ingest.
- Versioned local corpus with 30-100 normalized articles and 10 labeled questions.
- PostgreSQL + pgvector storage/index path: full-text search, cosine vector search, hybrid deterministic rank, index metadata, manual rebuild.
- Structured model protocol with OpenAI-compatible HTTP adapter and deterministic fake adapter for CI.
- Claim/evidence-span validator, allowlist/redaction, fail-closed typed errors, stable error codes, budgets and telemetry.
- FastAPI query use case/endpoint and React analyst query/result/citation/uncertainty/error states.
- TDD tests, deterministic eval, API integration, web tests, and one end-to-end analyst flow.
- G4 versioned noncanonical groups/topics with calibrated membership and primary-evidence fallback; G5 separate Postgres conversation store; G6 single-orchestrator read-only tools and paired evaluation.
### Must NOT have (guardrails, anti-slop, scope boundaries)
- Do not alter/delete existing dirty AI-worker WIP, `.omo` artifacts, or unrelated files.
- Do not use `FakeEventExtractor`, `InMemoryEventWriter`, or `event.v1` as RAG answer/persistence semantics.
- Do not add crawling, RSS, MinIO/Parquet ingestion, full RBAC/multi-tenancy, living-event automation, canonical event grouping/merge/create/write, Kafka/MCP/queues, fine-tuning, multi-agent orchestration, or production deployment. Noncanonical grouping and read-only tools belong only to G4 and G6, not baseline.
- Do not require network/provider secrets in CI; do not return raw model output after validation failure; do not send non-allowlisted fields to provider.
- Do not mutate canonical `Article`; do not write AI tags directly into `article.v1`; do not block article persistence or lexical search on enrichment success.

## Verification strategy
> Commands/tests are executable by implementers. Independent review and explicit policy approvals remain required; do not simulate unavailable reviewers.
- Test decision: TDD; pytest for Python contracts/retrieval/API, Bun test for web, deterministic eval command, mocked provider transport, no-network CI.
- Evidence: `.omo/evidence/` with one artifact per todo and final verifier; each artifact records exact command, exit code, and relevant assertion output.
- Global verification: `make check`; targeted commands are listed per todo. Every happy path has a failure-path test.
- Real database verification: T3 owns a pinned pgvector PostgreSQL test harness and `make rag-db-test`; the harness applies migrations to an isolated database and tests actual FTS/vector SQL, transactions, five-field uniqueness, lease fencing, atomic index publication and interrupted rebuild recovery. T9 wires the same target into CI with a healthy pgvector service and frozen install; G1-G6 run it as storage evolves, including grouping/conversation migrations at their gates. Provider/embedding ports stay deterministic fakes: real database testing never authorizes provider traffic. CI without this DB target passing cannot clear a storage gate.
- Browser verification: T8 owns an automated browser script against the local fake API; T9 runs it in CI with deterministic delayed responses. It checks A-slow/B-fast rapid resubmit, navigation/unmount before A returns, citation inspection, loading/empty/typed-error/retry states, and 320px plus desktop viewports. Preserve the manual citation check as additional QA, not the sole G3 browser gate; attach script result and viewport evidence.
- LLM eval split: enrichment eval measures field precision/recall, taxonomy validity, evidence support, abstention, and hallucination; answer eval remains separate.
- Claim-support gates are distinct: deterministic runtime validation rejects broken claim/citation linkage, mismatched article identity/hash/span and unallowlisted content; semantic support (whether cited text entails a claim) is measured separately against frozen labeled negation, numeric, date, conflict and paraphrase cases. Freeze semantic acceptance thresholds on train/dev before holdout; fake CI proves reproducible checks, not semantic truth or live-provider quality. A structurally valid citation must not be reported as verified factual support.
- Displayed-answer coverage: model output is a proposed set of claim blocks and uncertainty/abstention blocks, not independently trusted free-form prose. T1 defines a strict rendering contract and T6 builds the displayed `answer` deterministically from validated claim/uncertainty blocks only; reject or discard separate model-authored prose. T7/T8 must render only that validated result. Tests supply correctly cited claims plus an extra uncited sentence in proposed prose and prove the sentence never reaches API/UI. Mechanical coverage does not claim that a valid claim is semantically true; the separate claim-support eval remains mandatory.
- Source inspection (D37): T7 exposes an internal read-only article/span viewer endpoint addressed by an opaque source/span reference. It rechecks current caller visibility, tombstone/current content hash and span fingerprint at fetch time; stale or forbidden references return typed unavailable/denied, never raw cached content. T8 opens this authorized snapshot with cited span highlighted; external publisher URL is optional provenance/navigation, not the only citation target. API/web integration tests cover missing/changed external URL, corrected/deleted/revoked article and exact displayed text matching the cited span.
- Baseline acceptance covers T1-T9/G1-G3 only. Overall roadmap acceptance also requires T10-T15/G4-G6 plus repeated F1-F4. Fake CI proves control/safety, never real-model quality. Live quality evidence and policy approval are separate activation gates.
- The 10-question fixture set is a deterministic CI smoke/regression set, not a statistically credible p95 or live-quality sample. Before any live gate, freeze a separate representative held-out sample size/topic/time/access coverage and measurement protocol: hardware/provider/index versions, warmup, repeated runs, concurrency levels including the configured ceiling, per-stage latency, token/cost counts and uncertainty intervals. Report sample counts and interval alongside p95/quality; if the approved sample/protocol or actual runs are absent, report NOT MEASURED and keep live activation blocked. Do not use fake-provider latency or ten fixtures as production evidence.
- Human analyst time/value measurement (D42) is explicitly deferred. Technical fixture acceptance does not satisfy the approved design's human-workflow speed improvement criterion; report analyst time saved as NOT MEASURED, never infer it from API p95. A later user study must freeze the analyst task/manual baseline, participant/sample count, correctness rubric and timing protocol before making productivity claims.
- Commands and named files below are implementation targets, not claims that targets already exist. Record command, exit code, assertions, corpus/policy/version IDs, environment and scope in each evidence artifact; absent evidence means OPEN.

## Execution strategy
### Target file map

- Contracts: shared enrichment/evidence models under `contracts/python/src/eventlens_contracts/`; RAG-owned document/query/answer models under `apps/rag/src/eventlens_rag/domain/`. JSON Schemas are generated on demand from those models; no checked-in schema snapshots.
- Corpus/eval: `fixtures/rag/manifest.v1.json`, `fixtures/rag/articles/*.json`, `fixtures/rag/questions.v1.json`.
- RAG app: `apps/rag/pyproject.toml`; `apps/rag/src/eventlens_rag/{domain,application,ports,graph,adapters}/`; tests under `apps/rag/tests/`. LangGraph graph adapter remains framework-specific; domain/application/ports stay framework-free. Planned code modules stay under 250 pure code LOC; split by responsibility, not empty placeholders.
- Database: `apps/rag/migrations/001_enable_vector.sql`, `002_create_rag_index.sql`; update `compose.yaml` to pinned pgvector-enabled PostgreSQL image and healthcheck; add Make targets for migration/rebuild/eval.
- API: keep health paths; add `apps/api/src/eventlens_api/briefing.py` for use case, `briefing_http.py` for DTO/route, wire from `main.py`; API composition invokes the RAG briefing runner; tests `test_briefing_use_case.py`, `test_briefing_api.py`.
- Web: `apps/web/src/briefing.ts`, `BriefingView.tsx`, `BriefingView.test.tsx`; update `App.tsx`, `styles.css`; keep components under 250 pure LOC by extracting citation/error views only when needed.
- CI/docs: update `.github/workflows/ci.yml`, `Makefile`, `README.md`, `apps/worker/README.md` only where new commands/scope require it; never overwrite unrelated dirty hunks.
- Extended contracts: `contracts/python/src/eventlens_contracts/{grouping,conversation,tools}.py`; matching schemas and tests. Extended modules: `apps/rag/src/eventlens_rag/domain/{grouping,conversation,budgets}.py`, `application/{group_articles,discover_topics,conversation_turn,history,tool_search,tool_fetch,agent_eval}.py`, `ports/{groups,conversations,access,tools}.py`, `adapters/{postgres_groups,postgres_conversations}.py`, `graph/{tools,agent_nodes,agent_routes}.py`.
- Extended DB: `apps/rag/migrations/003_article_groups.sql`, `004_conversations.sql`; API `apps/api/src/eventlens_api/{groups_http,conversations_http}.py`; web `apps/web/src/{GroupView,ConversationView}.tsx` with matching tests. Fixtures: `fixtures/rag/{grouping,conversations,agentic}/`, split manifests and frozen policy files. CLI and Makefile own `grouping-eval`, `conversation-eval`, `agent-eval`, `conversation-retention` targets.

### Migration/source-reference mapping

Implementation target: `apps/worker` and `apps/rag`. Rename existing `apps/ai-worker` to `apps/worker` in G1 without behavior changes; keep Python distribution/import names `eventlens-worker`/`eventlens_worker`. Use reusable RAG app at `apps/rag`; keep names `eventlens-rag`/`eventlens_rag`. `contracts/python` is the relocated shared boundary package; its Python distribution/import names stay unchanged. These relocated directories are the planned implementation/test/CI paths; former package-directory paths are not active targets.

Dirty-WIP rename prerequisite (D41): before physical move, inventory every tracked/untracked pre-existing worker path and record content hashes plus baseline CLI/test outputs. Get affected owner approval for the move or an explicit clean checkpoint; do not auto-stash, delete, commit or push unrelated work. Without approval, keep `apps/ai-worker` as active source/compatibility path and mark G1 rename blocked rather than pretending `apps/worker` exists. After approval, verify per-file content hashes and worker validate/process behavior before/after path migration. This is a planning gate, not current authorization to rename.

### Parallel execution waves
> Waves follow actual dependencies. Shared contracts, migrations, graph builder, lockfile and CI each have one owner.

- Wave 0: T1 contracts; T2 corpus/eval after contract shapes are fixed.
- Wave 1: T3 reusable package/storage/index plus real StateGraph skeleton; T4 enrichment lifecycle after T1-T3.
- Wave 2: T5 retrieval and T6 answer synthesis proceed in parallel after enriched SearchDocument boundary exists.
- Wave 3: T7 API and worker composition, then T8 web; T9 evaluation/CI waits for all runtime paths.
- Baseline reviews: scoped F1-F4 after G1/T1-T3 and G2/T4-T7 before advancing; repeat after T1-T9/G3. All G3 reports must pass for baseline acceptance only. G1 QA covers worker/DB/graph, G2 covers API/retrieval/degraded/error paths; neither gate claims web coverage before T8.
- Wave 4: T10 grouping then T11 topic/group retrieval; repeat F1-F4 at G4.
- Extension lane G5: after G3 acceptance, T12 conversation storage/access then T13 multi-turn API/web; repeat scoped F1-F4 independently from G4.
- Extension lane G6: after G3 acceptance, T14 single-turn bounded graph tools then T15 paired eval/CI; repeat scoped F1-F4. Conversational tools additionally require G5 acceptance; group-aware tools additionally require G4 acceptance. Overall roadmap acceptance still requires all extension gates. Shared-file owners serialize edits even when capability dependencies permit parallel work.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| T1 Shared contracts | Dirty-worktree inventory + existing contracts | T2-T8 | none |
| T2 Corpus and eval fixtures | T1 | T3-T6, T9 | none |
| T3 RAG package + article/index storage + graph skeleton | T1, T2 | T4-T7, T9 | none |
| T4 Metadata enrichment + worker graph entrypoint | T1-T3 | T5-T7, T9 | none |
| T5 Retrieval | T3, T4 | T7, T9 | T6 |
| T6 Answer synthesis | T3, T4 | T7, T9 | T5 |
| T7 API + briefing graph composition | T5, T6 | T8, T9 | none |
| T8 Web analyst flow | T7 | T9 | none |
| T9 Evaluation/CI | T1-T8 | G3 review, T10 | none |
| T10 Grouping contracts/storage/calibration | G3 accepted | T11 | none |
| T11 Topics/group-aware retrieval | T10 | G4 review | T12 with disjoint owned files |
| T12 Conversation store/access | G3 accepted | T13 | G4 tasks with disjoint owned files |
| T13 Multi-turn API/web | T12 | G5 review; conversational T14 path only | independent G4/G6 files |
| T14 Bounded graph tools | G3 accepted; G5 for conversational path, G4 for group-aware path | T15 | G4/G5 tasks with disjoint owned files |
| T15 Paired agent eval/CI | T14 | G6 overall review | none |

### Gate command runbook (planned targets)

Run from repository root unless noted. Implement missing Make/CLI targets in their owning task; don't report a gate pass when command is missing. Test success isn't permission to commit or activate live mode.

Noninteractive gate harness (D40): own `make gate-g1` through `make gate-g6` as aggregate targets. Each starts only needed local dependencies with deterministic fixture/fake config, waits for health, applies pending migrations, runs scoped DB/pytest/web/browser/eval commands, captures exit codes and logs per gate, and tears down its own child processes in a guaranteed cleanup step. T3 owns shared infra/test harness and `gate-g1`; T7 owns `gate-g2` with API readiness/smoke; T8 owns the browser script; T9 owns `gate-g3` and CI wiring, not early gate prerequisites; T10-T15 own extension targets. Scoped G1/G2 F1-F4 depend only on their gate's tasks, never on T8/T9. A foreground `eventlens-api` followed by `curl` is illustrative manual smoke, not an executable one-shell acceptance command. No gate passes on placeholder commands or missing subtargets.

| Gate | Commands | Evidence |
|---|---|---|
| G1 | `uv sync --all-packages --group dev`; `uv run pytest contracts/python/tests apps/worker/tests apps/rag/tests/test_graph_state.py apps/rag/tests/test_graph_routes.py apps/rag/tests/test_architecture.py -q`; `make infra`; `make rag-db-create`; `make rag-migrate`; `make rag-load-fixtures`; `make rag-rebuild` (separate `eventlens_rag` DB); `uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json`; `uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json` | task-1/2/3 reports plus baseline worker output before and after rename |
| G2 | `uv run pytest apps/rag/tests/test_enrichment.py apps/rag/tests/test_enrichment_jobs.py apps/rag/tests/test_retrieval.py apps/rag/tests/test_synthesis.py apps/api/tests -q`; `uv run --package eventlens-api eventlens-api`; `curl --fail http://127.0.0.1:8000/health`; `curl --fail -H 'Content-Type: application/json' -d '{"question":"What do fixture articles report?"}' http://127.0.0.1:8000/briefing` | task-4/5/6/7 reports, fake-mode API payload and span checks |
| G3 | `make check`; `make enrichment-eval`; `make rag-eval`; from `apps/web`: `bun test`, `bun run check`, `bun run typecheck`, `bun run build`, `bun run dev`; local browser citation inspection | task-8/9 and `.omo/evidence/G3/final-*.md` |
| G4 | T10/T11 pytest/web commands; `make grouping-eval`; `make check`; baseline worker validate/process | task-10/11 and `.omo/evidence/G4/final-*.md` |
| G5 | T12/T13 pytest/web commands; `make conversation-eval`; `make conversation-retention`; `make check`; API restart/history/delete and baseline worker smoke | task-12/13 and `.omo/evidence/G5/final-*.md` |
| G6 | `make gate-g6`; T14/T15 pytest; `make check`; `make enrichment-eval`; `make rag-eval`; `make agent-eval`; baseline worker smoke; `make grouping-eval` only for group-aware path and `make conversation-eval` only for conversational path | task-14/15 and `.omo/evidence/G6/final-*.md`, explicit capability scope |

T3 owns `rag-migrate`/`rag-rebuild` and `apps/rag/src/eventlens_rag/cli.py`; T4 owns named enrichment tests above; T7 freezes strict request DTO and `/briefing` route used in smoke command. Invalid DTO must produce typed 4xx, never silently accepted extra fields. Browser QA is performed during implementation gates, not current plan edit.

### Data flow and enrichment state

```text
ArticleBundle
  -> validate + persist immutable ArticleDocument
  -> lexical index immediately
  -> create enrichment job [PENDING]
       -> claim job [PROCESSING]
       -> LLM structured metadata + evidence validation
           -> validated output -> stage ArticleEnrichment + SearchDocument metadata/vector under new version -> atomically publish READY + active index version
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

Idempotency key: `(article_id, content_hash, enrichment_version, model_version, prompt_version)`. Only one active job per key. Transport/timeout failures use bounded retry with backoff; schema/evidence failures become `FAILED` without blind retry. A new article hash or enrichment/model/prompt version supersedes prior output and marks it `STALE`.

Lease fencing: each successful claim increments a monotonic `claim_generation`. Completion, retry/failure writes and publication require a conditional DB write matching job ID, current generation and an unexpired lease using the database clock. Zero affected rows means lost ownership: discard late output and perform no READY/index publication. Do not hold a DB transaction open during a model call. T4 owns this policy; T3 provides atomic claim/conditional-write storage operations. A barrier-driven real-DB test expires worker A's lease, lets B reclaim, then releases A: A cannot change state or publish; B alone can complete. Also test expiry without reclaim, cancellation before commit and article/version supersession; lexical availability remains intact.

Cancellation is not a retryable provider failure: serialize cancellation acknowledgement against the fenced publication transaction, with a final cancellation check before entering that boundary. If cancellation wins, the invocation starts no further model/DB work, never marks READY or writes its own retry/FAILED state; durable lease recovery can reclaim the job after expiry. If publication commits first, it remains valid and later cancellation does not pretend to roll it back. T4 barrier tests both orders and assert no post-cancel invocation work; do not claim an already-committed transaction was undone.

Index publication: stage validated metadata, chunks and vectors under an inactive version. Once staging is complete, atomically mark the matching fenced enrichment job READY and switch the active article/index version in one short DB transaction. Retrieval reads only the active version, never staged rows; publication failure leaves the old active version and independently persisted lexical article available. Retrying an interrupted build is idempotent and cleans abandoned staged rows. T3 owns versioned staging/pointer storage; T4 owns the fenced publish transition; T5 reads the active pointer. Real-DB tests interrupt staging and publish, then assert no mixed READY/vector version, correct rollback and retry.

Embedding identity: each vector generation records embedding model identifier/version, vector dimension, chunker version and corpus/index generation. Query embeddings may only compare with the matching active embedding generation; matching dimension alone is insufficient. Changing the embedding model/version requires a separate full vector rebuild under an inactive generation, validation of row counts/dimensions and retrieval checks, then an atomic active-generation flip. During rebuild the existing active generation serves compatible queries or retrieval degrades explicitly to lexical; never mix vectors from different embedding models in one rank list. T3 owns generation metadata/rebuild, T5 enforces identity at query time, T9 evaluates before/after rebuild.

Embedding ownership: T3 defines the embedding port, deterministic fake embedding adapter, approved 1536-dimension baseline policy, startup dimension validation and versioned identity; T4 consumes this port to stage vectors. T6 owns chat/synthesis model and mocked HTTP transport, not a prerequisite embedding interface. T5 generates compatible query embeddings through the T3 port. A different embedding dimension requires an explicitly planned schema/index migration and approval, not just changing `EVENTLENS_EMBEDDING_MODEL` configuration.

Generation reuse (D36): immutable text/chunk embeddings have a key of article content hash, normalization version, chunker version, embedding model/version and dimension, separate from enrichment metadata model/prompt versions. A new metadata revision references an existing compatible complete embedding generation instead of regenerating identical vectors; absent/incompatible vectors are staged through T3's embedding port. Fenced atomic publication selects the consistent metadata-plus-embedding references together. Test prompt-only enrichment changes cause zero new embedding calls, while content/chunker/embedding changes create new generations; cleanup cannot remove a still-referenced active generation.

Current-content guard: an old active metadata/vector version may serve only while its article content hash matches the current authorized ArticleDocument and its enrichment remains eligible. On correction to a new hash, invalidate old enrichment/vector eligibility immediately; lexical retrieval uses the new persisted article while its replacement index is staged. Do not join old spans/metadata/vectors to new content or serve deleted articles. T3/T5 DB integration tests correct an article during an interrupted rebuild and assert only current lexical content is visible until matching READY publication; unchanged-hash rebuild failure may retain the compatible old active version.

Deletion visibility: T3 commits a synchronous tombstone/current-version invalidation before returning success for article deletion. Lexical/vector retrieval, grouping/topic aggregates and caches, saved citations, conversation history and evidence tools all check current tombstone state; no query may rely on asynchronous physical index cleanup for access enforcement. T10/T11 invalidate affected group/member views using the same current-state boundary. Rebuild/garbage collection follows asynchronously and is idempotent. Real-DB/API tests pause cleanup, delete an article, then assert immediate disappearance across scoped reads and no reappearance after stale worker replay.

Baseline access ownership: T1/T3 define immutable `CallerScope`/principal and versioned policy identity plus framework-free `ports/access.py` (`AccessPolicy`) with a single local fixture principal. T5 applies the policy to retrieval and T7 injects server-owned caller scope; T10/T11 reuse it for group/topic paths before G5 exists. Multiple fixture scopes are permitted in tests to prove isolation/revocation, never to enable shared runtime. T12 extends the existing boundary for conversation ownership instead of introducing access control for the first time. Loopback is a binding restriction, not authentication or authority to widen scope.

Fixture API binding (D34): T7 startup refuses any non-loopback bind address (including `0.0.0.0`/`::`), unapproved Host/forwarded-host or permissive CORS setting; Compose/container mappings must not bypass this check. Test allowed `127.0.0.1`/`::1`, public bind rejection, hostile Host and forwarded headers, and disallowed cross-origin request. Shared network binding remains blocked until a separate real authentication/authorization policy is approved; loopback alone is not a credential.

Authorization delivery boundary: use a final current-policy-epoch check immediately before a non-streaming API response is emitted; turn commit and final check must reject a policy change that committed before that boundary. Include the checked policy epoch in internal response/audit metadata. A revoke committed before the final check denies or re-evaluates cached and fresh claims; a revoke committed after that check cannot retroactively retract already authorized bytes, and the plan does not claim visibility remains current until client receipt. T12/T13 barrier tests exercise revocation before versus after this linearization point; do not stream partial answers.

Replay reauthorization: every returned cached claim must retain at least one current authorized, fingerprint-matching primary span and every displayed factual sentence must derive from currently validated claim blocks. If any material claim loses authorized support, deny the entire cached replay or start a new scoped retrieval/synthesis under current policy; never merely remove its citation while returning old answer text. Apply the same rule to saved history inserted into future model context and to tool evidence. Changed policy can turn an otherwise idempotent replay into a typed denial; it never bypasses current access.

## Todos
> Implementation + Test = ONE todo. Never separate.
- [x] 1. Add strict RAG and enrichment contracts
  Current status: T1 model/test acceptance complete for Python contracts; current-path checks and remaining gate boundaries are recorded in `.omo/evidence/task-1-eventlens-ai-rag-mvp.md`. This does not clear G1, cross-language highlighting in T8, semantic-support evaluation in T9, or live activation.
  What to do / Must NOT do: Keep shared `ArticleEnrichment`, `EnrichmentStatus`, `TopicTag`, `EnrichedEntity`, `EnrichedLocation`, and `EvidenceSpan` under `contracts/python`; keep RAG-owned `ArticleDocument`, `BriefingQuery`, `BriefingAnswer`, `Claim`, `Citation`, and `Uncertainty` under `apps/rag`. Controlled values cover `topics`, `content_type`, and `event_relevance`; canonical entity/location structures carry evidence and field confidence; `suggested_tags` captures out-of-taxonomy discovery but is excluded from primary filters. Models include model/prompt/enrichment versions, content hash, enriched_at, attempt count, failure code, and lifecycle invariants. Generate JSON Schemas on demand; retain semantic vectors. Do not mutate `Article`, write enrichment into `article.v1`, or put RAG claims into `event.v1`.
  Parallelization: Wave 0 | Blocked by: existing contract conventions | Blocks: 3, 4, 5, 6, 7
  References: `contracts/python/src/eventlens_contracts/{base,enrichment,evidence,taxonomy,schema}.py`; `apps/rag/src/eventlens_rag/domain/{article_document,answer,query}.py`; `apps/rag/src/eventlens_rag/schema.py`; `contracts/python/tests/{test_enrichment_models,test_enrichment_edges,test_schema,test_semantic_vectors}.py`; `apps/rag/tests/{test_rag_models,test_rag_answer_edges,test_rag_answer_linkage,test_rag_schema,test_rag_semantic_vectors,test_rag_t1_contracts}.py`; `docs/designs/ai-rag-briefing-mvp.md`.
  Acceptance criteria (agent-executable): RAG and enrichment contract tests pass; tests prove valid claim linkage, enrichment lifecycle combinations, controlled taxonomy rejection, suggestions acceptance, evidence/confidence bounds, content-hash/version identity, duplicate IDs, UTC, frozen/extra-forbid behavior.
  QA scenarios: `uv run pytest` on the ten T1 suites above validates valid answer payload against generated Draft 2020-12 JSON Schema plus isolated invalid cases; `uv run basedpyright` checks types. Evidence `.omo/evidence/task-1-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(contracts): add grounded briefing contracts`

- [x] 2. Create versioned corpus and labeled enrichment/RAG evaluation fixtures
  What to do / Must NOT do: Add `fixtures/rag/` with 30-100 normalized ArticleDocument cases, expected ArticleEnrichment outputs, and 10 fixed analyst questions. Label controlled topics, suggested tags, entities, locations, content type, event relevance, evidence spans, confidence, abstention, duplicate/follow-up/conflict/delayed publication/relative date/restricted-field/prompt-injection cases. Record provenance/license and expected IDs. Do not ingest unapproved external content or change existing fixtures.
  Parallelization: Wave 0 | Blocked by: T1 contracts | Blocks: 3, 4, 5, 8
  References (executor has NO interview context - be exhaustive): `docs/designs/ai-rag-briefing-mvp.md:138-147,309-326`; `fixtures/article_bundle.v1.json`; `contracts/python/tests/test_models.py`; `contracts/python/tests/test_semantic_vectors.py`; `.omo/drafts/eventlens-ai-rag-mvp.md:41-47`.
  Acceptance criteria (agent-executable): `uv run pytest apps/rag/tests/test_rag_fixtures.py apps/rag/tests/test_rag_fixture_linkage.py -q` passes; fixture manifest has fixed version/count, 10 questions, expected source/claim/span IDs, and every required edge; no pre-existing dirty path is modified.
  QA scenarios (name the exact tool + invocation): happy validate complete corpus; failure malformed fixture, missing expected source file or reference, or missing provenance/license fails. Evidence `.omo/evidence/task-2-eventlens-ai-rag-mvp.md`. T2 structural fixture acceptance complete; semantic entailment remains T9, DB gate remains G1.
  Proposed commit (only if separately authorized): `test(fixtures): add labeled RAG corpus and evaluation cases`

- [x] 3. Create reusable RAG package, article/enrichment storage, PostgreSQL + pgvector index, and baseline graph foundation
  Current status (2026-10-16): T3 technical checks PASS on fresh real pgvector (`make rag-db-test` 129 passed/0 skipped; `make gate-g1` technical PASS, exit 2 = acceptance OPEN); worker active path is `apps/worker` (move technically verified; historical D41 approval for the 2026-10-03 move unverified; user keeps current path as of 2026-10-16). Legacy Compose `eventlens` schema is not upgraded in place (typed fail-closed); local index uses separate DB `eventlens_rag`. F1-F4 for G1 exist only as self-reviews; G1 OPEN. Evidence `.omo/evidence/task-3-g1-selfcheck-2026-10-16.md`. Earlier status lines in task-3 evidence keep their historical labels. Update (2026-10-16, later): independent re-review PASS (`.omo/evidence/G1/rereview2-20261016-codex/review.md`); owner accepted the D41 evidence gap and accepted T3/G1 scoped to T1-T3 fixture/fake with separate DB `eventlens_rag`; see `.omo/evidence/G1/acceptance-decision-2026-10-16.md`. The sentences above saying "F1-F4 ... only as self-reviews; G1 OPEN" are superseded.
  What to do / Must NOT do: Rename worker path to `apps/worker` without changing current behavior, updating workspace, Ruff/pytest, Makefile, CI, container/compose, and current docs paths while preserving CLI/import names. Add `apps/rag` as uv workspace member with framework-free domain/application/ports, adapters/CLI, and `graph/{state,context,nodes,routes,graph,runner}.py`. Add LangGraph as an `eventlens-rag` compatible 1.x dependency with exact resolved `uv.lock` version verified for Python 3.12 and strict typing. Compile real `StateGraph` definitions with scripted fake ports, isolated invocation state/context, no production checkpointer, and no automatic tools. Add PostgreSQL tables for immutable article documents, enrichment jobs/versions, chunks, vectors, and index metadata. Persist ArticleDocument first and build lexical FTS immediately. Add pgvector `vector(1536)` + HNSW cosine index, manual rebuild, and transaction-safe job claim/idempotency constraints. Store `(article_id, content_hash, enrichment_version, model_version, prompt_version)` uniqueness; chunk 1,200 Unicode code points with 200 overlap. Correction/deletion means rebuild. CI uses fake embeddings. Do not require MinIO/Parquet, persistent graph checkpoints, or block article persistence on enrichment.
  Parallelization: Wave 1 | Blocked by: T1/T2 | Blocks: 4, 5, 6, 8
  References (executor has NO interview context - be exhaustive): `compose.yaml:1-59`; `README.md:86-96`; `docs/designs/ai-rag-briefing-mvp.md:75-108,254-270,294-307`; pgvector hybrid/SQL guidance from official pgvector docs; `apps/api/pyproject.toml:7-14`; `pyproject.toml:9-16`.
  Acceptance criteria (agent-executable): `uv sync --all-packages --group dev` includes `eventlens-rag`; API and worker can import `eventlens_rag` while neither app imports the other; existing worker regression suite passes under its renamed path; `uv run pytest apps/rag/tests/test_graph_state.py apps/rag/tests/test_graph_routes.py apps/rag/tests/test_architecture.py -q` proves real StateGraph compile/invoke, runtime validation, deterministic routes, state isolation, no framework imports in domain/application/ports, and no checkpointer; local `docker compose --profile infra up -d postgres` plus migration/rebuild command creates FTS and pgvector structures; indexed rows expose corpus/index versions, indexed_at, content hash and offsets; deterministic fake embeddings need no network/provider secret.
  QA scenarios (name the exact tool + invocation): happy run `make check`, existing worker validate/process fixture commands, `make infra`, graph fake-port invocation, rebuild command, indexed fixture query; failure malformed state, forbidden graph dependency, stop DB/index and assert typed unavailable error, change content hash and assert stale/rebuild requirement, duplicate document and assert suppression. Evidence `.omo/evidence/task-3-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(search): add versioned pgvector corpus index`

- [x] 4. Implement asynchronous metadata enrichment lifecycle and worker graph entrypoint
  Current status (2026-10-16): T4 implementation and independent re-review complete on `feature/rag-enrichment-t4` (baseline b1a3900), scoped to fixtures/fake providers. Re-review APPROVE, R1-R4 CLOSED (`.omo/evidence/T4-rereview-20261016-codex/review.md`); independent `make check` 499 passed/56 DB-dependent skipped, `make rag-db-test` 398 passed/0 skipped on real pgvector, 12/12 review regressions and an additional in-flight cancellation probe PASS. The owner authorized committing T4 and preparing T5 with "thực hiện commit rồi viết yêu cầu thực hiện T5"; see `.omo/evidence/T4/commit-check-20261016.md`. Earlier self-check/re-review-pending status is historical. G2 remains OPEN and still needs T5-T7; this checkpoint does not approve live providers, deployment or a legacy DB upgrade. Migration `006_enrichment_lifecycle.sql` added (001-005 unchanged). Full implementation evidence: `.omo/evidence/task-4-eventlens-ai-rag-mvp.md`.
  What to do / Must NOT do: Inside `apps/rag`, add `ArticleEnricher` and `EnrichmentRepository` ports, fake and mocked-transport OpenAI-compatible structured enrichment adapters, worker/CLI orchestration, allowlisted prompt context, controlled taxonomy + suggestions, evidence/confidence validation, state transitions, bounded retry/backoff, idempotency, stale/supersede handling, telemetry, and staging of SearchDocument metadata/vectors before the fenced atomic READY + active-index publication. Wire injected application services into real enrichment StateGraph from `apps/worker` composition root. DB job orchestration alone owns retry/attempt budget and lease recovery; graph has no persistent checkpoint or independent retry loop. Transport/timeout retry; schema/evidence failure becomes FAILED. Keep ArticleDocument and lexical index available on failure. Do not silently reuse enrichment across content hash/model/prompt/version changes, enable real provider traffic, or route RAG answers through `event.v1`.
  Parallelization: Wave 1 | Blocked by: T1/T2/T3 | Blocks: T5/T6/T7/T9
  References (executor has NO interview context - be exhaustive): enrichment contracts from T1; `apps/rag` storage/graph foundation from T3; existing worker ports/runner as source patterns only; user-approved persist-first async and controlled+suggestions decisions.
  Acceptance criteria (agent-executable): tests cover every legal/illegal state transition, duplicate delivery and concurrent claim on the five-field key, retry exhaustion, stale article hash, independent model-version or prompt-version changes with unchanged enrichment_version creating distinct jobs, malformed output, unsupported taxonomy, ungrounded metadata, prompt injection, redaction, FAILED lexical fallback, staged metadata/vector build before fenced atomic READY/index publication, telemetry without article body, real enrichment graph invocation, graph failure mapping, cancellation, and state isolation.
  QA scenarios (name the exact tool + invocation): happy persist article -> lexical search -> stage enrichment/index -> atomic READY publish -> metadata/vector search; failure model timeout -> retry/FAILED while lexical result remains; stale version creates new PENDING without overwriting history. Evidence `.omo/evidence/task-4-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(ai): add versioned article metadata enrichment`

- [ ] 5. Implement hybrid retrieval and deterministic evidence assembly
  What to do / Must NOT do: Inside `apps/rag`, implement repository/index ports and services for PostgreSQL lexical FTS + pgvector cosine retrieval, reciprocal-rank fusion with fixed `k=60`, publication-time filters, source/entity metadata filters, freshness, dedup, deterministic tie-break `(fused_score DESC, source_published_at DESC NULLS LAST, article_id ASC)`, max 20 articles, and max 12,000 context tokens. Return evidence spans with index version and query time basis `source_published_at`; measure lexical, semantic, combined Recall@k independently. Do not call model or create/update events here.
  Parallelization: Wave 2 | Blocked by: T1-T4 | Blocks: T7, T9 | Can parallelize with: T6
  References (executor has NO interview context - be exhaustive): existing worker ports/runner as source patterns only; `contracts/python/src/eventlens_contracts/article.py:61-85`; `docs/designs/ai-rag-briefing-mvp.md:75-108,149-157,309-326`.
  Acceptance criteria (agent-executable): `uv run pytest apps/rag/tests/test_retrieval.py apps/rag/tests/test_evidence_context.py -q` proves lexical/semantic/hybrid ranking, exact fusion/tie order, publication boundary rules, null publication handling, dedup, zero hits, stale index, max article/token budgets, and stage metrics; retrieval never imports worker/event modules or emits event writes.
  QA scenarios (name the exact tool + invocation): happy query known topic and filter by READY enrichment; failure enrichment pending/failed/stale falls back to lexical without pretending metadata exists; other retrieval errors remain typed. Evidence `.omo/evidence/task-5-eventlens-ai-rag-mvp.md`.
  Filter semantics: keep lexical ArticleDocument retrieval separate from optional READY metadata/vector joins. Both branches enforce current visibility plus source/publication filters. Queries without enrichment-dependent filters retain PENDING/FAILED/STALE lexical hits. Explicit topic/entity filters require supported READY metadata; never silently bypass the requested filter or claim unknown metadata matches. Report incomplete/filter-unavailable coverage explicitly and allow a separately requested unfiltered lexical retry. Tests compare the same unfiltered query before/after READY and on enrichment failure, plus explicit metadata-filter queries with absent metadata and no false matches.
  Filtered vector search: benchmark actual pgvector Recall@k and query plans against exact search across source/time/visibility filter selectivities. For small or highly selective authorized sets, use filtered exact cosine ranking; bounded iterative ANN scanning is an alternative only after supported pgvector-version verification and measured recall. Never widen visibility/time/source filters to fill k. T5 owns routing thresholds and candidate/scan caps; T9 records the frozen policy, EXPLAIN plans, query counts, recall and latency on the same corpus. Exact/ANN routes both keep deterministic tie-breaks and token/article limits.
  Ranking policy (D35): T5 freezes a versioned fixture policy before scoring/eval: per-branch lexical/vector candidate depths, missing-branch RRF treatment and k=60 formula, metadata/filter order, freshness decay function and query clock, near-duplicate fingerprint/similarity threshold and representative selection, plus deterministic tie-breaking at branch, dedup and fused stages. T9 records policy ID and per-question results; a policy revision invalidates previous acceptance evidence. Threshold choices are configured and measured on train/dev data, not reported as achieved scores before tests run.
  Proposed commit (only if separately authorized): `feat(search): add deterministic hybrid retrieval`

- [ ] 6. Add structured OpenAI-compatible answer synthesis, redaction, and grounding validator
  Provider activation boundary (D31): T6 defines a versioned, reviewed policy artifact containing policy ID/issuer, exact HTTPS endpoint allowlist and TLS validation, approved chat/embedding models and tokenizer identities, corpus/data classification and allowlisted fields, token/cost caps, and explicit `fixture` versus `live` mode. At startup, fail closed on missing/unapproved policy, URL/model mismatch, insecure endpoint or corpus classification mismatch; API key presence alone never enables live traffic. Mock transport tests exercise rejected configurations and verify no outbound request, while real activation remains separately approved and measured.
  Token accounting (D32): T6 owns a model-versioned token-counting port/policy. Count the fully serialized provider input (instructions, evidence, history, tool schemas/arguments and retry prompts) before each call; reserve final output capacity and include structured-output/tool responses and retries in the shared cumulative ledger. Compare provider-reported usage to local counts in measured runs and record drift. Unknown tokenizer mappings fail closed for live mode unless a conservative estimator with an explicitly approved safety margin is frozen in the activation policy. Unit tests cover exact/one-over limits, multilingual text, tool arguments and retry accumulation; Unicode code-point chunk length is not treated as token count.
  What to do / Must NOT do: Inside `apps/rag`, add chat/synthesis model protocol and deterministic fake adapter; reuse T3-owned embedding port/fake and add mocked HTTP embedding transport against that existing port if needed. Mocked OpenAI-compatible HTTP adapters use `EVENTLENS_OPENAI_BASE_URL`, `EVENTLENS_OPENAI_API_KEY`, `EVENTLENS_CHAT_MODEL`, and `EVENTLENS_EMBEDDING_MODEL`, constrained by the activation policy; add allowlisted context builder, structured output parser, claim/span citation validator, abstention, typed errors and telemetry. Allowed provider fields: article ID, title, normalized content spans, source name/domain, source publication time. Explicitly prohibit raw storage URI, internal processing metadata, credentials, and non-allowlisted fields. Enforce max 1,500 output tokens, p95 instrumentation, 30 concurrency guard, and fail closed for timeout/provider error/malformed output/mechanically invalid claim linkage/unknown span/fingerprint mismatch. Do not return raw model output after validation failure or enable a real provider without D4 activation approval.
  Parallelization: Wave 2 | Blocked by: T1-T4; uses fake CI model | Blocks: T7, T9 | Can parallelize with: T5
  References (executor has NO interview context - be exhaustive): existing worker models/ports as source patterns only; `docs/designs/ai-rag-briefing-mvp.md:92-108,159-167,223-234,282-326`; `contracts/python/src/eventlens_contracts/base.py:41-46`.
  Acceptance criteria (agent-executable): `uv run pytest apps/rag/tests/test_synthesis.py apps/rag/tests/test_citations.py apps/rag/tests/test_redaction.py apps/rag/tests/test_openai_adapter.py -q` proves valid answer, abstention, mechanical claim/span linkage and offset/fingerprint rejection, prompt injection containment, provider timeout/error, malformed JSON, budget/concurrency failure, telemetry without restricted content, no raw fallback, exact HTTP mapping, default fake selection, and live-mode rejection without an approved policy. Semantic unsupported-claim detection is an independently labeled evaluation gate, not a guarantee provided by structural citation validation.
  QA scenarios (name the exact tool + invocation): happy fake model answer uses content plus READY metadata; failure answer validator remains independent from enrichment failure and never trusts suggested tags as facts. Evidence `.omo/evidence/task-6-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(ai): add grounded synthesis and citation validation`

- [ ] 7. Expose briefing query use case and FastAPI endpoint through compiled graph runner
  What to do / Must NOT do: Add `eventlens-rag` as API workspace dependency. Extend framework-free API application layer with dependency-injected graph runner, repository/retriever/synthesizer, add strict query/answer HTTP DTOs, stable error mapping, internal-only allowlist boundary, publication-time basis metadata, and composition-root wiring. API invokes real compiled briefing StateGraph; map graph output/failures at validated boundary and never expose framework types. Keep route thin. Add query size/budget/concurrency validation. Do not put SQL, provider calls, graph construction, or event mutation in `http.py`; do not import worker app; do not require auth beyond explicit loopback-only development/internal-only boundary marker for MVP.
  Parallelization: Wave 3 | Blocked by: T5/T6 | Blocks: T8/T9
  References (executor has NO interview context - be exhaustive): `apps/api/src/eventlens_api/application.py:1-16`; `apps/api/src/eventlens_api/http.py:1-25`; `apps/api/src/eventlens_api/main.py:1-21`; `apps/api/tests/test_health.py`; `apps/api/tests/test_health_use_case.py`; `apps/api/tests/conftest.py`; `docs/designs/ai-rag-briefing-mvp.md:169-177,282-307`.
  Acceptance criteria (agent-executable): `uv run pytest apps/api/tests/test_briefing_api.py apps/api/tests/test_briefing_use_case.py -q` proves valid query, empty/oversized query, no evidence, retrieval unavailable/stale, model unavailable, invalid answer, budget exceeded, stable error payloads, source-published time basis, and health regression; `httpx` ASGI integration exercises full fake path through compiled briefing graph.
  QA scenarios (name the exact tool + invocation): happy API returns cited answer and enrichment freshness metadata; failure pending/failed enrichment still answers via lexical content when evidence suffices. Evidence `.omo/evidence/task-7-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(api): add grounded briefing query endpoint`

- [ ] 8. Build analyst web query, citation, uncertainty, and failure-state flow
  What to do / Must NOT do: Replace static disconnected foundation state with a focused analyst query/result experience using existing editorial tokens. Add Zod strict response parsing, query client, answer/claims/citations/uncertainty rendering, source inspection, time-basis label, loading, empty, no-evidence, partial, model error, validation error, and retry states. Do not add dashboard, event browser, auth UI, or unrelated redesign.
  Parallelization: Wave 3 | Blocked by: T7 API contract | Blocks: T9
  References (executor has NO interview context - be exhaustive): `apps/web/src/App.tsx:1-81`; `apps/web/src/foundation.ts:1-15`; `apps/web/src/foundation.test.ts:1-38`; `apps/web/src/tokens.css`; `apps/web/src/styles.css`; `apps/web/package.json:6-25`; `apps/web/DESIGN.md:9-98`.
  Acceptance criteria (agent-executable): `cd apps/web && bun test && bun run check && bun run typecheck && bun run build` passes; tests prove strict response parsing, cited claim rendering, source opening/inspection, uncertainty/time-basis display, empty/no-evidence/loading/partial/error/retry states at 320px and desktop widths; API client never renders unsupported answer as valid.
  QA scenarios (name the exact tool + invocation): happy inspect citations plus enrichment freshness; failure stale/pending metadata displays honest degraded state, not fabricated tags. Evidence `.omo/evidence/task-8-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(web): add analyst briefing experience`

- [ ] 9. Add deterministic enrichment/RAG eval, end-to-end verification, telemetry gates, and CI wiring
  What to do / Must NOT do: Add separate enrichment and answer eval targets. Enrichment report measures controlled-topic/entity/location/content-type/event-relevance precision/recall, taxonomy validity, evidence support, confidence calibration, abstention, hallucination, stale/retry behavior, latency/token/cost. RAG report keeps retrieval/citation/claim/abstention/conflict/temporal/security metrics. Add persist -> lexical -> enrich -> reindex -> query E2E through real compiled graph runners with fake adapters, no network/provider secrets. Compare lexical and hybrid modes with same graph, corpus/questions, scripted responses, and budgets; report graph overhead separately.
  Parallelization: Wave 3 | Blocked by: T1-T8 | Blocks: G3 F1-F4 only; G1/G2 reviews do not depend on T9
  References (executor has NO interview context - be exhaustive): `Makefile:1-35`; `.github/workflows/ci.yml:19-62`; `docs/designs/ai-rag-briefing-mvp.md:206-234,309-326`; `pyproject.toml:54-62`; `apps/web/package.json:7-11`.
  Acceptance criteria (agent-executable): `make check`, `make enrichment-eval`, and `make rag-eval` pass with no network; reports include corpus, index/model/prompt/enrichment versions, enrichment field metrics, evidence support, Recall@k, citation metrics, abstention/conflict/security results, p95 latency, token counts, cost estimate, concurrency, freshness, and graph overhead; CI runs same commands from clean frozen install. Tests use real StateGraph with deterministic fake retriever/provider/clock and assert default fake selection, no provider network calls in CI, cancellation/budget stop-work behavior, and no production checkpointer.
  QA scenarios (name the exact tool + invocation): happy clean install + `make check && make enrichment-eval && make rag-eval`; failure poisoned metadata, unsupported tag, missing evidence, stale enrichment or model outage keeps lexical search available while DB is healthy. DB outage returns typed unavailable without data loss; after DB recovery, current lexical search works again. Evidence `.omo/evidence/task-9-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `test(ci): add deterministic RAG evaluation gates`

- [ ] 10. Define and persist calibrated noncanonical article/story groups
  G4 data prerequisite (D28): T10 owns a versioned grouping dataset separate from the 10-question RAG smoke set. Before threshold tuning, freeze a labeling protocol for same-story, unrelated, ambiguous and difficult near-miss article pairs; source/time diversity, adjudication, split isolation and minimum counts of groups, positive/negative pairs and singleton cases must be specified in the G4 policy artifact. Freeze untouched holdout and thresholds before measurement; report denominators and uncertainty alongside false merges/splits and B-cubed metrics. If data are insufficient, exercise storage/mechanics only and leave automatic grouping quality gate OPEN rather than claiming calibrated approval.
  G4 execution path (D29): T10 adds a bounded manual `eventlens-worker group-articles` CLI accepting explicit corpus/index and frozen grouping-policy versions. It runs the same group application service against persisted current articles, caps candidates, uses an idempotency key covering article content hash and grouping policy, and reuses DB lease/fencing semantics so stale runners cannot publish membership. Correction/deletion invalidates current membership synchronously; manual rerun recomputes affected groups, with auditable failure/retry and no forced match on ambiguity. Test replay, interruption, concurrent runner, stale input and recovery against real DB. Automatic scheduling remains disabled, and G4 acceptance exercises this CLI path rather than only offline eval.
  What to do / Must NOT do: Add strict `ArticleGroup`, `GroupMember`, `GroupCandidate`, `GroupDecision` contracts in `eventlens_contracts/grouping.py`; `domain/grouping.py`, `application/group_articles.py`, `ports/groups.py`, `adapters/postgres_groups.py`, and `003_article_groups.sql`. Dedup means same article/content identity; topic is a thematic label; story group is a related-coverage cluster; canonical event is a separate authoritative record. Never conflate these IDs or write `event.v1`. Store stable group ID, group version, per-member article ID/content hash, evidence span IDs/offsets/fingerprints, algorithm/taxonomy version and decision provenance. Cap candidate retrieval per article; uncertain matches remain singleton or review queue, not forced merges. Transactionally replace membership on correction/deletion, close empty groups, keep auditable version history, invalidate affected indexes. Calibrate merge/reject/review thresholds on labeled train/dev data; freeze thresholds and untouched holdout before enabling auto-grouping. No model-generated summary may replace article spans as primary evidence.
  Parallelization: Wave 4 | Blocked by: G3 accepted | Blocks: T11. One owner for group migration and contracts.
  References: T1 ArticleDocument/EvidenceSpan and T3 storage, `apps/rag/src/eventlens_rag/application/`, `apps/rag/migrations/`, `fixtures/rag/manifest.v1.json`, `docs/designs/ai-rag-briefing-mvp.md` (citation boundaries). Targets above are planned, not present.
  Acceptance criteria: `uv run pytest contracts/python/tests/test_grouping.py apps/rag/tests/test_group_storage.py apps/rag/tests/test_group_calibration.py -q` tests ID/hash/span provenance, dedup vs topic/story/event distinctions, cap and thresholds, deterministic singleton/review, correction/deletion atomicity, concurrent membership updates, holdout immutability, and no canonical writes. `make grouping-eval` reports false merges, false splits, B-cubed precision/recall/F1 and singleton/review coverage on versioned labeled splits. Thresholds and acceptable values must be written/frozen before holdout measurement; no invented achieved scores.
  QA scenarios: insert known related and unrelated articles, inspect persisted version/member spans using `make infra` and `make grouping-eval`; simulate ambiguous candidate, deletion, transaction failure and replay; verify old memberships cannot leak into retrieval. Evidence `.omo/evidence/task-10-eventlens-ai-rag-mvp.md` with split/policy versions.
  Proposed commit (only if separately authorized): `feat(grouping): add versioned article groups`

- [ ] 11. Expose topic discovery and group-aware retrieval without losing primary evidence
  Group identity boundary (D27, superseding earlier global-public choice): keep stable global group ID and membership version for internal audit only. API/web expose caller-scope-derived opaque group ID and public version computed from currently authorized membership and policy identity; hidden-only member changes must not alter a caller-visible ID/version, count or cache entry. A visible-membership change must update that caller's public version. Do not leak internal group IDs, global versions, hidden lineage, or hidden-only changes through routes, responses or cache keys. Test two scopes, hidden-only updates, visible-member updates, revocation, and replay of stale public IDs.
  What to do / Must NOT do: Add `application/discover_topics.py` and group-aware path in `application/retrieve.py`; `apps/api/src/eventlens_api/groups_http.py`; `apps/web/src/GroupView.tsx` plus tests. Controlled taxonomy topics remain article metadata; topic discovery aggregates evidence-backed articles and may show suggested tags separately, never promote suggestions silently. Expose story group identity/version/member provenance and review/singleton status, never assert an authoritative event or group-level truth. Filter by current caller visibility before candidate selection, topic/group aggregation, counts, membership display and navigation; never leak forbidden article existence through group IDs, counts, suggested tags or summaries. Batch authorized membership/evidence reads; index `(article_id, content_hash)` and versioned membership lookup, cap candidate rows before scoring, invalidate cached group/topic views on article correction/deletion, membership or taxonomy version change and access revocation. Any cache key includes caller scope and access-policy/version identity; do not serve cached aggregates without current access validation. Ranking may diversify group repeats, but ungrouped lexical/hybrid retrieval remains available. Citations always resolve to current authorized article spans; group summary is navigation only.
  Parallelization: Wave 4 | Blocked by: T10 | Blocks: G4 review. API/web can follow frozen DTOs, not edit same files concurrently.
  References: T5 retrieval, T6 citation validator, T7 briefing API, T8 web and T10 group contracts/storage; `apps/rag/tests/test_retrieval.py`, `apps/api/tests/test_briefing_api.py`, `apps/web/src/BriefingView.tsx`.
  Acceptance criteria: `uv run pytest apps/rag/tests/test_group_retrieval.py apps/api/tests/test_groups_api.py -q`; `cd apps/web && bun test && bun run typecheck && bun run build`; `make grouping-eval` prove group filters, caller-scoped topic counts and group membership with filtering before aggregation, no forbidden ID/count/tag/summary leak across two visibility scopes, cache isolation and revocation invalidation, correction invalidation, ungrouped fallback, no stale member leak, permission-filtered primary spans, and no citation to summary-only text. Group false-merge/B-cubed and retrieval Recall@k reports stay separate.
  QA scenarios: navigate topic -> group -> member -> cited span against local fixture API/web; remove member and confirm cached view and citations refresh; empty grouping DB still returns baseline article answers. Evidence `.omo/evidence/task-11-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(search): add topic and group views`

- [ ] 12. Add durable conversation store and per-turn access policy
  Retention boundary (D30): local fixture history expires at read/context/replay time after the 30-day development default; `make conversation-retention` performs manual physical cleanup, not an enforced deletion SLA. Do not claim physical deletion by day 30 without an operating scheduler. Shared/live activation requires a separately approved scheduler, hard-delete deadline, retry/alerting, and tested purge/redaction of derived caches and logs. Tests advance the injected clock without running cleanup and prove expired history cannot be read, replayed or inserted into model context; manual cleanup then removes the stored rows.
  What to do / Must NOT do: Add strict conversation/turn/revision/idempotency models in `eventlens_contracts/conversation.py`; `domain/conversation.py`, `ports/{conversations,access}.py`, `adapters/postgres_conversations.py`, `application/history.py`, and `004_conversations.sql`. Postgres conversation/turn store is separate from LangGraph checkpoint; no graph resume across process restart. Store principal/visibility scope, turn key, expected_revision, question, validated answer/citation references and time basis. Check ownership and current article visibility on every turn, history read, context assembly, evidence fetch, immediately before turn commit and before response delivery, not only conversation creation. If visibility changes during synthesis, reject or re-evaluate before commit; never persist/return a now-forbidden citation. Fixture mode permits one local principal bound to loopback only; shared use stays disabled until explicit authentication/authorization policy approved. Default development retention: 30 days; scheduled/manual `make conversation-retention` expires turns and supports delete/redact of conversation and derived history; redact restricted content and logs. Apply optimistic revision compare-and-swap and unique idempotent turn key in one DB transaction. Budget at most last 8 turns and 2,000 history tokens inside existing 12,000 total input budget, not added to it.
  Parallelization: G5 extension lane | Blocked by: G3 accepted | Blocks: T13. Single owner for conversation migration; reuse baseline access port.
  References: T3 Postgres adapter, T6 redaction and token counting, T7 API boundary, D2/D4 graph/checkpoint policy. Target test files: `contracts/python/tests/test_conversation.py`, `apps/rag/tests/{test_conversation_store,test_conversation_access,test_history_budget}.py`.
  Acceptance criteria: `uv run pytest contracts/python/tests/test_conversation.py apps/rag/tests/test_conversation_store.py apps/rag/tests/test_conversation_access.py apps/rag/tests/test_history_budget.py -q` covers restart persistence without resume, duplicate turn replay only while current caller/access remains valid, denial or redaction after revoked visibility, competing expected_revision conflict, owner switch, permission change between fetch and commit/response, per-fetch filter, expired/redacted/deleted history, exact 8-turn/2,000-token bounds and 12k total. A barrier-driven DB/API test fetches a saved replay, revokes visibility before delivery, then asserts denial/redaction with no old citation; stale expected_revision plus duplicate idempotency key cannot partially write or bypass current access. Reuse the same current-scope guard for G6 evidence fetch. API cannot enable multi-principal traffic without approved auth policy.
  QA scenarios: `make infra`, save turn then restart API and read saved history; duplicate turn key returns same result only with unchanged access, revoked access denies or filters replay and never exposes old citations; stale revision conflicts without partial write; delete/redact and run `make conversation-retention` to confirm no restored context. Evidence `.omo/evidence/task-12-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(conversation): add scoped durable turn store`

- [ ] 13. Run grounded multi-turn briefing through API and web
  What to do / Must NOT do: Add `application/conversation_turn.py`, thin `apps/api/src/eventlens_api/conversations_http.py`, and `apps/web/src/ConversationView.tsx` with tests. Every turn re-retrieves fresh evidence under current caller scope; preceding answers are dialogue context, never independent factual evidence. Resolve follow-up references only when unambiguous; otherwise return clarification without inventing referent. Expose query clock and publication-time basis explicitly; historical relative dates use current turn clock, not prior response clock. Enforce T12 history cap alongside citations/input/output budget and revalidate visibility before returning cached idempotent response. Preserve baseline single-shot endpoint and worker CLI behavior.
  Parallelization: Wave 5 | Blocked by: T12 | Blocks: G5 review.
  References: T5 retrieval, T6 validation, T7 API graph, T8 web, T11 group navigation, T12 access/store; target `apps/rag/tests/test_conversation_turn.py`, `apps/api/tests/test_conversations_api.py`, `apps/web/src/ConversationView.test.tsx`.
  Acceptance criteria: `uv run pytest apps/rag/tests/test_conversation_turn.py apps/api/tests/test_conversations_api.py -q`; `cd apps/web && bun test && bun run check && bun run typecheck && bun run build`; `make conversation-eval` report follow-up correctness, context contamination, clarification frequency, temporal correctness and access leakage against frozen fixture labels. Tests cover ambiguous pronoun, changed source, revoked permission, concurrent edits, process restart and invalid cited prior answer; no stale answer used as citation.
  QA scenarios: run fixture API/web two-turn follow-up, inspect fresh source spans and clock label; ask ambiguous follow-up and see clarification; revoke visibility and verify history/context/fetch/return filter. Evidence `.omo/evidence/task-13-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(briefing): add grounded conversation turns`

- [ ] 14. Add bounded read-only tools to existing briefing graph
  What to do / Must NOT do: Extend one LangGraph briefing orchestrator through `graph/{tools,agent_nodes,agent_routes}.py`, `application/{tool_search,tool_fetch}.py`, `ports/tools.py`, `domain/budgets.py`, and `eventlens_contracts/tools.py`. Expose only `search_articles` and `fetch_evidence`; reuse T5/T6 ports, authorization and citation validator. Validate arguments at tool boundary, carry immutable caller scope from server context, never let model/session metadata widen access. No arbitrary SQL, web, filesystem, writes, event mutation or second agent. Fixture-test hard limits: 6 tool calls total, 2 query reformulations, 60 candidate rows per search, 20 unique evidence articles, 12,000 cumulative model input tokens including history and retries, 1,500 cumulative model output tokens including tool arguments/retries with final-response capacity reserved, 30 concurrent requests, 30s agent deadline. Live policy must approve/tune these before activation; they are not measured SLOs. Per-call context is separately capped by remaining input budget; every prompt input counts against cumulative model input budget, not just final context. Reserve output before model calls, count tool/retry usage once in shared ledger; repeat arguments or no new evidence stops. Deadline/cancel means no additional DB/model/tool work. Empty evidence abstains; invalid tool args, outage, budget or malformed model output produce typed failure, never partial unvalidated answer. Runtime remains disabled by default; explicit fixture/test opt-in with fake ports is permitted for T14-T15/G6 verification before review, while live traffic remains blocked pending activation approval.
  Parallelization: G6 extension lane | Blocked by: G3 accepted for single-turn tools; G5 accepted for conversational tools; G4 accepted for group-aware tools | Blocks: T15. Single graph-builder owner.
  References: D1-D7 baseline graph, T5 retrieval caps, T6 validator, T12 access, T13 history; targets `apps/rag/tests/{test_tools,test_agent_budget,test_agent_graph}.py`.
  Acceptance criteria: `uv run pytest apps/rag/tests/test_tools.py apps/rag/tests/test_agent_budget.py apps/rag/tests/test_agent_graph.py -q` with real compiled StateGraph/fake ports tests exact and one-over every limit, retries and reformulations counted cumulatively, per-call vs total distinction, argument validation, access revocation, repeating/no-new-evidence termination, cancel/deadline stop-work, empty abstention, typed errors, no forbidden capability and no graph checkpoint.
  QA scenarios: run scripted search -> fetch -> cited answer; replay same search and force stop; revoke article then fetch and assert denial; exceed tool call/input budget and assert no subsequent port invocation. Evidence `.omo/evidence/task-14-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `feat(rag): add bounded read-only retrieval tools`

- [ ] 15. Freeze paired agent evaluation and extended CI gates
  What to do / Must NOT do: Add `application/agent_eval.py`, `fixtures/rag/agentic/`, versioned acceptance policy, `make agent-eval`, extended `.github/workflows/ci.yml` and docs. Freeze quality/cost/latency/access thresholds and labeled holdout before enabling agent mode; compare tool graph against fixed T5 retrieval using same questions, corpus/index/model version, clock, visibility scope, budgets and hardware. Report claim support/citation correctness, abstention, follow-up correctness, context contamination, access leakage, quality delta, p95 latency, cumulative model tokens/cost, tool calls and repeated-search rate. Fake CI enforces determinism, safety, no-network and paired accounting only; real provider held-out measurements are required for quality activation, separately from fixture acceptance. No invented passing scores or automatic live enablement.
  Parallelization: Wave 6 | Blocked by: T14 | Blocks: G6 overall review.
  References: T9 eval/CI, T10 grouping metrics, T13 conversation metrics, T14 tools; target `apps/rag/tests/test_agent_eval.py`, `fixtures/rag/agentic/manifest.v1.json`, `Makefile`, `.github/workflows/ci.yml`.
  Acceptance criteria: `uv run pytest apps/rag/tests/test_agent_eval.py -q && make check && make agent-eval`; add `make grouping-eval` for group-aware capability and `make conversation-eval` for conversational capability, not as prerequisites for single-turn G6. Reports carry explicit capability scope, fixed policy/split IDs, paired per-question results, token/cost/latency environment, authorization failures and explicit NOT MEASURED for live quality without approved provider. `uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json` and `uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json` still work after rename; no auth bypass, provider call or network in CI.
  QA scenarios: run deterministic paired questions with successful, empty, ambiguous, revoked and repetitive searches; manually inspect cited span in local API/web; verify frozen policy change invalidates prior approval. Evidence `.omo/evidence/task-15-eventlens-ai-rag-mvp.md`.
  Proposed commit (only if separately authorized): `test(rag): gate paired agent evaluation`

## Final verification wave
> Repeat scoped F1-F4 after G1, G2, G3, G4, G5 and G6. G1 covers T1-T3 worker/DB/graph; G2 covers T1-T7 API/degraded paths; G3 covers baseline T1-T9 including web. G4 adds T10-T11 to baseline; G5 adds T12-T13 to baseline independently of G4; G6 adds T14-T15 plus only accepted integrated group/conversation capabilities. Full T1-T15 coverage is required for overall roadmap acceptance, not single-turn G6 acceptance. Each run uses `.omo/evidence/G<gate>/final-*.md`, names covered tasks/capabilities and versions, and records actual reviewer identity/results. G1/G2 do not claim web checks before web exists. Old gate approval never clears later scope. Independent review must be real; unavailable reviewers mean OPEN. Wait for explicit user acceptance, never treat document completion as implementation approval.
- [ ] F1. Plan compliance audit - inspect changed files against scoped Must/Never clauses; run `git diff --check`, verify no dirty pre-existing path was overwritten, and attach `.omo/evidence/G<gate>/final-plan-compliance.md`.
- [ ] F2. Code quality review - run `uv run ruff check .`, `uv run basedpyright`, `cd apps/web && bun run check && bun run typecheck`; inspect architecture direction and typed errors; attach `.omo/evidence/G<gate>/final-code-quality.md`.
- [ ] F3. Real manual QA - start local Postgres/index/API/web, exercise gate flows and analyst query, inspect primary citation and loading/empty/error/retry states; attach screenshots/logs in `.omo/evidence/G<gate>/final-manual-qa.md`.
- [ ] F4. Scope fidelity - compare scoped diff to plan/spec; baseline has no grouping/conversation/tools, G4-G6 have only their specified extensions. Verify no crawler, canonical event writes, multi-agent, forbidden tool, raw fallback, provider secret in CI, or unrelated worker-WIP changes; attach `.omo/evidence/G<gate>/final-scope-fidelity.md`.

Use gate names G1 through G6 for `<gate>`; do not overwrite previous reports. F2 also checks planned <250 code LOC modules and extension tests. F3 runs existing worker validate/process at every gate, adds briefing API query from G2 onward and browser flow from G3 onward; G4 exercises member correction/fallback, G5 restart/access/history/delete, G6 tool stop-work and paired comparison. Each review includes available baseline regression plus newly added scope.

## Commit strategy

Suggested commit subjects only, not commit authority. No commit, branch, push or deployment is authorized by this plan or current documentation task. If explicitly authorized later, use repository Git Flow and stage only intended files; never commit `.omo` session artifacts or unrelated pre-existing WIP:

1. `feat(contracts): add grounded briefing contracts`
2. `test(fixtures): add labeled RAG corpus and evaluation cases`
3. `feat(search): add versioned pgvector corpus index`
4. `feat(ai): add versioned article metadata enrichment`
5. `feat(search): add deterministic hybrid retrieval`
6. `feat(ai): add grounded synthesis and citation validation`
7. `feat(api): add grounded briefing query endpoint`
8. `feat(web): add analyst briefing experience`
9. `test(ci): add deterministic enrichment and RAG evaluation gates`
10. `feat(grouping): add versioned article groups`
11. `feat(search): add topic and group views`
12. `feat(conversation): add scoped durable turn store`
13. `feat(briefing): add grounded conversation turns`
14. `feat(rag): add bounded read-only retrieval tools`
15. `test(rag): gate paired agent evaluation`

These are implementation-sized checkpoints, not a mandate to make 15 final PR commits.
Pair implementation and tests in each checkpoint. If explicitly authorized to commit,
inspect `git status`, `git diff`, and `git diff --check`; stage only intended files.
Before a PR, follow repository Git Flow: rebase the feature branch onto the latest
`develop` and squash to one conventional commit. Never do either during plan editing.

## Success criteria

The following baseline criteria apply to G1-G3. Overall completion additionally requires G4 calibrated groups/topics with primary-evidence fallback, G5 durable scoped conversations, G6 bounded tools/paired evaluation and all scoped F1-F4 reports. Live/shared activation remains separately blocked by policy and real-quality evidence.

- A versioned 30-100 article corpus and 10-question evaluation set exists.
- Canonical articles persist and become lexically searchable before enrichment; LLM failure never removes or blocks them.
- ArticleEnrichment is immutable/versioned, evidence-backed, idempotent by article ID + content hash + enrichment/model/prompt versions, and follows tested PENDING/PROCESSING/READY/FAILED/STALE transitions.
- Controlled taxonomy drives filters; suggested tags remain discoverable but never silently become canonical filter values.
- Analyst query returns structured answer with every material claim bound to valid article/span evidence, or explicit abstention/error.
- Publication-time filter basis is visible; event occurrence is never inferred from publication time.
- T1 owns typed temporal claim value/range or unknown state, precision, confidence, evidence-span references and explicit relative-expression clock/timezone basis; T6 validates these fields against supplied spans and never substitutes publication time for occurrence time. T2/T9 fixtures cover date-only, precise instant, range, unknown, relative dates, timezone/DST boundary and delayed publication. API/UI expose precision/uncertainty rather than implying an exact occurrence timestamp from an imprecise statement.
- Hybrid retrieval is deterministic, bounded, deduplicated, and reports index metadata.
- Postgres + pgvector local path works; CI fake path needs no network/provider secret.
- Worker and API use reusable `eventlens_rag` runners without importing each other; both baseline workflows run through real compiled StateGraph with per-request isolated validated state/context.
- No production checkpoint persistence/resume claim exists; DB-backed enrichment jobs remain durable independently.
- Baseline runs approved fixtures and fake providers only. Live provider mode rejects configuration until explicit provider endpoint, allowed corpus/fields, and trusted-caller policy approval.
- Fixture-only baseline verification uses actual StateGraph with fake ports, real DB/API integration, ingest-to-citation E2E, and paired lexical/hybrid evaluation over same graph, responses, and budgets. Fake CI does not prove real-model quality or latency.
- Hard runtime gates enforce max 20 articles, 12,000 context tokens, 1,500 output tokens, and 30 concurrent queries; retry budgets are cumulative, cancellation prevents new work, and evidence reads are batched. Latency/cost are measured-environment reports; p95 8s remains target, not fake-CI production SLO.
- API and web expose recoverable stable states for all failure matrix cases.
- `make check`, `make enrichment-eval`, and `make rag-eval` pass from clean frozen install.

## Failure modes and test coverage

```text
ArticleBundle
  -> ArticleDocument validation
        +-- invalid canonical input ............ unit: reject, clear validation error
        +-- valid -> persist + lexical index ... integration: searchable immediately
  -> Enrichment job
        +-- duplicate/concurrent delivery ...... integration: one active idempotency key
        +-- provider timeout/5xx ............... unit+integration: bounded retry -> FAILED
        +-- malformed/ungrounded metadata ...... unit+EVAL: FAILED, no metadata reindex
        +-- prompt injection/restricted fields . EVAL: ignored/redacted
        +-- article/version changed ............ unit: old READY -> STALE; new PENDING
         +-- staged index -> atomic READY publish  integration: metadata/vector visible together
  -> Hybrid retrieval
        +-- enrichment pending/failed/stale .... integration: lexical degradation, explicit freshness
        +-- vector/index unavailable ........... integration: typed degraded/unavailable result
        +-- READY .............................. EVAL: lexical+metadata+vector ranking
  -> Answer synthesis
        +-- no evidence ........................ unit/E2E: abstention
        +-- invalid citation/span .............. unit: INVALID_ANSWER, no raw fallback
        +-- valid .............................. E2E: answer -> citation inspection
```

These are planned test requirements, not proof of coverage or clearance. Extended failure coverage and unresolved policy decisions follow below.

## What already exists

- Strict frozen Article/Event contracts, JSON Schema, semantic vectors, fixtures, pytest conventions: reused.
- Worker ports, typed models, deterministic fake adapters, runner/trace patterns: reused as patterns only; event semantics not reused.
- FastAPI application/HTTP split and React/Zod foundation: extended rather than rebuilt.
- PostgreSQL Compose now uses a pgvector-capable image for T3 fresh-DB checks; the existing legacy `eventlens` database is not upgraded in place (untracked pre-ledger tables fail closed); local RAG uses separate database `eventlens_rag`. MinIO remains unused.

### Current status inventory

| Surface | Current status | Remaining acceptance |
|---|---|---|
| Canonical contracts, fixture worker CLI, API health, web foundation | Existing repository foundation | Preserve dirty WIP and run regression commands before/after migration |
| RAG/enrichment contracts, schemas and tests (T1) | Python T1 accepted with current-path checks | Cross-language D33 highlighting belongs to T8; semantic-support evaluation belongs to T9; G1 remains open |
| D1-D7 LangGraph baseline changes in this plan | Existing uncommitted planning decisions preserved | Independent consolidated architecture/test review OPEN |
| Worker path migration and baseline RAG/LangGraph runtime (T3-T9) | Worker active at `apps/worker` (technical parity verified 2026-10-16; historical D41 approval unverified); T3 technical PASS on fresh pgvector; legacy `eventlens` DB not upgraded, separate `eventlens_rag` DB path verified; T4 implementation/re-review APPROVE and owner-authorized commit 2026-10-16 (fixture/fake); T5-T9 not started; G1 ACCEPTED 2026-10-16 (fixture/fake scope, DB `eventlens_rag`, D41 gap accepted by owner; `.omo/evidence/G1/acceptance-decision-2026-10-16.md`). Remaining: legacy in-place upgrade only if ever required, G2-G3 |
| Grouping/topics, conversation store, bounded tools (T10-T15) | Proposed roadmap, not implemented or accepted | G4-G6 and separate live/shared activation approvals |

Do not infer task completion from file presence or prior design review. The T3 scoped runtime results above are technical evidence only, not full T3 or G1 acceptance; inventory exact dirty paths and remaining gaps before further work.

## NOT in scope

- Crawling/source acquisition: normalized ArticleBundle remains input.
- Full RBAC/multi-tenancy: fixture-only local single-principal use; shared use requires approved auth policy first.
- Canonical event extraction/grouping/merge/living-event writes: never part of RAG. Noncanonical article/story groups and topic discovery belong to G4 only.
- Human-reviewed taxonomy management UI: controlled taxonomy lives in versioned contract/config; suggestions are output only.
- Production queue/Kafka/retry service: DB-backed enrichment job lifecycle + CLI/worker loop only.
- Fine-tuning, multi-agent enrichment, automatic truth arbitration, production deploy.

### Extended flows and failure coverage (G4-G6)

G4 proposed candidate default: at most 60 related articles per input article, deterministic tie-break and truncation; approve/freeze cap with train/dev policy, test exact/one-over before grouping activation. Stable group ID survives membership revisions; immutable version identifies member set and member hashes. Merge/split lineage records predecessor IDs, never silently reuses old ID for different story identity.

```text
G4  ArticleDocument + READY enrichment -> bounded candidate search
      -> dedup same content identity (not grouping)
      -> calibrated story match -> versioned noncanonical group + member spans
           | uncertain -> singleton/review
           | correction/deletion -> transactional member update + index invalidation
      -> topic aggregate -> group navigation -> authorized article/span citations
           | no group -> original article retrieval, never summary-only citation

G5  authenticated caller OR loopback single-principal fixture -> conversation turn
      -> check owner/visibility + expected_revision + idempotent turn key
      -> last <=8 turns AND <=2000 history tokens within total input budget
      -> fresh retrieval + current clock -> validated answer -> Postgres turn commit
           | ambiguous reference -> clarification
           | revoked source -> omit from history/context/fetch/response
           | restart -> durable turns, new graph invocation, no graph resume

G6  same briefing graph -> bounded search_articles / fetch_evidence
      -> validated args + current access + shared cumulative budget ledger
      -> new evidence? yes -> cite primary span -> validate -> answer
           | none/repeated args/no new evidence -> stop or abstain
           | cancel/timeout/limit/tool error -> typed failure, no further work
```

| Failure | Required test | Outcome |
|---|---|---|
| False story match or ambiguous candidate | T10 labeled train/dev + untouched holdout, false-merge/B-cubed | Review/singleton, no forced merge or event truth |
| Correction, deletion, concurrent group edit | T10 transaction/replay, T11 index invalidation | No orphan/stale member or summary citation; ungrouped fallback |
| Scope revoked or owner changes mid-thread | T12 history/context/fetch, T13 turn/response, T14 tools | No leaked prior answer/span; typed denial or safe abstention |
| Duplicate turn, competing revision, expiry/redaction | T12 DB integration + restart | Replay same key or conflict; no partial write or resurrected history |
| Ambiguous reference or old time basis | T13 follow-up/clock fixtures | Clarification or freshly cited answer with explicit clock |
| Tool loop, repeated args, candidate overflow, exhausted tokens | T14 exact/one-over fake-port tests | Hard stop, no extra work, no unvalidated partial answer |
| Provider quality degradation masked by fake CI | T15 paired fixed-vs-agent frozen holdout | Live activation blocked pending real-provider quality evidence |

G4 indexing: batch candidate retrieval and membership fetch, cap candidates before scoring, index article hash/member version, invalidate only affected group/topic views on correction/deletion; compare explain plans and query counts to baseline, no N+1. G5 history fetch batches visibility filtering; G6 60 candidate rows per search are an upper bound, not permission to exceed 20 unique evidence articles. G1-G3 12k context cap stays; G5-G6 allocate <=12k total model input per request including history, evidence, instructions and all model retries/calls. Each model call is also bounded by remaining input allowance. Reserve 1.5k output separately per final response; model-generated tool arguments/reformulations/retries also count in cumulative output usage, with a proposed 1.5k cumulative output cap per request, not unlimited output per intermediate call. Refuse next call if no safe output reservation remains. Reports distinguish per-call input/output from request totals and tool counts. These are proposed gates, not measured service levels.

## Approved baseline architecture

These are approved baseline plan decisions, not completed implementation. They govern G1-G3 and remain constraints when G4-G6 extend the same services; enrichment/vector/web scope remains intact. Expanded gates are proposed planning, not approval to execute live traffic.

- D1: Deliver complete baseline through G1 Foundation, G2 LangGraph RAG + API, and G3 Web + evaluation.
- D2: Use request-scoped graphs without production checkpoint persistence. Process loss starts a new request; DB enrichment jobs remain durable independently.
- D3: `apps/worker` may depend on reusable `apps/rag`; API invokes briefing runner and worker invokes enrichment runner. Neither app imports other app.
- D4: Use approved fixtures and fake providers only with real LangGraph runtime. Mock provider transport exists for adapter tests but live traffic stays disabled until explicit approval of provider endpoint, allowed corpus/fields, and trusted-caller policy. Development API binds loopback-only; this is not production authentication.
- D5: The approved architecture is consolidated throughout this plan; implementation tasks and commands do not rely on a later override section.
- D6: Full fixture-only verification covers StateGraph fake-port tests for state, nodes, routes, cancellation, budgets, request isolation; real DB/API integration; ingest-to-citation E2E; and paired lexical/hybrid evaluation. No persistent checkpoint tests beyond asserting disabled baseline.
- D7: Enforce hard gates of 20 articles, 12k context tokens, 1.5k output tokens, and 30 concurrent queries, including cumulative retry budgets, cancellation, and batched evidence reads. Latency/cost remain measured reports; p95 8s is target, not fake-CI production SLO.
- LangGraph is baseline orchestration for briefing and enrichment. Routing is fixed and baseline has no automatic tools.
- LangGraph belongs to `eventlens-rag` as compatible 1.x dependency; record exact resolved version in `uv.lock`, after Python 3.12 and strict typing verification.

### Target structure and dependency rules

```text
apps/
  api/src/eventlens_api/          HTTP adapter + composition root
  worker/src/eventlens_worker/    CLI/job runner + existing event pipeline
  rag/src/eventlens_rag/
    domain/                      internal policies, models, typed errors
    application/                 ingest, enrich, retrieve, synthesize, validate
    ports/                       repositories, model, workflow, telemetry
    adapters/                    postgres, provider HTTP, deterministic fakes
    graph/
      state.py                   internal state + validated input/output mapping
      context.py                 injected runtime dependencies
      nodes.py                   thin application-service wrappers
      routes.py                  exhaustive deterministic route functions
      graph.py                   build/compile briefing and enrichment StateGraph
      runner.py                  framework-free workflow port implementation
contracts/
  python/                        shared boundary models
```

Create modules only with real responsibilities; split oversized nodes by workflow when needed. `tools.py` belongs to the later tool-calling extension, not an empty baseline placeholder. Persistent `checkpoint.py` is deferred with persistence.

```text
api composition ------> rag graph runner <------ worker composition
                              |
                         application use cases
                              |
                       domain + owned ports
                              |
                        shared contracts

rag graph adapter ------> LangGraph
postgres/provider adapters implement ports
```

Domain/application/ports/contracts must not import LangGraph, FastAPI or concrete database/provider adapters. Framework-specific compiled graph types do not escape the runner into HTTP DTOs or use-case signatures. Nodes call injected application services, not raw SQL or provider SDKs. Existing event extraction/gate/writer semantics stay separate; worker's new enrichment command does not route RAG answers through `event.v1`.

### Baseline workflow and state

```text
BriefingQuery -> boundary validation -> briefing graph
  START -> retrieve -> assemble_context
                         | no evidence -> abstain -> END
                         | evidence
                         v
                      synthesize -> validate_citations
                                       | valid -> answer -> END
                                       | invalid -> typed failure -> END
  any stage: timeout/cancel/invalid state -> typed failure, no raw answer

ArticleBundle -> validate/persist ArticleDocument -> lexical index
  worker claims DB enrichment job -> enrichment graph
  START -> enrich -> validate_metadata -> stage version/index -> fenced atomic READY + active-version publish -> END
   provider/schema failure -> bounded job retry or FAILED; lexical article remains available
   cancellation -> no invocation-side retry/FAILED/publish; lease recovery after expiry
```

- State contains typed query/document references, version/hash identity, evidence references, validated result/status, typed failure code and usage counters. `TypedDict` provides static typing, not runtime validation or immutability: validate external input, node deltas and final output explicitly.
- Runtime context holds clients/ports, caller context, deadline and budget policy. Never put credentials, clients, raw provider output or chain-of-thought into graph state or audit records.
- Baseline is sequential with single-writer state fields; use replacement semantics. Any later parallel reducer needs deterministic merge, deduplication and collision tests. Avoid unbounded message-history accumulation.
- Synthesis remains probabilistic; routing, evidence assembly and structural/provenance citation validation are deterministic. A valid span reference alone does not prove semantic support: measure claim support separately in evals.
- Compile graphs at composition/setup, reuse compiled definitions; initialize isolated state/context per invocation. Run asynchronous provider/database calls under an end-to-end deadline; cancellation must propagate and prevent new work.
- Keep baseline 20-article, 12,000-context-token, 1,500-output-token and 30-concurrent-query limits. Account retries and model calls cumulatively; reserve output capacity before invocation. G5/G6 refine 12k into total cumulative model input including history/instructions/evidence and retries, with a separate per-call remaining-context check; this never increases baseline allowance. Configure node/retry/deadline limits explicitly. `recursion_limit` counts framework supersteps, not tool calls or elapsed seconds: derive it from topology and test every legal terminal path, rather than copying the earlier illustrative 6/8 values.
- Enrichment retry has one owner: DB job orchestration. Do not stack graph retries with job retries without a shared attempt budget. Job lease recovery/idempotency is not supplied by a graph checkpointer.
- Job writes and publication are fenced by the current `claim_generation` and database-clock lease validity; late or cancelled workers cannot publish READY or index changes after losing ownership.

### Delivery gates

| Gate | Tasks/modules | Depends on | Exit criteria |
|---|---|---|---|
| G1 Foundation | T1 contracts; T2 corpus; T3 worker migration, RAG/storage, graph foundation | existing worker/contracts | Worker regression suite passes under new path; real StateGraph compiles/invokes with fake ports; storage/corpus tests pass |
| G2 LangGraph RAG + API | T4 enrichment + worker entrypoint; T5 retrieval; T6 synthesis/validation; T7 API/briefing graph composition | G1 | API executes compiled briefing graph; worker executes enrichment graph; happy/degraded/abstain/error paths and resource limits pass |
| G3 Web + eval | T8 web; T9 eval/CI; F1-F4 | G2 | Full ingest-to-query-to-citation flow passes; deterministic safety gates pass; quality/latency/cost reports recorded |
| G4 Grouping/topics | T10-T11; scoped F1-F4 | G3 accepted | Frozen train/dev/holdout policy, group provenance/correction tests and false-merge/B-cubed reports; ungrouped fallback and primary citations verified |
| G5 Conversations | T12-T13; scoped F1-F4 | G3 accepted; group navigation integration additionally waits for G4 | Postgres history durable, no graph resume; per-turn visibility/owner checks, retention/delete/redact/revision/idempotency and multi-turn tests pass; shared use disabled |
| G6 Bounded agentic | T14-T15; scoped F1-F4 | G3 accepted for single-turn path; G5 for conversational path; G4 for group-aware path | Scoped tool allowlist/resource stop-work tests and paired fixed/agent reports pass; report unavailable extension paths explicitly; overall roadmap acceptance still requires G4-G6; live quality remains gated separately |

Parallel lanes: after G1 and T4, T5 and T6 may proceed with separate owned files/interfaces. T4 owns worker enrichment graph integration; T7 composes briefing graph after T5/T6. Assign one owner to workspace lockfile, Makefile, CI, and graph builder. No parallel edits to shared module files. G3 review clears baseline only; G4-G6 repeat reviews across baseline plus extensions.

### Test coverage and failure matrix

All entries below are planned requirements, not claims of existing coverage.

```text
state boundary -> malformed/unknown keys/version mismatch [UNIT]
node delta -> invalid types/secret/raw output leakage     [UNIT]
route -> every status/unknown status/no evidence          [UNIT]
real compiled graph -> happy/abstain/invalid citation      [INTEGRATION]
  | deadline/recursion/retry/budget exhaustion             [INTEGRATION]
  | cancellation/concurrent requests/state isolation      [INTEGRATION]
worker -> lease loss/duplicate delivery/retry exhaustion   [DB INTEGRATION]
API -> typed error mapping/no raw model fallback          [INTEGRATION]
web -> submit/citation/retry/rapid resubmit/navigation     [E2E]
LLM -> injection/unsupported claims/conflicts/time basis   [EVAL]
```

Tests use real `StateGraph`, not a fake graph runtime. Fake retriever/provider/clock usage must be scripted and deterministic. Test exact limits and one-over limits; assert no additional model/DB work starts after cancellation or exhausted cumulative retry budget. Test stale result suppression on rapid resubmit/navigation, request isolation, real DB/API integration, and ingest-to-citation E2E. Persistent checkpoint serialization, TTL and crash-resume tests are not required; baseline tests only assert no production checkpointer is configured.

Compare lexical and hybrid modes using the same graph, corpus/questions, model responses and budgets so framework changes are not confused with retrieval changes. Report per-question Recall@k, claim support, citation validity, abstention, temporal accuracy, token usage and stage latency. Fake CI verifies control flow and accounting, not real provider quality, latency or cost. Enforce hard resource gates and report latency/cost only for measured environments; p95 8 seconds remains target, not fake-CI production SLO. Record graph orchestration overhead separately; cap database candidate rows before context assembly and batch evidence reads to avoid N+1 queries.

### Deferred scope and remaining review

- Bounded single-turn agentic retrieval is planned in G6/T14-T15 after G3, reusing the same services and graph runner. Conversational and group-aware tool paths additionally wait for G5 and G4 respectively. No automatic tools in baseline; every capability requires scoped budget, injection, access and paired-value gates.
- Persistent checkpoints/resume/HITL are deferred by D2. No PostgresSaver dependency, checkpoint retention service or resume endpoint in baseline.
- Public package/container publishing and production deployment remain deferred; local workspace install, container builds and CI verification remain required.
- Real-data/provider use is a deferred activation gate under D4, not a blocker for fixture-only implementation. Provider credentials alone must not enable live mode: reject live-mode configuration until an explicit approved policy is supplied. Test default fake selection, live-mode rejection without approval, no provider network calls in CI, and loopback-only development binding. Real provider quality/latency/cost remain unverified until activation.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| Eng Review | `/plan-eng-review` | Architecture, code-quality boundaries, tests and performance | 1 interactive cycle | REVIEWED WITH CONCERNS | User decisions D3-D42 incorporated; D27 reselected to scope-derived public IDs; activation policies still open |
| Team review | Three read-only members | Architecture, quality/tests, performance/evaluation | 3 workstreams | COMPLETE | Findings considered interactively; team tasks completed and team deleted; no runtime acceptance claimed |
| Oracle review | Read-only document consistency | Challenge lifecycle and dependency changes | Initial review plus final follow-ups | PASS on final consistency check | READY staging, current hash, cancellation, DB outage, G6 independence, early gate owners and embedding ownership contradictions resolved |
| Outside voice | Codex CLI read-only | Independent challenge after section reviews | 1 | ISSUES FOUND, decisions incorporated | 25 recommendations examined; D19-D42 address them; human workflow measurement deferred by user; no clean post-edit Codex verdict claimed |
| Momus review | Plan critic | Executability and references | 1 | OKAY | Referenced existing files verified; planned targets and tests executable; no blocking contradiction reported |
| Design Review | `/plan-design-review` | Analyst/group/conversation UX | 0 this revision | NOT RUN | Recommended before affected UI implementation |
| Unified roadmap | User D1-D7 plus proposed G4-G6 | Naming, dependencies, tests, access, budgets, evaluation | Document edit only | OPEN | G1-G3 baseline preserved; T1 partial; G4-G6 proposed; F1-F4 acceptance evidence pending |

**VERDICT:** Technical review and approved plan corrections recorded; final Oracle document-consistency check PASS. Not ENG CLEARED for unrestricted execution: frozen gate-specific policies, task evidence, UI review and activation approvals remain pending. D1-D7 baseline preserved; extensions are capability-scoped branches after G3, with integrated paths gated separately. Baseline runtime implementation and physical rename have not started; contracts exist as partial T1 work, not accepted. (Historical review statement; 2026-10-16 status: T1/T2 accepted in scope, T3 technical PASS, worker moved to `apps/worker` without verified D41 approval, G1 OPEN.) No implementation, live activation, shared deployment or commit approval follows from this review.

**Decision record:** D3 five-field job identity; D4 fencing; D5/D15 staged atomic READY/index publish; D6/D21/D22 scoped access and final-check epoch; D7 real DB CI; D8 browser gate; D9 semantic versus mechanical support; D10 lexical/filter semantics; D11/D20/D36 embedding generation ownership/reuse; D12 smoke versus live measurement; D13 filtered vector fallback; D14/D23 replay race and support reauthorization; D16 current-content guard; D17 cancel linearization; D18 DB outage semantics; D19 claim-block answer rendering; D24 spec supersession; D25 temporal claims; D26 synchronous tombstone; D27 scope-derived public IDs (replaces initial global-public choice); D28 grouping dataset; D29 manual grouping CLI; D30 development versus enforced retention; D31 activation artifact; D32 tokenizer policy; D33 normalization/offset contract; D34 loopback startup gate; D35 ranking policy; D37 internal source viewer; D38 independent G1/G2 review; D39 branched extensions; D40 gate harness; D41 owner-approved dirty rename; D42 human productivity metric deferred.

**UNRESOLVED DECISIONS:**
- Final task-policy values and runtime acceptance remain OPEN; reviewer document agreement never replaces measured tests or user activation approval. No clean final Codex rerun or current-revision UI review is claimed.
- Historical (as of this review report, before 2026-10-16): "T1 gap audit and acceptance evidence pending; T2-T15/G1-G6 and scoped F1-F4 are not marked complete."
- Current (2026-10-16, see Todos checklist and `.omo/evidence/G1/acceptance-decision-2026-10-16.md`): T1-T3 are complete in their recorded scope; G1 is ACCEPTED for fixtures/fake providers on separate DB `eventlens_rag`, with the D41 historical evidence gap accepted by the owner, not resolved. T4 implementation and independent re-review APPROVE are complete; the owner authorized its commit and preparation of T5 (`.omo/evidence/T4/commit-check-20261016.md`). T5-T15 are not implemented; G2-G6 remain OPEN. T4 completion is not G2 acceptance or live-provider approval.
- Grouping candidate cap, train/dev split, false-merge/B-cubed acceptance thresholds and frozen holdout policy require approval before auto-grouping.
- Shared conversation authentication/authorization and retention/delete/redact policy require approval; 30-day local development retention is proposed default, not shared-use approval.
- Agent defaults and paired quality/cost/latency acceptance thresholds require frozen policy before activation; 30s deadline and other limits are proposed defaults, not measured SLOs.
- D4 real-data/provider endpoint, allowed corpus/fields and trusted-caller policy plus held-out real-provider quality evidence remain separate activation gates. Fake CI supplies no real quality clearance.
- Expanded roadmap is planning authorization only; implementation/start-work readiness, UI review, live/shared deployment and commit/push authority remain separate approvals.
