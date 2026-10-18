# T5 evidence: hybrid retrieval + deterministic evidence assembly

**Current status (2026-10-18, after final independent re-review): T5 technical APPROVE in fixture/fake scope under retrieval-policy.v3; R1-R7 CLOSED.** See `T5-rereview3-20261018-codex/review.md`. The owner authorized the scoped commit after successful review ("nếu được rồi thì commit nhé"); see `T5/commit-check-20261018.md`. G2 remains OPEN until T6/T7 and its scoped review; live/ANN quality is not accepted.

The implementation/self-check entries below are historical. Their earlier "Current", "pending review", "T5 not accepted" and "nothing committed" statements describe those runs and are superseded by the status above. Original run logs and review reports are preserved.

**Current status (2026-10-18, after re-review 2):** re-review 2
`T5-rereview2-20261018-codex/review.md` closed R5/R6 and raised R7 (v2 `chunk_pool`
no longer enforced). Resolved by **`retrieval-policy.v3`**; all v2 measurements
and runs below are superseded (kept as history). See "Re-review 2 fix: policy v3"
at the end. Self-check only; next independent re-review pending; T5/G2 OPEN.

Previous status (after re-review): re-review
`T5-rereview-20261018-codex/review.md` = REQUEST CHANGES (new R5, R6; original
R1-R4 scenarios closed). R5/R6 fixed with behavioral red->green regressions; see
"Re-review fixes R5-R6" at the end. Earlier: independent review
`T5-review-20261018-codex/review.md` = REQUEST CHANGES (R1-R4). R1-R4 are fixed
with regressions under **`retrieval-policy.v2`** (see "Review fixes R1-R4" at the
end). Self-check only; independent re-review pending; T5 NOT accepted; G2 OPEN.
Sections below dated 2026-10-16 describe the v1 implementation and are historical.

Status (2026-10-16): **T5 technical self-check PASS in fixture/fake scope; independent
review NOT yet done; G2 remains OPEN** (needs T6, T7 and its scoped review). Nothing
committed, pushed or deployed; no live provider, API key or real reranker used.
Exact-vs-ANN comparison was run on real pgvector, but ANN stays **disabled** by policy
(see "Vector routes"): the ANN quality item is OPEN, not passed.

- Worktree: `.claude/worktrees/rag-retrieval-t5`, branch `feature/rag-retrieval-t5`,
  base T4 commit `8167f2068dfaae7b7b40b05b2f45578036f8bb3d`.
- Main-repo WIP (D43 plan/design) was read only; the D43 parts were re-written into
  this worktree's plan/design with one deliberate correction from the T5 request:
  cancellation/deadline stop the query, they are never rerank degradation.
- Environment (`run-*/environment.txt`): macOS 25.4.0 arm64, Python 3.12.14, uv 0.12.5,
  bun 1.4.0, Docker 29.7.2, rtk 0.45.0, langgraph 1.2.12, psycopg 3.3.6, isolated
  auto-removed `pgvector/pgvector:0.8.0-pg17` containers (pgvector 0.8.0). uv cache and
  Docker access needed the sandbox bypassed for those commands only.
- No `AGENTS.md` exists in the repo/worktree; `~/.codex/RTK.md` followed (`rtk` prefix).

## Flow implemented

```text
BriefingQuery + trusted CallerScope (argument, never from query text) + QueryControl
  -> admit; fixture AccessPolicy; config check; query clock read once
  -> [equal publication bounds -> EMPTY empty_publication_range, zero storage calls]
  -> metadata eligibility (only if topic/entity filters): valid READY publication of the
     current revision + active ACTIVE generation, written by the job's current claim,
     exactly one -> coverage complete|partial|filter_unavailable
  -> lexical: rag_current_lexical FTS ('simple', OR of \w+ terms) top 50 docs ->
     T3 chunker -> chunk score (terms covered / query terms) + freshness -> top 50 chunks
  -> vector: EmbeddingPort (FakeEmbeddings) -> full EmbeddingIdentity check ->
     rag_current_vectors exact cosine over complete ACTIVE generations matching model,
     version, dimension, chunker, normalization and corpus generation -> top 100 ->
     freshness -> top 50 chunks
  -> RRF k=60 (missing branch 0) -> exact/near-duplicate suppression
  -> rerank window <=40 x <=512 tokens, slice min(1.5 s, remaining deadline), thread +
     cancel wake-up -> applied | skipped | degraded(timeout|error|invalid_output)
  -> article aggregation (best chunk, <=3 spans) -> pool 2 x cap
  -> admit; access re-check; ONE batched current_articles read (current hash, tombstone,
     scope, metadata re-evaluated, vector generation still active, chunk offsets equal
     the T3 chunker) -> cap min(result_limit, 20)
  -> EvidenceAssembler: JSONL context counted whole by TokenCounter, budget
     min(query.context_budget, 12000, caller remaining) -> admit -> RetrievalResult
```

## Files / interfaces (all in `apps/rag`)

- Policy: `src/eventlens_rag/policies/retrieval-policy.v1.json` (`retrieval-policy.v1`,
  fusion `rrf-k60.v1`), validated by `domain/retrieval.py::RetrievalPolicy`
  (tie-break strings must equal the implemented orders; caps bounded 50/40/512/1.5/20/12000).
- Domain: `domain/retrieval.py` (policy, `QueryControl`, `ChunkRef`, candidates,
  `RetrievalResult`, `RetrievedArticle`, `RetrievedSpan`, `EvidenceContext`,
  `FilterCoverage`, `RetrievalMetrics`, `RetrievalProvenance`, typed errors),
  `domain/fusion.py` (pure ranking/RRF/dedup/rerank validation/aggregation).
- Ports: `ports/retrieval.py` (`RetrievalStore`, `RetrievalScope`), `ports/reranker.py`
  (`RerankerPort`, `RerankRequest/Output`), `ports/tokens.py` (`TokenCounter`; T6 should
  extend this one port, D32).
- Application: `application/retrieve.py` (`RetrieveEvidence`, `RetrievalOptions`,
  `bundled_retrieval_policy`), `application/evidence_context.py` (`EvidenceAssembler`).
- Adapters: `adapters/postgres_retrieval.py` (one read-only statement per call,
  `statements` counter), `identity_reranker.py`, `scripted_reranker.py` (fake),
  `fixture_tokens.py`, `retrieval_smoke.py`, `vector_routes.py`; CLI
  `eventlens-rag retrieval-smoke` and `eventlens-rag vector-route-report` (both refuse
  a non-empty DB).
- Tests: `tests/test_fusion.py`, `test_reranker.py`, `test_evidence_context.py`,
  `test_retrieval.py` (real PG), `test_vector_routes.py` (real PG), helper
  `retrieval_support.py`, fixture `retrieval_url` in `conftest.py`, architecture guard
  in `test_architecture.py`.
- Docs: `apps/rag/README.md` (T5 section), plan T5/D43 status, design D43 sync.
- No migration added; migrations 001-006 untouched (portability tests still pass).

## Frozen policy (retrieval-policy.v1) and degraded behavior

| Item | Value |
| --- | --- |
| Branch depth | 50 lexical, 50 vector chunks (vector DB pool 100, lexical doc pool 50); <=3 chunks/article/branch; ranks from 1 |
| Lexical | `to_tsquery('simple', t1 | t2 ...)`, `\w+` casefold NFC terms, 35 English stopwords, <=32 terms |
| Freshness | `score + 0.05 * 0.5^(age_days/30)` inside each branch before rank assignment; missing/future time = 0; clock injected, fixed per query |
| RRF | `sum(1/(60+rank_b))`, missing branch 0; ties `fused DESC, published DESC NULLS LAST, article_id, chunk_id` |
| Dedup | before rerank; exact sha256 of casefold/whitespace-collapsed text; word-3-shingle Jaccard >= 0.7 across articles; representative = earliest published (nulls last), then IDs; cluster keeps best fused score |
| Rerank | <=40 candidates, <=512 tokens (exact prefix), slice 1.5 s capped by remaining deadline; unknown/duplicate/missing IDs or non-finite/non-numeric scores = invalid; tail beyond 40 appended in fused order; ties `rerank DESC, fused DESC, published DESC NULLS LAST, article_id, chunk_id` |
| Filters | after inclusive, before exclusive, equal bounds empty, NULL excluded when bounded; topics any-of, entities any-of, AND across kinds; metadata scan cap 5000 |
| Output | <=min(result_limit,20) articles, <=min(context_budget,12000,remaining) tokens, <=3 spans/article, truncation at token boundary (min 16 tokens) with new span ID/fingerprint, stop at first non-fitting span |
| Vector route | exact only; `ann_enabled=false` |

Degraded/typed outcomes: rerank disabled -> `skipped`; rerank timeout/error/invalid ->
`degraded` + reason, fused order kept; embedding provider error ->
`vector_branch_status=embedding_failed`, `lexical_only`; incompatible embedding ->
`embedding_incompatible`, `lexical_only`. Cancellation (`RetrievalCancelledError`),
deadline (`RetrievalDeadlineExceededError`), access (`RetrievalAccessDeniedError`) and
storage (`RetrievalStorageError`) are exceptions, never empty results or degradation.
Empty results carry `empty_reason`: `no_query_terms|no_matches|empty_publication_range|
filter_unavailable|dropped_at_final_check|context_budget_too_small`.

## Runs and exit codes

Run 1 `.omo/evidence/task-5-logs/run-20261016T090328Z/` (superseded, kept):

| Command | Exit | Result |
| --- | --- | --- |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 555 passed, 85 skipped (DB-dependent) |
| `... make rag-db-test` | 0 | 483 passed, 0 skipped |
| targeted pytest (test_retrieval, test_evidence_context, test_review_fixes) | 0 | 54 passed |
| `rtk git diff --check` | 0 | - |
| `eventlens-rag retrieval-smoke` attempt 1 | 1 | FAIL: question-file parser expected only list values (`schema_version` is a string); fixed + regression test `test_smoke_question_set_has_ten_questions_with_denominators`; log kept as `retrieval-smoke.attempt1-FAILED.log` |
| `eventlens-rag retrieval-smoke` rerun | 0 | all cases OK |
| `eventlens-rag vector-route-report` | 0 | exact route median ~330-356 ms: EXPLAIN showed a CTE estimated at 1 row joined by nested loop (9M filtered rows). Fixed by a correlated, index-backed completeness count. |

Run 2 (authoritative) `.omo/evidence/task-5-logs/run-20261016T090823Z/`:

| Command | Exit | Result |
| --- | --- | --- |
| `make check` attempt 1 | 2 | basedpyright: `reportImplicitRelativeImport` after ruff re-sorted an import; fixed; log `make-check.attempt1.log` |
| (`rag-db-test`/targeted/diff-check attempt 1 in the same pass) | 0/0/0 | 484 / 55 passed; kept as `*.attempt1.log` |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | lint+format+typecheck+web OK; **556 passed, 85 skipped** — all skips are "EVENTLENS_RAG_DATABASE_URL required" (25 of them T5 DB tests); not DB evidence |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | **484 passed, 0 skipped** on real PostgreSQL/pgvector (includes all T5 DB tests) |
| `rtk proxy ... sh apps/rag/with-test-db.sh uv run --package eventlens-rag pytest apps/rag/tests/test_retrieval.py apps/rag/tests/test_evidence_context.py apps/rag/tests/test_review_fixes.py -q -rs` | 0 | **55 passed**, 0 skipped (12 T4 review regressions included) |
| `rtk git diff --check` | 0 | clean |
| `eventlens-rag retrieval-smoke --output retrieval-smoke.json` | 0 | see below |
| `eventlens-rag vector-route-report --articles 3000 --queries 20 --k 10` | 0 | `vector-routes/` (JSON + 12 EXPLAIN ANALYZE files) |
| `rtk proxy ... make gate-g1` | 2 | technical PASS (exit 2 by design = G1 acceptance not decided by script); all 13 steps OK incl. worker-validate/worker-process, migrate, load-fixtures, rebuild x2 + generation reuse, DB suite 484 passed, db-unavailable exit 3. Run dir `.omo/evidence/G1/run-20261016T094240Z-rOmdvt` (new dir; G1 history untouched). Rerun because the shared test harness (`conftest.py`) changed |

`rtk` did not break any command (plain `rtk git diff --check`, `rtk proxy` for the rest).

## Requirement -> test mapping (spec section 7)

1. Lexical before READY / hybrid after / FAILED-PENDING-STALE lexical / no false metadata match:
   `test_retrieval.py::test_same_query_is_lexical_only_before_ready_and_hybrid_after`,
   `::test_pending_failed_and_stale_enrichment_keep_lexical_hits`,
   `::test_topic_filter_never_false_matches_missing_metadata`.
2. RRF arithmetic, missing branch, duplicate identity, stable/shuffled ties, freshness, near-dup:
   `test_fusion.py` (`test_rrf_arithmetic_and_missing_branch_contributes_zero`,
   `test_duplicate_chunk_identity_in_one_branch_keeps_best_rank_once`,
   `test_fused_ties_are_stable_under_shuffled_input` (all 120 permutations),
   `test_freshness_*`, `test_near_duplicate_wire_copy_keeps_earliest_original_as_representative`
   (fixtures 01/22; 03/04 conflict kept), `test_lexical_branch_ranks_chunks_with_shared_identity`).
3. Bounds/NULL, source domain, topic/entity semantics, denial, no backfill:
   `test_retrieval.py::test_publication_bounds_are_inclusive_exclusive_and_exclude_nulls`
   (lexical, vector-only, hybrid), `::test_equal_bounds_are_an_empty_range_without_storage_work`,
   `::test_source_domain_filter_applies_in_each_branch_without_backfill`,
   `::test_topic_any_of_entity_any_of_and_all_across_kinds`,
   `::test_fixture_denial_raises_before_any_storage_call`.
4. Exact/ANN on real DB, incompatible identity, STAGED/incomplete, history:
   `test_vector_routes.py` (exact == Python brute-force cosine under filter; report
   mechanism), `test_retrieval.py::test_incompatible_corpus_generation_finds_no_vectors`,
   `::test_incompatible_query_embedding_falls_back_to_lexical_only`,
   `::test_staged_and_incomplete_generations_are_never_searched`,
   `::test_only_current_publication_counts_never_history`.
5. Reranker: `test_reranker.py` (identity keeps ties, reorder known set, 40/512 + exact
   prefix, tail, unknown/duplicate/missing/NaN, error, timeout, disabled, slice <= remaining,
   cancel/deadline stop) + `test_fusion.py::test_invalid_rerank_outputs_are_rejected_whole`.
6. Aggregation/token cut: `test_evidence_context.py` (21->20, limit 5, exact limit,
   one-over, small budget, no room, multilingual Vietnamese/CJK/combining/emoji offsets,
   zero hits, lower budget never raised).
7. Races + batching: `test_retrieval.py::test_correction_during_retrieval_never_returns_old_spans`,
   `::test_tombstone_during_retrieval_drops_article`,
   `::test_metadata_supersession_during_filtered_retrieval`,
   `::test_vector_only_candidate_from_replaced_generation_is_dropped`,
   `test_reranker.py::test_access_revoked_during_rerank_is_denied_at_final_boundary`,
   `test_retrieval.py::test_final_check_is_batched_without_n_plus_one` (3 vs 20 articles:
   identical statement counts 2 lexical / 3 hybrid). Races use the scripted reranker as a
   deterministic barrier; no sleeps.
8. Cancel/storage: `::test_pre_cancelled_query_starts_no_work` (0 statements),
   `::test_cancel_during_embedding_starts_no_vector_query`,
   `test_reranker.py::test_cancel_*`, `::test_deadline_expiry_during_rerank_raises_typed_deadline`,
   `test_retrieval.py::test_missing_schema_is_typed_storage_failure_not_empty`,
   `::test_unreachable_database_is_typed_storage_failure` (no credential leak),
   `::test_rerank_degradation_is_not_used_for_storage_errors`,
   `::test_embedding_provider_failure_is_lexical_only_not_semantic`.
9. E2E: `test_retrieval.py::test_end_to_end_ingest_enrich_filtered_retrieval` (T4
   `IngestArticle` -> lexical -> `EnrichmentRunner` + fake enricher -> READY -> topic-filtered
   hybrid evidence); worker suites run inside `make rag-db-test`; 12 T4 regressions pass.

## Smoke (run 2, `retrieval-smoke.json`, query clock 2026-04-01T00:00Z)

| Case | status / reason | mode | vector | rerank | coverage | articles | storage calls |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lexical_only (before enrichment) | evidence | lexical_only | executed (no vectors yet) | applied (identity) | complete | 2 (01 represents dup 22) | 3 |
| filter_unavailable (topic, before enrichment) | empty / filter_unavailable | none | not_needed | skipped | filter_unavailable | 0 | 1 |
| zero_hit | empty / no_matches | none | disabled | skipped | complete | 0 | 1 |
| happy (topic=transportation, after READY) | evidence | hybrid | executed | applied | partial (24, 28 FAILED) | 14 | 4 |
| rerank_degraded (failing scripted reranker) | evidence | hybrid | executed | degraded / error | complete | 20 | 3 |

## Measurement (fixture: 30 articles, 10 questions; 2 abstain questions excluded)

Same corpus, questions, policy `retrieval-policy.v1`, FakeEmbeddings, k = 20 articles:

| Mode | Recall@20 (hits/denominator) | MRR | nDCG@10 |
| --- | --- | --- | --- |
| lexical_only | 1.000 (12/12) | 1.000 | 0.981 |
| vector_only | 0.833 (10/12) | 0.126 | 0.163 |
| RRF | 1.000 (12/12) | 0.823 | 0.875 |
| RRF + IdentityReranker | 1.000 (12/12) | 0.823 | 0.875 |

Limits: FakeEmbeddings are hash vectors with no semantics, so vector_only is near
random and RRF *lowers* MRR/nDCG vs lexical on this set; identity rerank equals RRF by
construction. These numbers prove mechanics only — not semantic quality, not a real
reranker gain, not p95 production latency. Labels/gold were not changed. T9 owns the gate.

## Vector routes (run 2, `vector-routes/vector-route-report.json`)

pgvector 0.8.0; synthetic deterministic corpus 3000 one-chunk articles, unit Gaussian
1536-d vectors (seed 5), 20 query vectors, k = 10, ANN = HNSW `ef_search=40`,
`iterative_scan=relaxed_order`, `max_scan_tuples=20000`. Exact = production query
(1 statement); ANN = settings + query (2 statements). Recall vs the exact top-10
(denominator 200 per row). "forced" disables seqscan/sort for the benchmark only.

| Scenario (eligible) | Route | Recall@10 | median ms | p95 ms | HNSW used |
| --- | --- | --- | --- | --- | --- |
| all 100% (3000) | exact | 1.000 | 124.8 | 156.8 | no |
| | ANN natural plan | 0.530 | 38.6 | 43.8 | yes |
| | ANN forced | 0.530 | 34.7 | 59.0 | yes |
| source 10% (300) | exact | 1.000 | 116.8 | 227.9 | no |
| | ANN natural plan | 1.000 | 59.2 | 76.7 | no (planner chose exact sort) |
| | ANN forced | 0.815 | 50.6 | 58.0 | yes |
| source 1% (30) | exact | 1.000 | 55.2 | 68.8 | no |
| | ANN natural plan | 1.000 | 32.4 | 36.5 | no |
| | ANN forced | 1.000 | 64.4 | 107.7 | yes |
| time 5% (150) | exact | 1.000 | 85.0 | 233.7 | no |
| | ANN natural plan | 1.000 | 29.2 | 38.0 | no |
| | ANN forced | 0.975 | 45.6 | 57.6 | yes |

Conclusion: ANN at these settings loses ~47% recall unfiltered and ~19% at 10%
selectivity on random high-dimensional vectors; the planner only picks HNSW naturally
for the unfiltered case. ANN therefore stays disabled (`ann_enabled=false`); routing
thresholds for ANN are **OPEN** pending representative embeddings/corpus and an
ef_search/recall study. The exact route never uses HNSW (MATERIALIZED distance CTE).
Run 1 (before the completeness-query fix) measured exact at ~330-356 ms median; the
fix kept correctness (brute-force test) and cut latency. Visibility (tombstone)
selectivity is enforced in both routes but not a separate benchmark scenario (the
DB race/tombstone tests cover correctness). Synthetic latencies are a local laptop
measurement, not production p95.

## Open issues / limits

- ANN route quality/thresholds OPEN (above); production uses exact cosine (O(N)).
- Tokenizer `\w+`: unspaced CJK runs are single terms (CJK substring queries miss);
  Postgres `simple` parser and Python split differ on some tokens (e.g. dates/hyphens).
- Fixture token counter is not a model tokenizer; T6 owns D32 accounting.
- Metadata scan cap 5000 reports `partial` if exceeded (not exercised at fixture size).
- Fixture access is the frozen fixture policy only; no production RBAC.
- Independent review of T5 pending; G2 OPEN; real reranker/provider not enabled.

## Interfaces for T6/T7

`RetrieveEvidence.execute(BriefingQuery, CallerScope, QueryControl,
RetrievalOptions(remaining_context_budget=...)) -> RetrievalResult` with
`articles[*].spans[*].span: EvidenceSpan` (IDs, offsets, fingerprints),
`context: EvidenceContext` (JSONL text, token_count, tokenizer identity),
`provenance` (policy/fusion/index version, reranker, time basis), `coverage`,
`retrieval_mode`, `rerank_status`, `metrics`; typed `RetrievalError` subclasses with
stable `code`. T6 reuses `ports/tokens.py::TokenCounter`.


## Review fixes R1-R4 (2026-10-18, policy `retrieval-policy.v2`)

Review file (not modified): `.omo/evidence/T5-review-20261018-codex/review.md`.
Run dir: `.omo/evidence/task-5-logs/run-20261018T150213Z-review-fixes/` (earlier run
dirs kept unchanged).

Before fixing: the reviewer's `independent_probes.py` reproduced all four defects on
real PostgreSQL (`review-probes-before-fix.log`, exit 0 = reproduced). The new
regression module `apps/rag/tests/test_t5_review_fixes.py` was run against the
pre-fix code and failed (`regressions-before-fix.log`, exit 2: collection fails on
the not-yet-existing v2 API `LexicalLimits`/`LexicalPage`; the R1/R2 scenarios are
the reviewer's reproduced probes).

| Finding | Fix | Regression tests (`test_t5_review_fixes.py`) |
| --- | --- | --- |
| R1 [P1] revocation during final fetch returned evidence | `RetrieveEvidence._final_check` re-checks `AccessPolicy.allows` immediately after the single batched `current_articles` read; `_finish` checks again at the return boundary after assembly. One policy call each, independent of article count (no N+1). Boundary frozen in v2 `filters.order` | `test_r1_access_revoked_during_final_fetch_returns_no_evidence` (real PG, Event barrier around the returned final SQL read, revoke while in flight, asserts `RetrievalAccessDeniedError` and still 2 statements); `test_r1_access_revoked_during_assembly_is_denied_at_return_boundary` |
| R2 [P2] malformed rerank row raised `ValueError` | `domain/fusion.py::validate_rerank_scores` validates the whole untrusted shape (tuple/list of 2-element rows, `UUID`, finite real via `finite_score` that rejects bool/overflow/hostile `__float__`); `application/retrieve.py::_validated_scores` requires a `RerankOutput` and turns any remaining exception from validation into `invalid_output`. Reranker call/cancel/deadline/storage handling unchanged | `test_r2_malformed_rerank_output_degrades_to_fused_order` x7 (3-element row, bare IDs, `scores=None`, wrong outer type, `10**400`, hostile float, string IDs) asserting `degraded`/`invalid_output` and fused order; `test_r2_malformed_output_on_real_db_degrades`; `test_r2_cancel_inside_malformed_rerank_still_stops_the_query` |
| R3 [P2] Python casefold (`Straße`->`strasse`) missed PG lexemes | Query lexemes are now produced by PostgreSQL: `unnest(to_tsvector('simple', NFC(query)))`, stopwords removed, first 32 by first position, OR tsquery from quoted lexemes. Chunk matching uses `to_tsvector('simple', title || ' ' || chunk)` in the same statement. Python `query_terms` removed; casefold remains only for dedup similarity. Offsets are on the original NFC text (`substr` by code point) | `test_r3_query_lexemes_match_the_index_config` x8 on real PG: `Straße`, `east-pier`, `2026-03-01`, Vietnamese `ĐÓNG`/`đóng`, `İstanbul`, Greek final sigma, NFD `Cafe\u0301` vs NFC `Café`, e-mail token; each asserts the article is found and `content[start:end] == excerpt`; `test_r3_stopword_only_query_reports_no_query_terms` |
| R4 [P2] unbounded intermediate lexical chunk work | Chunk windows are generated in SQL with the T3 arithmetic, at most `max_chunks_scanned_per_article = 40` per article, at most `max_chunks_per_article = 3` kept per article and a pool `LIMIT chunk_pool = 150` before rows leave PostgreSQL; Python never materializes all chunks. Articles whose windows exceed the scan cap are reported as `metrics.lexical_truncated_articles` (never widened). Final check validates offsets with `domain/chunking.py::chunk_bounds` arithmetic instead of re-chunking | `test_r4_policy_freezes_scan_and_pool_caps`; `test_r4_exact_and_one_over_scan_limit` (40 windows: found; 41 with the word only in window 41: EMPTY + `lexical_truncated_articles == 1`); `test_r4_long_article_intermediate_pool_is_bounded` (312,000 code points + 60 articles: lexical rows <= 150, <= 3 spans/article); `test_r4_cancel_and_deadline_after_long_scan_stop_the_query`; `test_fusion.py::test_chunk_bounds_arithmetic_equals_t3_chunker` |

Policy change: `retrieval-policy.v2.json` (new; `supersedes: retrieval-policy.v1`,
`change_reason` recorded). Changed: tokenizer (`postgres-simple-lexemes.v2`), lexical
caps (`max_chunks_scanned_per_article 40`, `chunk_pool 150`,
`scan_limit_exceeded: report_lexical_truncated_articles_never_widen`), chunk score
on lexemes, `rerank.malformed_output: invalid`, `aggregation.final_check_offsets:
chunk_arithmetic_no_rechunking`, access steps in `filters.order`. Unchanged: depths
50/50, RRF k=60 and all tie-breaks, freshness, dedup, rerank 40/512/1.5 s, output
20/12000, exact vector route, ANN disabled. `retrieval-policy.v1.json` is kept
byte-unchanged as history; the current code loads only v2, so all v1 results above
are superseded and were re-run under v2.

### Commands (run-20261018T150213Z-review-fixes)

| Command | Exit | Pass / skip |
| --- | --- | --- |
| reviewer `independent_probes.py` (before fix) | 0 | 4/4 defects reproduced |
| `pytest test_t5_review_fixes.py` (before fix) | 2 | collection error (expected fail) |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 575 passed, 100 skipped (all "EVENTLENS_RAG_DATABASE_URL required"; not DB evidence) |
| `... make rag-db-test` | 0 | 518 passed, 0 skipped |
| targeted pytest (`test_retrieval`, `test_evidence_context`, `test_review_fixes`, `test_t5_review_fixes`) under `with-test-db.sh` | 0 | 80 passed, 0 skipped |
| `rtk git diff --check` | 0 | clean |
| `eventlens-rag retrieval-smoke` | 0 | 5 cases, all `retrieval-policy.v2` |
| `eventlens-rag vector-route-report --articles 3000 --queries 20 --k 10` | 0 | see below |
| reviewer `independent_probes.py` (after fix) | 1 | ImportError: the probe imports the removed `query_terms`; it cannot run unmodified against v2. The same four scenarios are covered by the passing regressions above. Review file/script not modified |

`make gate-g1` was not re-run in this round: no migration, storage schema or shared
harness file (`conftest.py`, `with-test-db.sh`, `db-suite.sh`, `db-test.sh`) changed
after the 2026-10-16 gate run (exit 2 technical PASS); the DB suite it wraps was
re-run via `make rag-db-test`. Migrations 001-006 unchanged.

### Results under v2

- Smoke cases identical in status/mode/rerank/coverage/article counts to v1
  (filter_unavailable empty; happy hybrid 14 articles partial coverage; lexical_only
  2 articles; rerank_degraded `degraded/error` 20 articles; zero_hit `no_matches`).
- Fixture measurement unchanged from v1: lexical-only Recall@20 1.0 (12/12),
  MRR 1.0, nDCG@10 0.981; vector-only 0.833 (10/12), 0.126, 0.163; RRF and
  RRF+identity 1.0 (12/12), 0.823, 0.875. Same limits as above (fake embeddings).
- Vector routes (exact SQL unchanged): exact recall 1.0 with median 42-61 ms;
  ANN unfiltered recall 0.465 (run 2: 0.53 — HNSW graph construction is randomized,
  so ANN recall varies between builds), source 10% forced index 0.80, time 5% forced
  0.97. ANN remains disabled; ANN thresholds OPEN.

### Still open after R1-R4

- Independent re-review of these fixes; T5 not accepted; G2 OPEN.
- ANN routing thresholds / representative-embedding recall study; separate
  visibility-selectivity benchmark not measured.
- Passages beyond the first 40 chunk windows (~40,200 code points) of an article
  are not lexically scanned (reported, not widened). Final check still fetches full
  `document_json` for <= 40 articles (bounded by article count, not by length).
- CJK substring matching (PostgreSQL `simple` parser treats an unspaced CJK run as
  one lexeme); fixture token counter and fixture access scope as before.


## Re-review fixes R5-R6 (2026-10-18, still `retrieval-policy.v2`)

Re-review (not modified): `.omo/evidence/T5-rereview-20261018-codex/review.md`.
Run dir: `.omo/evidence/task-5-logs/run-20261018T152150Z-rereview-fixes/` (all earlier run dirs kept).

### Red before fix

- Reviewer `independent_probes_v2.py`, unmodified, on pre-fix code: exit 0 =
  R1-R4 corrected **and** R5/R6 reproduced (`rereview-probes-before-fix.log`).
- New module `apps/rag/tests/test_t5_rereview_fixes.py` on pre-fix code, failing on
  behavior (not import/collection):
  - attempt 1 (`regressions-before-fix.attempt1.log`, exit 1): 2 failed / 1 passed.
    The zero-hit test passed by mistake: its fixture title "Harbor report" matched the
    query, so it went through the already-guarded evidence path. Fixture fixed
    (title "Bakery news"), re-run.
  - attempt 2 (`regressions-before-fix.log`, exit 1): **3 failed** —
    both R5 tests `DID NOT RAISE RetrievalAccessDeniedError`; R6
    `assert [1, 0, 2] == [1, 6, 0]` (SQL kept chunk indices by index instead of
    by the frozen chunk_id tie-break).

### Fixes and tests

| Finding | Fix | Regression (real PG, Event barrier, no sleep) |
| --- | --- | --- |
| R5 [P2] empty returns skipped the final access check | `RetrieveEvidence._empty` now runs the same response-boundary check as `_finish` (`access.allows` after `admit`) before building any EMPTY result. Every return path (equal-bounds empty range, filter_unavailable, no matches, no query terms, dropped at final check, budget too small, evidence) passes it; a denied caller gets `RetrievalAccessDeniedError` only, with no coverage/counts | `test_r5_revoked_during_metadata_read_denies_filter_unavailable_return` (pauses after the real `metadata_eligibility` read, revokes, releases); `test_r5_revoked_during_zero_hit_lexical_read_denies_empty_return` (pauses after the real zero-hit lexical read). Both assert typed denial and that the error text carries no counts |
| R6 [P2] SQL per-article cut used `chunk_index`, not the frozen `chunk_id` tie-break | `chunk_id` is UUIDv5 (SHA-1). PostgreSQL has no SHA-1 without an extension, which would need a migration, so the SQL cannot apply it. The SQL now uses `rank() OVER (PARTITION BY article_id ORDER BY matched DESC)` and keeps every window ranked within `max_chunks_per_article` **including ties**. Within one article only `matched` varies before `chunk_id`, so this is a safe superset. The SQL returns metadata only (no text). Python `rank_lexical` applies the exact frozen key (score, document rank, published, article_id, chunk_id), the per-article cap 3 and depth 50. A second statement `chunk_texts` then fetches exact NFC slices only for the <= 50 selected chunks | `test_r6_per_article_cut_uses_frozen_chunk_id_tie_break`: 4 seven-window articles inserted in shuffled order; title-only matches make all windows tie. The expected order is computed independently from the chunk-id definition (`uuid5(CHUNK_NAMESPACE, ...)` sorted ascending), not from SQL. Asserts that the kept span indices equal that order |

Bounds after R6: candidate **metadata** rows <= `article_pool x
max_chunks_scanned_per_article` (50 x 40 = 2000) per query and <= 40 per article,
and only when windows tie at the cut. Typically <= 3 per article. Text-bearing rows
<= `depth` 50 <= `chunk_pool` 150. Python never materializes all chunks of a long
article. Lexical storage calls rise from 1 to 2 (constant, independent of article
count). The N+1 test now expects 3 statements lexical-only and 4 hybrid, the same
for 3 and 20 articles.

### Policy decision

**No v3.** R6 makes the code match v2's declared tie-break. No frozen value changed:
depth 50, scan 40, per-article 3, RRF and all ties are as before. Note for review:
the R1-R4 section above said the 150-row pool limit applied "before rows leave
PostgreSQL". That implementation description is superseded. `chunk_pool` (150) now
bounds text-bearing rows (<= depth), and metadata rows are bounded by the scan cap
as stated above. If the reviewer reads `chunk_pool` as a cap on all rows leaving
PostgreSQL, that requires a v3. The alternative (pgcrypto + migration to compute
UUIDv5 in SQL) was rejected because migrations must stay unchanged.

### Commands (`.omo/evidence/task-5-logs/run-20261018T152150Z-rereview-fixes`)

| Command | Exit | Pass / skip |
| --- | --- | --- |
| reviewer `independent_probes_v2.py` before fix | 0 | R1-R4 fixed, R5/R6 reproduced |
| `pytest test_t5_rereview_fixes.py` before fix (attempt 1 / attempt 2) | 1 / 1 | 2 failed 1 passed / **3 failed** (behavioral) |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 575 passed, 103 skipped (all "EVENTLENS_RAG_DATABASE_URL required") |
| `... make rag-db-test` | 0 | 521 passed, 0 skipped |
| targeted pytest (`test_retrieval`, `test_evidence_context`, `test_review_fixes`, `test_t5_review_fixes`, `test_t5_rereview_fixes`) under `with-test-db.sh` | 0 | 83 passed, 0 skipped |
| `rtk git diff --check` | 0 | clean |
| `eventlens-rag retrieval-smoke` | 0 | 5 cases unchanged (only storage-call counts +1); measurement unchanged |
| reviewer `independent_probes_v2.py` unmodified, after fix | 1 | `TypeError`: probe passes the removed `chunk_pool` kwarg to `LexicalLimits` |
| `independent_probes_v2.adapted.py` (see `independent_probes_v2.adaptation.diff`: drops the `chunk_pool` kwarg and runs each probe isolated; **no assertion changed**) | 1 | original_r1/r2/r3 assertions held. `empty_return_revocation` now raises `RetrievalAccessDeniedError` (R5 defect gone). `sql_chunk_tie_selection` defect assertion `got != want` fails with `[0,6,5] == [0,6,5]` (R6 defect gone). **`original_r4` fails at its store-page assertion `len(page.chunks) <= 3`**: for the 312k-code-point article all 40 scanned windows tie, so the store returns 40 metadata rows by the R6 design above. Its result-level checks (truncated=1, <= 3 spans) are covered by `test_r4_*` |
| original review `independent_probes.py`, adapted (`independent_probes.adaptation.diff`: `query_terms` import shim only) | 1 | Stops at its first defect assertion: final-fetch revocation now raises `RetrievalAccessDeniedError` (R1 fixed). This defect-asserting script cannot pass after fixes by design |

The vector route report was not re-run: the vector SQL is unchanged since the
review-fixes run. `make gate-g1` was not re-run: no migration, schema or shared
harness changed. Migration diff vs HEAD is empty.

### Still open after R5-R6

- Independent re-review. T5 not accepted; G2 OPEN.
- Reviewer decision on the `original_r4` page-size expectation versus the R6 safe
  superset (bounded by the scan cap; text only for selected chunks).
- Items listed under "Still open after R1-R4" are unchanged: ANN thresholds,
  visibility-selectivity benchmark, the 40-window scan limit, full document fetch at
  the final check for <= 40 articles, CJK substrings, fixture token counter/access.


## Re-review 2 fix: policy v3 (2026-10-18)

Review (not modified): `.omo/evidence/T5-rereview2-20261018-codex/review.md`.
Run dir: `.omo/evidence/task-5-logs/run-20261018T154307Z-policy-v3/` (all earlier run dirs kept). **Every v2 result above is superseded by
the v3 runs below.** The v2 logs remain as historical evidence and were not relabeled.

### v3 diff summary (`apps/rag/src/eventlens_rag/policies/retrieval-policy.v3.json`)

- New `schema_version`/`policy_id` v3, with `supersedes: retrieval-policy.v2` and a
  `change_reason`. v1 and v2 are byte-identical (sha256 `34d25ee7...` and
  `add4d6cf...`, equal to the reviewer's manifest; pinned by
  `test_v1_and_v2_policies_stay_byte_identical_history`).
- `lexical`: the dead `chunk_pool` key is removed and replaced by keys the runtime reads:
  - `max_metadata_candidates: 2000`. The model validator requires it to equal
    `article_pool x max_chunks_scanned_per_article` (50 x 40). It is passed to SQL as
    `LIMIT cap+1` and checked in `RetrieveEvidence._lexical`; overflow raises
    `RetrievalConfigurationError` and is never a silent cut of a tie group
    (`metadata_bound_exceeded`).
  - `max_text_chunks: 50` (must be >= `depth`). Selected chunks are sliced to it
    before the single `chunk_texts` read.
- `selection_stages` states the two-step fetch explicitly: SQL FTS article pool ->
  scan of the first 40 windows -> per-article `rank() <= 3` metadata candidates
  (`tie_boundary: keep_all_windows_tied_at_per_article_cut_superset`; at most 40 per
  article and 2000 per query) -> Python frozen tie-break incl. chunk_id, per-article
  cap 3, depth 50 -> `chunk_texts` for at most 50 chunks.
- Unchanged: tokenizer, filters (incl. access boundary order), vector, freshness,
  RRF k=60 and all ties, dedup, rerank 40/512/1.5 s, aggregation, output 20/12000.
  No migration (no 007); migration diff vs HEAD is empty.
- Runtime loads v3 (`POLICY_RESOURCE`). Smoke provenance and CLI vector-report string
  say v3. README and plan were updated; the design doc has no policy-version reference.

### Tests (`apps/rag/tests/test_t5_policy_v3.py`) and red -> green

| Test | What it proves | Before v3 (`policy-v3-tests-before.log`, exit 1) | After |
| --- | --- | --- | --- |
| `test_every_numeric_v3_cap_is_wired_or_explicitly_covered` | the set of numeric policy leaves equals the wired table plus named dedicated tests, so a new or dead cap fails | RED (policy id v2) | green |
| `test_v3_cap_changes_runtime_behavior[...]` (14 caps) | changing each cap changes observed port arguments or results | 13 green (existing wiring pinned); `lexical.max_text_chunks` RED | green |
| `test_metadata_candidate_overflow_is_a_typed_failure_not_a_silent_cut` | cap passed to storage, breach is a typed error, no text read | RED | green |
| `test_rerank_caps_are_wired` | `rerank.max_candidates` and `max_tokens_per_candidate` wired | green (pins existing behavior) | green |
| `test_min_truncated_span_tokens_is_wired` | `output.min_truncated_span_tokens` wired | RED, but from a **test bug** (a 120-token budget could not fit a header), not runtime. Fixed to derive the budget from the full context; then it pins existing behavior | green |
| `test_50_articles_x_40_tied_windows_hit_exactly_2000_metadata_rows` (real PG) | exactly 2000 metadata rows, `chunk_texts` for 50, 0 truncated, 3 statements, <= 3 spans/article | green: **pins already-correct behavior** (the bound already existed; v3 freezes it) | green |
| `test_51st_article_and_41st_window_are_bounded_deterministically` (real PG, 51 x 41) | 2000 rows from articles 1..50 (`article_id ASC` among equal rank and time), window 41 never scanned, `lexical_truncated_articles == 50`, 50 text rows, 3 statements, article 51 absent | green: **pins already-correct behavior** | green |
| `test_v1_and_v2_policies_stay_byte_identical_history` | history unchanged | green | green |

Also updated: `test_fusion.py` and `test_t5_review_fixes.py` (v3 id and caps),
`test_retrieval.py` (index version prefix v3).

### Commands (`.omo/evidence/task-5-logs/run-20261018T154307Z-policy-v3`)

| Command | Exit | Pass / skip |
| --- | --- | --- |
| reviewer rereview2 `independent_probes.py` on v2 code | 0 | R5/R6 corrected; v2 pool mismatch reproduced (2000/50/3 statements/50 truncated) |
| `pytest test_t5_policy_v3.py` on v2 code | 1 | red as in the table above |
| `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 594 passed, 105 skipped (all "EVENTLENS_RAG_DATABASE_URL required") |
| `... make rag-db-test` | 0 | 542 passed, 0 skipped |
| targeted pytest (`test_retrieval`, `test_evidence_context`, `test_review_fixes`, `test_t5_review_fixes`, `test_t5_rereview_fixes`, `test_t5_policy_v3`) under `with-test-db.sh` | 0 | 104 passed, 0 skipped |
| `rtk git diff --check` | 0 | clean |
| `eventlens-rag retrieval-smoke` | 0 | `policy_id` and every case provenance `retrieval-policy.v3`. The 5 cases and the fixture measurement are unchanged (lexical 1.0/1.0/0.981; vector-only 0.833/0.126/0.163; RRF and RRF+identity 1.0/0.823/0.875) |
| rereview2 probe, unmodified, on v3 | 1 | `TypeError`: `LexicalLimits` now requires `max_metadata_candidates` |
| rereview2 probe, adapted (`independent_probes_rereview2.adaptation.diff`: adds the new kwarg and runs each probe isolated; **no assertion changed**) | 1 | `revocation_empty` held (R5 metadata and zero-hit denied). `tie_order` held (R6 `[0,6,5]`). `worst_case_rows`: its assertions `metadata==[2000]`, `texts==[50]` and `lexical_truncated_articles==50` (probe lines 96-98) ran and held. The next line asserts `metadata > policy.lexical.chunk_pool` (the v2 defect) and raises `AttributeError`, because v3 intentionally has no `chunk_pool` |

`vector-route-report` was not re-run: the vector code and the `vector` policy section
are unchanged from v2. `make gate-g1` was not re-run: no migration, schema or shared
harness change.

### Still open after R7

- Independent re-review of v3. T5 not accepted; G2 OPEN.
- Unchanged open items: ANN thresholds, visibility-selectivity benchmark, passages
  beyond 40 windows not scanned (reported), full document fetch at the final check
  for <= 40 articles, CJK substrings, fixture token counter/access.
