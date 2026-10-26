# T4 independent re-review — 2026-10-16

Reviewer: Codex, independent of the Claude implementation/fix agent.
Worktree: `.claude/worktrees/agent-a821d85b56097a3d5`.
Branch: `feature/rag-enrichment-t4`; baseline HEAD `b1a3900`, T4 changes uncommitted.
Scope: fixes for R1–R4 from `../T4-review-20261016-codex/review.md`, affected
runtime behavior, regression coverage, and unchanged T4/G1 boundaries.

**Verdict: APPROVE technical T4 implementation in fixture/fake scope.**
**R1, R2, R3 and R4 are CLOSED. No new material finding in this re-review.**

The earlier REQUEST CHANGES report remains historical evidence; this report
supersedes its open technical findings for the recorded source snapshot. Owner
acceptance of T4 is separate; G2 remains incomplete until T5–T7. This review does
not authorize commit, push, live providers or deployment.

## Findings disposition

| Finding | Disposition and verification |
| --- | --- |
| R1 — obsolete reconciliation strands current article | CLOSED. Reconciliation takes the current article row lock, re-reads its hash and visibility, then enqueues the revision current under that lock. Tests cover a correction with a new job already present and a correction persisted without a job. Current revision remains claimable; no obsolete candidate job is inserted. |
| R2 — retry budget/backoff reset | CLOSED. Under the article lock, INSERT ON CONFLICT DO NOTHING distinguishes creation from duplicates. The conflict path locks the existing job; initialization/requeue is tied to the transition actually performed. Reconciliation never requeues STALE. Duplicate delivery preserves attempts, failure code, next_attempt_at and fence. Concurrent retry and duplicate-delivery regressions both pass. |
| R3 — post-cancellation work | CLOSED. Scope admission checks cover lookup/provider/each embedding; writes, reconciliation and claim/bind are serialized against cancellation acknowledgement. Pre-cancelled runs do not reconcile or claim. The original mid-stage failure regression now creates zero embeddings/generations/chunks/publications. Additional independent multi-chunk test confirms an in-flight first embedding may finish, but no subsequent embedding or stage transaction occurs. Existing publication-first/cancel-first tests pass in the full DB suite. |
| R4 — casefold offsets | CLOSED. Keyword matches now use the original normalized string with case-insensitive regex search and original code-point match positions. Unicode expansion cases pass; real-DB ingest/enrich of `Straße harbor` becomes READY with excerpt `harbor` at [7,13). |

Cancellation boundary: work admitted before cancellation may complete; its result
does not admit further work after cancellation. Writes already serialized ahead
of cancellation commit before acknowledgement. Publication retains the separate
DB-row-lock ordering (commit first remains valid). This review does not claim
remote/in-flight calls can be forcibly aborted.

## Independent commands and new results

| Command/check | Exit | Result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | Ruff/typecheck/web tests/build PASS; 499 Python passed, 56 DB-dependent skipped |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 398 passed, 0 skipped on real PostgreSQL/pgvector in an owned ephemeral container |
| `rtk proxy env -u VIRTUAL_ENV sh apps/rag/with-test-db.sh uv run --package eventlens-rag pytest apps/rag/tests/test_review_fixes.py -v` | 0 | 12/12 regression tests pass; DB cases use real pgvector |
| Independent `inflight_cancellation.py` through the same isolated harness | 0 | Four chunks available; one embedding started before cancel; ACKNOWLEDGED; no next call, zero generation/chunk/publication rows |
| `rtk proxy git diff --check` | 0 | Clean |
| `docker ps -a --filter name=eventlens-rag-test-` | 0 | No owned test containers remain |

Raw evidence: `make-check.log`, `rag-db-test.log`, `review-fixes.log`,
`inflight-cancellation.log`. Additional reproducer: `inflight_cancellation.py`.
Interleavings use Events, not sleeps. No implementation result values are mocked
in the DB checks. The old defect-asserting `reproduce.py` was not reused because
its assertions expect failures and its private enqueue hook was removed.

The aggregate `make gate-g1` was not rerun by this reviewer during this narrow
follow-up; the implementation's successful technical gate result remains its own
evidence. The full independent DB suite includes the regression/publication tests.

## Other observations and scope

- Worker README connection password now matches Compose/Makefile (`eventlens-local`).
- Fix-run logs have distinct names and a FAILURES.md. The explanation for the
  earlier overwritten ruff failure remains an implementer account, not a recovered
  original log; the independent current make-check result above is directly observed.
- Changed sources relative to the original review are the plan/README status,
  enqueue repository, cancellation application/runner, fake enricher, and the new
  regression test. Contracts and migration SQL 001–006 were not changed by fixes.
- Fixture/fake remains the runtime boundary. No shipping live HTTP transport or
  semantic-quality evidence is claimed; those remain deferred as planned.
- Lazy tombstone job-state updates, single configured identity, SIGTERM/lease
  recovery and bounded multi-article reconciliation locks remain documented limits.
- No main-repo WIP, existing review reports, branch history or legacy DB was changed.
  This reviewer only added evidence and ran isolated verification.

## F1–F4 update (T4 only)

| Review | Verdict |
| --- | --- |
| F1 Plan compliance | PASS in implemented T4 fixture/fake scope; R1–R4 requirements verified |
| F2 Code quality | PASS; open technical findings closed, automated checks pass |
| F3 Runtime QA | PASS; real-DB suite, all 12 regressions and independent in-flight cancellation check |
| F4 Scope fidelity | PASS; no T5–T15 implementation, live provider, broker, canonical event writer or legacy DB operation introduced |

T4 is technically ready for the owner's scoped acceptance and an explicitly
authorized commit. G1 acceptance is unchanged. G2 is not accepted by this report.
