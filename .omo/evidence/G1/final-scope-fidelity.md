# G1 F4 - Scope Fidelity Review

Reviewer: Sisyphus-Junior (independent read-only scope review), 2026-10-03.
Verdict: **FAIL** for G1 scope-fidelity clearance. Covered: current T1-T3 worktree versus `.omo/plans/eventlens-ai-rag-mvp.md` and `docs/designs/ai-rag-briefing-mvp.md`; not G2-G6, live activation, semantic quality, or general code-quality acceptance.

## Blocking finding

- **D41 authorization not established for physical worker move.** Plan requires affected owner approval or explicit clean checkpoint *before* moving dirty worker WIP; without it, old path must stay active (`.omo/plans/eventlens-ai-rag-mvp.md:75-77`). Earlier T3 correction records owner choice to keep `apps/ai-worker`, with rename blocked (`.omo/evidence/task-3-eventlens-ai-rag-mvp.md:296-302`). Later move receipt records inventory, move and hash/CLI parity, but no subsequent owner approval (`.omo/evidence/task-3-d41-rename-2026-10-03.md:3-11,49-59`). Current `apps/worker/pyproject.toml:7-10` and `pyproject.toml:68-74` make new path active; old tracked directory appears deleted in `git diff HEAD --name-status`. `apps/rag/gate-g1.sh:37-42` checks only directory presence before writing `PASS`, not D41 approval. Gate's `PASS` file (`.omo/evidence/G1/run-20261003T074008Z-TjfwQn/gate-status:1`) cannot establish this policy precondition. Supply dated approval/checkpoint identifying affected WIP and authorizing move, then rerun independent F4 review. If approval exists outside repo, this review has not verified it; do not read finding as proof approval was never given.

## Boundaries checked

- **No new acquisition/canonical event mutation found in T1-T3 RAG paths.** Approved design takes normalized `ArticleBundle`, not crawler/RSS (`docs/designs/ai-rag-briefing-mvp.md:82-100`); RAG persistence writes `rag_article_documents`, `rag_article_current`, and `rag_enrichment_jobs` (`apps/rag/src/eventlens_rag/adapters/postgres.py:27-77,91-145`). SQL declares only `rag_*` document/job/generation/chunk tables (`apps/rag/src/eventlens_rag/migrations/002_create_rag_index.sql:1-74`). RAG graph has one `persist` node and no model/tool/checkpointer branch (`apps/rag/src/eventlens_rag/graph/graph.py:16-50`). Existing worker's `FakeEventExtractor`/`InMemoryEventWriter` is still its local event demonstration, explicitly not RAG semantics (`apps/worker/README.md:1-29,45-57`); do not misclassify it as newly added canonical writer. Text/code search across app Python/SQL/CI found no implemented G4 grouping, G5 conversation, G6 tools, crawler, or network model adapter.
- **Raw fallback not present in checked contract; runtime answer not yet built.** `BriefingAnswer` requires claim/citation linkage and exact deterministic display text, rejecting mismatched free prose (`apps/rag/src/eventlens_rag/domain/answer.py:123-160,188-195`). `ArticleDocument` ties NFC text, hash, span identity, and exact excerpt offsets (`apps/rag/src/eventlens_rag/domain/article_document.py:55-99`). No T6 synthesis/API return path exists in G1, so this is contract evidence, not end-to-end guarantee against raw model fallback.
- **Fixture/fake provider boundary intact in present runtime.** Rebuild constructs `FakeEmbeddings` (`apps/rag/src/eventlens_rag/cli.py:76-100`); RAG dependency list has LangGraph, psycopg, Pydantic and Typer, not model HTTP client (`apps/rag/pyproject.toml:7-13`). CI runs frozen install and `make check`, has no provider secret or live-provider step (`.github/workflows/ci.yml:19-62`); `make check` runs lint/type/tests/web build, not DB gate (`Makefile:7-23,43-47`). Current CI also does not run `rag-db-test`, as plan assigns real-DB CI wiring to T9 (`.omo/plans/eventlens-ai-rag-mvp.md:44-50`); local DB pass is not CI evidence.
- **Worker move content parity verified independently:** compared every `HEAD:apps/ai-worker/*` tracked file to corresponding `apps/worker/*` using `cmp`; all 27 targets exist, only `pyproject.toml` differs from HEAD by added `eventlens-rag` dependency (`apps/worker/pyproject.toml:7-10`), documented as pre-move dirty WIP (`.omo/evidence/task-3-d41-rename-2026-10-03.md:7-8,49-58`). Receipt claims pre/post 138 tests and byte-identical validate/process output (`:9-15,51-56`); independent comparison confirms present content, not historical approval or baseline hashes. Path refs updated in `compose.yaml:18-23`, `pyproject.toml:34-40,57-74`, and `README.md:12-16`.

## Caveats / provenance

- Gate log records `303 passed` scoped tests and `127 passed` DB tests, zero skips (`.omo/evidence/G1/run-20261003T074008Z-TjfwQn/graph-tests.log:1-6`, `pytest-db.log:1-3`), with child exit codes zero (`exit-codes.tsv:1-10`). Reviewed logs only; no DB mutation or fresh test execution by this reviewer. Tests do not prove authorization, lack of external side effects, or live-provider quality.
- Worktree contains unrelated modified `.omo/run-continuation/ses_f4020bf6dffehpuZ3BeMWIfV0V.json:3-8` (timestamps) and `.gitignore:16-19` (ignore agent evidence/runtime files). Git diff shows these changes, but origin/owner cannot be attributed from current snapshot; they are outside T1-T3 product scope and were not edited or reverted in review. Plan itself is modified, including future G4-G6 design; this review assesses implemented G1 surface against its current text, not approval of plan edits.
- Legacy Compose `eventlens` schema recovery remains blocked and separate from isolated fresh DB success (`.omo/evidence/task-3-eventlens-ai-rag-mvp.md:252-262,296-302`). No old DB was touched by this review.

Method: read plan/design/current sources, CI, gate and receipts; inspect `git diff HEAD`, tracked/untracked paths, and worker byte parity. Only this report written. No product/plan edit, commit, push, provider request, or DB operation.

---

## Addendum 2026-10-16 - D41 status split (Claude self-review)

**Self-review by the agent that made the 2026-10-16 changes; not independent.** The
FAIL verdict above (independent reviewer, 2026-10-03) is historical and unchanged.

1. **Technical correctness of the move: PASS.** 27/27 receipt SHA-256 digests match at
   `apps/worker`; workspace/ruff/pyright/pytest/uv.lock/Dockerfile/Compose agree;
   138 worker tests pass; validate/process output identical to the receipt; container
   build and validate pass. The only remnant under `apps/ai-worker` (a whitespace-only
   variant of `memory_writer.py`, mtime 2026-10-10) was removed with a backup in
   `selfcheck-20261016/stray-ai-worker/`; `make gate-g1` now fails if the old path exists.
2. **Decision to keep the current path: recorded.** The user's 2026-10-16 instruction is
   to keep `apps/worker` and not move back. This is a forward decision only.
3. **Historical approval evidence: OPEN (chưa xác minh được).** Searched `.omo/evidence`,
   `.omo/drafts`, `.omo/run-continuation`, plan, docs and git history. Only the
   2026-10-02 owner choice to keep `apps/ai-worker` and the 2026-10-03 receipt (no
   approval reference) exist. No approval was created or inferred.
4. `gate-g1.sh` no longer writes acceptance PASS from technical checks or from
   `apps/worker` existing; the old `run-20261003T074008Z-TjfwQn/gate-status` PASS is
   superseded as an acceptance signal (file left unchanged as history).

Other scope boundaries re-checked: no crawler, canonical event write, provider client,
checkpointer, tools, grouping/conversation code or T4-T9 implementation. F4 clearance
for G1 still requires an independent reviewer and resolution of item 3.
