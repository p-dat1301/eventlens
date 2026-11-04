# Search platform (PostgreSQL truth, Elasticsearch projection)

Status: implemented on `feature/rag-search-platform`, **opt-in**. The production
default is unchanged: PostgreSQL retrieval (`retrieval-policy.v3`), identity
(no-op) reranker, no second hop. Elasticsearch/hybrid is used only when
`EVENTLENS_RAG_SEARCH_BACKEND=elasticsearch` is set, and the pilot benchmark does
not justify switching the default (`docs/search-evaluation.md`).

Related: `docs/search-indexing.md` (schema, outbox, index lifecycle),
`docs/search-evaluation.md` (benchmark), `docs/query-intents.md` (intents and
bounded multi-hop), `docs/rag.md` (RAG lifecycle).

## Principles

- **PostgreSQL is the source of truth.** Articles, immutable revisions
  (`rag_article_documents`), the current pointer and tombstones
  (`rag_article_current`), enrichment lifecycle and access stay in PostgreSQL.
- **Elasticsearch holds a derived, rebuildable projection** of the *current*
  revision of each article: one document per chunk, tied to `article_id`,
  `revision_id` (UUIDv5 of `article_id|content_hash`), `content_hash`,
  `chunk_id`, `chunk_version` and `generation_id`. Dropping every index and
  running `eventlens-rag index rebuild` restores it from PostgreSQL alone
  (proved by `test_postgres_to_outbox_to_index_flow_and_full_rebuild` and the
  end-to-end search suite).
- **The index can never widen what a caller sees.** It only proposes
  candidates. `RetrieveEvidence` then runs the unchanged PostgreSQL final check
  (current revision, tombstone, scope, metadata) before assembling evidence, so
  a stale or tombstoned index entry is dropped even before the outbox caught up.

## Data flow

```text
write path (ingest / load-fixtures / correction / tombstone / publication)
  PostgreSQL transaction
    rag_article_documents + rag_article_current (+ enrichment publication)
    trigger -> rag_search_outbox row                    (same transaction)
          |
          v
eventlens-rag index drain-outbox   (claim batch, FOR UPDATE SKIP LOCKED, lease)
  re-read the article's *current* state from PostgreSQL
    tombstoned / unknown -> delete every chunk of the article
    current              -> chunk (paragraph-900-1.v1) -> embed (cache first)
                            -> bulk index (refresh=wait_for) -> delete other chunks
  DONE only after every write was acknowledged; else retry with backoff,
  dead letter (FAILED) after the attempt budget
          |
          v
Elasticsearch index eventlens-rag-search-g<generation>-<suffix>

query path (POST /briefing, unchanged API)
  classify_intent -> apply_filters (PostgreSQL metadata eligibility)
  -> retrieve_primary: BM25 top 50 + kNN top 50 with identical filters
     (ES outage -> PostgreSQL branches when fallback is allowed)
  -> [retrieve_related: off by default] -> fuse (RRF k=60, dedup, rerank)
  -> assemble_evidence: PostgreSQL final current/tombstone/scope check
  -> synthesize -> validate -> finalize
```

## Components

| Layer | Module |
| --- | --- |
| Domain | `domain/paragraph_chunking.py`, `domain/search_index.py` (projection, filters, `rrf_fuse`), `domain/search_embedding.py` (identity/dimension checks), `domain/outbox.py`, `domain/query_intent.py` |
| Ports | `application/ports/search_index.py` (`SearchIndexPort`), `application/ports/search_sync.py` (outbox, projection source, generations, embedding cache), `application/ports/candidates.py`, `BatchEmbeddingPort` in `application/ports/embeddings.py` |
| Use cases | `use_cases/search_projection.py`, `use_cases/search_sync.py` (drain, rebuild, reconcile, health), `use_cases/search_candidates.py`, phased `use_cases/retrieve.py` |
| Adapters | `infra/elasticsearch/` (`client`, `mappings`, `indexes`, `bm25`, `vector`, `hybrid`, `projection`, `errors`), `infra/postgres/search_platform.py`, `infra/search/memory_index.py` (tests/CI), `infra/embeddings/{bge_m3,fake_bge_m3}.py`, `infra/rerankers/{cross_encoder,fake_overlap}.py` |
| Wiring | `composition.py` (`SearchSettings`, `build_search_runtime`, `runtime_from_environment`) |
| CLI | `eventlens-rag index {rebuild,reconcile,health,drain-outbox}`, `search-eval`, `search-review-sheet`, `ollama-bench` |

Elasticsearch, model libraries and HTTP stay in `infra`/`composition`/`cli_main`
(and the offline `evaluation` harness); `tests/architecture` enforces it. The
Elasticsearch client is stdlib HTTP (no SDK); the ES `rrf` retriever is not used
(licence), fusion is the domain `rrf_fuse`.

## Configuration (`SearchSettings`, read once at startup)

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENTLENS_RAG_SEARCH_BACKEND` | `postgres` | `elasticsearch` adds the index as candidate source (needs `EVENTLENS_API_RAG_DATABASE_URL` for the PostgreSQL truth) |
| `EVENTLENS_RAG_SEARCH_FALLBACK` | `postgres` | `none` fails closed (`retrieval_unavailable`) when the index is down |
| `EVENTLENS_RAG_ELASTICSEARCH_URL` | — | required for `elasticsearch`; non-loopback endpoints must be `https` with an API key |
| `EVENTLENS_RAG_ELASTICSEARCH_API_KEY` | — | sent as `Authorization: ApiKey …`, never logged |
| `EVENTLENS_RAG_SEARCH_EMBEDDINGS` | `none` | `fake-bge-m3` (tests) or `bge-m3` (needs the revision below) |
| `EVENTLENS_RAG_BGE_M3_REVISION` | — | pinned Hugging Face snapshot; weights load with `local_files_only=True` |
| `EVENTLENS_RAG_RERANK` | `off` | `off` = identity reranker (fused order kept); `cross-encoder` needs `EVENTLENS_RAG_RERANK_REVISION` |
| `EVENTLENS_RAG_MULTI_HOP` | `off` | `on` enables the bounded second hop (`docs/query-intents.md`) |
| `EVENTLENS_RAG_MODEL_DEVICE` | `cpu` | `mps`/`cuda` for local models |
| `EVENTLENS_RAG_RETRIEVAL_TUNING` | `baseline` | named profile from `application/policies/retrieval-tuning.v1.json` (weighted RRF, selection/fill, index-branch diversity, intent freshness, BM25 boosts); `baseline` is the frozen `retrieval-policy.v3` behaviour and the way back; unknown names fail at startup |

Unknown values and incomplete activations fail at startup
(`SearchConfigurationError`); a real model is never silently replaced by a fake.
Model libraries come from the optional `bench` dependency group
(`uv sync --group dev --group bench`); CI and runtime images do not install it.

## Behaviour on failure

| Situation | Result |
| --- | --- |
| Elasticsearch unreachable / 5xx / timeout at query time | `search_backend = postgres_fallback` (PostgreSQL branches) when fallback is `postgres`; typed `retrieval_unavailable` (503) when `none` |
| No ACTIVE generation | same as an outage (`no_active_generation`) |
| Query embedding fails or identity/dimension differs | semantic branch disabled (`embedding_failed` / `embedding_incompatible`), lexical continues |
| Reranker timeout / error / invalid output | fused order kept, `rerank_status = degraded`; the request never fails |
| Index write refused, bulk response empty/short/mismatched, cleanup failed | outbox event retried with backoff, dead letter after the budget; old chunks are not deleted |
| Two workers project the same article out of order (late old worker, expired lease) | Article fence plus externally versioned chunk writes preserve newer projections, including enrichment updates sharing the same chunk ID. A chunk conflict is superseded only after its article's newer fence is confirmed; see `docs/search-indexing.md` |
| Rebuild repair fails or drift persists | generation FAILED, previous ACTIVE generation kept |
| Embedding model snapshot/config changed | different fingerprint: no cache reuse, query refused for the old generation (`embedding_incompatible`) until a rebuild |

Public API (`POST /briefing`, `GET /briefing/sources/{source_ref}`, envelopes,
error codes, citations) is unchanged. Search diagnostics (backend, index
generation, branch ranks, intent, hops) live in internal `RetrievalProvenance`
and are not returned to the frontend.

## Defaults and the production decision

Kept: PostgreSQL retrieval; Elasticsearch/hybrid opt-in or shadow only;
reranker off; multi-hop off; Ollama never wired into the API. Changing a default
needs a benchmark on a larger, independently labelled corpus that shows stable
gains in passage/citation recall and nDCG across repeats, with p95 inside the
SLO and measured cost (`docs/search-evaluation.md`).

## Retrieval tuning (opt-in, 2026-10-28)

`domain/retrieval_tuning.py` defines every knob and formula; profiles are
listed in `retrieval-tuning.v1.json` and recorded as
`RetrievalProvenance.tuning_id` plus new `RetrievalMetrics` counters
(`branch_cap_dropped`, `backfill_calls`, `selection_overlap_dropped`,
`selection_cap_dropped`, `fill_spans_sent`, `freshness`). Nothing changes
unless a profile is selected; `rag-eval` digests are unchanged.

| Knob | Definition |
| --- | --- |
| weighted RRF | `w_lex/(k+rank_lex) + w_sem/(k+rank_sem)`, k = 60; weight 0 disables the branch (no call, no query embedding, its candidates never fused) |
| selection | ≤ `max_spans_per_article` (v3: 3) per article, articles by best chunk; optional overlap filter `|a∩b|/min(|a|,|b|) ≥ t` within one article revision |
| fill | `fill_spans_per_article`: further spans of selected articles, packed only into budget left after every base span |
| index-branch diversity | per-article cap per BM25/kNN branch, one backfill to ≤ depth 100 when the cap dropped hits, the branch filled its depth and ≥ `min_remaining_seconds` remain |
| intent freshness | intent `update`, no explicit window: `W·(1/(k+1) − 1/(k+1+R))·0.5^(age/H)` on the fused score; missing/future `source_published_at` = 0 |
| BM25 boosts | title:sapo:content (default 3:1.5:1) |

Measured on the real 200-article batch (`.omo/evidence/search-platform/retrieval-improvement/final-decision.md`):
only `fill6` improves the payload (gold passages dev 47→48/48, test 64→66/70,
holdout unchanged, +48 % payload tokens inside the same budget); branch caps,
breadth-first packing, 1:2 weights, freshness (no real update labels) and
boost changes show no gain. Default stays `baseline`.

## Known limits

- 30-article / 40-question synthetic pilot with single-annotator labels; most
  configurations hit the ceiling. It validates mechanics, not ranking quality.
- No Vietnamese word segmentation: `vi_text` is syllable tokens plus ASCII
  folding (`TP.HCM` stays one token). Measured in `tests/search`, not tuned.
- Data written before `rag_0003_search_platform` produces no outbox events;
  run `eventlens-rag index rebuild` once after upgrading.
- Generation status is cached for 5 s per API process; activation becomes
  visible within that window.
- If `feature/rag-search-selection` merges, its `rag_0002_source_versions` is
  byte-identical to this branch's copy (see `docs/search-indexing.md`).
