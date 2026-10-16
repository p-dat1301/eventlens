# T2 Evidence: Synthetic Corpus And Labeled RAG Fixtures

Date: 2026-10-02 (reverified; historical results below are dated 2026-09-28)
Scope: T2 only. This artifact does not mark G1, ENG, retrieval quality, production p95, grouping calibration, or live activation accepted.

## Fixture Versions

- Corpus manifest: `rag_corpus_manifest.v1`
- Corpus version: `rag-fixture-corpus.v1`
- Article count: `30`
- Question count: `10`
- Normalization: `nfc.v1`
- Enrichment: `rag-fixture-enrichment.v1`
- Ground-truth model label: `manual-ground-truth.v1`
- Prompt label: `rag-fixture-prompt.v1`
- Embedding label: `fixture-embedding.v1`
- Provenance/license/source type: `synthetic-authored` / `synthetic-authored` / `internal fixture`

## Assertions Covered

- Every ArticleDocument and expected ArticleEnrichment validates against T1 contracts; all article/enrichment IDs and content hashes pair exactly.
- Manifest resolves expected article/span/claim IDs and purpose for all declared cases.
- Ten fixed questions resolve every expected article/span/claim reference and enforce allowed fixture scope.
- Coverage includes duplicate, follow-up, conflicting claims, delayed publication, precise/date/range/unknown/relative temporal cases, UTC/DST, unknown metadata, restricted scope, prompt injection, uncited prose, malformed evidence negative case, Vietnamese/CJK, combining marks, non-BMP emoji, and controlled topics versus suggestions.
- Corpus text and labels are synthetic, manually authored ground truth. Manifest says it is not live model-quality proof.
- `fixtures/rag/claims.v1.json` declares labeled claim/article/span triples independently of manifest and question references. Typed validation checks every reference against that exact registry triple.
- `fixtures/rag/coverage.v1.json` maps every declared purpose to question references or an explicit fixture-only reason. Abstention, restricted scope, and negative evidence cases need not become positive answer references.
- Semantic support gate: **T9 OPEN**. These checks prove structural linkage only, not that cited text entails a claim or that retrieval/model quality passes.
- Positive ground truth includes `Solar cooperative` entity and `Green Hall` location, each backed by the exact ArticleDocument span in fixture `21` with no invented canonical ID.
- Fixture loading rejects duplicate article or enrichment IDs before records are indexed. Every enrichment span must exactly match its corresponding source ArticleDocument span; fixture 21 additionally requires equality of the complete span collections.
- Actual deletion of a source article file rejects at load time; blank license, duplicate source ID in linkage-only validation, and whitespace-only fixture reason reject. Article 023 is a question-only reference with a registered claim, not a missing manifest purpose.

## Current Verification (2026-10-02)

| Command | Exit | Result |
| --- | --- | --- |
| `uv run pytest apps/rag/tests/test_rag_fixtures.py apps/rag/tests/test_rag_fixture_linkage.py -q` | 0 | `26 passed` |
| `uv run pytest -q` | 0 | `389 passed, 7 skipped` (DB integration remains unverified) |
| `uv run ruff check .` | 0 | clean |
| `uv run ruff format --check .` | 0 | `120 files already formatted` |
| `uv run basedpyright` | 0 | `0 errors, 0 warnings, 0 notes` |
| `uv lock --check` | 0 | lock resolved |
| `git diff --check` | 0 | no whitespace errors |

Original fixture-test paths in historical commands below predate move to `apps/rag/tests/`; they are not current invocations. T2 acceptance is structural only: no claim that labels entail source text, retrieval/model quality passes, or G1 clears.

## Historical Commands (2026-09-28; paths stale)

| Command | Exit | Result |
| --- | --- | --- |
| `uv run pytest packages/contracts-python/tests/test_rag_fixtures.py -q` | 0 | `10 passed` |
| `uv run pytest packages/contracts-python/tests/test_rag_fixtures.py packages/contracts-python/tests/test_rag_models.py packages/contracts-python/tests/test_rag_t1_contracts.py packages/contracts-python/tests/test_enrichment_models.py packages/contracts-python/tests/test_schema.py packages/contracts-python/tests/test_semantic_vectors.py -q` | 0 | `84 passed` |
| `uv run pytest -q` | 0 | `300 passed` |
| `make check` | 0 | web checks/build plus Python checks; `300 passed` |
| `uv run ruff check .` | 0 | clean |
| `uv run basedpyright` | 0 | `0 errors, 0 warnings, 0 notes` |
| `git diff --check` | 0 | no whitespace errors |
| `uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json` | 0 | valid article, 2 media items |
| `uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json` | 0 | `ACCEPT`, `inserted` trace |

## Review Fix Verification (2026-09-28)

- New linkage suite: `12 passed`. Mutation cases reject unknown/cross-claim IDs, swapped article/span linkage, undeclared/missing purposes, and purposes swapped across question references.
- Existing corpus validation: `7 passed`. Full Python suite: `296 passed`; `ruff check .`: clean; `basedpyright`: `0 errors, 0 warnings, 0 notes`.
- Claim registry covers all expected manifest/question triples, including question-only claims 004 and 023. Coverage artifact maps all 20 purposes; fixture-only reasons preserve negative and abstention cases without invented answer references.
- No semantic entailment implementation, provider/API/network/dependency work, git operations, or T3+ runtime changes. Semantic support remains **T9 OPEN**.

## Blocker Fix Verification (2026-09-28)

- Positive entity/location labels, exact source evidence, and duplicate-ID regression tests pass in the targeted corpus suite.
- READY/STALE metadata without evidence remains permitted for fields other than entities/locations; no unapproved policy tightening was made.

## Scope Boundaries

- No web content downloaded or retained. No embedding/LLM API call or provider traffic.
- No worker rename, runtime/storage/index/API/UI work, grouping, conversations, tools, commit, push, or PR.
- `make enrichment-eval` and `make rag-eval` remain T9-owned planned targets; not created or claimed here.
