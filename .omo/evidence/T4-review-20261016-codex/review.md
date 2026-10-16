# T4 independent review — 2026-10-16

Reviewer: Codex, independent of the Claude implementation agent.
Worktree: `.claude/worktrees/agent-a821d85b56097a3d5`.
Branch: `feature/rag-enrichment-t4`; HEAD/baseline `b1a3900` plus uncommitted T4 work.
Scope: T4 implementation, tests, migration 006, worker composition/CLI, and evidence.

**Verdict: REQUEST CHANGES. T4 acceptance remains OPEN. G2 is not accepted.**

Independent review is now performed; the blocker is reproducible implementation
defects, not the absence of a reviewer. Existing suite success does not cover the
interleavings below. No product code, plan, baseline acceptance, original logs,
persistent/legacy database, branch history, or main-repo WIP was changed.

## R1 — P1: Stale reconciliation can invalidate the current article's job

Location: `apps/rag/src/eventlens_rag/adapters/postgres_enrichment.py:96-103`,
with the unlocked candidate selection in `enqueue_missing` at lines 135-153.

`enqueue_missing` reads (article_id, old content_hash), then later `_enqueue`
creates that job without rechecking the current revision under the article lock.
Its supersession UPDATE marks every other PENDING/PROCESSING job for the article
STALE, even when the new job belongs to an obsolete revision.

Real-DB reproduction (same identity, no multi-identity configuration):

1. Persist revision A with no enrichment job.
2. Reconciler selects A; pause before its `_enqueue` call.
3. Ingest corrected revision B and create B's PENDING job.
4. Resume reconciliation: it creates A's obsolete job and marks B's job STALE.
5. `claim_next` marks A STALE and returns None. Another `enqueue_missing` returns
   zero because B's STALE job already exists. Both jobs remain STALE while B is
   the current lexical article.

The current article no longer enriches through repeated worker passes; manual
re-ingest is needed to revive it. This violates recovery/current-content behavior.

Fix: acquire the current article lock and revalidate content hash/tombstone before
enqueue and supersession. An obsolete reconciliation candidate must not supersede
a current job. Ensure reconciliation can recover an eligible current revision
when appropriate, without reviving genuinely superseded identities.

Probe: `stale_reconciliation` in `reproduce.py`, `R1a_REPRODUCED...` log record.

## R2 — P1: Concurrent reconciliation resets consumed attempts and backoff

Location: `apps/rag/src/eventlens_rag/adapters/postgres_enrichment.py:68-94`.

`prior` is read before `enqueue_revision_job` obtains the article row lock. It
can remain None even after another transaction creates the job and the worker
already consumes an attempt. The later reset uses the stale Python value and
only checks that the job is PENDING, so it resets a legitimate scheduled retry.

Real-DB reproduction:

1. Reconciler reads `prior=None`; pause before the enqueue primitive/row lock.
2. Another transaction ingests the same article/key, claims attempt 1, and schedules
   provider_timeout retry 30 seconds in the future.
3. Resume the reconciler. Before: `(attempt_count=1, provider_timeout, future=True)`.
   After: `(attempt_count=0, failure_code=NULL, future=False)`.

Duplicate concurrent reconciliation therefore erases consumed budget and bypasses
backoff. This is possible with the single configured identity used by the CLI.

Fix: determine insert/requeue/no-op atomically under the serialization lock and
initialize/reset budget only for the transition actually performed by that
transaction. Do not infer it from a pre-lock status snapshot. Preserve claim
generation, attempts, failure code and schedule on duplicate delivery.

Probe: `reset_retry_budget`, `R1b_REPRODUCED_retry_budget_reset` log record.

## R3 — P1: Acknowledged cancellation does not stop subsequent stage work

Location: `apps/rag/src/eventlens_rag/application/enrichment.py:179-186` and
`apps/rag/src/eventlens_rag/graph/enrichment.py:275-277`.

`stage` checks cancellation once before `EnsureGeneration.execute`. That service
then performs separate find_complete, embedding and staging operations with no
cancellation admission check. After find_complete returns from its DB call,
cancellation can be acknowledged and the invocation still starts embedding and
the stage transaction.

Real-DB reproduction using Events, no sleeps:

1. Pause the staging adapter after the real find_complete call, before returning None.
2. `CancellationScope.cancel()` commits and returns `ACKNOWLEDGED`.
3. Release staging. One embedding call and a new stage transaction start after ack;
   one generation and one chunk are committed. The final outcome is CANCELLED and
   publication count remains zero.

Publication fencing itself holds; the violated requirement is no new model/DB
work after cancellation acknowledgement. Additionally a scope cancelled before
`runner.run` still executes reconciliation and creates a job, because reconcile
is called before checking stopped.

Fix: serialize cancellation acknowledgement against admission of each external
operation, propagate cancellation into generation lookup/embedding/staging, and
check before reconcile/claim. Preserve both existing cancellation/publication
commit orders. Add tests for cancellation inside a multi-operation step and for
an already-cancelled invocation, not just immediately before publish.

Probes: `cancellation_during_stage`, `already_cancelled_run`; both reproduced on
real PostgreSQL/pgvector.

## R4 — P2: Casefold offsets are applied to the original Unicode string

Location: `apps/rag/src/eventlens_rag/adapters/fake_enricher.py:45-56`.

Unicode `casefold()` can change string length. The fake searches the folded
string but uses those positions against normalized_content. For valid NFC text
`Straße harbor` (13 code points), folded text has 14 code points. The fake emits
the harbor span [8,14), which exceeds the original text. Full ingest/enrich on
the real DB ends FAILED/ungrounded_evidence for this otherwise valid article.
With more trailing text, the excerpt can instead point to the wrong substring.

Fix: locate matches on the original string (for example a position-preserving
case-insensitive search) or maintain a folded-to-original offset map. Test
casefold expansions preceding keywords and verify both outcome and exact excerpt.

Probe: `unicode_offsets`, `R3_REPRODUCED_valid_unicode_article_fails` log record.

## Additional observations

- Original `exit-codes.tsv` records final-make-check exit 2 at 06:06:54 and exit 0
  at 06:07:23, but only a success `final-make-check.log` is retained. The earlier
  failure cause cannot be reconstructed from that overwritten log. Preserve a
  unique log per attempt. The independent fresh run below passes, so this is an
  evidence limitation rather than a currently reproduced make-check failure.
- `apps/worker/README.md` uses `eventlens:eventlens` in its new connection example;
  Compose/Makefile use password `eventlens-local`. Align the example so the
  documented local command works.
- An injected mocked OpenAI transport is within the requested T4 scope; absence
  of a shipping live HTTP transport is not a defect for this fixture/fake gate.
- SIGTERM crash recovery via lease, lazy stale marking after tombstone while
  publication is immediately blocked, and the documented identity selection are
  not the root causes of R1/R2. Both races reproduce with one identity.
- The reused-dev-DB T3 test issue is not reproduced by the supported fresh-container
  harness in this review. No existing T3/D41 acceptance decision is reopened.

## Independent verification

| Command | Exit | New result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | Ruff/typecheck/web checks/build PASS; 494 Python passed, 49 DB-dependent skipped |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 386 passed, 0 skipped, real pgvector in an owned ephemeral container |
| `rtk proxy env -u VIRTUAL_ENV sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/T4-review-20261016-codex/reproduce.py` | 0 | All five defect probes reproduced on real pgvector; exit 0 means reproductions matched, not implementation acceptance |
| `rtk proxy git diff --check` | 0 | Clean |
| Docker owned-container inventory after completion | 0 | No eventlens-rag-test-* containers left |

Logs: `make-check.log`, `rag-db-test.log`, `reproduce.log`. Reproducer: `reproduce.py`.
Synchronization is by Events/thread boundaries; no sleep-based interleaving.
Patch wrappers only pause production calls; SQL and state transitions remain the
actual PostgreSQL implementation. No result values are faked in these DB probes.
The aggregate gate was not rerun here; its result in the implementation report
remains historical/self-check evidence.

## F1–F4 disposition for T4 only

| Review | Verdict |
| --- | --- |
| F1 Plan compliance | REQUEST CHANGES: current-content recovery, retry budget and cancellation stop-work requirements are violated |
| F2 Code quality | REQUEST CHANGES: R1–R4; automated checks pass |
| F3 Runtime QA | Existing suite PASS; additional real-DB failure probes reproduce R1–R4 |
| F4 Scope fidelity | PASS in reviewed T4 scope: fixture/fake, no live provider, no T5–T15 runtime, no canonical event writer or broker |

Required next step: fix R1–R4, promote the reproductions into regression tests,
rerun affected tests plus required checks, and obtain re-review before claiming T4
ready for owner acceptance or commit. G2 still requires T5–T7.
