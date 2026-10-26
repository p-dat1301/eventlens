# G1 commit verification — 2026-10-16

Executor: Codex. G1 acceptance is already recorded in
`G1/acceptance-decision-2026-10-16.md`; this check adds technical evidence only.

Exported the staged index into an owned temporary directory using
`git checkout-index`, installed frozen Python/web dependencies, and ran checks
without the original checkout's untracked files, virtualenv or node_modules.
The index was unchanged throughout validation.

Validated source tree: `d36bcaaa426488f6ef589076820cbb6714eee5a2`.
Only this verification record/archive are added after the validated snapshot;
no product code changes follow these checks.

| Check | Exit | Result |
| --- | ---: | --- |
| `uv sync --frozen --all-packages --group dev` | 0 | Clean install succeeds |
| `bun install --frozen-lockfile` | 0 | Clean install succeeds |
| `make check` | 0 | Ruff/typecheck PASS; 398 Python tests passed, 26 DSN-dependent skips; web tests/build PASS |
| `make gate-g1` | 2 | Technical PASS; 303 scoped tests, 129 real pgvector tests / 0 skips; migrate/load/rebuild x2/reuse and unavailable-DB checks PASS |
| Owned-container cleanup | 0 | No eventlens-rag-test-* containers remain |

Gate exit 2 is the script's technical-only result, not a reversal of the owner's
recorded G1 acceptance. No persistent/legacy database was changed.

Logs, exact command results and the verification script are preserved in
`G1/commit-check-20261016.zip`.

The commit includes T1–T3 foundation fixes, worker relocation, and acceptance
evidence. Existing D43/T5 design edits, the thesis outline and local continuation
metadata remain outside the commit. No old worktree is removed and no push occurs.
