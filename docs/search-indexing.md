# Search indexing: schema, outbox, generations, index lifecycle

Overview and defaults: `docs/search-platform.md`.

## Migration chain (single head)

```text
rag_0001_baseline          historical 001-007 schema (reviewed, catalog-tested)
  -> rag_0002_source_versions  upstream handoff provenance (rag_source_versions)
  -> rag_0003_search_platform  search generations, outbox, embedding cache
```

- `rag_0002_source_versions` is byte-identical (SHA-256
  `5f713ea74bb335c1f72a6b508d25bc16cff8cb228da1a422e3082ac9770cd0b8`) to the
  revision on `feature/rag-search-selection` (`c1041c4`, PR #7). It was taken
  from that committed branch so both branches share one chain: when PR #7 merges,
  the revision is already present and identical; this branch adds only
  `rag_0003`. Do not edit either file in place.
- Both new revisions are additive. `rag_0003` downgrade drops only its derived
  objects (generations, outbox, embedding cache, triggers); source rows and
  provenance stay (`test_downgrade_to_source_versions_drops_only_derived_search_objects`).
  `rag_0002` downgrade refuses while `rag_source_versions` holds rows;
  `rag_0001` downgrade is always refused.
- Data written before `rag_0003` emits no events; after upgrading run
  `eventlens-rag index rebuild` once.

## Tables (`rag_0003_search_platform`)

| Table | Content |
| --- | --- |
| `rag_search_generations` | one row per index build: `index_name` (unique, `prefix-g<id>-<random>`), `chunk_version`, `embedding_identity`/`model`/`dimensions`/`revision`, status `BUILDING → ACTIVE → RETIRED` or `FAILED`; partial unique index allows one ACTIVE row |
| `rag_search_outbox` | `event_type`, `article_id`, `content_hash`, `generation_id`, `status` (`PENDING`/`PROCESSING`/`DONE`/`FAILED`), `attempts`, `next_attempt_at`, `lease_expires_at`, `claim_token`, `last_error` (code only) |
| `rag_search_embedding_cache` | `(text_hash, embedding_identity)` → `double precision[]` vector with a checked `embedding_dimensions`; `embedding_identity` holds the full fingerprint (below); independent of `rag_chunks.vector(1536)` |

Triggers write outbox rows in the same transaction as the change:
`rag_article_current` insert → `article_revision_created` (or
`article_tombstoned`), content-hash change → `article_revision_updated`,
tombstone → `article_tombstoned`; `rag_enrichment_publications` insert →
`enrichment_published`. Activating a generation inserts
`embedding_generation_activated`. A rolled-back write leaves no event
(`test_outbox_events_commit_with_the_data_and_roll_back_with_it`).

## Outbox delivery (`DrainOutbox`)

- Claim: due `PENDING` or lease-expired `PROCESSING` rows, oldest first,
  `FOR UPDATE SKIP LOCKED`, `attempts + 1`, a fresh `claim_token`, lease
  120 s, batch 100. Ack/retry/fail match the token, so a worker whose lease
  expired cannot complete an event someone re-claimed.
- Idempotent: the event carries no document; the worker re-reads the
  article's current state, so replays and reordering are harmless and an old
  revision is never re-indexed. Events with no writable generation stay
  `PENDING`.
- Writes go to every writable generation (ACTIVE and BUILDING).
- **Ordering fence.** Each projection carries a version: the article's highest
  outbox `event_id`, read in the same statement as its current state (every
  state change writes an event in its own transaction, so newer state ⇒ larger
  version; 0 = data from before the outbox). Every write path (drain, rebuild,
  reconcile repair) is a fenced `write_revisions`: (1) index the article's
  fence document `fence:<article_id>` with `version_type=external_gte` — an
  older version gets 409 and is *superseded*, never applied; a replay of the
  same version is accepted; (2) bulk-write the chunks, each stamped with the
  write's `projection_version` and protected by its own `external_gte`
  version (`projection_version + 1`, keeping pre-outbox version 0 positive
  for Elasticsearch's internal deletes). A delayed chunk request therefore
  cannot replace a newer chunk sharing its ID, including metadata-only updates
  and content that returns to an earlier hash. (3) Re-read the article fence:
  chunk-level 409 conflicts are superseded only if that article's fence is
  newer; an unexplained conflict fails without cleanup. If a newer write
  overtook this one in flight, delete this write's own chunks; (4) delete the
  article's chunks with an older (or missing) version. A late old worker can
  therefore neither overwrite a newer revision nor resurrect a tombstone, and
  an expired lease owner can neither write nor ack
  (`tests/backend/rag/application/test_review_regressions.py`,
  `tests/search/test_elasticsearch_index.py::test_fence_orders_writes_and_tombstones_on_a_real_cluster`,
  `test_delayed_chunk_write_preserves_the_newer_completed_projection`).
  Fence documents (`doc_kind: fence`) are excluded from searches, counts and
  digests.
- **Upgrading an older search generation.** Rebuild into a fresh generation
  before resuming index delivery with this protocol. Indexes written by the
  earlier unversioned chunk bulk may have Elasticsearch internal versions
  unrelated to their projection versions. Such conflicts fail closed and are
  retried; they are never ignored as superseded without a newer article fence.
  `eventlens-rag index rebuild` creates the fresh index from PostgreSQL and
  preserves the previous ACTIVE generation until the new one is consistent.
- DONE only after Elasticsearch acknowledged everything: the bulk response must
  list exactly one item per document, with a successful index action or a
  version conflict confirmed by a newer fence for the same article;
  the cleanup `_delete_by_query` (other chunks of the article) must
  report no failures. Otherwise the event is retried with backoff
  `5 s × 4^(n-1)` (cap 600 s) and becomes `FAILED` after 6 attempts. A failed
  bulk never triggers cleanup, so old chunks stay searchable
  (`tests/search/test_elasticsearch_bulk.py`).
- Operator: `eventlens-rag index drain-outbox --requeue-failed` returns
  dead letters to `PENDING`.

## Projection

`ProjectRevision` chunks the current revision with `paragraph-900-1.v1`,
embeds `title + "\n\n" + chunk` through the cache (an unchanged chunk is never
embedded twice), and refuses to project when the embedder's fingerprint or
dimension differs from the generation (`embedding_identity_mismatch`,
`embedding_dimension_mismatch`) or the chunker differs.

Embedding fingerprint: `SearchEmbeddingIdentity.key` =
`<identity>@<revision>:<sha256[:16]>` over model, revision, dimensions,
max tokens, pooling, normalization and input format
(`title-blank-chunk.v1`). It is stored as the generation's
`embedding_identity`, keys `rag_search_embedding_cache`, is written on every
chunk, and is compared with the query embedder before the semantic branch runs
(a different snapshot → `embedding_incompatible`, lexical only). Two
revisions of BGE-M3 therefore never share cached vectors or a vector space.

### Chunker `paragraph-900-1.v1`

Adjacent paragraphs packed to ~600-1,000 code points; never cuts inside a
sentence when a boundary exists (Vietnamese abbreviations such as `TP.`, `GS.`,
single capitals are not boundaries); over-long paragraphs split at sentences,
then whitespace; one short trailing sentence (or short paragraph, ≤ 300) is
repeated as overlap. Every chunk is exactly
`normalized_content[char_start:char_end]` with `paragraph_start/end`; the
version is part of `chunk_id` and of the final-check offset validation, so
citations cut by these offsets match the stored text. `codepoint-1200-200.v1`
remains the PostgreSQL retrieval chunker.

A generation may instead be built with the baseline `codepoint-1200-200.v1`
(`ProjectRevision(policy=DEFAULT_POLICY)`, `chunk_revision`): windows keep the
baseline offsets and fingerprints, `paragraph_start/end` only record which
paragraphs a window touches, and reconcile counts chunks with the same chunker
(`chunk_count`). This exists so the benchmark can compare chunkers on one
engine; production generations stay `paragraph-900-1.v1`.

## Handoff batches (data-team exports)

`eventlens-rag-handoff-v1` batches (`articles.jsonl` + `manifest.json`) enter
through the same persist-first path as ingest:

```text
handoff-validate (sha256, bytes, record count, schema, IDs, current-only,
                  timestamps vs exported_at, URL host, manifest counts/ranges;
                  warnings: invisible chars, HTML entities, near-duplicates)
  -> document_from_handoff (raw = exported content, NFC normalized_content,
     RAG nfc.v1 content hash; published_at kept null, never fetched_at;
     no invented raw/duplicate/media fields)
  -> ImportHandoff -> PostgresSourceVersions.persist_batch: ONE transaction
     per batch: revision + current pointer (outbox event via trigger) +
     rag_source_versions row (article_version_id, upstream content_hash,
     parse/normalization version, export_id, fetched_at, updated_at_source)
```

- Idempotent: a repeat import inserts no revision, provenance, outbox event,
  job or chunk (same content hash ⇒ the trigger stays silent).
- An `article_version_id` already bound to other content, parse or
  normalization version, language or word count raises
  `ProvenanceConflictError` and rolls the whole batch back.
- A rejected batch (any error-level finding) writes nothing.
- Text is never cleaned: U+200B/U+00A0/HTML entities stay, so offsets and the
  RAG hash refer to exactly the exported string. Upstream and RAG hashes differ
  by design (different normalization input); both are stored.
- Near-duplicates are reported, never dropped.
- `handoff-import` only writes a database whose name ends in `_eval`
  (`infra/postgres/eval_database.py`). `scripts/rag/handoff-import-check.sh`
  proves the whole chain on an owned PostgreSQL + Elasticsearch.

### Document fields (mapping `search-mapping.v2`, `dynamic: strict`)

Kind/version: `doc_kind` (`chunk` | `fence`), `projection_version` (long).
Identifiers (`keyword`): `article_id`, `revision_id`, `chunk_id`,
`content_hash`, `chunk_version`, `source_name`, `source_domain`, `topics`,
`entity_ids`, `access_scopes`, `embedding_identity`, `embedding_model`.
Text (`vi_text` = standard tokenizer + lowercase + ASCII folding with original):
`title` (+ `title.raw`), `sapo` (lead paragraph, ≤ 400 chars; `ArticleDocument`
has no sapo field), `text`. Numbers: `chunk_index`, `char_start`, `char_end`,
`paragraph_start`, `paragraph_end`, `embedding_dimensions`, `generation_id`.
`published_at` (date), `is_current`, `tombstoned` (boolean), `embedding`
(`dense_vector`, cosine, HNSW m=16 ef_construction=100, float).

## Search requests

Both branches share one non-scoring filter clause: `is_current`, not
`tombstoned`, `access_scopes` contains the caller scope, source domains,
`published_at` (`gte` after, `lt` before; missing date never matches a bound),
topics any-of, entities any-of, metadata-eligible article IDs.

- BM25: `multi_match most_fields` over `title^3`, `sapo^1.5`, `text`; top 50.
- kNN: `k = 50`, `num_candidates = max(4k, 100)`, filter applied as pre-filter.
- Fusion: `rrf_fuse` (k=60, pool 45), rank-based; raw scores are never summed.

## Index lifecycle

| Command | Effect |
| --- | --- |
| `eventlens-rag index rebuild [--embeddings none\|fake-bge-m3\|bge-m3] [--drop-previous]` | new BUILDING generation, fenced page writes of every current revision, then up to 3 rounds of deep reconcile + repair; activation happens only after a clean deep reconcile (previous → RETIRED). Any refused write (raised or item-level), or drift still present after the rounds, marks the generation FAILED and the previous ACTIVE generation keeps serving. |
| `eventlens-rag index reconcile [--repair] [--deep]` | compare PostgreSQL current revisions with the ACTIVE index: missing, stale (other hash or several hashes), extra (tombstoned/unknown), and with `--deep` chunk-count mismatches. Exit 1 on drift without `--repair`. |
| `eventlens-rag index health` | reachability, engine version, index presence/doc count, outbox counts, oldest pending age. Exit 1 when unhealthy. |
| `eventlens-rag index drain-outbox [--max-batches N] [--requeue-failed]` | deliver outbox events (see above). |

All read `EVENTLENS_RAG_DATABASE_URL`, `EVENTLENS_RAG_ELASTICSEARCH_URL` and
`EVENTLENS_RAG_SEARCH_EMBEDDINGS` (which must match the ACTIVE generation for
drain/reconcile; a mismatch is refused, not projected). Exit codes: 3 index or
database unreachable, 4 refused/failed, 2 invalid configuration.

Make targets (URLs passed through the environment, not the command line):

```sh
make search-eval-up                       # developer ES, project eventlens-search-dev, 127.0.0.1:9200
make rag-migrate rag-load-fixtures        # RAG database (RAG_DATABASE_URL)
make search-index-rebuild SEARCH_INDEX_EMBEDDINGS=fake-bge-m3
make search-index-health SEARCH_INDEX_EMBEDDINGS=fake-bge-m3
make search-index-reconcile SEARCH_INDEX_EMBEDDINGS=fake-bge-m3
make search-eval-down                     # removes only project eventlens-search-dev
```

`SEARCH_ES_PORT` changes the developer port (`search-eval-up` refuses a port
already served by something outside its project).


### Handoff replay and source metadata

Handoff batches are atomic. Imports serialize article/version decisions with
transaction locks acquired in sorted order. A known upstream version is validated
and then returns without promoting its historical revision or invalidating jobs.
For an unseen version whose body changes, promotion requires a strictly newer
upstream timestamp (`updated_at_source`, otherwise `fetched_at`) than the known
history. Older, equal-time or unorderable updates are rejected; local import time
is never used to infer recency.

A RAG revision keyed by `(article_id, content_hash)` has immutable business
metadata. A different title, URL, publisher/source identity, publication time or
raw text for the same key is rejected, including for a new upstream version.
Supporting metadata-only corrections needs an explicit revision contract; the
importer does not report success while discarding such changes. Local ingest
clocks and assembled evidence spans are excluded from replay comparisons.
