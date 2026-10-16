# T3 Partial Implementation Evidence

Date: 2026-09-29. Status: OPEN. Not review-ready. G1 OPEN; worker rename BLOCKED.

## Scope And Inventory

Active worker remains `apps/ai-worker`. No worker, contract, schema, or fixture
files were edited by this T3 execution. No rename, commit, push, stash, API,
UI, live provider, or checkpointer activation occurred.

Orchestrator supplied a preflight baseline of 126 passing worker tests plus
validate/process output. The preflight content hashes were not supplied to this
executor; this artifact cannot attest hash equality or recreate a pre-edit
inventory after edits. That required evidence remains OPEN.

## Recorded Commands

| Command | Outcome |
| --- | --- |
| `uv sync --all-packages --group dev` | Exit 0; 69 resolved packages, 65 checked |
| `uv run pytest packages/rag/tests -q` before additional DB tests | 10 passed, 1 skipped |
| `make gate-g1` | Exit 2; 308 passed, 4 skipped; Docker unavailable |
| Worker validate within gate | Valid article `550e8400-e29b-41d4-a716-446655440000`, 2 media items |
| Worker process within gate | ACCEPT; ready; inserted |
| `uv run basedpyright packages/rag` | Failed: 5 errors in graph runner, including incomplete LangGraph typing |
| `lsp_diagnostics packages/rag` | Not clean; output also references pre-edit/import-resolution state |
| `docker info --format '{{.ServerVersion}}'` | Exit 1; Docker socket missing |

Docker error: `dial unix /Users/phungdinhdat/.docker/run/docker.sock: connect: no such file or directory`.
No real PostgreSQL acceptance run occurred. Skipped tests are not DB evidence.

## Implemented Repairs

- Removed graph type-ignore suppressions and added package typing marker.
- Fixed strict UUID/tuple test inputs and CLI Annotated options.
- Added concurrent graph route test using compiled StateGraph.
- Added transactional migration checksum ledger with advisory lock.
- Added immutable-revision trigger, active-generation foreign key, current lexical/vector views.
- Added tombstone guard to completion and correction pointer invalidation.
- Replaced persistent Compose DB test target with uniquely named ephemeral Docker container,
  dynamic localhost port, tmpfs storage, and cleanup restricted to that container.
- Gate executes existing worker path and always reports rename BLOCKED with exit 2.

## Outstanding Defects

- Graph still echoes messages rather than implementing persistence application nodes;
  runtime context validation and complete graph module boundaries remain incomplete.
- LangGraph strict typing still fails; LSP not clean.
- CLI rebuild is migration-only, not staging/rebuild recovery. CLI db-test is not a full test runner.
- Atomic fenced metadata/vector publication, full identity/reuse metadata, access port,
  validated vector dimensions, and complete immutable ArticleDocument persistence remain incomplete.
- DB tests cover only basic storage, checksum drift, immutability, and vector SQL;
  FTS, competing leases, publication rollback, correction/rebuild races and recovery coverage are missing.
- In-memory storage is not a valid substitute for PostgreSQL acceptance and has weaker semantics.
- Full lint/build verification and preflight hash comparison remain OPEN.

T3 must not be marked complete; T4+ remains out of scope.

## Continuation Checks

- Added framework-free `AccessPolicy` and `ArticlePersistence` protocols,
  fixture-only revocable access adapter, authorized persist-first application
  service, scripted persistence fake, and embedding generation identity.
- Access and embedding identity tests were run red before their implementations.
- `uv run pytest packages/rag/tests -q`: 13 passed, 4 skipped.
- `uv run basedpyright packages/rag`: 4 remaining LangGraph typing errors
  after removing direct Any output consumption and using explicit generics.
- `uv run ruff check packages/rag`: 27 issues in 12 files, not clean.
- The new persistence service and scripted adapter still require dedicated tests
  and graph integration. Embedding identity is not yet connected to storage.
- Original preflight hashes remain referenced but unavailable to this executor;
  no fabricated inventory or hash comparison is included.

This continuation does not satisfy T3 acceptance. Docker remains unavailable,
but missing implementation and failing static checks are separate blockers,
not consequences of Docker availability.

## Latest Implementation Checkpoint

- Real persistence StateGraph now invokes injected `PersistArticle` service with
  isolated Pydantic-validated caller context and output. Denied policy produces
  no persistence calls. Internal TypedDict channels avoid LangGraph interpreting
  inherited Pydantic `model_config` as a state channel.
- Added generation staging, current-content guarded activation and separate fenced
  metadata publication transaction. Added staging validation for exact chunks,
  vector count, identity, dimensions, finiteness and nonzero vectors.
- CLI rebuild now stages fake vectors for current persisted articles and activates
  complete matching generations. These database paths remain unexecuted.
- `uv run pytest packages/rag/tests -q`: 17 passed, 4 skipped (before latest
  lint-only/type-only changes).
- Latest `uv run basedpyright packages/rag`: 0 errors, 0 warnings, 0 notes.
  A narrow local stub describes only consumed LangGraph APIs; no runtime substitute
  or type-ignore is used.
- Ruff remains failing; most recent run reported 20 issues before stub cleanup.
- Remaining acceptance gaps: broad real DB concurrency/recovery test coverage,
  cancellation publication boundary, global corpus generation flip, reuse avoiding
  redundant embedding calls, accurate CLI db-test dispatch, full lint/LSP checks.
  The standalone route function is not yet wired as a conditional graph edge.

This supersedes earlier implementation descriptions, not their historical results.
T3 remains OPEN; no DB or G1 PASS is claimed.

## Final Continuation Results (2026-09-29)

| Command | Observed result |
| --- | --- |
| `uv run ruff check packages/rag` | Clean |
| `uv run ruff format packages/rag` | 44 files unchanged |
| `uv run basedpyright packages/rag` | 0 errors, 0 warnings, 0 notes |
| `uv run pytest -q` | 317 passed, 7 skipped |
| `uv build --package eventlens-rag` | Built sdist and wheel successfully |
| `uv run --package eventlens-rag eventlens-rag --help` | migrate/rebuild/db-test commands displayed |
| `make rag-db-test` | Failed before DB creation: Docker daemon socket missing |
| `make gate-g1` | Exit 2; 315 passed, 7 skipped; worker validate/process unchanged; G1 OPEN, rename BLOCKED |

LSP directory diagnostics were clean before conditional-edge wiring. The latest
LSP call reports three unknown `add_conditional_edges` errors against an older
stub view, while the fresh basedpyright process is clean. Do not report LSP clean
without refreshing and rechecking that server.

Conditional routing is now wired into the real persistence StateGraph. CLI db-test
dispatches the isolated container harness. Seven PostgreSQL tests are prepared:
basic persistence/tombstone, migration checksum, revision immutability, vector SQL,
FTS/staging retry/tombstone replay, lease expiry/reclaim, and fenced atomic
READY/metadata/pointer publication. All seven skipped without a DSN; none count
as real database acceptance evidence.

Remaining limits must stay explicit: multi-article atomic corpus-generation flip,
prompt-only zero-embedding-call reuse, cancellation/publication race coverage,
barrier-driven DB concurrency and interrupted-publication rollback tests are not
complete. Migration resources currently resolve from the source checkout; wheel
build success does not establish installed-wheel migration portability.
Original preflight hash list remains unavailable, not reconstructed or fabricated.

## Final Verification Verdict (2026-09-30)

- `lsp_diagnostics packages/rag`: 0 diagnostics after refreshing the local
  LangGraph consumption stub.
- `uv sync --all-packages --group dev`: exit 0; 69 packages resolved and 65
  checked.
- Scoped G1 Python command: exit 0; 302 passed.
- `uv run pytest -q`: exit 0; 317 passed, 7 skipped. All seven skips require a
  real PostgreSQL DSN and are not acceptance evidence.
- `make check`: exit 0; Ruff, basedpyright, Python tests, Bun checks/tests and
  web build passed.
- `git diff --check`: exit 0.
- Worker regression commands under `apps/ai-worker`: exit 0; validate reports
  2 media items and process reports `ACCEPT`, `ready`, `inserted`.
- `make rag-db-test`: BLOCKED before creating resources because Docker socket
  `/Users/phungdinhdat/.docker/run/docker.sock` does not exist.
- `make rag-migrate` and `make rag-rebuild`: BLOCKED because no PostgreSQL
  process listens on `127.0.0.1:5432`; both fail with psycopg connection
  refusal. No fallback or mock acceptance used.
- `make gate-g1`: exit 2. Technical child checks pass, but Docker-backed checks
  are blocked and worker rename has no owner approval. Gate status remains
  `OPEN`; it does not report G1 PASS or ENG CLEARED.

T3 code and non-DB checks are ready for review. Full T3 storage acceptance is
blocked by unavailable Docker/PostgreSQL and the independent rename approval
gate. T4 and later work remain unstarted.

## Continuation 2026-10-02: Real Database Acceptance

Status: T3 technical storage/graph/harness checks PASS on real pgvector.
T3 remains OPEN only because the worker rename (D41) is BLOCKED; G1 OPEN.
Owner decision recorded this session: keep `apps/ai-worker`, no rename, no
stash/commit/push. Raw logs: `.omo/evidence/task-3-baseline-2026-10-02/`.

### Environment

Docker 29.7.2 reachable (earlier "socket missing" no longer applies; the agent
sandbox blocks the socket, so Docker/uv commands ran outside it). Image
`pgvector/pgvector:0.8.0-pg17`. Python 3.12 via uv workspace.

### D41 inventory (before changes)

- `apps/ai-worker`: 27 tracked files, 0 untracked, 0 dirty;
  per-file SHA-256 in `worker-sha256.txt`.
- Baseline: `eventlens-worker validate` exit 0 (`validate.out`), `process`
  exit 0 (`process.out`, ACCEPT/ready/inserted), `pytest apps/ai-worker/tests`
  exit 0, 138 passed.
- After changes: hashes identical, validate/process output byte-identical.

### Defects found and fixed

- `rag_current_lexical` view (003) expanded `document.*` before 004 added
  `document_json`, so `eventlens-rag rebuild` always failed with
  `UndefinedColumn`. Fixed by new migration `005_current_lexical_document.sql`
  (applied migrations untouched; checksum ledger intact).
- Publication DB test used an invalid READY enrichment; now uses fixture
  `fixtures/rag/enrichments/01.json` re-keyed to the test article.
- Removed `complete_job`/`publish_generation`: they set READY without metadata
  and pointer publication, contradicting atomic fenced publication.
- `claim_job` now claims only PENDING or lease-expired PROCESSING jobs (not
  STALE/FAILED) using `clock_timestamp()`.
- New content hash marks older-hash jobs STALE in the persist transaction.
- Typed `StorageUnavailableError` (`rag_storage_unavailable`, no credentials in
  message); CLI exits 3.
- Added `EnsureGeneration` (D36 reuse: zero embedding calls when a complete
  generation with the same embedding key exists), `GenerationStaging` port,
  `activate_all` (all-or-nothing multi-article flip; corrected/tombstoned
  articles skipped), `load-fixtures` CLI.
- Harness: `with-test-db.sh` (isolated container, random loopback port, tmpfs,
  guaranteed removal); `db-suite.sh` fails on any skipped test; `gate-g1.sh`
  runs migrate, load-fixtures, rebuild x2 and the DB suite inside the harness.

### Commands

| Command | Exit | Result |
| --- | --- | --- |
| `make rag-db-test` (before fixes) | 2 | 6 passed, 1 failed (invalid READY fixture) |
| `make rag-db-test` | 0 | 115 passed, 0 skipped, real pgvector |
| `make gate-g1` (before 005) | 2 | DB step failed: `column "document_json" does not exist` |
| `make gate-g1` | 2 | Every technical step exit 0; 298 scoped tests; rebuild 30 published then 30 reused; DB suite 115 passed; `Technical checks: PASS`; `G1 OPEN: rename BLOCKED` |
| `make check` | 0 | ruff format/check clean, basedpyright 0/0/0, 393 passed + 17 DB-only skips (covered by rag-db-test), bun check/test/build pass |
| `uv run basedpyright apps/rag` | 0 | 0 errors, 0 warnings, 0 notes |
| `git diff --check` | 0 | clean |
| `docker compose --profile infra up -d --wait postgres` | 0 | healthy |
| `make rag-migrate` on compose DB `eventlens` | 2 | BLOCKED: legacy tables (see below) |
| migrate / load-fixtures / rebuild / rebuild on fresh compose DB `eventlens_rag_g1` | 0 each | 30 published, then 30 reused; vectors expose `rag-fixture-corpus.v1`, `codepoint-1200-200.v1`, dim 1536, indexed_at; FTS `catalog` returns article ...030 |
| `eventlens-rag rebuild` with compose Postgres stopped | 3 | `rag_storage_unavailable` |

### Real-DB coverage (all executed, none skipped)

Migration checksum drift; immutable revisions; duplicate document suppression;
five-field uniqueness incl. raw duplicate INSERT -> UniqueViolation and each of
enrichment/model/prompt version creating a distinct job; vector(1536) SQL;
lexical-before-vector visibility; HNSW cosine plan (`rag_chunks_embedding_hnsw_idx`)
and index metadata/offsets; correction during rebuild (old text/vectors hidden,
old job STALE and unclaimable, stale generations not activatable); synchronous
tombstone surviving replay; owner-fenced publication; expired lease without
reclaim and late owner after reclaim (no state change); 8-thread concurrent
enqueue+claim -> one job, one winner; interrupted publication rollback then
retry; interrupted staging leaves zero rows then idempotent retry; incomplete
generation aborts the whole corpus flip; real-DB embedding reuse; CLI rebuild.

### Blockers / open items

1. D41 rename `apps/ai-worker` -> `apps/worker`: BLOCKED (owner chose to keep
   current path). T3 acceptance "worker suite passes under renamed path" and
   G1 cannot pass until approved. Gate exits 2 by design.
2. Local compose DB `eventlens` holds 4 empty legacy tables not in git
   (`rag_chunks` without `generation`, `rag_document_versions`, `rag_jobs`,
   `rag_index_metadata`), so `make rag-migrate` fails there. Not dropped
   without owner approval; fresh DB proves the path.
3. Cancellation/publication race barrier tests belong to T4 (plan), not run.
4. Independent G1 F1-F4 review not performed.

No commit, push, PR, provider/network traffic, MinIO/Parquet wiring or
production checkpointer. T4+ not started. T3 NOT marked complete.

## Technical Review Correction (2026-10-02)

The continuation above is historical evidence, not the current acceptance
verdict. In particular, line 165's "only because the worker rename" is false:
the existing Compose database `eventlens` also has an incompatible legacy RAG
schema. Migration there remains BLOCKED; no legacy tables or data were deleted.
The fresh isolated pgvector database proves the new path, not an upgrade of that
existing database. Populated pre-004 legacy rows cannot reconstruct the full
`ArticleDocument` from stored title/content. Migration now fails closed before
recording 004/005 as applied; recovery needs exact original documents from an
approved source, not fabricated fields. The real-DB test covers this rollback.

Line 182's whole-worker hash equality is also superseded. Baseline inventory
recorded 27 tracked worker files; the later comparison is **26/27 unchanged**.
`apps/ai-worker/pyproject.toml` changed intentionally for the dependency, so
do not claim all hashes are identical. Worker validate/process regression ran
successfully in the new gate run; this is not a post-rename comparison.

Fresh technical verification used a temporary repository copy at
`/var/folders/8g/v6hblscd16xczwt29872t33w0000gn/T/opencode/g1-verify.uvkGFL`.
Gate logs: `.omo/evidence/G1/run-20261002T202101Z-GJ5ZYg/` **inside that copy**,
not in this repository's `.omo/evidence/G1/` directory. Its `exit-codes.tsv`,
`pytest-db.log`, `graph-tests.log`, `technical-status`, and `gate-status` record:

| Check | Recorded result |
| --- | --- |
| G1 isolated DB harness (`db-suite.sh`, same DB suite as `make rag-db-test`) | 127 passed, 0 skipped on real pgvector; DB child exit 0 |
| Scoped G1 graph/contract/worker pytest | 303 passed; exit 0 |
| Migration, load-fixtures, rebuild twice, worker validate/process | Each child exit 0; second rebuild reused 30 generations |
| `make gate-g1` | Exit 2 by D41 policy; `technical-status` = `PASS`, `gate-status` = `BLOCKED: D41 worker rename requires owner approval` |

Separate technical review reported `make check` exit 0 after `bun install` in
the temporary copy: 398 passed, 24 skipped in the no-DSN Python run, with web
checks/tests/build passing. Those skips are not DB acceptance; the 127-test
real-DB suite above supplies that evidence. The older checked-in
`task-3-baseline-2026-10-02/make-check.log` reports 393 passed, 17 skipped and
must not be cited as the fresh 398/24 run. Review also reported `uv lock`
verification and `git diff --check` clean. The targeted installed-wheel
migration test timed out when run alone; it was included in the successful
real-DB suite, so do not claim the standalone invocation passed. Fresh 398/24,
lock, hash comparison, and timeout claims have no log in this repository's
evidence directory; keep their review-reported provenance distinct from the
gate files above.

Verdict: new T3 migration/storage/integration technical code reviewed PASS
within tested scope. T3 is **NOT fully complete**, G1 **OPEN**. D40 gate auto
captures child exit codes and logs and reports technical PASS separately from
gate acceptance. D41 rename remains BLOCKED by owner choice to keep
`apps/ai-worker`; no physical move or renamed-path regression was done. Legacy
Compose schema upgrade remains BLOCKED without approved recovery. No G1 PASS,
ENG CLEARED, or independent F1-F4 acceptance is claimed.

## Re-verification 2026-10-16 (supersedes current-status claims above)

Sections above keep their historical labels and results. Current state, verified by
Claude (Claude Code agent, self-verification, not independent) on 2026-10-16, is in
`.omo/evidence/task-3-g1-selfcheck-2026-10-16.md`:

- Worker path is `apps/worker` (moved 2026-10-03; receipt
  `task-3-d41-rename-2026-10-03.md`). The "keep `apps/ai-worker`" statements above are
  history. Approval for that move is unverified; the user keeps the current path.
- `make check` 0 (398 passed, 26 no-DSN skips); `make rag-db-test` 0 (129 passed,
  0 skipped); `make gate-g1` 2 = technical PASS, acceptance OPEN.
- Legacy Compose `eventlens`: 6 untracked tables, 0 rows, no ledger. Migration now
  raises typed `LegacySchemaError` before any DDL; Make targets default to separate DB
  `eventlens_rag` (verified on an isolated Compose project).
- T3 technical PASS; T3 acceptance as part of G1 and G1 itself remain OPEN.
