# T5 independent review — 2026-10-18

**Verdict: REQUEST CHANGES. T5 is not approved; G2 remains OPEN.**

Reviewer: Codex, independent of the T5 implementation/self-check agent.
Worktree: `.claude/worktrees/rag-retrieval-t5`.
Branch: `feature/rag-retrieval-t5`; parent HEAD
`8167f2068dfaae7b7b40b05b2f45578036f8bb3d`; implementation uncommitted.
Scope: the T5 request at the main worktree's `docs/tasks/t5-hybrid-retrieval.md`,
T5/D35/D43 plan, implementation, tests and recorded evidence, fixture/fake only.

The existing automated suite and smoke pass independently. Four additional
probes reproduce defects outside that suite. Test success does not close them.
No implementation, policy, plan or implementer report was edited by this review.

## Findings

### R1 [P1] Recheck authorization after the final batch fetch

Location: `apps/rag/src/eventlens_rag/application/retrieve.py:545`
(`_final_check`, especially the subsequent `current_articles` call at line 551).

Authorization is checked before the final database fetch. After that fetch, only
cancellation/deadline is checked; assembly/return never checks access again. If
the fixture policy is revoked while this last fetch is in flight, the request
still returns evidence despite the completed revocation.

Independent probe uses the real Postgres store and an Event barrier around return
from its final SQL read: start retrieval, wait for batch read, revoke the injected
fixture policy, release the read. Actual result: `status=evidence`, one article,
`access.revoked=true`, only two authorization checks. Expected:
`RetrievalAccessDeniedError`, no returned evidence. No sleep or mocked SQL result
is used. The existing test revokes during rerank, before this window.

Recheck the current policy after the fetch and at the response/assembly boundary;
define the fixture authorization boundary explicitly. Add this interleaving as a
regression, while keeping batched reads and the current fixture-only scope. This
finding does not claim production multi-tenant authorization has been enabled.

### R2 [P2] Malformed reranker output can escape instead of degrading

Locations: `apps/rag/src/eventlens_rag/domain/fusion.py:324` and
`apps/rag/src/eventlens_rag/application/retrieve.py:528`.

The validator immediately unpacks each score row into two values; the caller
accesses `slot.value.scores` without validating its outer shape. These operations
run outside the exception handling used for the reranker call. A returned
`RerankOutput` containing a three-element score row raises a raw `ValueError`
(`too many values to unpack`) and aborts the query. The port explicitly describes
adapters as untrusted and output as runtime-validated; Python dataclass annotations
do not enforce this structure.

Independent probe returns `(known_candidate_id, 1.0, 2.0)` in one score row while
all database reads remain real. Actual: uncaught `ValueError`. Expected: preserve
fused order and return `rerank_status=degraded`, reason `invalid_output`.

Validate the complete output shape and numeric conversion safely before consuming
it. Test malformed rows, wrong containers/outer output and unsafe numeric values,
in addition to the current unknown/duplicate/missing-ID tests. Keep cancellation,
end-to-end deadline and unrelated storage failures outside degradation handling.

### R3 [P2] Query casefold does not match the persisted PostgreSQL lexemes

Locations: `apps/rag/src/eventlens_rag/domain/retrieval.py:617` and
`apps/rag/src/eventlens_rag/adapters/postgres_retrieval.py:300`.

`query_terms` casefolds Unicode before passing terms to `to_tsquery('simple', ...)`.
The persisted generated `search_vector` uses PostgreSQL's `simple` normalization,
which does not perform the same casefold expansions. For a current article
containing `Straße`, searching for the identical word becomes `strasse` and the
FTS prefilter discards the article before Python's compatible chunk scoring.

Independent real-DB probe: persist `Straße reopened today.` with a neutral title,
then query `Straße` with the vector branch disabled. Direct PostgreSQL
`search_vector @@ plainto_tsquery('simple', 'Straße')` finds one article; the T5
service returns `no_matches`, zero articles. This affects literal lexical fallback
without requiring semantic embeddings or CJK substring support.

Unify the query/index token normalization (or preserve suitable original lexemes
for FTS), with a documented policy version and regression tests for Unicode
expansions. Cover representative punctuation/date token differences too; do not
solve the failure by widening explicit filters or relying on vector results.

### R4 [P2] Lexical intermediate chunk work is unbounded by the policy caps

Location: `apps/rag/src/eventlens_rag/domain/fusion.py:120` (append at line 125,
full sort at line 79); related rechunking in `application/retrieve.py:586`.

The database limits the number of article rows, but each article can have arbitrary
content length. `chunk_document` materializes every chunk; `rank_lexical` scores
and stores all matches, and `_assign_ranks` sorts the full collection before
applying the per-article/depth limits. The final check rechunks the full document
again. A depth of 50 and three output chunks per article therefore do not bound
the intermediate chunk pool, allocation or CPU work.

Independent real-DB probe with one valid 312,000-code-point article observes 312
scored intermediate chunks, despite depth 50 and only two returned spans after
dedup. Larger accepted article bodies grow this count without a policy bound.
The probe wraps the rank-assignment entry only to count its real inputs; it does
not replace ranking results or SQL. The T5 request explicitly requires bounded
intermediate pools/chunk work, not only final article/token caps.

Freeze enforceable document/chunk scan limits or another bounded selection path
before full materialization/sort, and avoid rebuilding all chunks just to validate
selected offsets. Define honest partial/budget-exceeded behavior if a limit is
hit; test exact/one-over limits and cancellation/deadline within long scans.
Preserve deterministic ranking and lexical availability within the stated policy.

## Independent verification

| Check | Exit | Observed result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 556 Python passed, 85 DB-dependent skipped; lint/format/typecheck/web checks pass |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 484 passed, 0 skipped, isolated PostgreSQL/pgvector; includes T4 review regressions and T5 tests |
| `independent_probes.py` through `apps/rag/with-test-db.sh` | 0 | All four defect probes above reproduced against real PG reads; exit 0 means reproduction succeeded, not acceptance |
| `eventlens-rag retrieval-smoke --output .../retrieval-smoke.json` through the same isolated harness | 0 | All five named scenarios exercised; results and fixture-mode metrics saved |
| `rtk proxy git diff --check` | 0 | Clean |
| Diff of both migration directories against HEAD | 0 | Empty; migrations 001–006 unchanged |
| Source manifest recheck | 0 | No file changed from the review-start fingerprint |
| Owned test-container inventory after verification | 0 | No `eventlens-rag-test-*` container remains |

Commands/exits are saved in `*.exit.json`; raw logs use separate filenames in
this directory. `source-sha256.json` records the reviewed source snapshot;
`source-check-after.json` records the final comparison. The evidence directory is
ignored by git and has not been staged/committed.

To reproduce the independent probes from this worktree:

```sh
rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/T5-review-20261018-codex/independent_probes.py
```

These assertions describe current defects. Convert the scenarios into regression
tests for the corrected behavior; the defect-asserting script is not a passing
acceptance suite after fixes.

The full 3,000-article/20-query benchmark and aggregate `gate-g1` were not repeated
in this review. The independent DB suite does rerun the exact-vs-ANN mechanism
tests on its smaller synthetic corpus, including brute-force exact correctness
and HNSW-plan checks. Existing benchmark/gate logs remain implementer evidence.
Gate exit 2 retains its existing technical-only meaning; G1 acceptance is unchanged.

## Scope and remaining measurement items

- Keeping ANN disabled is consistent with its reported poor synthetic recall.
  Exact cosine is the active fixture route. ANN routing/quality thresholds remain
  OPEN; neither the synthetic report nor the fake fixture scores approve live
  semantic quality. No real embedding/provider activation is required to fix R1–R4.
- The vector benchmark covers source/time selectivity; visibility is tested for
  correctness, but a separate visibility-selectivity benchmark remains unmeasured.
  Do not label the full routing/measurement requirement complete.
- The fake token counter and fixture access scope are explicit baseline limits;
  T6 must reuse the TokenCounter boundary and supply D32 model input accounting.
- The CJK substring limitation remains documented. R3 is a distinct literal-word
  false negative reproduced for an otherwise supported current lexical article.
- No T6/T7 synthesis/API, real reranker/provider, event writer, legacy DB mutation,
  commit, push or deployment was performed by this review.

## F1–F4 disposition

| Review | Disposition |
| --- | --- |
| F1 Plan/requirements | REQUEST CHANGES: final access boundary, malformed-output degradation, consistent lexical normalization and bounded intermediate work are incomplete; ANN measurement item stays OPEN |
| F2 Code quality | REQUEST CHANGES: R1–R4 need implementation fixes and regressions; current lint/typecheck passes |
| F3 Runtime QA | Existing suite/smoke PASS, but independent defect probes reproduce all four findings; not acceptance PASS |
| F4 Scope fidelity | PASS for observed fixture/fake T5 scope; no prohibited subsystem or live traffic introduced |

Next: fix R1–R4 with regressions, run affected tests plus standard verification,
update implementation evidence without editing this report, then request an
independent re-review. Leave T5/G2 OPEN and do not commit on the basis of this
REQUEST CHANGES report.
