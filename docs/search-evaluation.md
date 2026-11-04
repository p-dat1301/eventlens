# Search evaluation (search-selection benchmark)

Code: `src/backend/rag/evaluation/search_eval/` (dataset, scoring, systems,
factory, runner, review), `evaluation/ollama_bench.py`. Fixture:
`fixtures/rag/search-selection/` (`LABELING.md`). Offline only: nothing here is
wired into rag-api.

## Two kinds of runs

| Run | Embeddings / reranker | Proves |
| --- | --- | --- |
| mechanics (default `make search-eval`) | `fake-bge-m3` (feature hashing) / `fake-overlap-reranker` | wiring, filters, scope traps, offsets, repeatability, failure handling — **not** model quality |
| real models | `BAAI/bge-m3` dense 1024 (`bge-m3-1024.v1`) / `BAAI/bge-reranker-v2-m3`, pinned snapshot revisions, local Hugging Face cache only | retrieval quality on the pilot |

A real-model run never falls back to fakes: a missing revision, library
(`bench` group) or cached weight fails the run.

## Configurations

`postgres-lexical` (production PostgreSQL FTS branch, `simple` lexemes,
`codepoint-1200-200.v1`, isolated empty database), `es-bm25`, `es-semantic`,
`es-hybrid` (BM25 top 50 + kNN top 50, RRF k=60, pool 45), `es-hybrid-rerank`
(hybrid + reranker on the top 40). In-process `memory-*` variants run without
Docker (CI tests). Each configuration runs at least 3 times over the same
corpus, questions and qrels. Every index-backed configuration also indexes trap
copies (other scope, tombstoned, superseded) that must never be returned.

## Metrics

Recall@1/3/5/10 and MRR over the article ranking (grade-2 required articles),
graded nDCG@10, article recall@10 (grade ≥ 1), passage recall@10 (gold span
fully inside a top-10 chunk), citation exact/overlap support@5, freshness@1
(update questions whose top article is the latest), no-answer empty rate,
scope violations (trap hits), repeat consistency (identical top-10 in every
repeat), execution errors, latency p50/p95 with sample counts, rerank outcomes
(`applied`/`degraded`/`timeout`), intent-classifier agreement. Retrieval alone
cannot abstain, so false-answer rate and no-answer precision come from the
synthesis benchmark (`ollama-bench`).

Each report has two files: `<label>.json` (deterministic, `report_digest`) and
`<label>.measured.json` (latency, indexing, metadata: git commit, dirty flag,
`worktree_digest` = SHA-256 of `git diff HEAD` plus untracked files excluding
`.omo/`/`.private/`, model revisions, device).

## Running

Prerequisites: Docker (Compose v2 with `--wait`), uv, ~2 GB free RAM for the
evaluation containers, and for real models `uv sync --group dev --group bench`
plus the snapshots in `~/.cache/huggingface`.

```sh
# mechanics (fake models), owned throwaway ES + PostgreSQL, >= 3 repeats
make search-eval
# real models (same configurations, pinned revisions, offline cache)
EVENTLENS_RAG_BGE_M3_REVISION=5617a9f61b028005a4858fdac845db406aefb181 \
EVENTLENS_RAG_RERANK_REVISION=953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e \
HF_HUB_OFFLINE=1 make search-eval SEARCH_EVAL_EMBEDDINGS=bge-m3 \
  SEARCH_EVAL_RERANK=cross-encoder SEARCH_EVAL_DEVICE=mps SEARCH_EVAL_LABEL=search-eval-bge-m3
# Elasticsearch adapter + PG -> outbox -> ES end-to-end suite (any skip fails)
make search-index-test
# pooled review sheet for a second annotator (CSV + HTML under .private/)
uv run eventlens-rag search-review-sheet
```

Make variables: `SEARCH_EVAL_CONFIGS`, `SEARCH_EVAL_REPEATS` (≥ 3),
`SEARCH_EVAL_EMBEDDINGS`, `SEARCH_EVAL_RERANK` (`none|fake|cross-encoder`),
`SEARCH_EVAL_DEVICE`, `SEARCH_EVAL_LABEL`, `SEARCH_ARTIFACT_DIR`, `DOCKER`.
External endpoints (`EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL`,
`EVENTLENS_RAG_EVAL_DATABASE_URL` — the database must be empty) are used as-is
and never started or torn down.

### Ownership, ports, memory

- Runs use `docker/search-eval.compose.yaml` under project
  `eventlens-search-eval-<run-id>`; scripts refuse any project not named
  `eventlens-search-dev` or `eventlens-search-eval-*`, so the shared `eventlens`
  project (web, rag-api) is never touched. Cleanup is
  `--profile postgres down --volumes --remove-orphans` for that project only,
  then verified with `ps --all` (`cleanup = verified | failed | not-owned`;
  an unverified cleanup turns success into exit 5).
- Ports bind to 127.0.0.1 and are chosen by Docker (never 9200) for runs; the
  developer ES (`make search-eval-up`) uses `SEARCH_ES_PORT` (default 9200) and
  refuses a port already served outside its project.
- Elasticsearch heap `EVENTLENS_SEARCH_ES_HEAP` (default 512m) inside a
  container limit `EVENTLENS_SEARCH_ES_MEMORY` (default 1536m, swap equal);
  PostgreSQL `EVENTLENS_SEARCH_PG_MEMORY` (512m). Verified with `docker inspect`
  (`HostConfig.Memory=1610612736`, heap max 536870912) in
  `.omo/evidence/search-platform/final-gates/es-memory-smoke/`.
- Artifacts per run: `run.env` (no URLs or credentials), `steps.tsv` (each step
  actually run and its exit code), one log per step, `diagnostics-*.log` on
  failure, `cleanup`, `exit-code`, reports (and `junit.xml` for the suite).
  Exit codes: benchmark/pytest code; 2 skipped tests; 3 startup failed;
  4 dependency not healthy or migration failed; 5 cleanup not verified;
  130/143 interrupted.

## External corpus (real handoff batch)

The same benchmark runs on a data-team handoff batch plus a
`search-questions.v1` labels file; the fixture stays the CI regression set.
Paths are operator inputs (never in code); nothing with publisher text is
written outside the directories the operator names.

```sh
SEARCH_EVAL_ARTICLES=/path/articles.jsonl SEARCH_EVAL_MANIFEST=/path/manifest.json \
SEARCH_EVAL_QUESTIONS=fixtures/rag/search-selection/search-test-200-v1/questions.json \
SEARCH_EVAL_PRESCREEN=.private/<run>/labels/ai-prescreen.json \
SEARCH_EVAL_EXPECT_EXPORT_ID=search-test-200-v1 SEARCH_EVAL_EXPECT_ARTICLES=200 \
SEARCH_EVAL_COMPARE="es-bm25@codepoint-1200-200.v1:es-bm25@paragraph-900-1.v1" \
SEARCH_EVAL_REVIEW_DIR=.private/<run>/review \
make search-eval SEARCH_EVAL_CONFIGS="postgres-lexical,es-bm25@codepoint-1200-200.v1,es-bm25@paragraph-900-1.v1,es-semantic@paragraph-900-1.v1,es-hybrid@paragraph-900-1.v1,es-hybrid-rerank@paragraph-900-1.v1"
```

- The batch is validated (`handoff-validate`), imported atomically into the
  run's empty `rag_eval` database (`ImportHandoff`, provenance + outbox) and
  the revisions are **read back from PostgreSQL**; the read-back must equal the
  batch exactly. Labels must name the same `export_id` and corpus digest.
- `--expect-export-id` / `--expect-articles` turn a missing or different
  corpus into exit 2; the fixture is never substituted.
- `name@chunker` picks the chunker per configuration
  (`codepoint-1200-200.v1` | `paragraph-900-1.v1`); analyzer, boosts,
  filters and depths are identical, so `es-bm25@codepoint…` vs
  `es-bm25@paragraph…` isolates chunking. Gold passages are code-point spans
  on the RAG text (checked by `excerpt_sha256`), so coverage is recomputed for
  every chunk set (`gold_span_ceiling`) and never uses old chunk IDs.
- Questions keep `question_id`, `split`, `label_status` and `origin`
  verbatim; an AI prescreen is a separate `ai_prescreen` field and never
  upgrades a label. Label problems are listed in `dataset.label_issues`.
- Query clock = manifest `exported_at` (PostgreSQL freshness bonus input).
  Elasticsearch ranking has no freshness term; the batch is current-only, so
  `freshness@1` has no samples (no update questions).
- Report additions: dataset kind/export/articles SHA-256/label digest/label
  status counts/splits, per configuration `chunks` (count, length
  distribution), `gold_span_ceiling`, `metrics_by_split` (dev/test),
  `breakdown` (category, derived numeric-query, source, article-length
  tercile, parser/normalization), text-free `per_question` rows. With
  `--compare`, `<label>.compare.json` has paired bootstrap CIs (10,000
  resamples, seed 20261028) and win/loss/tie per split.
- `--checkpoint-dir` (the script sets `$ART/checkpoints`) saves each finished
  configuration; `SEARCH_EVAL_RESUME=1` reuses checkpoints whose key (corpus,
  labels, repeats, commit, worktree digest) matches and never re-imports.
- `--review-dir` writes the private review sheet: question, label status,
  AI prescreen, filters, reference answer marked as a machine summary, gold
  quotes cut by offsets, top-5 chunks of every configuration with source/date,
  gold coverage, full-article pages and original URLs, `review.csv` with blank
  reviewer columns.
- `ollama-bench --articles … --question-ids <fixed list> --evidence-config
  es-bm25@paragraph-900-1.v1 --elasticsearch-url …` runs the synthesis
  benchmark on evidence from that configuration. Protocol
  (`ollama_synthesis_bench.v2`): the model digest is pinned before any
  generation (no digest → exit 2, nothing generated); a payload longer than
  `--evidence-k` stops the run unless `--allow-evidence-truncation` (the cut is
  then reported); every response (prompt, raw output, usage, latency, evidence
  identity) is written atomically to `--checkpoint` (must be under `.private/`)
  as soon as it arrives; a rerun reuses a response only when its request
  identity (corpus, query, exact evidence and order, retrieval configuration,
  model digest, prompt/schema versions, generation options) matches, keeps the
  original run id, and reports replayed latency separately from network
  latency. Unload / final-digest failures are auxiliary errors; the report
  status is `complete | partial | failed` (exit 0 only when complete) and every
  rate keeps all scheduled questions in view (`outcomes`: answered, abstained,
  schema_invalid, execution_error, missing). `--plan-only` retrieves and sizes
  prompts without generating; `ollama-bench-report --checkpoint …` rebuilds the
  report offline; `--shared-output` writes a text-free copy.

## Product pipeline configurations (`name#profile`)

`es-hybrid@paragraph-900-1.v1#fill6`, `postgres-lexical#baseline`, … run the
production `RetrieveEvidence` with the named retrieval tuning
(`evaluation/search_eval/pipeline.py`) at the public API defaults
(`result_limit` 5, `context_budget` 12 000; `+rN` sets `result_limit` N,
1–20). Three points are scored on the same question:

* **A** candidates (fused, deduplicated): the ranking metrics above;
* **B** selection (`RetrieveEvidence.select`, first `result_limit` articles);
* **C** payload after the PostgreSQL final check and token packing.

`evidence` / `evidence_by_split` / `evidence_by_category` report grade-2
article coverage, all-required (multi-source completeness), gold-span
exact/overlap coverage, distinct articles, spans per article, overlap/cap/
branch-cap drops, backfill calls, selected-but-not-sent spans, tokens,
freshness decisions and fill spans; `calls` the storage/embedding calls;
per-question rows add `b_*`, `c_*` and `c_payload` offsets, and `--compare`
adds paired `b_/c_` metrics. Index profiles over one base share one index
build (identical chunks/embeddings). Checkpoint keys include the full tuning.
`--fixture-dir` (`SEARCH_EVAL_FIXTURE_DIR`) scores another synthetic fixture,
e.g. `freshness-updates-v1`; `ollama-bench --evidence-config name#profile
--evidence-k 15` sends the model the exact payload.

## Real batch results (2026-10-28, `search-test-200-v1`, 3 repeats)

Corpus: 200 real articles (10 sources × 20), `articles.jsonl` SHA-256
`bdb79182…c644ab`, corpus digest `sha256:30e474d5…` (RAG revisions read back
from PostgreSQL), labels `search-questions.v1` digest `sha256:eb00d9b6…`:
96 questions, dev 38 / test 58, **all `candidate` (machine-drafted), 0
human-reviewed**; an AI prescreen flags 17 `needs_fix`. Query clock
`exported_at`. Evidence (text-free): `.omo/evidence/search-platform/real-200/`
(reports, paired comparisons, `error-analysis.md`, `chunker-comparison.md`,
`import-idempotency.md`, `qa-review.md`, `final-decision.md`).

Real models (BGE-M3 `5617a9f6…`, bge-reranker-v2-m3 `953dc6f6…`, MPS),
**test split** (53 answerable / 5 no-answer):

| Config | chunker | R@1 | R@10 | MRR | nDCG@10 | passage R@10 | cite exact@5 | p50 / p95 ms (n=288) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A postgres-lexical | codepoint | 0.827 | 0.953 | 0.934 | 0.924 | 59/70 | 58/70 | 187 / 206 |
| B es-bm25 | codepoint | 0.805 | 0.943 | 0.918 | 0.914 | 65/70 | 60/70 | 10 / 19 |
| C es-bm25 | paragraph | 0.805 | 0.943 | 0.917 | 0.913 | 64/70 | 55/70 | 9 / 13 |
| D es-semantic (BGE-M3) | paragraph | 0.871 | 1.000 | 0.972 | 0.974 | 70/70 | 67/70 | 43 / 50 (+33 embed) |
| E es-hybrid (RRF) | paragraph | 0.843 | 1.000 | 0.954 | 0.960 | 68/70 | 61/70 | 51 / 63 |
| F E + reranker (40) | paragraph | 0.890 | 1.000 | 0.981 | 0.981 | 70/70 | 68/70 | 3,357 / 3,998 (rerank 2,970) |

All: 0 errors, 0 scope violations, repeat consistency 96/96, metrics identical
across repeats. Paired bootstrap (test): D − C nDCG +0.061 [+0.001, +0.135];
E − C +0.047 [+0.013, +0.088]; C − B −0.001 [−0.004, 0] (52/53 ties);
F − E +0.021 [−0.007, +0.056]; E − A +0.036 [−0.027, +0.102]; B − A −0.010
[−0.101, +0.076]. Selected on dev by nDCG@10: D (dev 0.991). Freshness has no
samples (current-only batch); the index path has no freshness term.

**Decision: keep PostgreSQL default.** Semantic/hybrid beat BM25 on this
batch with CIs excluding 0, but not PostgreSQL itself (E − A CI contains 0),
the labels are unreviewed machine candidates, the sample is 53 test
questions, the paragraph chunker shows no gain on the same engine, the
reranker costs ~3 s per query for a non-significant gain, and the index path
lacks a per-article chunk cap and freshness. Old pilot numbers (`search-
selection` PR #7, other chunk sets and engines) are not directly comparable.

## Pilot results (2026-10-28, `search-selection.v1`, 3 repeats)

Fixture digest `sha256:3480d494…`, corpus `sha256:080a7c40…`, commit `3fe5fc0`
+ uncommitted tree (`worktree_digest` in the measured reports). Full reports:
`.omo/evidence/search-platform/final-benchmark/{fake-mechanics,real-bge-m3}/`.

Real models (BGE-M3 `5617a9f6…`, reranker `953dc6f6…`, MPS):

| Config | R@1 | R@3 | MRR | nDCG@10 | passage R@10 | cite exact@5 | fresh@1 | scope viol. | p50 / p95 ms (n=120) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| postgres-lexical | 0.886 | 0.986 | 0.986 | 0.978 | 1.0 | 0.975 | 1.00 | 0 | 8.6 / 12.5 |
| es-bm25 | 0.857 | 1.0 | 0.971 | 0.973 | 1.0 | 1.0 | 0.75 | 0 | 7.5 / 13.8 |
| es-semantic | 0.829 | 1.0 | 0.986 | 0.972 | 1.0 | 1.0 | 0.50 | 0 | 39.9 / 50.3 |
| es-hybrid | 0.829 | 1.0 | 0.971 | 0.967 | 1.0 | 1.0 | 0.75 | 0 | 43.7 / 51.3 |
| es-hybrid-rerank | 0.829 | 1.0 | 0.986 | 0.972 | 1.0 | 1.0 | 0.50 | 0 | 1097.9 / 1193.2 (40/40 applied) |

All configurations: repeat consistency 1.0, zero errors, zero scope violations.
One question changes a recall@1 by 0.029; every difference above is ≤ 2
questions out of 35 answerable ones, on single-annotator synthetic labels.

**Decision: INCONCLUSIVE.** The pilot cannot separate the configurations
(ceiling effect). PostgreSQL lexical leads on freshness because
`retrieval-policy.v3` adds a freshness bonus that the index path does not
have. The reranker adds ~1.1 s p50 on MPS with no measurable gain. Keep the
defaults (PostgreSQL retrieval; Elasticsearch/hybrid opt-in or shadow;
reranker off; multi-hop off).

To choose a production configuration: a private 2k–5k article corpus with
real questions, two independent annotators (review sheet), confidence
intervals over repeated runs, a freshness-aware index ranking, p95 inside the
SLO under load, and measured cost.

## Ollama synthesis benchmark (offline)

`eventlens-rag ollama-bench` sends identical prompts and evidence (top 5
`memory-bm25` chunks) to each model in turn (`seed 42`, `temperature 0`,
512 output tokens, schema-constrained JSON, `think: false`, model unloaded
before the next) and reports model digest, latency, token counts, schema
validity, citation linkage, gold-span support (mechanical proxy), false
abstention, false answers on no-answer questions and unsupported claims.
The endpoint (`EVENTLENS_EVAL_OLLAMA_BASE_URL`) must be loopback or a private
network IP literal (RFC 1918 or Tailscale/CGNAT `100.64.0.0/10`); public hosts
and names are refused because Ollama has no authentication. Models come from
`EVENTLENS_EVAL_OLLAMA_MODEL` (comma list), `EVENTLENS_EVAL_OLLAMA_NUM_THREAD`
sets `num_thread` for every model alike. The per-call timeout (`--timeout`,
default 600 s) is an offline benchmark value, not the production 15 s. It
never touches provider activation or the production synthesizer.

```sh
EVENTLENS_EVAL_OLLAMA_BASE_URL=http://100.105.48.32:11434 \
EVENTLENS_EVAL_OLLAMA_MODEL=qwen3.5:4b,qwen3.5:9b \
EVENTLENS_EVAL_OLLAMA_NUM_THREAD=8 \
uv run eventlens-rag ollama-bench --label ollama-bench-qwen3.5 --timeout 900
```

Status 2026-10-28: see the v2 run below. The first 4b run (v1) is kept as
`ollama-bench-qwen3.5-4b.v1-schema-unverified.json` but its schema metric is
**unverified**: its judge coerced types (`bool("false")`) and did not require
`answer`, and raw responses were not retained, so it cannot be re-scored.

Judge v2 (`parse_output`): strict Pydantic model of `OUTPUT_SCHEMA` (exact JSON
types, required `answer` that may be null, non-empty claim text and sources, no
extra keys; the schema sent to Ollama also sets `additionalProperties: false`)
plus abstention invariants (abstain ⇒ `answer: null` and no claims; answer ⇒
non-empty answer and ≥ 1 claim). Schema validity is counted over **all**
questions including execution errors, and every raw response is stored in the
report (`models.<model>.responses`) so metrics can be recomputed.

**v2 run (judge v2, `qwen3.5:4b` only by the user's decision; 9b not
benchmarked).** Shared CPU-only Ollama 0.40.2 server on the Tailscale network
(models already pulled there; nothing installed, pulled or reconfigured).

| Metric (`qwen3.5:4b`, digest `d8b0f5e9760c…`, 40 questions, seed 42, 512-token cap, `num_thread` 8) | Value |
| --- | --- |
| latency p50 / p95 / max | 24.6 s / 40.4 s / 59.2 s (n=40, CPU server) |
| tokens prompt / output | 22,973 / 3,795 |
| strict schema validity (incl. invariants, denominator = all questions) | 1.0 (40/40); independently re-scored from the stored responses: 40/40 |
| citation linkage | 1.0 (40/40) |
| semantic support (mechanical gold-span proxy) | 1.0 (24/24 answered) |
| false abstention (answerable questions abstained) | 0.314 (11/35) |
| false answer on no-answer / false-premise | 0.0 (0/5) |
| unsupported claims | 0.088 (3/34) |
| execution errors | 0 |

Reading: conservative (no answer to unanswerable questions, but abstains on
almost a third of answerable ones) and far outside the production 15 s
per-call budget on this hardware. One model on 40 diagnostic questions with a
mechanical support proxy is not a basis for a production model choice; Ollama
stays out of the API. Report with raw responses:
`.omo/evidence/search-platform/final-benchmark/ollama/ollama-bench-qwen3.5-4b-v2.json`.


### Checkpoint compatibility

A checkpoint is reusable only for the same corpus, labels, query clock, repeat
count, code identity and effective system configuration (chunker, embedding
identity/revision, reranker, analyzer/engine and retrieval depths). Execution
options such as device are also bound. Older checkpoints are recomputed after
this compatibility-key change. Requests to generate private evidence review
sheets recompute instead of resuming score-only checkpoints.

## Layer traces, label audit and facet diagnostics (2026-10-29)

* `search-eval --trace-dir DIR` (`SEARCH_EVAL_TRACE_DIR`) writes one text-free
  JSON line per pipeline question (first repeat): raw branch hits (with trap
  flag) or the PostgreSQL FTS window pool, the fused list with branch ranks,
  every selection group, the final-check pool size, payload spans (with
  truncation flag), drop counters and stage timings incl. `embed_query`.
  Measured reports add `latency_by_repeat` (repeat 1 runs right after the
  index build).
* `search-label-audit` checks a derived candidate label set against its source
  sets and the batch: same IDs/splits, `candidate` only, no reviewer or truthy
  `human*`/`*verified*` field, AI provenance, content hash + article version,
  NFC code-point offsets + excerpt SHA-256, gold inside filters, facets point
  at gold. Exit 2 on any issue.
* `search-eval-diagnose` scores traces against a facet sidecar (required answer
  facets; alternatives are substitutes, spans inside one alternative are
  complements) and a frozen evaluation manifest: facet coverage at branch /
  fused / top-10 / selection / payload, judged share of the top-k (unjudged
  is never treated as grade 0 outside nDCG), per-facet loss attribution
  (CANDIDATE_MISS, RANKING_MISS, EVIDENCE_SELECTION_MISS, BUDGET_TRUNCATION,
  SCOPE_OR_REVISION_FAILURE) and paired differences with a cluster bootstrap
  over shared-source groups. Inputs are validated before anything is
  written (exit 2 otherwise): the manifest's label/facet digests must match,
  facets must be well formed (an empty alternative is an error, never a
  vacuous match) and the trace file must hold exactly one first-repeat record
  per (manifest configuration, question) — missing, duplicate, unknown or
  malformed records are rejected.

Result on candidate label v2 (AI-reviewed, not human; 103 eligible questions;
`.omo/evidence/search-platform/label-v2-20261029/`): all required facets in
the payload — PostgreSQL 87, BM25 85, semantic 92, hybrid 96; PostgreSQL loses
evidence at candidate generation (≤ 3 windows/article, pool), semantic/hybrid
at the 3-spans/article selection cap. PostgreSQL stays the default; hybrid
continues as opt-in/shadow.
