# T6 independent integration review — 2026-10-24

Reviewer: Codex, performed after T4 and T5 were fast-forwarded into
`feature/rag-foundation`. Scope is T6 grounded synthesis in fixture/fake mode.
The review covers the merged source snapshot at `45051be` plus the T6 files
being prepared for commit.

## Verdict

**APPROVE technical T6 for fixture/fake scope.** The fresh integrated checks
passed. T6 does not accept G2, live provider activation, semantic answer
quality, or deployment; T7 remains required for API and graph composition.

## Fresh verification

| Check | Exit | Result |
|---|---:|---|
| `make check` | 0 | 925 passed, 125 DB-dependent skips; lint, typecheck and web build passed |
| `make rag-db-test` | 0 | 893 passed, 0 skipped on isolated PostgreSQL/pgvector |
| T5 → T6 `synthesis-smoke` | 0 | `all_passed=true`, 7/7 cases passed, limiter returned to zero |
| `uv build --package eventlens-rag` | 0 | sdist and wheel built successfully |
| `git diff --check` | 0 | clean for tracked changes |

## Behavior reviewed

- Structured synthesis is behind `SynthesisModelPort`; deterministic fake is the
  default and the OpenAI-compatible adapter uses injected transport only.
- Provider payload is allowlisted and excludes hashes, offsets, canonical URLs,
  storage URIs, credentials and internal metadata.
- The answer is rendered from validated claim/span blocks and versioned
  uncertainty templates. Invalid, unknown, stale or unsent spans fail closed;
  raw model prose is not returned.
- Access and current-content checks run before and after the model call, with
  batched source reads and typed storage/access errors.
- Token, output, cost, deadline, model-call and process-concurrency budgets are
  enforced from bundled policies. Cancellation and provider failures settle the
  reservation and do not trigger hidden retries.
- Smoke covers cited answer, lexical-only answer, no-evidence abstention, budget
  refusal, invalid citation, provider timeout and access revocation.

## Limits kept open

- Only fixture/fake behavior was activated; no provider network request or live
  policy approval is present.
- Real tokenizer drift, provider cost/rate, production latency and semantic
  entailment quality are not measured. These remain T9/live-activation gates.
- API endpoint, briefing graph composition, source viewer and web analyst flow
  remain T7/T8 work.

