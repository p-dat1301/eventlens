# G1 F3 independent manual QA

**Reviewer:** Sisyphus-Junior (independent G1 manual QA, 2026-10-03). **Verdict: PASS** for scoped G1 worker/DB/graph checks. G2 API briefing and G3 web excluded.

## Isolation and commands

Run-specific evidence: [`manual-20261003-FhBbvE/`](manual-20261003-FhBbvE/). Exact command arguments and exit codes: [`commands.tsv`](manual-20261003-FhBbvE/commands.tsv), [`exit-codes.tsv`](manual-20261003-FhBbvE/exit-codes.tsv). Full stdout/stderr per command: matching `*.log`. Reproducer: [`qa.sh`](manual-20261003-FhBbvE/qa.sh); invalid JSON: [`malformed.json`](manual-20261003-FhBbvE/malformed.json).

From repo root, ran `sh .omo/evidence/G1/manual-20261003-FhBbvE/qa.sh` (exit **1**, expected because harness tracks intentional negative-case nonzero exits). Harness created owned `pgvector/pgvector:0.8.0-pg17` container `eventlens-g1-manual-1791013727-59700`, `--tmpfs /var/lib/postgresql/data`, random loopback host port, `rag_test` DB. URL pointed **only** to owned container; see [`owned-db.txt`](manual-20261003-FhBbvE/owned-db.txt). Never connected to or dropped Compose DB. Explicit container removal returned 0; subsequent `docker inspect` returned 1 (not found); see [`cleanup.txt`](manual-20261003-FhBbvE/cleanup.txt). `docker ps -a --filter 'name=eventlens-rag-test-' --filter 'name=eventlens-g1-manual-' --format '{{.Names}} {{.Status}}'` returned empty, exit 0 after runs. No product edits, commit, or push.

| Command / SQL check | Exit | Observed output |
|---|---:|---|
| `uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json` | 0 | `Valid article 550e8400-e29b-41d4-a716-446655440000 with 2 media items.` |
| `uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json` | 0 | JSON trace: `verdict=ACCEPT`, `reasons=["ready"]`, `write_disposition=inserted`; [`worker-process.log`](manual-20261003-FhBbvE/worker-process.log). |
| `uv run --package eventlens-rag eventlens-rag migrate` (twice) | 0, 0 | `rag migrations applied` both times. |
| `uv run --package eventlens-rag eventlens-rag load-fixtures` | 0 | `rag persisted 30 fixture articles`. |
| `docker exec <owned-container> psql -X -v ON_ERROR_STOP=1 -U rag_test -d rag_test -c "<SQL>"` before rebuild | 0 | `rag_current_lexical`: 30 documents / 30 unique articles / 3 `harbor` matches; `rag_current_vectors`: 0 rows. Exact SQL in [`commands.tsv`](manual-20261003-FhBbvE/commands.tsv); results in [`lexical-before.log`](manual-20261003-FhBbvE/lexical-before.log). |
| `uv run --package eventlens-rag eventlens-rag rebuild` (twice) | 0, 0 | First: `rag fake rebuild published 30 current articles (0 reused generations)`; second: `rag fake rebuild published 30 current articles (30 reused generations)`. Both snapshots: 30 active articles, 30 active chunks, 30 total generations; [`generation-first.log`](manual-20261003-FhBbvE/generation-first.log), [`generation-second.log`](manual-20261003-FhBbvE/generation-second.log). |
| Real lexical `plainto_tsquery('simple', 'harbor')` and vector `embedding <=>` SQL | 0, 0 | Lexical returns IDs 001, 002, 015 with positive rank. Vector returns seed ID 001 at cosine distance `0.00000`, four other matches; [`lexical-query.log`](manual-20261003-FhBbvE/lexical-query.log), [`vector-query.log`](manual-20261003-FhBbvE/vector-query.log). |
| Index definitions and `EXPLAIN` with `enable_seqscan=off` | 0 | GIN `rag_article_documents_fts_idx` and HNSW `rag_chunks_embedding_hnsw_idx` exist; plans show `Bitmap Index Scan` and HNSW `Index Scan`; [`index-evidence.log`](manual-20261003-FhBbvE/index-evidence.log). Forced index plan verifies usable indexes, not optimizer choice under normal settings. |
| Source span SQL on persisted `document_json` | 0 | Article `00000000-0000-4000-8000-000000000001`, span `20000000-0000-4000-8000-000000000001`, offsets `[0,126)`, excerpt `Harbor Authority bulletin: East Pier gate closes at 08:00 UTC on 2026-03-01 for crane inspection. Cargo trucks use North Gate.`; substring comparison `matches_source=t`; [`source-span.log`](manual-20261003-FhBbvE/source-span.log). |
| `uv run pytest apps/worker/tests apps/rag/tests/test_graph_state.py apps/rag/tests/test_graph_routes.py apps/rag/tests/test_persistence_graph.py -q` | 0 | `146 passed in 0.45s`; [`graph-tests.log`](manual-20261003-FhBbvE/graph-tests.log). |
| `sh apps/rag/with-test-db.sh sh apps/rag/db-suite.sh` | 0 | Separate owned ephemeral DB; `127 passed in 4.68s`; [`db-suite-isolated.log`](manual-20261003-FhBbvE/db-suite-isolated.log). |

## Negative paths and cleanup

| Command | Exit | Observed output |
|---|---:|---|
| `uv run --package eventlens-worker eventlens-worker validate .omo/evidence/G1/manual-20261003-FhBbvE/malformed.json` | 2 | `error:bundle_validation_failed` |
| `uv run --package eventlens-worker eventlens-worker process .omo/evidence/G1/manual-20261003-FhBbvE/malformed.json` | 2 | `error:bundle_validation_failed` |
| `uv run --package eventlens-rag eventlens-rag load-fixtures --directory .omo/evidence/G1/manual-20261003-FhBbvE/missing` | 1 | `rag_fixture_missing: .omo/evidence/G1/manual-20261003-FhBbvE/missing` |
| With `EVENTLENS_RAG_DATABASE_URL=postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test`, `uv run --package eventlens-rag eventlens-rag migrate`, `rebuild`, `load-fixtures` | 3 each | `rag_storage_unavailable: RAG PostgreSQL storage is unavailable`; no DSN printed. |

Expected negative exits explain manual harness exit 1. Cleanup trap executed despite exit 1: `docker_rm_exit=0`, `docker_inspect_after_exit=1`; [`cleanup.txt`](manual-20261003-FhBbvE/cleanup.txt), [`docker-remove.txt`](manual-20261003-FhBbvE/docker-remove.txt). Separate `with-test-db.sh` run exited 0 and auto-removed its container.

**Limitations:** fake deterministic embeddings and local in-memory worker writer only; no production model, MinIO, or API/web end-to-end. `EXPLAIN` forces index eligibility on 30-row corpus; does not benchmark natural planner or large corpus. No normal-condition query `EXPLAIN ANALYZE` performance claim.

---

## Addendum 2026-10-16 - self-run QA (Claude, Claude Code agent)

**Self-run by the agent that made the 2026-10-16 changes; not independent.** The
2026-10-03 section above is historical and unchanged. Evidence:
`selfcheck-20261016/`, `run-20261016T044334Z-44Ofkn/`.

| Check | Exit | Observed |
| --- | ---: | --- |
| worker validate / process (`fixtures/article_bundle.v1.json`) | 0 / 0 | 2 media items; ACCEPT/ready/inserted |
| worker container build + `compose run --rm ai-worker` | 0 / 0 | same validate line |
| `make gate-g1` | 2 | technical PASS; migrate, load 30, rebuild 30 (0 reused) then 30 (30 reused), DB suite 129 passed, unreachable DB -> exit 3 `rag_storage_unavailable`; acceptance OPEN |
| `make rag-db-test` | 0 | 129 passed, 0 skipped |
| Compose-project `make rag-db-create/migrate/load-fixtures/rebuild` on `eventlens_rag` | 0 each | ledger 5, lexical 30, vectors 30, FTS `harbor` -> …001/…002/…015 |
| Same `make rag-migrate` on legacy-shaped `eventlens` copy | 2 (CLI 1) | typed `rag_migration_failed … rag_chunks`; schema dump hash unchanged |

Verdict: scoped G1 worker/DB/graph checks behave as expected. No API/web (G2/G3).
G1 still needs independent QA sign-off per plan.
