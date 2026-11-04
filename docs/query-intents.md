# Query intents and bounded multi-hop

Code: `src/backend/rag/domain/query_intent.py` (rules, hop policy),
`application/use_cases/briefing.py` (steps), `workflows/query/` (graph).

## Graph

```text
START -> prepare (admit, budget) -> classify_intent -> apply_filters
      -> retrieve_primary -> [retrieve_related] -> fuse -> assemble_evidence
      -> synthesize | abstain (validation inside) -> answered | abstained
```

Every node routes typed failures to the existing six failure terminals. There
is no back edge (`test_graph_has_no_back_edge_so_retrieval_cannot_loop`), so
retrieval can never loop. The compiled graph is built once per runtime.

## Intent classifier (`query-intent-rules.v1`)

Deterministic cue lists over the casefolded NFC query (Vietnamese and English);
no model is called. Priority order, first match wins:

| Intent | Example cues |
| --- | --- |
| `no_answer_candidate` | fewer than 3 words; "giả sử", "nếu như", "what if" |
| `comparison` | "so sánh", "so với", "khác nhau", "compare", "versus" |
| `update` | "mới nhất", "cập nhật", "hiện nay", "tiến độ", "latest", "still", "now" |
| `multi_source` | "các nguồn", "các bên", "phản ứng", "tổng hợp", "sources", "reactions" |
| `numeric` | "bao nhiêu", "mấy", "tỷ lệ", "phần trăm", "how many", `NN%` |
| `temporal` | "khi nào", "bao giờ", "ngày nào", "when", years, "tháng N", "ngày N" |
| `factual` | default |

On the search-selection pilot the rules agree with the author labels on
31/40 questions (0.775); see the confusion table in the search-eval report.
The intent only gates hop 2; it never changes filters, scope or access, and it
is recorded in `RetrievalProvenance.query_intent`.

## Bounded second hop (`bounded-hops.v1`)

`HopPolicy`: `enabled` (default **off**), at most 2 retrieval hops, 1 query
expansion, 1 rerank, hop 2 needs ≥ 8 s left before the request deadline and a
positive remaining context budget. Hop 2 runs only when every bound holds and
the intent is `update` or `multi_source` and hop 1 found candidates; otherwise
the decision reason is recorded (`disabled`, `intent_not_eligible`,
`no_primary_results`, `max_hops_reached`, `max_expansions_reached`,
`budget_exhausted`, `deadline_near`). The deadline is re-checked right before
the hop.

Hop 2 issues one expansion query (the user query plus new terms from the first
200 characters of the top two hop-1 chunks, ≤ 16 terms) with the **same**
scope and filters. Hop results merge with RRF over hop positions, followed by
the single bounded rerank and the PostgreSQL final check. Each hop is traced
(`HopTrace`: hop, kind, candidates, latency, decision) in
`RetrievalProvenance.hops`.

Why off: with hop 2 enabled, the T9 fixture changed answers (citation gold
precision of the lexical mode dropped from 0.52 to 0.30 because the expansion
widened recall on "When did East Pier close and reopen?", which an earlier
cue list classified as `update` via "reopen"; that cue was removed). With the default
policy the rag-eval report digest is identical to the pre-change baseline.
Enable with `EVENTLENS_RAG_MULTI_HOP=on` only for measurement until a benchmark
shows a gain.

Not built (by design): free agentic loops, LLM-based intent classification,
tool calls outside the fixed node list.

## Intent freshness (opt-in tuning, `retrieval-tuning.v1`)

The intent also gates an optional freshness term (`mode = "intent"` in a
tuning profile; default off). It applies only to `update` questions without an
explicit `published_after`/`published_before` window and adds
`W·(1/(k+1) − 1/(k+1+R))·0.5^(age_days/H)` to the fused RRF score (`W` = sum of
active branch weights, `R = max_rank_shift`, default 5). RRF gaps shrink
down the list, so the bonus alone does not bound movement; the final order is
therefore built under a hard guard: relative to the baseline fused order no
candidate moves up more than `R` positions (deterministic, same candidate set;
demotion is not bounded). In `RetrieveEvidence` the guard runs after
duplicate suppression (on the deduplicated, unboosted order; a cluster's
representative is boosted by its own publication time), so it is the last
reordering of the hop-1 list; with rerank and multi-hop off (the defaults)
that is the returned order. An enabled reranker or hop-2 fusion reorders by
its own scores afterwards and is outside this bound. The guard bounds promotion only — it does not show
that a promoted chunk is on-topic; a new off-topic chunk can still move up to
`R` positions. Age is `query clock −
source_published_at` (never `fetched_at`/`ingested_at`; publication time is
not event time); missing or future timestamps get 0. In that mode the v3
per-branch freshness bonus is switched off. Historical and temporal questions
are never boosted. Mechanism test: `fixtures/rag/search-selection/freshness-updates-v1`
(synthetic, fixed clock 2026-05-20).
