# G1 acceptance decision (2026-10-16)

Decision made by: project owner (user), answering an explicit confirmation question in the Claude Code main session on 2026-10-16. Recorded by Claude; this file records the decision only and adds no new test result.

Owner statement (selected option "Xác nhận gộp"):

> Tôi chấp nhận khoảng trống bằng chứng D41, giữ apps/worker và nghiệm thu G1 trong phạm vi T1–T3 fixture/fake, sử dụng DB riêng eventlens_rag.

## What this accepts

- G1 Foundation is ACCEPTED, scoped to T1-T3 with fixtures and fake providers only (no real provider, no live traffic).
- D41: the historical approval for the 2026-10-03 move from `apps/ai-worker` to `apps/worker` remains unverified. The owner explicitly accepts that evidence gap. The move is not recorded as approved retroactively; `apps/worker` stays the current path by the owner's 2026-10-16 decision.
- Local RAG storage uses the separate database `eventlens_rag`. The legacy Compose `eventlens` database is not upgraded in place; its 6 legacy tables are left untouched. A future in-place upgrade remains OPEN and needs its own approval.

## Basis

- Self-check: `.omo/evidence/task-3-g1-selfcheck-2026-10-16.md` (with addenda 1 and 2).
- Independent review: `G1/review-20261016-codex/review.md` (2 findings), `G1/rereview-20261016-codex/review.md` (P3 closed, P2 partially open), `G1/rereview2-20261016-codex/review.md` (PASS, P2 and P3 closed, no new findings).

## Not covered

T4-T15, G2-G6, real-provider quality, legacy in-place migration, commit/push authority.
