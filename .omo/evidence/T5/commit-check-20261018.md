# T5 commit checkpoint — 2026-10-18

Owner instruction: "nếu được rồi thì commit nhé". The condition is met by the
final independent re-review APPROVE, recorded at
`../T5-rereview3-20261018-codex/review.md`. This authorizes the scoped T5 commit;
no push, deployment, live provider/reranker activation or G2 acceptance is inferred.

Branch: `feature/rag-retrieval-t5`.
Parent: `8167f2068dfaae7b7b40b05b2f45578036f8bb3d` (approved T4).
Worktree: `.claude/worktrees/rag-retrieval-t5`.

Scope: lexical + exact-vector retrieval, deterministic RRF/dedup/bounded rerank,
evidence context/token budgets, metadata/current/access guards, cancellation,
versioned v1-v3 policy history, smoke/measurement tools, regressions and evidence.
No T6/T7 implementation or new migration. SQL 001–006 remain unchanged.
Main-worktree WIP and older worktrees remain untouched.

## Review and validation

- Initial review and re-reviews are preserved at `../T5-review-20261018-codex/`,
  `../T5-rereview-20261018-codex/`, `../T5-rereview2-20261018-codex/` and
  `../T5-rereview3-20261018-codex/`. Earlier REQUEST CHANGES verdicts are historical;
  the final re-review closes R1-R7 for the recorded fixture/fake source snapshot.
- Independent `make check`: exit 0; 594 Python passed, 105 DB-dependent skipped;
  lint/format/typecheck/web checks pass. Skips are not DB evidence.
- Independent `make rag-db-test`: exit 0; 542 passed, 0 skipped on isolated real
  PostgreSQL/pgvector, including T4 and all T5 regressions.
- Independent v3 probes: R5 typed denial on metadata/zero-hit races, R6 exact
  UUID tie order, 2,000 metadata/50 text limits, 51st-article/41st-window boundaries,
  and a real-DB cap+1 overflow with no subsequent text fetch all pass.
- Independent smoke: all five cases and provenance use `retrieval-policy.v3`.
- v1/v2 policy files and shared DB harness are byte-identical to the earlier review
  snapshot. Current v3 enforces the new stage-specific caps rather than redefining
  v2's old chunk_pool setting.
- Every reviewed source matched the final manifest after verification. Only
  plan/README/current-evidence status changed afterward; no runtime change or
  additional full-suite repetition was needed for those documentation edits.

## Remaining scope limits

G2 stays OPEN for T6/T7 and its review. ANN is disabled; quality/routing thresholds
and separate visibility-selectivity measurement remain OPEN. Fake embeddings and
the small fixture set prove mechanics, not real semantic quality or production
latency. Long articles are lexically scanned only through their first 40 windows;
truncation is reported. Full-document final reads, CJK substring behavior and the
fixture-only TokenCounter/access boundary remain documented limits.

## Evidence packaging

Readable implementation and independent review reports are staged directly.
`verification-20261018.zip` preserves original raw self-check/fix/review logs,
source fingerprints, probe scripts/diffs, benchmark plans/reports, command exits,
and the associated T5 G1 technical run. Archive paths begin with `.omo/evidence/`;
extract at the repository root to restore the reports' referenced paths. Original
local files remain in place. No failing run or historical review was relabeled.

Archive SHA-256:
`606e22401ff2c45e75317e6868c25761b7a7be82f7c3d0b295710a6f25e45532`.
