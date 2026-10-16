# T4 evidence: asynchronous metadata enrichment lifecycle + worker graph entrypoint

Current status (2026-10-16, after independent re-review): **T4 technical APPROVE;
R1-R4 CLOSED; owner authorized commit and preparation of T5**. See
`T4-rereview-20261016-codex/review.md` and `T4/commit-check-20261016.md`.
G2 remains OPEN (T5-T7 remain). This does not approve live providers or deployment.

The implementation/self-check account below is historical. Its original
"self-check only", "nothing committed" and "pending re-review" statements describe
their respective runs and are superseded by the current status above.

- Date: 2026-10-16
- Worktree: `/Users/phungdinhdat/Documents/git/eventlens/.claude/worktrees/agent-a821d85b56097a3d5`
- Branch: `feature/rag-enrichment-t4`, created from accepted baseline `b1a3900`
  (`feature/rag-foundation`). `feature/rag-enrichment` already existed (also at
  b1a3900), so the `-t4` name was used. Nothing committed, pushed or deployed.
- Main-repo WIP (`.omo/plans/...`, `docs/designs/...`, `docs/de-cuong-do-an-rag.md`)
  was read only by `diff` against the worktree copy; not modified.
- Run logs: `.omo/evidence/task-4-logs/run-20261016T060157Z/` (`exit-codes.tsv`,
  one `.log` per command, `cli-smoke.sh`).
- Environment: macOS Darwin 25.4.0 arm64, Python 3.12.14, uv 0.12.5, bun 1.4.0,
  Docker 29.7.2, `pgvector/pgvector:0.8.0-pg17` (isolated, auto-removed containers),
  langgraph 1.2.12, psycopg 3.3.6. Commands ran with the worktree's own `.venv`
  (`VIRTUAL_ENV` of the main repo unset). Docker/uv cache access needed the sandbox
  to be bypassed for those commands only.

## Flow implemented

```text
ArticleBundle --validate--> Article --document_from_article (NFC + RAG hash)--> ArticleDocument
  --PostgresEnrichmentRepository.ingest: ONE transaction-->
     rag_article_documents + rag_article_current (lexical FTS visible now) + job PENDING
  --eventlens-worker enrich--> enqueue_missing (reconcile) -> claim_next (FOR UPDATE SKIP LOCKED,
     claim_generation+1, attempt+1, lease on DB clock) -> compiled StateGraph:
     load -> enrich (1 provider call) -> validate (structural) -> stage (EnsureGeneration)
     -> publish (fenced tx: READY + rag_enrichment_versions + rag_enrichment_publications
        + generation ACTIVE + active_index_generation)   | any typed failure -> fail
        (one fenced retry-schedule or FAILED write)
```

## Changed files

Storage / migration (eventlens_rag only):
- `apps/rag/src/eventlens_rag/migrations/006_enrichment_lifecycle.sql` + byte-identical
  copy `apps/rag/migrations/006_enrichment_lifecycle.sql` (new; 001-005 untouched):
  job columns `attempt_count`, `next_attempt_at`, `failure_code`, `retry_policy_id`,
  `cancel_requested_generation`, `updated_at`; eligibility index; append-only
  `rag_enrichment_publications` (immutable trigger).
- `adapters/migrations.py` (manifest +006), `adapters/postgres.py` (persist/enqueue
  refactored into connection-scoped `persist_revision`/`enqueue_revision_job`, SQL
  unchanged), `adapters/publication.py` (`publish_fenced`: cancel re-check under row
  lock, history row, older READY -> STALE, `publish()` bool API kept).

Domain / ports / application (framework-free):
- `domain/article_ingest.py`, `domain/enrichment_lifecycle.py`,
  `domain/enrichment_draft.py`, `ports/enrichment.py`, `application/ingest.py`,
  `application/enrichment.py`, `policies/enrichment-retry.v1.json`.

Graph: `graph/enrichment.py` (EnrichmentGraph, EnrichmentRunner), `graph/context.py`
(+EnrichmentContext), `typings/langgraph/graph/state.pyi` (context bound).

Adapters: `adapters/fake_enricher.py`, `adapters/openai_enricher.py`,
`adapters/postgres_enrichment.py`, `adapters/memory_enrichment.py` (unit fake),
`adapters/telemetry.py`.

Worker: `apps/worker/src/eventlens_worker/enrichment.py` (composition root),
`apps/worker/src/eventlens_worker/cli.py` (+`ingest`, `enrich`; `validate`/`process`
code unchanged).

Tests: `apps/rag/tests/{enrichment_support,test_article_ingest,test_enrichment_lifecycle,
test_enrichment_validation,test_enrichment_graph,test_openai_enricher,
test_postgres_enrichment}.py`, `apps/worker/tests/test_enrichment_cli.py`;
`test_architecture.py` (+1 test), `test_migration_portability.py` (count 5 -> 6).

Harness/docs: `apps/rag/db-suite.sh` (also runs `apps/worker/tests`, still fails on any
skip), `apps/rag/README.md`, `apps/worker/README.md`, `.omo/plans/eventlens-ai-rag-mvp.md`
(worktree copy: T4 status lines).

## CLI commands (actual)

```bash
export EVENTLENS_RAG_DATABASE_URL=postgresql://.../eventlens_rag
uv run --package eventlens-worker eventlens-worker ingest fixtures/article_bundle.v1.json
uv run --package eventlens-worker eventlens-worker enrich --max-jobs 5
uv run --package eventlens-worker eventlens-worker enrich --provider fake-timeout --max-jobs 5
```

`--max-jobs` 1..25 (default 25). `--provider fake|fake-timeout` only. Exit 0 (incl.
FAILED/RETRY outcomes), 2 invalid bundle/option, 3 `rag_storage_unavailable`, 1
`rag_migration_failed`.

## Fixed policy `enrichment-retry.v1`

| Item | Value |
|---|---|
| total attempts | 3 (every claim counts, incl. lease-expired claims) |
| provider deadline per attempt | 30 s (passed to transport as timeout) |
| lease | 120 s (validated >= 2 x deadline) |
| backoff | 30 s after attempt 1, 120 s after attempt 2 (x4, cap 600 s, no jitter), `next_attempt_at = clock_timestamp() + delay` |
| retryable | `provider_timeout`, `provider_unavailable` only |
| straight to FAILED | malformed_output, unsupported_taxonomy, ungrounded_evidence, ungrounded_metadata, invalid_metadata, provider_refusal, provider_rejected, invalid_stage |
| lease expired at budget | FAILED `lease_expired` |
| jobs per pass | 25 |

## Commands, exit codes, counts

| Command | Exit | Result |
|---|---|---|
| `rtk proxy make check` (1st) | 2 | `biome: command not found` (web deps not installed in new worktree); fixed with `bun install --frozen-lockfile` in `apps/web` |
| `rtk proxy make check` (final) | 0 | ruff format/check clean, basedpyright 0 errors, pytest **494 passed, 49 skipped** (skips = DB tests without URL, by convention), bun test 2 pass, vite build OK |
| `rtk proxy make rag-db-test` (final) | 0 | **386 passed, 0 skipped** on fresh pgvector (apps/rag 239 + apps/worker 147) |
| `rtk proxy make gate-g1` (final) | 2 | Technical checks PASS (exit 2 = technical-only, G1 acceptance remains as recorded); G1 run dirs `.omo/evidence/G1/run-20261016T060307Z-aVTFkG`, `run-20261016T060759Z-q1JHoe`; rebuild 30/30 reused |
| `eventlens-worker validate fixtures/article_bundle.v1.json` | 0 | `Valid article 550e8400-... with 2 media items.` |
| `eventlens-worker process fixtures/article_bundle.v1.json` | 0 | output equals `fixtures/event_pipeline_accept.trace.v1.json` (`worker-process-golden-compare.log`: True) |
| `sh apps/rag/with-test-db.sh sh .../cli-smoke.sh` | 0 | happy + failure CLI flows on isolated DB (below) |
| `git diff --check` | 0 | no whitespace errors (untracked new files scanned separately: none). `rtk git ...` is refused by this agent's worktree-isolation guard, so plain `git` was used |

T4 test files (collected): article_ingest 6, enrichment_lifecycle 28,
enrichment_validation 14, enrichment_graph 24, openai_enricher 16,
postgres_enrichment 21 (real DB), worker test_enrichment_cli 9 (2 real DB), plus 1
architecture test = 119.

CLI smoke (`cli-smoke.log`): ingest -> `job_created: true`; re-ingest -> same job,
`job_created: false`; lexical hit for `công` before enrichment; job `PENDING 0`;
`enrich --max-jobs 5` -> READY, `READY 1 1`, 1 current vector, 1 publication. Second
article with `fake-timeout`: RETRY 30 s (`PENDING 1 provider_timeout 30`), immediate
rerun processes 0, after DB-time shift RETRY 120 s, then FAILED at attempt 3, further
runs process 0, lexical hit for `bão` still present. `--max-jobs 26` exit 2, invalid
bundle exit 2, unreachable DB exit 3 without credentials.

## Requirement -> test mapping

| Requirement | Tests |
|---|---|
| E2E ingest -> lexical -> enrich -> publish (real DB) | `test_postgres_enrichment::test_end_to_end_ingest_lexical_enrich_publish`, `test_enrichment_cli::test_cli_happy_flow_ingest_then_enrich_publishes` |
| Article -> ArticleDocument (immutable, NFC, RAG hash, timestamps, no provenance) | `test_article_ingest::*` |
| Legal/forbidden transitions | `test_enrichment_lifecycle::test_legal_transition_table_*`, `test_every_other_transition_is_forbidden` (15 params); in-memory repo enforces `require_transition` on every write |
| Duplicate delivery | `test_postgres_enrichment::test_duplicate_delivery_*`, `test_enrichment_graph::test_duplicate_delivery_*`, `test_article_ingest::test_ingest_service_*` |
| Concurrent claims (Barrier, 8 threads) -> one owner | `test_postgres_enrichment::test_concurrent_claims_have_exactly_one_valid_owner` |
| A expires, B reclaims + publishes, A late write rejected (Events) | `test_postgres_enrichment::test_expired_owner_a_cannot_write_after_b_reclaims_and_publishes`; unit `test_enrichment_graph::test_expired_owner_*` |
| Lease expiry without reclaim | `test_postgres_enrichment::test_lease_expiry_without_reclaim_rejects_late_owner`; unit equivalent; budget: `test_repeated_lease_expiry_consumes_budget_then_fails` |
| Stale fence rejected for every write | `test_postgres_enrichment::test_stale_fence_rejected_for_every_ownership_write` |
| Retry budget/backoff, schedule survives restart | `test_postgres_enrichment::test_retry_schedule_on_db_clock_survives_restart_then_exhausts`, `test_enrichment_graph::test_transport_retry_*` (injected ManualClock), `test_enrichment_lifecycle::test_policy_*` |
| Only model or only prompt changes -> new identity; worker claims own identity only | `test_postgres_enrichment::test_model_or_prompt_change_is_new_identity_and_reuses_embeddings`, `test_enrichment_graph::test_model_version_change_*`, `test_worker_never_claims_*`, `test_enrichment_lifecycle::test_model_or_prompt_change_*` |
| Metadata-only revision: zero embedding calls, generation reused, history kept | same DB test (embedding calls unchanged, 1 generation, 3 publications); `test_enrichment_graph::test_metadata_only_prompt_revision_*` |
| Correction / tombstone / supersession while processing | `test_postgres_enrichment::test_change_while_processing_blocks_publication[3]`, unit equivalent |
| Malformed output / unsupported taxonomy / evidence outside article, wrong offsets, other hash | `test_enrichment_validation::*`, `test_openai_enricher::test_bad_provider_output_*`, `test_enrichment_graph::test_output_failures_become_failed_without_retry[3]`, DB `test_validation_failure_is_failed_without_retry_and_lexical_survives` |
| Provider-declared identity/status/attempt/timestamps not trusted | `test_enrichment_validation::test_draft_schema_rejects_provider_declared_identity_and_bounds`, `test_openai_enricher` (status/attempt_count in output -> malformed) |
| Prompt injection + redaction/allowlist | `test_enrichment_validation::test_prompt_injection_*`, `test_prompt_context_is_allowlisted_*`, `test_openai_enricher::test_payload_sends_only_allowlisted_fields_*` |
| FAILED keeps lexical | DB retry/validation tests, CLI failure flow |
| Crash between persist and enqueue | `test_postgres_enrichment::test_crash_inside_ingest_rolls_back_both_halves_then_retry_succeeds`, `test_article_persisted_without_job_is_reconciled_after_restart`, unit `test_crash_between_persist_and_enqueue_*` |
| Interrupted stage/publish -> rollback, retry, consistent publication | `test_postgres_enrichment::test_interrupted_stage_or_publish_rolls_back_and_retry_is_consistent[rag_chunks, rag_enrichment_publications]` |
| Cancellation before publication: no further model/DB work, no retry/FAILED write, lease recovery | `test_postgres_enrichment::test_cancel_acknowledged_before_publication_starts_no_more_work`, `test_cancel_committed_before_publish_transaction_wins`; unit `test_cancel_acknowledged_before_publication_stops_all_further_work` (repository call log empty after `request_cancel`) |
| Publication first, later cancel cannot undo | `test_postgres_enrichment::test_publication_committed_first_survives_concurrent_cancel` (cancel thread observed blocked on the row lock via `pg_stat_activity`, then `ALREADY_PUBLISHED`), unit equivalent |
| Real compiled graph, typed errors, state isolation, no checkpointer | `test_enrichment_graph::test_compiled_graph_isolates_invocations_and_has_no_checkpointer`, `test_pass_size_is_bounded_by_policy`, all graph tests run the compiled StateGraph |
| OpenAI adapter with mocked transport, one call, typed errors | `test_openai_enricher::*` (16) |
| Worker CLI | `apps/worker/tests/test_enrichment_cli.py` (9) + CLI smoke |
| validate/process regression | existing `apps/worker/tests/*` (138, unchanged) + golden compare |
| Layering | `test_architecture::*` (no langgraph/psycopg/typer/adapters/graph in domain/application/ports) |
| Upgrade from G1 schema | `test_postgres_enrichment::test_upgrade_from_g1_schema_keeps_rows_and_adds_lifecycle`; fresh DB: every DB test; wheel: `test_migration_portability` |
| History append-only | `test_postgres_enrichment::test_publication_history_is_append_only` |

## Legacy DB

The Compose `eventlens` database was never contacted: no Compose Postgres was running
(`docker-ps.log` shows only the temporary dev container, since removed), and every DB
command used isolated `pgvector` containers with database `rag_test` (or per-test
schemas inside them).

## Remaining limits / risks

- Self-check only; needs independent review (F1-F4 style) and owner acceptance.
- No live provider: no HTTP transport ships; the OpenAI-compatible adapter is proven
  only against a mocked transport. Provider activation policy (D31/D4) is T6/owner.
- Validation is structural/linkage only; semantic entailment and model quality are T9.
- Tombstones mark jobs STALE lazily (next claim sweep of that identity); publication is
  blocked immediately by the current-article check.
- "Latest enqueued identity wins": running two workers with different identities on
  the same articles makes them supersede each other; one configured identity is
  expected.
- T3's `enqueue_job` still deletes the per-job current row in `rag_enrichment_versions`
  on STALE -> PENDING requeue (accepted T3 behavior, test-covered); full history is
  kept in the new append-only `rag_enrichment_publications`.
- Cancellation is in-process (`CancellationScope`) plus the DB flag; there is no
  SIGTERM handler, so an interrupted process relies on lease recovery.
- An unexpected exception (e.g. DB error mid-stage) propagates out of `enrich`; the job
  keeps its lease and is recovered after expiry, consuming one attempt.
- `.omo/evidence/` is git-ignored; this file and the log folder need `git add -f` if
  they are ever committed.
- The pre-existing T3 test `test_lexical_first_then_hnsw_query_exposes_index_metadata`
  assumes a fresh database (it failed once on a reused dev DB because a previous run
  left an identical vector); it passes on the harness's fresh containers.

## Review fixes (fix1, 2026-10-16)

Independent review `.omo/evidence/T4-review-20261016-codex/review.md` (REQUEST
CHANGES, R1-R4) was read and left unmodified. Fix run logs:
`.omo/evidence/task-4-logs/run-20261016T062312Z-fix1/` (numbered logs, one per
attempt, `exit-codes.tsv`, `FAILURES.md` explaining every failed attempt). The earlier
run's final `make check` failure (06:06:54, exit 2) was a `ruff format --check`
failure on `application/enrichment.py` (one blank line), fixed by `ruff format`; its
log was overwritten by the wrapper used then, which is why the new wrapper never
reuses a log name.

Regression tests: `apps/rag/tests/test_review_fixes.py` (12 tests; DB cases on real
pgvector in fresh schemas; interleavings use Events plus a connection gate that pauses
the reconciler thread right before its article row lock, no sleeps). Before fix:
`01-before-fix-regression.log` 10 failed / 2 passed (the 2 are guards). After fix:
`02-after-fix-regression.log` 12 passed.

| Finding | Fix | Regression test(s) | Before -> after |
|---|---|---|---|
| R1 P1 obsolete reconciliation candidate strands current revision | `postgres_enrichment._lock_current` locks `rag_article_current` FOR UPDATE and re-reads the current hash before any enqueue/supersession; `enqueue_missing` enqueues the revision current under the lock (never the candidate), never requeues STALE jobs; supersession only after this transaction actually inserted/requeued the verified current job | `test_r1_obsolete_reconciliation_candidate_cannot_strand_current_revision`, `test_r1_reconciliation_enqueues_the_current_revision_not_the_candidate` | FAIL (B STALE / A enqueued) -> PASS |
| R2 P1 concurrent enqueue resets budget/backoff | `_enqueue_locked`: lock first, then `INSERT ... ON CONFLICT DO NOTHING`; on conflict `SELECT ... FOR UPDATE` and only a STALE->PENDING requeue (explicit ingest) resets budget; duplicate delivery never touches attempts/failure_code/next_attempt_at/fence. No pre-lock snapshot is used | `test_r2_concurrent_reconciliation_never_resets_budget_or_backoff`, `test_r2_duplicate_ingest_preserves_scheduled_retry` (guard) | FAIL `(0, None, False)` -> PASS `(1, provider_timeout, True)` |
| R3 P1 work after acknowledged cancel | `CancellationScope.admit()` / `serialized()` share the lock `cancel()` holds: reconcile, claim(+bind), load, provider call, generation lookup, each embedding call, stage write, publish and retry/FAILED writes are admitted individually (`_AdmittedStaging`, `_AdmittedEmbeddings` wrap the T3 ports inside `EnsureGeneration`); writes are serialized so they commit before a concurrent ack; publication keeps the DB row-lock ordering; a pre-cancelled run does no reconcile/claim | `test_r3_cancel_acknowledged_inside_stage_starts_no_embedding_or_rows`, `test_r3_already_cancelled_run_does_no_reconcile_or_claim`; existing both-order tests still pass | FAIL (1 embedding, 1 generation/chunk; reconciled 1) -> PASS (0 embeddings, 0 rows, reconciled 0) |
| R4 P2 casefold offsets | fake enricher matches with `re.search(re.escape(kw), content, re.IGNORECASE)` on the original text (length-preserving), offsets are code points of `normalized_content` | `test_r4_fake_offsets_are_code_points_of_the_original_text` (ß, ﬁ/ﬃ ligatures, İ, ǅ, combining dot), `test_r4_valid_unicode_article_publishes_on_real_db` | FAIL (excerpt `arbor`, FAILED ungrounded_evidence) -> PASS (READY, excerpt `harbor` [7,13)) |
| Observation: README password | `apps/worker/README.md` example now uses `eventlens-local` (matches Makefile) | n/a | fixed |

No contract changed; no new migration (001-006 untouched).

| Command (fix1) | Log | Exit | Result |
|---|---|---|---|
| regression before fix | `01-before-fix-regression.log` | 1 | 10 failed / 2 passed (expected) |
| regression after fix | `02-after-fix-regression.log` | 0 | 12 passed |
| `rtk proxy make check` | `03-make-check.log` | 0 | ruff/format clean, basedpyright 0 errors, pytest 499 passed / 56 skipped (DB tests without URL, by convention), web test+build OK |
| `rtk proxy make rag-db-test` | `04-rag-db-test.log` | 0 | 398 passed, 0 skipped (fresh pgvector) |
| `rtk proxy make gate-g1` | `05-gate-g1.log` | 2 | technical PASS (exit 2 = technical-only); G1 run dir `.omo/evidence/G1/run-20261016T062827Z-GPO2V2` |
| worker validate / process | `06-*.log`, `07-*.log`, `08-*compare.log` | 0 / 0 | unchanged; process output equals golden trace |
| CLI smoke (happy, timeout failure, Unicode R4, typed errors) | `09-cli-smoke.log` (script `cli-smoke.sh`) | 0 | READY; RETRY 30 s -> 120 s -> FAILED with lexical kept; `Straße harbor` READY with excerpt `harbor` [7,13); exits 2/2/3 |
| `git diff --check` | `exit-codes.tsv` | 0 | clean |

The reviewer's `reproduce.py` was not rerun: it asserts the defects and patches the
removed private `_enqueue`, so it cannot pass by design; its probes are covered by the
regression tests above.

Remaining limits after fix1: an operation admitted before cancellation (for example
an in-flight provider or embedding call, or the publish transaction already holding
the job row lock) finishes; its result is discarded unless publication commits first.
Reconciliation holds article row locks for up to `max_jobs_per_pass` articles in one
transaction. Other limits listed above are unchanged. Status: technical PASS
(self-check after review fixes); T4 acceptance OPEN pending re-review; G2 not
accepted.
