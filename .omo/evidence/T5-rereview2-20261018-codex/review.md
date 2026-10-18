# T5 independent re-review 2 — 2026-10-18

**Verdict: REQUEST CHANGES — R5/R6 are CLOSED; one P2 policy-contract issue (R7)
remains. The bounded two-phase lexical design is acceptable for fixture/fake
scope, but it must be frozen under a new policy version before T5 approval.**

Reviewer: Codex, independent of the implementation/fix agent.
Worktree: `.claude/worktrees/rag-retrieval-t5`; branch `feature/rag-retrieval-t5`.
Parent HEAD: `8167f2068dfaae7b7b40b05b2f45578036f8bb3d`, T5 changes uncommitted.
Scope: R5/R6 fixes, consequences for R4's intermediate-pool bounds, policy
versioning, affected runtime behavior and existing fixture/fake boundaries.

No implementation, policy, plan, earlier review or implementer report was changed
by this review. Only new independent evidence was written. No commit or push.

## R7 [P2] Version the changed intermediate-pool contract; chunk_pool is now unused

Locations:

- `apps/rag/src/eventlens_rag/policies/retrieval-policy.v2.json:82`
  (`chunk_pool: 150`).
- `apps/rag/src/eventlens_rag/application/retrieve.py:441`
  (the store limits no longer carry that cap).
- `apps/rag/src/eventlens_rag/adapters/postgres_retrieval.py:164`
  (the tie-preserving metadata pool has no chunk-pool limit).

The previously frozen v2 lexical pool limited all returned chunk rows to 150,
with at most three per article before the rows left PostgreSQL. The R6 fix now
returns the equal-score boundary superset as metadata, ranks it in Python, then
fetches text only for the selected chunks. This fixes the tie-break, but changes
the storage/application intermediate bound and adds a statement to the lexical
path. `chunk_pool` is retained in the JSON/model and validation, yet is no longer
passed to storage or used to select/limit candidates. Describing it afterward as
a text-row limit does not preserve the original frozen policy semantics.

Independent real-DB worst-case probe: load 51 equally ranked articles with 41
windows each, match the query in the title so all scanned windows tie. The
50-article/40-window policy caps select 50 articles and return exactly:

- **2,000 metadata candidate rows** from the first lexical statement;
- **50 text rows** from the second lexical statement;
- **3 total storage statements** for lexical-only retrieval including the final
  batch check;
- `lexical_truncated_articles=50`, with no widening beyond the 40-window scan.

The code is bounded by the product of article and scan caps; this is not a return
to R4's unbounded materialization. However, 2,000 exceeds the frozen 150-row pool,
and changing the unused `chunk_pool` setting cannot control this behavior. Old
and new runtime/evaluation evidence must not share a policy ID while their
intermediate-resource contracts differ. D35 requires a versioned frozen policy
and records that policy revisions supersede prior acceptance evidence.

### Reviewer decision and minimal resolution

**Keep the bounded metadata-first / selected-text design, and introduce
`retrieval-policy.v3`. Do not require migration 007 for this fixture/fake task.**
This is a technical design recommendation, not owner/G2 acceptance or approval
for live providers. Adding a stored sort key or extension would be an alternative
if preserving the old 150-row contract were required, but it is unnecessary to
accept a clearly versioned bounded two-phase design.

Before approval:

1. Keep v1/v2 byte-identical as history. Freeze v3 with the actual selection
   stages and distinct bounds: 50 articles, 40 scanned windows/article, at most
   2,000 metadata candidates, at most three kept chunks/article and 50 selected
   text chunks. Preserve RRF, tie-breaks, rerank and final article/token limits.
2. Remove or explicitly replace the unused `chunk_pool` setting with meaningful
   stage-specific limits. The declared bounds must agree with the values passed
   to storage and enforced by runtime logic, including the article/scan product.
   Do not silently cut off a tie group before the frozen chunk-ID selection.
3. Add real-DB worst-case coverage for 50 x 40 metadata rows, the 51st article,
   the 41st window, text-row count <= 50, and constant statement counts. The
   independent probe here provides a reproducer/reference scenario.
4. Load/report v3 at runtime, update README/plan/evidence, rerun affected checks
   and smoke/fixture measurements with v3 recorded in their provenance. Retain
   the earlier v2 evidence as historical rather than relabeling its logs.

No new live-provider evaluation or ANN study is needed to resolve R7. The existing
ANN/visibility-measurement limitations remain OPEN independently.

## R5/R6 disposition

| Finding | Independent result |
| --- | --- |
| R5 empty return authorization | CLOSED. `_empty` now performs the final policy check. Two independent Event-barrier cases pause a real metadata read or zero-hit lexical read, complete revocation and release the read. Both raise `RetrievalAccessDeniedError` and return no coverage/result. The full suite includes the implementer's real-DB regressions. |
| R6 chunk tie-break | CLOSED for ranking correctness. SQL `rank()` keeps boundary ties as a safe superset; within an article the pre-ID ordering depends only on matched count. Python then applies the declared full key and caps, before a batch text fetch. The independent seven-window probe now selects `[0,6,5]` both at ranking and returned-evidence levels, matching the independently computed UUIDv5 order. R7 separately records the changed intermediate contract. |

R1–R3 remain closed in their recorded scenarios; their tests pass in the full DB
suite. R4's original unbounded work is addressed by finite scan caps, while its
v2 150-row expectation no longer holds after the R6 design change. The older
probe's page-size failure is therefore a real policy-bound mismatch, not an
assertion that merely expects the original defect to persist. R7 resolves how to
version and verify this new bounded design without undoing R6.

The implementer's before-fix R5/R6 regression logs show behavioral failures rather
than import/collection errors. This review did not reconstruct the old worktree;
the prior independent report/probe already documents the defects before fixes.
The new independent probe uses the current API and explicitly asserts corrected
R5/R6 behavior, so its results do not depend on interpreting a failed old script.

## Independent verification

| Command/check | Exit | Observed result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 575 Python passed, 103 DB-dependent skipped; lint/format/typecheck/web PASS |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 521 passed, 0 skipped on owned isolated PostgreSQL/pgvector; includes T4 and all T5 regressions |
| New `independent_probes.py` through `with-test-db.sh` | 0 | R5 metadata/zero-hit denial PASS; R6 independent tie/evidence order PASS; 2,000 metadata / 50 text-row mismatch to v2 reproduced |
| `eventlens-rag retrieval-smoke --output .../retrieval-smoke.json` through the isolated harness | 0 | Five cases under current v2, with raw result/provenance saved separately |
| Migration directories vs HEAD | 0 | Empty diff, SQL 001–006 unchanged |
| v1/v2 policy and shared harness vs previous review manifest | 0 | Byte-identical v1/v2, `conftest.py`, `with-test-db.sh`, `db-suite.sh` |
| `rtk proxy git diff --check` | 0 | Clean |
| Final source fingerprint comparison | 0 | No reviewed source changed during verification (`source-check-after.json`) |
| Owned test-container inventory | 0 | No `eventlens-rag-test-*` container remains |

Commands/exits, raw logs and source hashes are in this directory. The new probe
script's exit 0 means corrected R5/R6 assertions and the explicitly described v2
pool-mismatch reproduction all held; it does not mean T5 acceptance PASS.

Reproduce from the T5 worktree:

```sh
rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/T5-rereview2-20261018-codex/independent_probes.py
```

The vector benchmark and aggregate G1 gate were not repeated: no vector SQL,
migration/schema or shared harness change was part of this fix. The full DB suite
does rerun vector mechanism and old regressions. G1 acceptance is unchanged.

## Scope and review status

- F1 policy compliance: REQUEST CHANGES until R7 is resolved under a genuine new
  frozen version. F2/F3: existing checks and R5/R6 logic PASS; the policy-bound
  discrepancy remains observable. F4 scope fidelity: PASS for fixture/fake T5.
- The 40-window long-article cutoff, full-document final reads, fixture token
  counter/access, CJK substring limitations, disabled ANN and missing separate
  visibility-selectivity measurement remain documented limits. No semantic-quality
  or production latency guarantee is inferred.
- No T6/T7, event writer, live provider/reranker, legacy DB operation or deployment
  was introduced by this review.

**T5 and G2 remain OPEN.** Resolve/version the one remaining policy issue, preserve
these reports and logs, then request final scoped re-review before commit.
