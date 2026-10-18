# T5 independent re-review 3 — 2026-10-18

**Verdict: APPROVE technical T5 in fixture/fake scope under retrieval-policy.v3.**
**R1–R7 are CLOSED in their recorded technical scenarios. No new material finding.**

Reviewer: Codex, independent of the implementation/fix agent.
Worktree: `.claude/worktrees/rag-retrieval-t5`; branch `feature/rag-retrieval-t5`.
Reviewed parent HEAD: `8167f2068dfaae7b7b40b05b2f45578036f8bb3d`, T5 changes
uncommitted at review time. `source-sha256.json` records the exact reviewed files;
`changed-since-review.json` records the narrow changes since re-review 2.

The owner's latest instruction, "nếu được rồi thì commit nhé", authorizes the
T5 commit after successful review. This report establishes that technical condition;
the commit checkpoint is recorded separately. G2 remains OPEN until T6/T7 and its
scoped review. No live provider, deployment, push, legacy upgrade or broader quality
acceptance follows from this approval.

## R7 disposition

R7 is CLOSED. `retrieval-policy.v3.json` is a new version that supersedes v2 and
describes metadata selection and text fetch separately. The unused `chunk_pool`
field is removed from the current policy/model/runtime path. v1/v2 are byte-identical
to the previous review's source manifest and are pinned by committed regression
tests. Earlier v1/v2 measurements remain historical; the new smoke records v3.

The new policy caps have actual runtime enforcement:

- `max_metadata_candidates=2000` must equal article pool x scan cap (50 x 40).
  It is passed to SQL (`LIMIT cap+1`) and checked before ranking/text fetch.
  A breach raises `RetrievalConfigurationError`; no partial tie group is silently
  accepted or text fetched afterward.
- `max_text_chunks=50` is read before the single batch text fetch; branch depth
  and its validation agree with that bound.
- The policy freezes selection stages, the tie-boundary superset, 40 windows/article,
  three final lexical chunks/article, exact chunk-ID ties, and bounded output.
  RRF k=60, rerank 40/512/1.5s, and final 20-article/12k-token caps stay unchanged.

Independent real-DB probe uses 51 articles x 41 windows with equal title matches:
only 50 articles are selected, exactly 2,000 metadata candidates and 50 text chunks
are fetched, `lexical_truncated_articles=50`, and normal lexical-only retrieval
uses three statements including the final batch check. All selected windows lie
inside the frozen scan. The full DB suite also covers exactly 50 x 40 windows,
the excluded 51st article and the unscanned 41st window.

A separate independent injected-overflow check lowers the cap to 1 deliberately
without production policy revalidation, to exercise the backstop: SQL returns
two metadata rows (cap+1), the application raises `retrieval_configuration_invalid`,
and no text fetch occurs. The real v3 validator rejects inconsistent product/cap
configuration; the injected case verifies defense at the use-case boundary.

## Earlier findings

| Finding | Current disposition |
| --- | --- |
| R1, final-fetch access revocation | CLOSED; post-fetch/nonempty return checks retained; real-DB regressions pass |
| R2, malformed reranker output | CLOSED; whole-shape/numeric checks and degradation retained; cancel/deadline stay typed stop conditions |
| R3, Unicode lexical mismatch | CLOSED; PostgreSQL `simple` query/chunk lexemes retained; multilingual/date/hyphen DB regressions pass |
| R4, unbounded lexical chunks | CLOSED within the explicitly versioned v3 bounds; no full source rechunking for final offsets |
| R5, empty-result access revocation | CLOSED; independently rechecked with Event barriers on real metadata and zero-hit reads; typed denial with no result/counts |
| R6, tie-break before per-article cut | CLOSED; independent UUIDv5 ordering still selects `[0,6,5]` from seven tied windows, including returned spans |
| R7, policy pool mismatch | CLOSED; new v3 freezes and enforces the observed two-stage limits; v1/v2 unchanged |

New tests distinguish existing behavior guards from regressions that failed on
the prior implementation. The two large real-DB cases already passed before v3
and now pin the adopted design; cap-wiring/overflow tests cover the actual fixes.
The original scripts that assert defects or use removed v1/v2 APIs remain unchanged
and are not acceptance checks. This review wrote an adapted v3 script with explicit
correct-behavior assertions and saved its diff separately.

## Independent verification

| Check | Exit | Result |
| --- | ---: | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 594 Python passed, 105 DB-dependent skipped; lint/format/typecheck/web PASS |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | 542 passed, 0 skipped on owned isolated PostgreSQL/pgvector, including T4 and all T5 regressions |
| Independent `independent_probes_v3.py` through `with-test-db.sh` | 0 | R5/R6 correctness, 2,000/50 limits, 51st-article/41st-window boundaries and real-DB overflow backstop PASS |
| `eventlens-rag retrieval-smoke --output .../retrieval-smoke.json` through the isolated harness | 0 | All five cases PASS; policy/provenance v3; fixture measurements match the reported values |
| v1/v2 and shared harness vs previous review manifest | 0 | Byte-identical policies, `conftest.py`, `with-test-db.sh`, `db-suite.sh` |
| Source fingerprint before status/evidence documentation updates | 0 | No reviewed file changed during verification (`source-check-after.json`) |

Raw logs and `*.exit.json` record exact commands/exits. The first independent probe
attempt passed but printed a combined counter of four after running both normal
retrieval and the extra overflow check. `independent-probes-v3.attempt2.log` fixes
that measurement label and asserts the phases separately: three normal statements
plus one deliberate overflow statement. Both logs are preserved; no runtime code
changed between the attempts.

The 3,000-article vector benchmark and aggregate G1 gate were not repeated in this
narrow v3 review: vector SQL, migrations/schema and shared harness did not change.
The full DB suite includes vector mechanism, storage and existing regression
tests. Gate exit 2 retains its technical-only meaning; G1 acceptance is unchanged.

## Limits and scope

- Approval covers deterministic mechanics with fixtures/fake providers and exact
  vector retrieval, not live semantic quality or production latency/cost.
- ANN remains disabled; routing/quality thresholds and a separate visibility-
  selectivity benchmark remain OPEN. No semantic or ANN quality gate is claimed.
- Lexical passages beyond the first 40 windows (~40,200 code points) may be missed;
  truncation metrics report this. Final batch reads still load full documents,
  bounded by article count rather than source bytes.
- CJK substring limitations, fixture token counting/access and all live-provider
  activation boundaries remain documented. T6 must supply full D32 accounting
  through the shared TokenCounter port.
- No T6/T7 implementation, canonical event writes, legacy DB mutation or deployment
  was introduced. SQL migrations 001–006 remain unchanged; no 007 is needed here.

## F1–F4

| Review | Verdict |
| --- | --- |
| F1 Plan/policy | PASS in recorded fixture/fake scope; v3 resolves R7 and honestly preserves deferred measurement items |
| F2 Code quality | PASS; no open material finding in the reviewed change, standard checks pass |
| F3 Runtime QA | PASS; real-DB suite, independent boundary/overflow/access/tie probes and v3 smoke |
| F4 Scope fidelity | PASS; T5 capability boundary, fake/live restrictions and G1/G2 distinction preserved |

T5 is ready for the owner-authorized scoped commit. G2 is not accepted by this
report; T6 and T7 remain outstanding.
