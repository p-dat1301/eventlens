# T1 Evidence: Shared RAG And Enrichment Contracts

Date: 2026-10-02 (updated T1 acceptance)
Scope: Python T1 contracts only. This artifact does not mark G1, ENG, live activation, or any T3+ scope accepted.

## Contract Versions

- Article document schema: `article_document.v1`
- Normalization: `nfc.v1`
- Enrichment schema: `article_enrichment.v1`
- Briefing query/answer schemas: `briefing_query.v1`, `briefing_answer.v1`
- Fixture caller policy: `fixture-policy` / `v1`
- Embedding policy: `text-embedding-3-small`, dimension `1536`, version `openai.text-embedding-3-small.v1`

## Assertions Covered

- Strict frozen/extra-forbid Pydantic contracts and UTC timestamps.
- NFC searchable text preserves separate raw source; document hash and fingerprint derive from normalized UTF-8 text.
- Evidence spans use Unicode code-point half-open offsets on normalized text only. Span excerpt and SHA-256 fingerprint must match exact normalized slice.
- Tests cover Vietnamese/CJK, combining marks, non-BMP emoji, bounds, bad document/span fingerprints, and malformed evidence.
- Controlled topics remain separate from non-taxonomy `suggested_tags`.
- Enrichment identity includes article ID, content hash, enrichment version, model version, and prompt version. Entities/locations retain optional canonical IDs and require matching evidence.
- Temporal claims require evidence, model known/unknown and range state, precision/confidence, and UTC clock/timezone basis for relative expressions. Publication time is not occurrence time.
- `BriefingAnswer.answer` must equal deterministic rendering of validated claim/uncertainty display blocks. Structural citation linkage is not semantic-support proof.
- Temporal evidence must be a subset of spans cited by the same claim, not spans cited anywhere in the answer. A cross-claim negative test locks this boundary.
- Semantic support gate: **T9 OPEN**. T1 validates structural linkage only; no entailment heuristic or model is implemented or accepted.
- Current ownership: shared enrichment/evidence models live in `contracts/python`; RAG document/query/answer models live in `apps/rag`. JSON Schema is generated on demand; no checked-in schema snapshots.
- Isolated negative tests cover lifecycle combinations, taxonomy, suggestions, confidence/evidence bounds, ID uniqueness, content hash/version identity, UTC, frozen and extra-forbid. Draft 2020-12 validation checks a serialized valid answer and rejects a missing required field.

## Commands

| Command | Exit | Result |
| --- | --- | --- |
| `uv run pytest contracts/python/tests/test_enrichment_models.py contracts/python/tests/test_enrichment_edges.py contracts/python/tests/test_schema.py contracts/python/tests/test_semantic_vectors.py apps/rag/tests/test_rag_models.py apps/rag/tests/test_rag_answer_edges.py apps/rag/tests/test_rag_answer_linkage.py apps/rag/tests/test_rag_schema.py apps/rag/tests/test_rag_semantic_vectors.py apps/rag/tests/test_rag_t1_contracts.py -q` | 0 | `138 passed`; includes valid/invalid generated-schema payload checks |
| `uv run pytest -q` | 0 | `385 passed, 7 skipped` (DB integration requires running Docker/PostgreSQL) |
| `uv run basedpyright` | 0 | `0 errors, 0 warnings, 0 notes` |
| `uv run ruff check .` | 0 | all checks passed |
| `uv run ruff format --check .` | 0 | `120 files already formatted` |
| `uv lock --check` | 0 | `74 packages` resolved |

## Review Fix Verification (2026-09-28)

- Temporal cross-claim regression first failed with `DID NOT RAISE`; after claim-local citation mapping, the negative case is rejected with `unknown_temporal_evidence`.
- `pytest packages/contracts-python/tests/test_rag_answer_linkage.py packages/contracts-python/tests/test_rag_fixture_linkage.py -q`: `13 passed`.
- `pytest -q`: `296 passed`; `ruff check .`: clean; `basedpyright`: `0 errors, 0 warnings, 0 notes`; changed Python files pass `ruff format --check` and LSP diagnostics.
- Direct library usage constructed a valid same-claim temporal answer, rendered it, and round-tripped its JSON successfully. This checks structure only; semantic support remains **T9 OPEN**.

## Scope Boundaries

- No embedding or LLM API call. No secret read. No provider traffic.
- No worker rename; `apps/ai-worker` remains active.
- No canonical `Article`/`event.v1` semantic change, storage/index, graph runtime, API/UI, grouping, conversations, or tools.
- D33 Python Unicode/NFC/span contract covered; JavaScript UTF-16 highlighting and cross-language agreement remain T8. Authorized source viewer remains T7. Semantic support remains T9.
- Database integration and G1 acceptance remain OPEN; seven DB tests skipped. No commit, push, rebase, stash, or PR in this T1 completion pass.
