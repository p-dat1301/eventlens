# T5 independent re-review — 2026-10-18

**Verdict: REQUEST CHANGES. Original R1–R4 reproductions are fixed; two additional
P2 findings (R5/R6) prevent approval. T5 and G2 remain OPEN.**

Reviewer: Codex, independent of the implementation/fix agent.
Worktree: `.claude/worktrees/rag-retrieval-t5`; branch `feature/rag-retrieval-t5`.
Parent HEAD: `8167f2068dfaae7b7b40b05b2f45578036f8bb3d`, changes uncommitted.
Scope: fixes from the first independent T5 review, affected SQL/application/policy
behavior, fixture/fake scope and preservation of T4/G1 boundaries.

The first review and its scripts remain unchanged. This report records a new
source fingerprint and a new adapted probe script; it does not edit implementation,
the plan, policy or the implementer's evidence. No commit/push/deployment occurred.

## New findings

### R5 [P2] Apply the final access check to empty returns too

Location: `apps/rag/src/eventlens_rag/application/retrieve.py:787`
(`_empty`, reached by the early metadata-filter branch in `execute`).

The new response-boundary authorization check is only in `_finish`. Empty results
from metadata eligibility, no matches or other early branches go directly to
`_empty`, which only checks cancellation/deadline. Consequently, a policy revoked
while the metadata read is in flight still receives an EMPTY result with internal
coverage counts, rather than `RetrievalAccessDeniedError`.

Independent real-DB probe: persist one current article without READY metadata;
query `harbor` with a transportation topic filter; finish the real
`metadata_eligibility` read, revoke the injected fixture access policy before the
read returns to the application. Actual: `status=empty`,
`empty_reason=filter_unavailable`, `coverage.scoped_articles=1`, and only one access
check. Expected: typed access denial, with no returned corpus statistics. The
query is neither cancelled nor expired; the result is not a storage error.

This is a remaining return path outside the fixed R1 final-fetch scenario. The v2
policy already declares `caller_access_before_return`, so implement that boundary
for every result path, including `_empty`; do not replace access denial with an
empty outcome. Add regression tests for both a metadata-filter empty return and
an unfiltered zero-hit return after revocation, keeping constant/batched checks.
The demonstrated disclosure is fixture corpus coverage, not an article-body or
production multi-tenant access claim.

### R6 [P2] Preserve the frozen chunk tie-break before the SQL per-article cut

Location: `apps/rag/src/eventlens_rag/adapters/postgres_retrieval.py:157`
(`row_number` / `article_rank <= per_article`, with related ordering at line 163).

The SQL now selects the three chunks kept per article using `matched DESC,
chunk_index ASC`. The frozen lexical tie-break is `score DESC, document_rank DESC,
source_published_at DESC NULLS LAST, article_id ASC, chunk_id ASC`, and Python's
`rank_lexical` still uses that rule. Within one article with equal matched counts,
all components except `chunk_id` tie; selecting by index discards eligible chunks
before Python can apply the declared tie-break. Re-sorting the surviving three
does not recover the omitted chunks.

Independent real-DB probe uses one seven-chunk article (well below the 40-window
scan cap), a title matching the single query term and distinct chunk content.
All seven chunks have equal lexical relevance/freshness/document rank. The SQL
keeps indices `[0,1,2]`; Python orders that subset `[0,2,1]`. Applying the frozen
tie-break to the same scanned pool before cutting to three instead selects
`[0,6,5]`. Different passages are therefore returned under a policy that claims
the tie-break is unchanged. This is not the documented beyond-window truncation.

Make candidate selection at the per-article cut consistent with the frozen rule,
and add a real-DB equal-score multi-chunk regression (including shuffled insert
order). Retain bounded work/row counts. If a different preselection rule is
intentional, it must be explicit and versioned as a policy behavior change;
do not describe chunk-index selection as the existing chunk-ID tie-break.

## Original findings disposition

| Finding | Disposition and independent checks |
| --- | --- |
| R1 [P1], revoked during final fetch | Original scenario CLOSED. A real batch read paused by Events, followed by completed policy revocation, now raises `RetrievalAccessDeniedError`. Extra post-fetch and nonempty response-boundary checks are present; the new assembly-time regression passes in the full suite. R5 separately covers an unguarded empty return path. |
| R2 [P2], malformed reranker shape | CLOSED. Adapted original three-element-row probe now returns evidence with `degraded/invalid_output`. Whole-shape and safe numeric conversion validation is present. Additional wrong-container/overflow/hostile-float cases and cancel behavior pass in the suite. |
| R3 [P2], Straße lexical false negative | CLOSED. Adapted original real-DB literal query now returns the correct article. Query and chunk matching both use PostgreSQL `simple` lexemes of NFC text; multilingual/date/hyphen cases pass in the DB suite and offsets remain source code points. |
| R4 [P2], unbounded intermediate chunk materialization | Original scenario CLOSED under the explicit v2 scan policy. The 312,000-code-point article returns a three-row lexical pool, `truncated_articles=1`; scan is limited to 40 windows/article and output pool to 150. Final offsets use arithmetic, without rechunking the full document. Exact/one-over tests pass. R6 concerns the order used for the per-article cut, not the existence of these caps. |

The first review's `independent_probes.py` expected defects and imported the v1
API; its import failure after fixes is not behavioral verification. The new
`independent_probes_v2.py` adapts those scenarios to the v2 API and asserts corrected
behavior for all four, then documents R5/R6. The old before-fix regression run
failed during collection, so it is not evidence of individual behavioral failures.
The original independent before-fix probes remain valid evidence of those defects.

## Independent commands and results

| Check | Exit | Observed result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 575 Python passed, 100 DB-dependent skipped; lint/format/typecheck/web checks pass |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 518 passed, 0 skipped on owned isolated PostgreSQL/pgvector; includes the new regressions and T4 regressions |
| Adapted `independent_probes_v2.py` through `with-test-db.sh` | 0 | Original R1–R4 checks PASS; R5/R6 defects both reproduced with real PG reads |
| `eventlens-rag retrieval-smoke --output .../retrieval-smoke.json` through the isolated harness | 0 | Five cases pass under `retrieval-policy.v2`; fixture measurements match the implementer's report |
| `rtk proxy git diff --check` | 0 | Clean |
| Both migration directories vs HEAD | 0 | Empty diff; SQL 001–006 unchanged |
| v1 policy and harness vs original review manifest | 0 | Byte-identical `retrieval-policy.v1.json`, `conftest.py`, `with-test-db.sh`, `db-suite.sh` |
| Source fingerprint after verification | 0 | No file changed from the re-review-start snapshot (`source-check-after.json`) |
| Owned test-container inventory | 0 | No `eventlens-rag-test-*` container remains |

Raw logs, smoke JSON, exact command/exits and source fingerprints are in this
directory. The probe script's exit 0 means its stated assertions, including
defect reproductions, succeeded; it is not acceptance PASS. After fixes, convert
the two new scenarios to regressions for the corrected behavior.

Reproduce from this worktree:

```sh
rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag python .omo/evidence/T5-rereview-20261018-codex/independent_probes_v2.py
```

The full 3,000-article benchmark and aggregate `gate-g1` were not repeated in this
narrow re-review. The independent DB suite reruns exact/brute-force and ANN
mechanism tests on its test corpus. G1 acceptance is unchanged; gate exit 2 retains
its existing technical-only meaning.

## Policy and scope limits

- `retrieval-policy.v2` is a new artifact; v1 remains historical and byte-identical.
  Query lexemes, bounded chunk selection and access steps changed. RRF k=60,
  declared ties, rerank 40/512/1.5 seconds and 20-article/12k context limits remain.
  R6 identifies where SQL selection currently disagrees with those declared ties.
- The first 40 chunk windows cover up to approximately 40,200 code points with the
  current 1200/200 chunker. Matching passages later in a long article can be missed;
  this is explicitly reported through lexical truncation metrics. This review
  does not claim complete lexical recall for arbitrarily long articles.
- Final article fetches still load full immutable documents, bounded by article
  count rather than source bytes. The R4 fix bounds chunk candidates, not all
  database work or memory independently of document length.
- ANN stays disabled, its routing/quality thresholds OPEN. Separate visibility-
  selectivity measurement is still missing. Exact fixture retrieval and fake
  Recall/MRR measurements do not prove real semantic quality or production SLOs.
- CJK substring behavior, fixture-only token counting/access, and live-provider
  activation boundaries remain as documented; no live traffic was introduced.

## F1–F4 disposition and next step

| Review | Disposition |
| --- | --- |
| F1 Plan/policy | REQUEST CHANGES: R5 return-boundary access and R6 frozen tie selection remain; ANN measurement item stays OPEN |
| F2 Code quality | REQUEST CHANGES: two reproducible P2 fixes/regressions needed despite passing automated checks |
| F3 Runtime QA | Existing full suite/smoke and corrected original probes PASS; new independent probes demonstrate R5/R6 |
| F4 Scope fidelity | PASS in observed fixture/fake scope; no T6/T7, event writer, legacy DB operation or live provider added |

Fix R5/R6 and add regressions, preserve existing review evidence, then request an
independent re-review. T5 has no approval to commit from this report, and G2 is not
accepted. No owner decision or live activation is inferred from technical tests.
