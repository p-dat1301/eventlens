# G1 F2 - Code quality (self-review)

Reviewer: Claude (Claude Code agent), 2026-10-16. **Self-review, not independent.**
Scope: `apps/rag`, `apps/worker`, `contracts/python` as of this run.

Verdict: **PASS for automated checks; independent F2 still required for G1.**

| Check | Exit | Result |
| --- | ---: | --- |
| `rtk proxy make check` (ruff format --check, ruff check, bun check, basedpyright, bun typecheck, pytest, bun test, web build) | 0 | 129 files formatted; ruff clean; basedpyright 0/0/0; 398 passed, 26 skipped (no DSN); bun 2 pass; build ok. Log `selfcheck-20261016/make-check.log` |
| Same command before removing stray file | 2 | ruff format failure in `apps/ai-worker/.../memory_writer.py` |
| Module size (non-blank, non-comment lines) | - | largest `domain/answer.py` 198, `adapters/staging.py` 172, `adapters/migrations.py` 126; all < 250 |
| Architecture | - | `test_architecture.py` passes (framework imports only in graph/adapters; domain import does not load LangGraph); no `eventlens_worker` import in API or `eventlens_api` import in worker; API and worker both import `eventlens_rag` |
| Typed errors | - | `StorageUnavailableError` exit 3 (gate `db-unavailable`); `LegacySchemaError`/`LegacyDocumentError`/`MigrationResourcesError` -> `rag_migration_failed` exit 1 |

Observations (not blocking technical PASS): CI does not run the real-DB target (T9);
`apps/rag/migrations/` duplicates the packaged SQL, now guarded by a set+bytes test.
