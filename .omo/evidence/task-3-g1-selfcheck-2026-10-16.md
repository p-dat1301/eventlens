# T3/G1 foundation re-verification (2026-10-16)

Executor: Claude (Claude Code agent, Opus), acting on the user's instruction of
2026-10-16. This is **self-verification by the implementing agent**, not an
independent review. Raw logs: `.omo/evidence/G1/selfcheck-20261016/` (exit codes in
`exit-codes.tsv`) and gate run `.omo/evidence/G1/run-20261016T044334Z-44Ofkn/`.
Historical results in other files (e.g. 2026-10-03: 127 DB / 303 scoped passed)
keep their labels; they are not re-asserted here.

## Verdicts

| Item | Status | Basis |
| --- | --- | --- |
| T3 technical (storage, index, graph, worker regression, harness) | **PASS (technical, fresh DB)** | All commands below; real pgvector, 0 skips in DB suite |
| G1 acceptance | **OPEN** | F1-F4 are self-reviews only; no independent review; D41 historical approval unverified; no user acceptance |
| D41 historical approval for the 2026-10-03 move | **Chưa xác minh được (unverified)** | No approval/checkpoint found in accessible sources (see D41 section) |
| D41 current path decision | `apps/worker` kept (user instruction 2026-10-16) | Not retroactive approval of the earlier move |
| Legacy Compose `eventlens` DB upgrade | **OPEN / not upgradable in place** | Fails closed by design; recovery needs owner decision (see Legacy) |
| Dedicated local DB path (`eventlens_rag`) | **PASS** (isolated Compose project) | Fresh-DB success does not prove legacy upgrade |

## Environment

macOS (Darwin 25.4.0), Docker 29.7.2 / Compose v5.4.0 (Docker Desktop was not
running at start; started by the agent), image `pgvector/pgvector:0.8.0-pg17`,
Python 3.12 via uv workspace (`uv lock --check` exit 0, 74 packages), bun 1.4.0.
uv ran with `UV_CACHE_DIR=$TMPDIR/uv-cache`; Docker/DB commands ran outside the
agent sandbox (socket and port binding). No provider/network model traffic.

## Inventory before edits

`selfcheck-20261016/git-status-before.txt` and `git-diffstat-before.txt` (54 files
changed vs HEAD). All pre-existing modifications (T1-T3 WIP, worker move, plan/design
D43 edits from an earlier session today) were left in place. Not present:
`AGENTS.md` (no file in repository). `.omo/evidence/` and `.omo/run-continuation/`
are git-ignored. `.claude/.cc-writes` directories in `apps/rag` are tool artifacts;
untouched. Worktree `../eventlens-langgraph-rag-foundation` untouched.

## Changes made in this run

| File | Change | Reason |
| --- | --- | --- |
| `apps/ai-worker/src/eventlens_worker/memory_writer.py` | Removed (copy kept in `selfcheck-20261016/stray-ai-worker/`) | Only file left under old path; equals HEAD minus one blank line (mtime 2026-10-10), broke `ruff format --check` so `make check` exited 2. `apps/worker` copy is byte-identical to HEAD and the D41 receipt digest. See `stray-ai-worker/comparison.txt` |
| `apps/rag/src/eventlens_rag/adapters/migrations.py` | `LegacySchemaError` + `_guard_unrecorded` | Untracked pre-ledger tables (Compose `rag_chunks` without `generation`) crashed with raw `UndefinedColumn`; now typed fail-closed before any DDL, same transaction rollback |
| `apps/rag/src/eventlens_rag/cli.py` | Map `LegacySchemaError` to `rag_migration_failed`, exit 1 | Typed operator error |
| `apps/rag/tests/test_legacy_schema.py` (new) | 2 real-DB tests with the Compose legacy shape | Prove typed failure, no ledger, legacy tables/rows unchanged, CLI output |
| `apps/rag/tests/test_migration_portability.py` | Sync test also asserts identical file sets = `EXPECTED_MIGRATIONS` | Old test passed even if a checked-in copy was missing |
| `Makefile` | `COMPOSE`, `RAG_DATABASE_NAME`, `RAG_DATABASE_URL` (default DB `eventlens_rag`); new `rag-db-create`, `rag-load-fixtures` | `rag-migrate`/`rag-rebuild` targeted the legacy `eventlens` DB and could only fail |
| `apps/rag/gate-g1.sh` | Never writes acceptance PASS; exit 2 = technical PASS + G1 OPEN; adds `stale-worker-path` and `db-unavailable` (expects exit 3) steps | Old script wrote `gate-status=PASS` from technical checks + `apps/worker` existing |
| `apps/rag/gate-g1-db.sh` | `generation-reuse` step: second rebuild must reuse all published generations | Reuse was only logged, not asserted |
| `apps/rag/README.md`, `README.md` | Migration source of truth, legacy behaviour, dedicated DB commands, gate exit semantics, `ai-worker` service name note | Docs contradicted behaviour |

Migration SQL files were not modified. Applied checksums in the Compose
`eventlens_rag_g1` database (read-only query) equal current file digests.

## Commands (this run)

| Command | Exit | Result |
| --- | ---: | --- |
| `rtk uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json` | 0 | `Valid article 550e8400-… with 2 media items.` |
| `rtk uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json` | 0 | `verdict=ACCEPT, reasons=[ready], write_disposition=inserted` (same line as 2026-10-03 receipt) |
| `rtk proxy uv run pytest apps/worker/tests -q` | 0 | 138 passed |
| `shasum -a 256 -c worker-receipt-digests.txt` (27 receipt digests at `apps/worker/*`) | 0 | 27/27 OK |
| `rtk proxy make check` (before fix) | 2 | `ruff format --check`: stray `apps/ai-worker/.../memory_writer.py` |
| `sh apps/rag/with-test-db.sh uv run … pytest test_legacy_schema.py test_migration_portability.py` | 0 | 8 passed |
| `rtk proxy make check` | 0 | ruff/basedpyright 0 errors; pytest **398 passed, 26 skipped** (all skips: no DSN, not DB evidence); bun 2 pass; web build ok |
| `rtk proxy make rag-db-test` | 0 | **129 passed, 0 skipped**, isolated pgvector, container removed |
| `rtk proxy make gate-g1` | 2 | technical-status `PASS`; gate-status `OPEN: technical PASS only…`; scoped pytest 303 passed; migrate/load 30/rebuild 30 (0 reused) then 30 (30 reused); generation-reuse check 0; DB suite 129 passed; db-unavailable exit 3 `rag_storage_unavailable` |
| `docker compose --profile tools config --quiet` | 0 | |
| `docker compose --profile tools build ai-worker` / `run --rm ai-worker` | 0 / 0 | Image builds from `apps/worker/Dockerfile`; container validate prints the same valid-article line (local image `eventlens-ai-worker` now exists) |
| `rtk git diff --check` | 0 | clean (tracked files; new files covered by ruff format) |

## Dedicated DB and legacy upgrade (isolated Compose project)

Script `selfcheck-20261016/compose-verify.sh`: project `eventlens-g1-verify`, same
`compose.yaml` plus override (port 55432, tmpfs, no volume), legacy DDL from the
read-only dump loaded into that project's own `eventlens` DB.

| Step | Exit | Result |
| --- | ---: | --- |
| `make rag-migrate RAG_DATABASE_URL=…/eventlens` (legacy copy) | 2 (CLI 1) | `rag_migration_failed: Untracked legacy RAG tables … rag_chunks. Nothing was changed.`; `pg_dump --schema-only` hash identical before/after |
| `make rag-db-create` ×2 | 0, 0 | idempotent |
| `make rag-migrate` ×2 (`eventlens_rag`) | 0, 0 | ledger 5 rows |
| `make rag-load-fixtures` | 0 | 30 articles (lexical-before-vector visibility is covered by the DB suite) |
| `make rag-rebuild` ×2 | 0, 0 | 30 published (0 reused), then 30 (30 reused); 30 generations total |
| FTS `harbor` | – | articles …001, …002, …015 |
| `compose down` | 0 | no `eventlens-g1-verify*` volume left |

Real Compose DB (read-only, `default_transaction_read_only=on`, container started
then stopped again): `eventlens` has 6 untracked tables (`article_documents`,
`article_enrichment_versions`, `rag_chunks`, `rag_document_versions`,
`rag_index_metadata`, `rag_jobs`), **0 rows each**, no `rag_schema_migrations`.
Separate `eventlens_rag_g1` (created 2026-10-02) has 5 ledger rows matching current
checksums, 30 docs/30 chunks. Dump: `legacy-eventlens-schema.sql`. Nothing written,
dropped or migrated on the real Compose server or its volume.

**Fresh DB PASS does not prove legacy upgrade PASS.** In-place upgrade is not
automatic: the legacy tables have no ledger and a foreign shape. Since they hold no
rows there is no legacy content to re-ingest; recovery options are (a) keep them and
use `eventlens_rag` (implemented default), or (b) owner-approved drop/archive of
the six empty tables, then `make rag-migrate RAG_DATABASE_NAME=eventlens`. Should
any legacy rows ever exist, carry them forward only by re-ingesting the exact
original ArticleDocuments; never rebuild documents from title/content.

## D41

- Technical correctness: 27/27 receipt digests match at `apps/worker`; workspace,
  ruff/pyright src, pytest testpaths, uv.lock editable path, Dockerfile/Compose
  build, CLI names and 138 worker tests all consistent; gate checks `apps/ai-worker`
  absent. No remaining code/config reference to the old path (Compose service name
  `ai-worker` kept intentionally).
- Current-path decision: the user's 2026-10-16 instruction is to keep `apps/worker`.
  This is a forward decision, not historical approval.
- Historical evidence: searched `.omo/evidence`, `.omo/drafts`,
  `.omo/run-continuation` (236 files), plan, docs and git log. Found only the
  2026-10-02 record of the owner choosing to keep `apps/ai-worker` and the
  2026-10-03 move receipt without an approval reference. Approval for the
  2026-10-03 move is **chưa xác minh được**; no approval was created or inferred.

## Limitations

- DB evidence is from ephemeral containers and an isolated Compose project, not the
  user's persistent Compose volume.
- F1-F4 written this run are self-reviews by the same agent that made changes.
- CI still runs `make check` only; real-DB CI wiring belongs to T9.
- Index `EXPLAIN` evidence is from the DB suite and the 2026-10-03 manual QA, not
  re-run separately; no performance claim.
- T4-T9 untouched; D43 plan/design edits from earlier today are T5/T9 design only.

## Addendum 2026-10-16: fixes for independent review findings (`G1/review-20261016-codex/review.md`)

Self-applied by Claude (main session); not an independent re-review.

- [P2] `Makefile` `rag-migrate`, `rag-load-fixtures`, `rag-rebuild`: `EVENTLENS_RAG_DATABASE_URL` value is now single-quoted so `&` in multi-parameter URLs is not parsed by the shell.
  - Verification: `make rag-migrate RAG_DATABASE_URL='postgresql://eventlens:x@127.0.0.1:1/eventlens_rag?sslmode=disable&connect_timeout=2'` -> make exit 2 (CLI exit 3), `rag_storage_unavailable` (the full URL reached the CLI; before the fix the CLI reported missing `--database-url`). `make -n` shows the URL intact. Not re-run against a live DB in this addendum.
- [P3] `.omo/plans/eventlens-ai-rag-mvp.md` unresolved-decisions line about T1/T2 status: the old text is kept with a "Historical" label, and a "Current (2026-10-16)" line was added that matches the Todos checklist.
- `git diff --check`: exit 0.

G1 status unchanged: OPEN until the fixes are re-verified by the independent reviewer, the D41 historical approval evidence gap is resolved or explicitly accepted, and an explicit G1 acceptance decision is recorded.

## Addendum 2 2026-10-16: fix for re-review P2 (`G1/rereview-20261016-codex/review.md`)

Self-applied by Claude (main session); not an independent re-review.

- `Makefile`: `rag-migrate`, `rag-load-fixtures` and `rag-rebuild` no longer put the URL on the shell command line. A target-specific `export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)` passes it through the recipe environment, so `&` and `'` are never parsed by the shell.
- Verification (PATH shim `uv` that prints `$EVENTLENS_RAG_DATABASE_URL`): with URL `postgresql://eventlens:x@127.0.0.1:1/eventlens_rag?sslmode=disable&application_name=g1_review'quoted`, all three targets exit 0 and the received value equals the input byte for byte. The default (no override) resolves to `.../eventlens_rag`. A real `make rag-migrate` with that URL reaches the CLI and returns `rag_storage_unavailable` (CLI exit 3) for the closed port, not a shell syntax error. Not re-run against a live DB in this addendum.
- Known limit: Make itself still expands `$` in a command-line variable value; a literal `$` in the URL must be written as `$$`.
- `git diff --check`: exit 0.

G1 status unchanged: OPEN until independent re-verification, the D41 decision and an explicit G1 acceptance decision.
