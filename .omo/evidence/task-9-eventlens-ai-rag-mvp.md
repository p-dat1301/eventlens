# T9 evidence: deterministic enrichment/RAG eval, E2E, telemetry gates, CI

**Status: T9 technically complete — all four CI jobs green on GitHub** for
commit `9c6af84463073c3fdf96620f3d80042033cec521` (PR #3 into `develop`).
Self-reviewed by the implementer through 5 external review rounds; this is
not an F1–F4 verdict. **G3 and F1–F4 stay OPEN** until independent reviews
exist and the owner accepts. Fixture/fake scope only: no live provider, no
provider key, no provider network call; fake results are not evidence of real
model quality.

## CI on GitHub (PR #3)

| Item | Value |
| --- | --- |
| PR | https://github.com/p-dat1301/eventlens/pull/3 (`feature/rag-eval-ci-t9` → `develop`; carries the 19 unmerged T1–T8 commits from `feature/rag-foundation` plus one T9 commit, owner-approved) |
| CI-verified commit | `9c6af84463073c3fdf96620f3d80042033cec521` (`test(ci): add deterministic RAG evaluation gates`) |
| Run | https://github.com/p-dat1301/eventlens/actions/runs/40319409530 — conclusion `success`, event `pull_request` |
| Check | success — https://github.com/p-dat1301/eventlens/actions/runs/40319409530/job/121560191879 — basedpyright 0 errors; pytest 1083 passed / 139 skipped (DB-dependent skips, not DB evidence); bun 69 pass; build |
| Database suite (PostgreSQL/pgvector) | success — https://github.com/p-dat1301/eventlens/actions/runs/40319409530/job/121560191951 — migrations applied; rag-api smoke `health 200`, `briefing 200 outcome=answered mode=hybrid citations=5`, `source 200 span=[0,126) excerpt_matches=True`; DB suite **1064 passed, 0 skipped** (skip fails the script) |
| Enrichment and RAG evals | success — https://github.com/p-dat1301/eventlens/actions/runs/40319409530/job/121560191958 — gating checks held; rerun `cmp` identical; Linux digests equal the local macOS digests (`a806275d…`, `be3348b5…`) |
| Browser gate | success — https://github.com/p-dat1301/eventlens/actions/runs/40319409530/job/121560191998 — `bunx playwright-core install --with-deps chromium` on ubuntu-latest ("Installing dependencies...", Chrome for Testing 156.0.8078.4 / v1248 downloaded), `EVENTLENS_BROWSER=chromium`, 9 pass / 0 fail |

CI-found issues fixed in the same single T9 commit (no test removed, no gate
lowered, no provider enabled):

1. First push (`1ad19b9`): GitHub rejected the workflow file (run
   40313369677, 0 s) — `${{ runner.temp }}` used in job-level `env`, where the
   `runner` context is unavailable. Fixed by using `$RUNNER_TEMP` inside step
   shells; actionlint (Docker image) printed no findings on the fixed file.
2. Second push (`7dea545`, run 40316657398): `eval`, `database`, `browser`
   green; `check` failed in the pre-existing
   `tests/backend/compat/test_worker_alias.py::test_alias_forwards_rag_worker_commands`
   — under `GITHUB_ACTIONS=true` Rich colours Typer help and ANSI escapes split
   `--max-jobs`. Reproduced locally with `GITHUB_ACTIONS=true`; the test now
   compares ANSI-stripped output (same assertion). Full suite with
   `GITHUB_ACTIONS=true CI=true`: 1083 passed / 139 skipped.
3. Third push (`9c6af84`, run 40319409530): all four jobs green.

The evidence-only amend that records this section produces a later commit on
the same PR; its CI run is linked on PR #3. Code and tests are identical to
`9c6af84`.

## 0. Review rounds

### Review round 1 (2026-10-26) — findings and fixes

| Finding | Fix | Regression evidence |
| --- | --- | --- |
| P1: benchmark ignored per-question `allowed_article_ids`; Q007 cited restricted article 014 | New `evaluation/scope.py`: `ScopedRetrievalStore` wraps the full store inside the ONE composed runtime and narrows every read to the active question's allowed set (`RetrievalScope.allowed_article_ids` intersection, `current_articles` filter, foreign `chunk_texts` refused). `scope_violations` independently checks retrieved (incl. suppressed duplicates), sent-to-model (parsed from each request payload) and cited article IDs per question. `scope_violations_total` must be 0 per mode (gate). | Both modes: `scope_compliance` 10/10, 0 violations. `test_scope.py`: the unscoped runtime leaks 014 for Q007 and the check reports it; the scoped runtime has none; the wrapper narrows reads and refuses foreign chunks. |
| P1: gate passed when cases/lifecycle/repeat sections were missing or malformed | `gating.py` rewritten fail-closed: known `schema_version` required; frozen required case lists per report kind (8 lifecycle; 15 RAG gating + 1 informational); every entry must have `expected`/`observed` objects, boolean `held` equal to `expected == observed`, the required gating flag; required safety values with exact types; `repeat_consistent` with exactly lexical+hybrid; both modes present; `report_digest` recomputed and compared. | `test_gating.py` (24 tests): 5 minimal reports, 6 enrichment and 10 RAG mutations (missing section, missing case, malformed entry, lying `held`, gating flag flip, network attempt, live default, repeat inconsistency, scope violation, missing mode) all fail; tampering without a new digest fails; quality-metric changes never gate; required lists equal the code registries. |
| P2: enrichment eval had no active network guard | `enrichment-eval` now runs the main pass and lifecycle cases under `no_network()`; report gains `safety: {enricher, network_connect_attempts}` and the gate requires `FakeArticleEnricher` / `0`. | report: `FakeArticleEnricher`, 0 attempts; gate tests. |
| P2: gold precision scored articles, not spans | Kept `citation_gold_article_precision`; added `citation_gold_span_exact_precision`, `citation_gold_span_overlap_precision` and `gold_span_coverage` (gold spans from `expected_references[].span_id` resolved to fixture offsets; matched by offsets because citation span IDs are chunk-derived). | Section 6. Limitation: fixture v1 gold spans cover each expected article's full text, so span metrics equal article metrics here. |

### Review round 4 (2026-10-26) — findings and fixes

| Finding | Fix | Evidence |
| --- | --- | --- |
| P1: gate accepted a benchmark row with an execution error (`route: "error"`, e.g. `retrieval_storage_unavailable`) once the digest was recomputed | `gating.py`: every per-question row in `modes.*` and `ranking.*` must have a completed route (`answered`, `abstained_no_evidence`, `abstained_by_model`); any other route, including `error`, fails | `test_gating.py`: ranking.hybrid row replaced by a `retrieval_storage_unavailable` error row and modes.lexical row by a `synthesis_budget_exceeded` row (both re-digested) → fail; `test_valid_abstention_rows_pass_the_gate`: the real report's Q007 `abstained_no_evidence` row passes |
| P2: timing test allowed 9/10 overhead samples | `rag-eval.measured.json` records `execution_errors` per pass; the test requires 0 errors and exactly 10 end-to-end and 10 overhead samples per pass (repeats=1) | `test_measured_report_times_every_pass_including_ranking`; round-5 report: 0 errors and 50/50 samples in each pass (section 6) |

### Review round 3 (2026-10-26) — findings and fixes

| Finding | Fix | Evidence |
| --- | --- | --- |
| P2: measured report dropped the two ranking passes' timings | `rag-eval.measured.json` now has `ranking.lexical` / `ranking.hybrid` with end-to-end and graph-overhead summaries and sample counts | Section 6 measured table; `test_measured_report_times_every_pass_including_ranking` (10 samples per pass at repeats=1) |
| P2: evidence fixture digest stale (`321d…`); evidence claimed all four CI jobs check credentials but `browser` lacked the step | Fixture digest updated to the reports' `23e664…`; credentials step added to the `browser` job and the CI section reworded | YAML parse: 1 credentials step in each of `check`, `database`, `eval`, `browser` |

### Review round 2 (2026-10-26) — findings and fixes

| Finding | Fix | Regression evidence |
| --- | --- | --- |
| P1: gate read only `summary.scope_violations_total`; an out-of-scope citation in one per-question row (re-digested) or deleting most rows still passed; a required case without `gating` passed (default True) | `gating.py`: for `modes.{lexical,hybrid}` and `ranking.{lexical,hybrid}` the per-question IDs must equal exactly the 10 fixture question IDs (loaded from `questions.v1.json`); every row must have a string `route` and `scope_violations` with exactly `retrieved`/`sent_to_model`/`cited` lists of strings; violations are recomputed from the rows, every row must be 0, and the summary `scope_violations_total` and `scope_compliance` (numerator/denominator) must match the rows. Every case entry (lifecycle and RAG) must carry a boolean `gating`; lifecycle entries now emit `gating: true`. `repeat_consistent` must hold exactly the 4 passes. | `test_gating.py` new cases: row cites 014 (re-digested) → row + summary failures; 9 rows deleted → IDs differ; malformed ranking row; ranking row leak; compliance lie; missing ranking mode; missing `gating` on a RAG and a lifecycle case; missing ranking repeat flag. Gate on the reports on disk: `[]`. |
| P1: Recall@k could not compare ranking (allowed == gold for all 8 answerable questions) | New versioned `fixtures/rag/ranking_scope.v1.json` (`fixture-analyst-ranking.v1`): one analyst scope of 29 permitted articles, restricted article 014 excluded (loader rejects overlap with restricted or a gold article outside scope). New `ranking` section: same compiled graph, questions, fake model and budget, every question searches the 29-article scope, scope-checked per row (gated). Added Recall@1/@3, MRR and nDCG@10 and per-question `ranks_of_expected`. | Section 6b: lexical vs hybrid now differ (Recall@1 8/12 vs 5/12; MRR 1.0 vs 0.8125; nDCG@10 0.981 vs 0.861). `test_ranking_comparison_searches_permitted_distractors`: 29 articles, 014 never cited, distractors cited (>12 distinct). |
| Note: span metrics cannot judge passage choice | Recorded as a data limitation (gold spans cover each expected article's full text). | Section 10. |

## 1. Environment and base

| Item | Value |
| --- | --- |
| Worktree | `/Users/phungdinhdat/Documents/git/eventlens/.claude/worktrees/rag-eval-ci-t9` |
| Branch | `feature/rag-eval-ci-t9` (uncommitted working tree) |
| Base commit | `6818415b6810236526549c1eaa02face2b08229a` (`fix(build): exclude local worktrees from checks and images`) on `feature/rag-foundation` |
| Host | macOS arm64, Python 3.12, uv 0.12.5, Bun 1.4.0, Docker 29.7.2, `pgvector/pgvector:0.8.0-pg17` (owned, tmpfs, removed after each run) |
| Install | `uv sync --frozen --group dev`, `bun install --frozen-lockfile`; lockfiles unchanged |
| Main checkout | untouched; user dirty files (`.omo/run-continuation/…`, `docs/tasks/`, `docs/de-cuong-do-an-rag.md`) preserved |
| Docs read | `docs/architecture.md`, `docs/designs/ai-rag-briefing-mvp.md`, plan T9/G3/D40. `AGENTS.md` and `RTK.md` **do not exist** in the repo at this base. Plan paths `apps/*` map to `src/backend/{rag,crud,event_pipeline,shared}` and `src/frontend/web`. |
| Tooling note | `uv` and Docker run outside the Claude Code sandbox (sandbox denies `~/.cache/uv` and the Docker socket). |

## 2. Versions recorded in every report

| Kind | Value |
| --- | --- |
| Corpus | `rag-fixture-corpus.v1`, 30 articles, `synthetic-authored`, normalization `nfc.v1` |
| Fixture digest (all `fixtures/rag/**/*.json`) | `sha256:23e664d057d4a592a8770cd06dfb71cfddd2a3a9d4c213cbcd089a13603d5a22` (includes `ranking_scope.v1.json`; same value in both reports' `versions.fixture_digest`) |
| Questions | `rag_questions.v1` (10 questions: 8 answerable, 2 abstain) |
| Semantic labels (new) | `rag_semantic_support.v1`, 8 implementer-authored cases (`fixtures/rag/semantic_support.v1.json`) |
| Gold enrichment | `fixtures/rag/enrichments` (`rag-fixture-enrichment.v1`, model `manual-ground-truth.v1`, prompt `rag-fixture-prompt.v1`) |
| System enrichment | `article-enrichment.v1`, enricher `fake-enricher.v1` / `enrichment-prompt.v1`, retry `enrichment-retry.v1` |
| Index / embedding | fixture embedding `fixture-embedding.v1` (fake embeddings, fixed distance in the in-memory store) |
| Retrieval / synthesis policy | `retrieval-policy.v3` / `synthesis-policy.v1` |
| Chat model | `fixture-chat-model` (DeterministicFakeSynthesizer; scripted synthesizer for cases) |
| Tokenizer | `fixture-regex-tokens@v1` (not a provider tokenizer) |
| Clock | fixed `2026-04-01T00:00:00+00:00` |
| Budget | `context_budget=12000`, `result_limit=20` (same for every mode and case) |
| Report digests (round-5 final run; unchanged since round 3 because timings and error counts live in the measured file) | enrichment `sha256:a806275dae9f7f56171b44eeb0d3c8d78d17952edb51fbc28f52ed3c952c2dd0`; RAG (`repeats=5`) `sha256:be3348b5a8bf364a906140db74c7745dffce602a32713a4ce9ea405b31c6786d` — identical in the 3 report files of this run (`make` target, gate eval, gate rerun; gate `cmp` exit 0). Earlier digests are superseded. |
| Ranking scope | `rag_ranking_scope.v1/fixture-analyst-ranking.v1` (29 permitted, 014 excluded) |

## 3. Command / exit / result (final run)

Round-5 (after review round 4) logs: `.omo/evidence/T9/final-run-r5/`
(`codes` holds the exit codes; includes the bundled-Chromium browser run) and
the gate run `.omo/evidence/G3/run-20261026T105713Z-loW2Kx/` (`exit-codes.tsv`:
21 steps exit 0, `db-unavailable` exit 3 as expected). Earlier logs
(`final-run/`, `final-run-r2..r4/`, older `G3/run-*`) are kept as history and
superseded.

| Command | Exit | Result |
| --- | --- | --- |
| `make check` | 0 | 1083 passed / 139 skipped. The skips are DB-dependent (no DSN) and are **not** DB evidence. Ruff, basedpyright (0 errors), biome, tsc, Vite build passed. |
| `make rag-db-test` | 0 | **1064 passed / 0 skipped** on real PostgreSQL+pgvector (any skip fails the script) |
| `make enrichment-eval` | 0 | report written, gating checks held |
| `make rag-eval` | 0 | report written, gating checks held |
| `make gate-g3` | **2** | by design: every technical step exit 0 → `Technical checks: PASS`, `gate-status = OPEN` (acceptance never decided by the script) |
| `cd src/frontend/web && bun test` | 0 | 69 pass / 0 fail |
| `bun run check` | 0 | biome clean (5 pre-existing-style `useLiteralKeys` infos, not errors) |
| `bun run typecheck` | 0 | clean |
| `bun run build` | 0 | built |
| `git diff --check` | 0 | clean (untracked new files grep-checked for trailing whitespace: none) |

`make gate-g3` step exit codes: sync 0, web-install 0, check 0,
enrichment-eval 0, rag-eval 0, enrichment-eval-rerun 0, rag-eval-rerun 0,
enrichment-eval-reproducible 0 (`cmp`), rag-eval-reproducible 0 (`cmp`),
migrate 0, load-fixtures 0, rebuild 0, api-health 0, api-briefing-citation 0
(`briefing 200 outcome=answered mode=hybrid citations=5`; `source 200
span=[0,126) excerpt_matches=True`), db-suite 0 (1064 passed), database 0,
db-unavailable **3** (expected typed exit), web-test 0, web-check 0,
web-typecheck 0, web-build 0, browser 0 (9 pass / 0 fail).

Cleanup checks after the gate: 0 `eventlens-rag-test-*` containers, no
`uvicorn backend.rag` process. Failure-path cleanup was verified separately by
running `gate-g3-db.sh` with the briefing step replaced by `false` inside
`with-test-db.sh`: exit 1, exit codes `migrate 0 … api-health 0
forced-failure 1`, afterwards 0 containers and no API process.

## 4. Eval architecture

```
fixtures/rag (corpus, gold enrichment, questions, coverage, semantic labels)
        │  evaluation/corpus.py  (one loader, fixture digest, versions)
        ├── make enrichment-eval → eventlens-rag enrichment-eval
        │     EnrichmentWorld (in-memory repo/staging, fixed clock)
        │     → compiled ingestion StateGraph (EnrichmentRunner) with FakeArticleEnricher
        │     → READY metadata vs manual gold (enrichment_metrics.py)
        │     + 8 scripted lifecycle cases (enrichment_cases.py)
        └── make rag-eval → eventlens-rag rag-eval
              one InMemoryRetrievalStore + ONE compose_briefing runtime
              (one compiled briefing StateGraph, one fake model, one budget)
              persist 30 docs → 10 questions (LEXICAL)
              → fake enrichment through the compiled ingestion graph
              → reindex READY metadata + fake vectors → same 10 questions (HYBRID)
              + 16 scenario cases (rag_cases / rag_control / rag_freshness)
              + 8 labelled semantic cases (rag_semantic)
```

- Package `src/backend/rag/evaluation/` is an outer-ring harness like
  `cli_main`. Architecture tests: only `cli_main.py` imports it; it never
  imports OpenAI/httpx/live adapters or `infra.postgres`.
- Each report has a deterministic JSON (`<name>.json`, byte-stable,
  `report_digest`) and a separate `<name>.measured.json` for wall-clock
  numbers that are not reproducible.
- Every rate is `{value, numerator, denominator, definition}`; denominator 0
  gives `value: null` + `not_measurable`.
- Gating (`evaluation/gating.py`) is fail-closed contract checking: schema
  version, required case lists and entry shape, recomputed `held`, safety
  values (fake default, fixture activation, no checkpointer, 0 network
  attempts), repeat consistency, 0 scope violations, matching
  `report_digest`. **No quality threshold exists**; precision/recall/
  abstention/conflict are reported, never gated. `scoped_abstention` is a
  required but informational case (fake-model answer quality).
- Both evals run under `no_network()` (socket connect/connect_ex/
  create_connection blocked and counted).
- Every benchmark question runs inside its fixture `allowed_article_ids`
  scope via `ScopedRetrievalStore` (eval wiring: G1–G3 have no per-article
  ACL), and every retrieved/sent/cited article is checked against that scope.

## 5. Enrichment report (30 articles, fake enricher vs manual gold)

| Metric | Value | n / denominator | Definition / note |
| --- | --- | --- | --- |
| Coverage (READY) | 1.0 | 30/30 | articles READY / gold |
| Topic precision | 0.266667 | 8/30 | correct (article, topic) pairs / predicted |
| Topic recall | 0.205128 | 8/39 | correct pairs / gold pairs |
| Entity precision | not measurable | 0/0 | fake predicts no entities |
| Entity recall | 0.0 | 0/1 | gold has entities only for article 021 |
| Location precision | not measurable | 0/0 | fake predicts no locations |
| Location recall | 0.0 | 0/1 | gold has locations only for article 021 |
| Content type accuracy | 0.966667 | 29/30 | `news_report` P 29/30 R 29/29; `opinion` R 0/1 |
| Event relevance accuracy | 0.066667 | 2/30 | fake always says `uncertain`; P 2/30, R 2/2 for `uncertain`; 0 recall for the other 4 classes |
| Taxonomy validity | 1.0 | 30/30 | published topics in controlled TopicTag; suggested tags promoted to topics: 0 |
| Evidence support (structural) | 1.0 | 9/9 | published spans: hash matches, offsets select excerpt |
| Evidence support (semantic) | NOT MEASURED | — | no labelled topic-evidence entailment set |
| Confidence calibration | Brier 0.313333, ECE 0.366667 | 30 | constant confidence 0.6, exact-topic-set accuracy 0.233333 (one bin) |
| Topic abstention (`other`) | appropriate 0.047619 | 1/21 | abstention recall 1/1 |
| Relevance abstention (`uncertain`) | appropriate 0.066667 | 2/30 | abstention recall 2/2 |
| Hallucination: topic | 0.733333 | 22/30 | predicted topics absent from gold / predicted |
| Hallucination: entity / location | not measurable | 0/0 | nothing predicted |
| Lifecycle cases held | 1.0 | 8/8 | see below |
| Network guard | 0 attempts | — | enricher `FakeArticleEnricher` (gated) |
| Tokens (fixture tokenizer) | input 3421, output 2821 | 30 calls | 0 unmeasured calls |
| Cost estimate | NOT MEASURED | — | no enrichment pricing policy; fake cost 0 |
| Latency (measured, this Mac, fake) | provider call p50 0.086 ms, p95 0.11 ms | 30 | not reproducible, not an SLO |

Lifecycle cases (expected fixed from the T4 contract before running):
`unsupported_taxonomy` → FAILED `unsupported_taxonomy`, 0 published, document
retained; `ungrounded_entity` → FAILED `ungrounded_metadata`;
`missing_evidence_reference` → FAILED `ungrounded_evidence`;
`poisoned_suggested_tag` → READY, topics stay `[transportation]`, injection
text kept only as a suggestion; `timeout_then_success` → RETRY_SCHEDULED, 0
claimable before backoff, READY on attempt 2; `retry_exhaustion` → 2×
RETRY_SCHEDULED then FAILED, 3 provider calls; `stale_revision_mid_call` →
LOST_OWNERSHIP then READY for the new hash only; `cancel_stops_further_work`
→ CANCELLED, 1 provider call, 0 embedding stage calls, 0 publications, second
job not claimed.

Failure noted honestly: the first run of `stale_revision_mid_call` expected
`STALE`; the system returned `LOST_OWNERSHIP`. The established T4 contract
(`tests/backend/rag/workflows/test_enrichment_graph.py::test_change_while_processing_blocks_old_publication`)
specifies `LOST_OWNERSHIP`, so the expectation was corrected to the contract,
with that test cited in the code. The safety property (old revision never
published) held in both runs.

## 6. RAG report (10 questions × lexical/hybrid, same graph/corpus/model/budget, per-question scope)

Per-question (repeats=5, all repeats identical; scope = fixture
`allowed_article_ids`; 0 scope violations in every row):

| Q | lexical route / mode | hybrid route / mode | citations lex / hyb | input tok lex / hyb | fixture cost lex / hyb |
| --- | --- | --- | --- | --- | --- |
| 01 | answered / lexical_only | answered / hybrid | 2 / 2 | 1908 / 1908 | 9908 / 9908 |
| 02 | answered / lexical_only | answered / hybrid | 2 / 2 | 1896 / 1896 | 9896 / 9896 |
| 03 | answered / lexical_only | answered / hybrid | 2 / 2 | 1916 / 1916 | 9916 / 9916 |
| 04 | answered / lexical_only | answered / hybrid | 1 / 1 | 1730 / 1730 | 9730 / 9730 |
| 05 (abstain) | answered / lexical_only | answered / hybrid | 1 / 1 | 1724 / 1724 | 9724 / 9724 |
| 06 | answered / lexical_only | answered / hybrid | 1 / 1 | 1737 / 1737 | 9737 / 9737 |
| 07 (abstain) | **abstained_no_evidence** / none | answered / **vector_only** | 0 / 1 | 0 / 1734 | 0 / 9734 |
| 08 | answered / lexical_only | answered / hybrid | 2 / 2 | 1904 / 1904 | 9904 / 9904 |
| 09 | answered / lexical_only | answered / hybrid | 1 / 1 | 1729 / 1729 | 9729 / 9729 |
| 10 | answered / lexical_only | answered / hybrid | 1 / 1 | 1738 / 1738 | 9738 / 9738 |

Output tokens charged: 1500 per model call (usage MISSING → full reservation).

Aggregates:

| Metric | Lexical | Hybrid | Definition |
| --- | --- | --- | --- |
| Scope compliance | 1.0 (10/10), 0 violations | 1.0 (10/10), 0 violations | no out-of-scope article retrieved, sent or cited (gated) |
| Recall@5 | 1.0 (12/12) | 1.0 (12/12) | expected articles in top 5 / expected (8 answerable) |
| Recall@10 | 1.0 (12/12) | 1.0 (12/12) | same, top 10 |
| Answered | 0.9 (9/10) | 1.0 (10/10) | questions answered |
| Citation structural validity | 1.0 (13/13) | 1.0 (14/14) | span sent, current, offset-exact, bound to a claim (recomputed independently of T6) |
| Citation gold article precision | 0.923077 (12/13) | 0.857143 (12/14) | distinct cited articles in expected set / distinct cited |
| Citation gold span exact precision | 0.923077 (12/13) | 0.857143 (12/14) | citation offsets equal a gold span / citations |
| Citation gold span overlap precision | 0.923077 (12/13) | 0.857143 (12/14) | citation overlaps a gold span of the same article / citations |
| Gold span coverage | 1.0 (12/12) | 1.0 (12/12) | gold spans overlapped by ≥1 citation / gold spans |
| Claims verbatim in cited span | 1.0 (13/13) | 1.0 (14/14) | copy check only, not entailment |
| Abstention correct | 0.5 (1/2) | 0.0 (0/2) | abstain questions that abstained |
| False abstention | 0.0 (0/8) | 0.0 (0/8) | answerable questions that abstained |
| Conflict flagged | 0.0 (0/1) | 0.0 (0/1) | Q02 has a `source_conflict` uncertainty |
| Time basis reported | 1.0 (10/10) | 1.0 (10/10) | `source_published_at` |
| Temporal claims emitted | 0 | 0 | fake model emits none |
| Temporal accuracy | NOT MEASURED | NOT MEASURED | no gold occurrence values per question in fixtures v1 |
| Tokens input / output charged | 16282 / 13500 (9 calls) | 18016 / 15000 (10 calls) | fixture tokenizer |
| Cost estimate | 88282 | 98016 | `fixture_micro_usd` (synthesis-policy.v1 fixture rates, not a price) |

Reading: inside each scope, allowed == expected articles for every
answerable question, so Recall@k is near-trivial and is **not** a ranking
quality signal; an unscoped ranking diagnostic was intentionally dropped
because it would retrieve restricted articles. Gold spans in fixtures v1
span the whole article text, so span metrics equal article metrics here.
The only lexical/hybrid difference is Q007: lexical finds nothing in scope
and abstains; fake vectors (fixed distance) still return the one allowed
article, so hybrid answers `vector_only` — a fake-embedding artifact, not
evidence about real hybrid retrieval.

### 6b. Ranking comparison (one 29-article analyst scope with distractors)

Same compiled graph, 10 questions, fake model and budget; every question
searches all 29 permitted articles (014 excluded); 0 scope violations in every
row of both modes (gated).

| Metric | Lexical | Hybrid | Definition |
| --- | --- | --- | --- |
| Recall@1 | 0.666667 (8/12) | 0.416667 (5/12) | expected articles at rank 1 / expected (8 answerable) |
| Recall@3 | 0.916667 (11/12) | 0.916667 (11/12) | top 3 |
| Recall@5 | 1.0 (12/12) | 1.0 (12/12) | top 5 |
| Recall@10 | 1.0 (12/12) | 1.0 (12/12) | top 10 |
| MRR | 1.0 (n=8) | 0.8125 (n=8) | mean 1/rank of first expected article |
| nDCG@10 | 0.981293 (n=8) | 0.860739 (n=8) | binary gain on expected articles |
| Citation gold article / span precision | 0.521739 (12/23) | 0.06 (12/200) | distinct cited articles in expected set; citations overlapping a gold span |
| Gold span coverage | 1.0 (12/12) | 1.0 (12/12) | gold spans overlapped by ≥1 citation |
| Citation structural validity | 1.0 (23/23) | 1.0 (200/200) | recomputed independently |
| Answered / abstention correct / conflict flagged | 10/10, 0/2, 0/1 | 10/10, 0/2, 0/1 | |
| Tokens input / output charged | 19560 / 15000 | 49148 / 15000 | 10 calls each |
| Fixture cost | 99560 | 129148 | `fixture_micro_usd`, not a price |

Ranks of expected articles (lexical → hybrid): Q01 [1,2]→[1,2]; Q02 [2,1]→[2,1];
Q03 [2,1]→[2,1]; Q04 [1]→[2]; Q06 [1]→[1]; Q08 [5,1]→[5,2]; Q09 [1]→[2];
Q10 [1]→[1]; Q05/Q07 have no gold articles.

Reading: with distractors, hybrid ranks worse than lexical (fake vectors at
a fixed distance inject 20 arbitrary candidates into RRF and send them to the
model). This is a measurement of the fake embedding adapter on 8 answerable
smoke questions, not evidence about real hybrid retrieval quality, and no
threshold is applied.

Structural vs semantic support (8 labelled cases, scripted model emits the
labelled claim citing a real sent span): structural acceptance **8/8**;
labelled supported among accepted **2/8**; **6 accepted claims are labelled
unsupported or contradicted** (e.g. invented cause, wrong time,
publication-as-occurrence, injected instruction). Automatic semantic check:
NOT MEASURED (no entailment judge). Labels are implementer-authored, not an
independent annotation.

Scenario cases (16, all required by the gate; 15 gating, all held;
security 5/5):

| Case | Gating | Held | Observed |
| --- | --- | --- | --- |
| restricted_article_never_sent (Q05/Q07 scoped) | yes | yes | restricted article 014 never in any model payload |
| prompt_injection_data_channel | yes | yes | 1 distinct system prompt; injection reaches model only as data |
| model_cites_unsent_restricted_span | yes | yes | `synthesis_grounding_failed`, no answer, 1 call |
| model_returns_raw_prose | yes | yes | `synthesis_malformed_output`, no fallback |
| access_revoked | yes | yes | `synthesis_access_denied`, 0 model calls, 0 store reads |
| cancel_before_start | yes | yes | `synthesis_cancelled`, 0 calls, 0 reads |
| cancel_during_model_call | yes | yes | `synthesis_cancelled`, 1 call, 0 reads after cancel |
| budget_context_too_small | yes | yes | `synthesis_budget_exceeded` / `context_too_small`, 0 calls |
| concurrency_capacity_full | yes | yes | `synthesis_concurrency_rejected`, 0 calls; answered after release |
| concurrency_isolation | yes | yes | 10 concurrent = 10 sequential results; fresh runtime matches |
| storage_outage_and_recovery (in-memory) | yes | yes | `retrieval_storage_unavailable`, 0 calls, 30 docs retained, answered after recovery |
| enrichment_outage_keeps_lexical | yes | yes | RETRY_SCHEDULED only, 0 reindexed, answered `lexical_only` |
| model_outage_typed_no_fallback | yes | yes | `synthesis_provider_failed`, no answer |
| freshness_revision | yes | yes | new hash cited at once; old reference `SourceStaleError`; no stale metadata served |
| freshness_tombstone | yes | yes | tombstoned article cited before, never after |
| scoped_abstention (Q05/Q07) | **no (informational)** | **no** | both `answered`: fake model answers whatever is sent and fake vectors always return candidates |

Safety section: default model `DeterministicFakeSynthesizer`, activation
`fixture`, briefing graph checkpointer `None`, network connect attempts 0.

Measured (`rag-eval.measured.json`, round 5; this Mac, fake adapters;
**not reproducible, not a production SLO; the 8 s p95 target remains
unmeasured**). Graph overhead = use-case wall − retrieval stage latencies −
synthesis total. 5 repeats × 10 questions = 50 samples per pass;
`execution_errors` = 0 in every pass:

| Pass | End-to-end p50 / p95 / max (ms) | Graph overhead p50 / p95 / max (ms) | Samples | Errors |
| --- | --- | --- | --- | --- |
| modes.lexical (question scope) | 8.284 / 11.989 / 16.307 | 3.422 / 5.842 / 9.363 | 50 / 50 | 0 |
| modes.hybrid (question scope) | 8.541 / 10.21 / 15.149 | 3.542 / 5.123 / 8.343 | 50 / 50 | 0 |
| ranking.lexical (29-article scope) | 10.601 / 28.324 / 73.358 | 3.823 / 14.894 / 24.652 | 50 / 50 | 0 |
| ranking.hybrid (29-article scope) | 24.295 / 57.81 / 80.7 | 4.141 / 9.672 / 16.396 | 50 / 50 | 0 |

Wall-clock tails vary between runs on the same machine (round 4 had
ranking.lexical p95 10.178 ms); only the sample counts and error counts are
stable. Ranking hybrid is slower because it sends 20 articles per question to the
fake model; orchestration overhead stays similar across passes.

## 7. E2E on real PostgreSQL/pgvector (`tests/backend/rag/db/test_t9_e2e.py`, 7 tests, in DB suite)

- `persist → lexical → enrich → reindex → query → inspect citation`: worker
  runtime from `build_runtime` (compiled ingestion graph, PostgreSQL repo,
  fake enricher), query via `compose_briefing` (compiled briefing graph,
  PostgreSQL retrieval + source references). Lexical answer has no metadata;
  enrichment READY; hybrid answer carries metadata; `InspectSource` resolves
  the citation to the exact span.
- Enrichment model outage (`Provider.FAKE_TIMEOUT`): RETRY_SCHEDULED, lexical
  answer still cited.
- Unsupported tag / poisoned metadata / missing evidence (scripted drafts):
  FAILED with the matching code, 0 READY rows, lexical answer still served.
- DB outage/recovery: a loopback TCP proxy (`tests/support/tcp_proxy.py`)
  cuts the real connection while PostgreSQL keeps running. Query →
  `RetrievalStorageError` (`retrieval_storage_unavailable`); ingest →
  `StorageUnavailableError`; row counts (documents, READY jobs) identical
  before, during and after (1, 1); after restart the **same runtime** answers
  hybrid with the same cited spans. HTTP boundary: 503 `retrieval_unavailable`
  during the cut, 200 after recovery.

## 8. Browser gate

`src/frontend/web/e2e/layout.e2e.ts` (T8 script, extended): 9 tests, 9 pass,
run against the built app with Google Chrome locally. New `request lifecycle`
suite at **320 px and 1280 px**: slow A then fast B (loading status +
`aria-busy`, B wins, late A never replaces it, no page/console errors);
navigation away before A returns (server sees the abort, app reloads idle, no
errors); empty (no-evidence), typed error (`Model unavailable`), retry, and
citation inspection. Component-level unmount-before-answer and late-A tests
were added to `BriefingWorkspace.test.tsx` (React unmount aborts the request,
no console error) — a true React unmount cannot be triggered from the
browser without a test hook, so it is covered at component level.

CI uses Playwright's pinned Chromium (`EVENTLENS_BROWSER=chromium`,
`bunx playwright-core install --with-deps chromium`, revision 1248 pinned by
the locked `playwright-core@1.64.1`). Executed locally on macOS
arm64: round 3 `bunx playwright-core install chromium` exit 0 (Chrome Headless
Shell 156.0.8078.4, v1248, `final-run-r3/ci-browser-install.log`); rounds 4 and 5
`EVENTLENS_BROWSER=chromium bun run test:e2e` exit 0, 9 pass / 0 fail
(`final-run-r5/ci-browser-bundled.log`; also round 4). The
Linux `--with-deps` install ran green on `ubuntu-latest` in CI run
40319409530.

## 9. CI (`.github/workflows/ci.yml`)

Four jobs, all from frozen installs, `permissions: contents: read`. Each of
the four jobs (`check`, `database`, `eval`, `browser`) has an "Assert no
provider credentials in the environment" step (round 3 review found it
missing in `browser`; added in round 4 and verified by parsing the YAML:
1 such step per job):
`check` (`make check`, with a note that its DB skips are not DB evidence),
`database` (pgvector service, `gate-g3-db.sh`: migrate, load fixtures,
rebuild, loopback rag-api smoke, full DB suite with skip-is-failure),
`eval` (`make enrichment-eval`, `make rag-eval`, rerun + `cmp`), `browser`
(pinned Chromium, `bun run test:e2e`). The workflow ran on GitHub for PR #3
(see "CI on GitHub" at the top). Local equivalents executed:
`make check`, the gate's `gate-g3-db.sh` steps inside the isolated pgvector
harness (same script the `database` job runs against its service), both evals
plus rerun/`cmp`, and the bundled-Chromium browser path on macOS. YAML parsed
locally only.

## 10. Limits and open items

- NOT MEASURED: live model quality, live latency/cost, real tokenizer usage,
  analyst time saved (D42), automatic semantic entailment, temporal accuracy
  against gold, enrichment cost, pgvector ANN/EXPLAIN plans in T9 (T5's
  `vector-route-report` remains the source).
- 10 smoke questions and 30 synthetic articles prove wiring, accounting and
  safety control flow, not answer quality. Fake latency/cost figures are not
  quality evidence or SLOs.
- Gold entity/location labels exist only for article 021; semantic labels
  are implementer-authored (8 cases).
- Fake synthesizer: never abstains once evidence is sent, never flags
  conflicts, emits no temporal claims; fake embeddings always return in-scope
  candidates (Q007 hybrid answers vector_only).
- In-memory eval store approximates PostgreSQL FTS; real DB evidence is the DB
  suite (1064 passed / 0 skipped) and the gate's rag-api smoke.
- Per-question scope is eval wiring (`ScopedRetrievalStore`); the product has
  no per-article ACL in G1–G3, so production scope enforcement is not proven
  by this eval. Recall in `modes` is near-trivial (allowed == expected); the
  ranking comparison is the `ranking` section (8 answerable questions only).
- Gold spans in fixtures v1 cover each expected article's full text, so span
  metrics equal article metrics and cannot judge passage selection; a
  passage-level gold set is needed for that.
- `.omo/evidence/` is gitignored: this file and `.omo/evidence/T9/` need
  `git add -f` if they should be committed.
- G3 and F1–F4: **OPEN** (no independent review yet). T10–T15 untouched. No
  live provider enabled.
- PR #3 also contains the 19 T1–T8 commits that were never on `develop`;
  merging it lands T1–T9 together (owner decision). Merge needs CI green plus
  one reviewer approval per git flow.
