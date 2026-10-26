# T4 commit checkpoint — 2026-10-16

Owner instruction: "thực hiện commit rồi viết yêu cầu thực hiện T5".
This authorizes committing the independently reviewed T4 changes and writing the
T5 implementation request. It does not authorize a push, deployment, live provider
activation or acceptance of G2. G2 remains OPEN until T5-T7 and its scoped review.

Branch: `feature/rag-enrichment-t4`; parent `b1a3900f7da268373487bbbaf28a510bccad3a4a`.
The commit contains T4 ingest/enrichment lifecycle, worker entrypoint, migration
006, regression coverage, documentation and evidence. T5 design WIP in the main
worktree is excluded. Main worktree and older worktrees remain untouched.

## Review and verification

- Initial independent review: `../T4-review-20261016-codex/review.md` (historical
  REQUEST CHANGES, R1-R4).
- Final independent re-review: `../T4-rereview-20261016-codex/review.md` (APPROVE,
  R1-R4 CLOSED, no new material finding, fixture/fake scope).
- Independent `make check`: exit 0, 499 Python passed, 56 DB-dependent skipped;
  lint/typecheck/web checks also pass.
- Independent `make rag-db-test`: exit 0, 398 passed, 0 skipped on isolated real
  PostgreSQL/pgvector.
- Independent review regressions: 12/12 pass; additional multi-chunk in-flight
  cancellation probe passes with no subsequent embedding or stage writes.
- Implementer `make gate-g1`: exit 2 = technical PASS under the existing gate
  convention. The final narrow re-review did not rerun that aggregate gate.
- Immediately before documentation-only checkpoint updates, every file in the
  final re-review's source manifest matched its recorded SHA-256 (`[]` differences).
  No runtime code changed after review; the full suite was not repeated for these
  status/evidence edits.

The earlier self-check report is retained at
`../task-4-eventlens-ai-rag-mvp.md`, with historical statements labelled and the
current status at its top. It is not a substitute for the independent review.

## Raw evidence archive

`verification-20261016.zip` preserves both independent review directories,
implementation self-check/fix logs and the three associated G1 technical runs.
Archive paths begin with `.omo/evidence/`; extract at the repository root to
restore paths referenced by the reports. Original local files are retained.

Archive SHA-256:
`81c0713df7febbe8cbbe3fc3abb87659117d1da727e345d61bf43a5886d3c75b`.
The staged `git diff --check` is clean.

The archive contains original source manifests, test output, scripts, exit codes
and failure notes. Historical failing runs remain evidence of their own runs;
only the final re-review results above describe the approved source snapshot.
Readable reports are also committed outside the archive for direct review.

Known limits remain in the implementation/re-review reports: fake/mock transports
only, no semantic-quality claim, single configured enrichment identity, lazy job
STALE marking after tombstones, and lease recovery without a SIGTERM handler.
No legacy `eventlens` database operation is part of this checkpoint.
