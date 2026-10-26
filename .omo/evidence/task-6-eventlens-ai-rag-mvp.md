# T6 evidence: grounded synthesis, citation validation, model budgets

**Status: technical SELF-CHECK PASS by the lead. This is not an independent
review.** T6 is not accepted, G2 stays OPEN, and nothing is committed or pushed.
Fixture/fake scope only: no live provider, no network and no provider key were
used.

## 1. Environment and base

| Item | Value |
| --- | --- |
| Worktree | `/Users/phungdinhdat/Documents/git/eventlens/.claude/worktrees/rag-synthesis-t6` |
| Branch | `feature/rag-synthesis-t6` (uncommitted working tree) |
| Base commit | `45051be669a95651d66eb701a725daf05cca41e3` (T5 `feat(rag): add deterministic hybrid retrieval`) |
| Host | macOS arm64, Python 3.12 via uv 0.12.5, Docker 29.7.2, `pgvector/pgvector:0.8.0-pg17` (owned, tmpfs, removed after each run) |
| Spec | main repo `docs/tasks/t6-grounded-synthesis.md` |
| Frozen contract | `.omo/evidence/T6/interfaces.md` (§1–6 phase 1; §7 phase 3 decisions) |
| Tooling notes | `uv` must run outside the Claude Code sandbox because the sandbox denies `~/.cache/uv`. The `rtk` prefix worked for every command. The worktree needed `uv sync --all-packages --group dev --frozen` and `bun install --frozen-lockfile` once; lockfiles are unchanged. |

Work phases: phase 1 froze the interfaces (lead). Phase 2 was split
between Worker A (core) and Worker B (provider boundary). Phase 3 covered
integration, the DB/E2E tests, the smoke and this evidence (lead).

## 2. Command / exit / pass / skip (final run)

The final run is `.omo/evidence/task-6-logs/run-20261019T002222Z-phase3-final/`
(`exit-codes.tsv`, one log per step; the script is `run.sh` in the same
directory).

| Step | Command | Exit | Result |
| --- | --- | --- | --- |
| 01 | `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check` | 0 | 925 passed / 125 skipped. The skips are DB-dependent (no DSN) and are **not** DB evidence. Lint, typecheck and web build passed. |
| 02 | `rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test` | 0 | **893 passed / 0 skipped** on real PostgreSQL+pgvector (`apps/rag/tests` + `apps/worker/tests`) |
| 03 | `sh apps/rag/with-test-db.sh uv run --package eventlens-rag pytest <all 13 T6 test files> test_architecture.py -q -rs` | 0 | 355 passed, 0 skipped |
| 04 | `rtk git diff --check` | 0 | clean |
| 05 | `with-test-db.sh … eventlens-rag synthesis-smoke --output … --evidence …` | 0 | 7/7 cases asserted, `all_passed=true`, limiter `in_use=0` afterwards |
| 06 | `with-test-db.sh … eventlens-rag retrieval-smoke` (T5 compatibility) | 0 | All 5 T5 cases produced their expected status |
| 07 | `git diff --quiet HEAD --` on migrations, retrieval-policy v1/v2/v3, enrichment-retry.v1, db harness scripts, `conftest.py`, `Makefile` | 0 | byte-identical |
| 08 | `rtk git diff --check` after the README/plan edits | 0 | clean |

The baseline before T6 was 594 passed / 105 skipped (`make check`) and
542 / 0 (`rag-db-test`).

`gate-g1` was **not rerun**. The shared schema, migrations, storage
adapters, DB harness, `conftest.py` and worker are unchanged (step 07). The
only shared file touched is `cli.py`, which gained a new command.

Earlier runs are kept unchanged:

- `run-20261018T164548Z-phase1` (interface freeze; `make check` 597/105).
- `run-20261019T000419Z-B` (Worker B).
- `run-20261019T001102Z-A` (Worker A). The `pytest-owned-1..5` exit 4 entries
  there are a harness error: zsh did not word-split the file list, so no test
  ran. The reruns are `r1..r5`, exit 0.
- `run-20261019T001334Z-phase3-dev`:
  - `02-regression-red-prefix.log`: 3 failed on the pre-fix code.
  - `03-regression-green.log`: 9 passed.
  - `04-smoke-dev`.
  - `05-wheel-build`.
  - `06-wheel-install-load`. Attempt 1 failed because offline dependency
    resolution needs network that was not available. Attempt 2, which
    installs the wheel with `--no-deps`, passed.

## 3. Policies (versioned, fixture label)

| Policy ID | File | Content |
| --- | --- | --- |
| `synthesis-policy.v1` | `apps/rag/src/eventlens_rag/policies/synthesis-policy.v1.json` | 12,000 cumulative input tokens and 1,500 cumulative output tokens per user request. 1,500 output reserved per call. 20 articles, 1 call, 0 retries, framing reserve 256, minimum evidence 16. Deadline 20 s, call timeout 15 s (capped by the remaining time). The 8 s p95 is a target, not an SLO. 30 concurrent requests, process-local, rejected when full and never queued. Fixture pricing: 1 per input token, 4 per output token, 2,000 per call, cap 20,000 `fixture_micro_usd`. Ledger settlement rules and versioned uncertainty templates (`uncertainty-templates.v1`). The rationale is in the file. |
| `provider-activation.fixture.v1` | `…/policies/provider-activation.fixture.v1.json` | Issuer `eventlens-t6-fixture`, mode fixture. One exact endpoint rule `https://provider.example.invalid:443/v1`. Chat binding `fixture-chat-model` and embedding binding `fixture-embedding`, both on `fixture-regex-tokens/v1` (basis `fixture`). Corpus `eventlens-fixture-corpus`, classification `internal`. The five-field allowlist plus a prohibited list. The exact HTTP contract: `/chat/completions`, POST, `json_schema_strict`, `max_tokens`, temperature 0, finish `stop`, no redirects, TLS verify, no env proxy, 0 client retries. The budget is bound to `synthesis-policy.v1`. |
| `retrieval-policy.v3`, `enrichment-retry.v1` | unchanged | byte-identical (step 07) |

Every number is read from these files at runtime. Tests assert the wiring
through observed behavior, for example `test_*_is_read_from_policy`,
`test_single_call_at_full_caps_costs_exactly_the_cost_cap` and
`test_cost_cap_refuses_second_call_within_token_caps`.

Packaging: the built wheel `eventlens_rag-0.1.0-py3-none-any.whl` contains
both new policy JSONs and migrations 001–006. Installed with `--no-deps` into
a fresh venv, it imports `eventlens_rag` from that venv, not the checkout.
From there it loads `synthesis-policy.v1`, `provider-activation.fixture.v1`
(sha256 `00db09d3…2336`), `retrieval-policy.v3` and the draft schema
(`06-wheel-install-load.log`). No new dependency or tokenizer was added.

## 4. Interfaces for T7

See `.omo/evidence/T6/interfaces.md` §2–§4 and §7.1. In short:

```text
lease = ProcessConcurrencyLimiter(30).try_acquire()      # one per runtime
control = QueryControl(20.0); ledger = RequestTokenLedger(counter, policy)
plan = PrepareSynthesis(counter, policy).execute(query, ledger)
retrieval = RetrieveEvidence(...).execute(query, caller, control,
            RetrievalOptions(remaining_context_budget=plan.remaining_context_budget))
result = SynthesizeAnswer(model, sources, access, tokens, policy, activation,
                          telemetry).execute(query, retrieval, caller, control, ledger, lease)
lease.release()
```

- **Activation.** Composition calls
  `application/activation.py::activate(policy, sha256, synthesis_policy,
  environ, requested_mode, trust, counter.identity)`. Fixture mode is the
  default. Live mode fails closed in T6.
- **Result and errors.** `SynthesisResult` is `answered`,
  `abstained_no_evidence` or `abstained_by_model`, with usage, provenance and
  metrics. Failures are typed `SynthesisError` codes. The §8 outcome mapping
  is in interfaces.md §4.3.
- **Source documents.** `BriefingAnswer.source_documents` is internal; T7 maps
  a public DTO.

## 5. Requirement → code / test map

| Spec | Code | Tests (behavioral) |
| --- | --- | --- |
| §2 service + DI; fake default; no framework imports | `application/synthesize.py`, `ports/synthesis.py`, `adapters/fake_synthesis.py` | `test_architecture.py` (layering), `test_synthesis.py::test_happy_fake_answer_is_cited_rendered_and_server_owned` |
| §3 reuse T5/T1 contracts, server-owned citations, sent-span only, truncation identity | `application/citations.py`, `application/rendering.py`, `domain/synthesis.py` | `test_citations.py` (`valid_but_not_sent`, `truncated_span_keeps_post_truncation_identity`, `tampered_span_identity`, `other_article`) |
| §4 five-field payload, strict draft, no uncited prose, templates, temporal, NFC | `domain/model_input.py`, `domain/synthesis_draft.py`, `adapters/redaction.py` | `test_synthesis_contracts.py`, `test_redaction.py`, `test_citations.py` (`rendered_answer_contains_only_claims_and_templates`, temporal/Unicode cases), `test_synthesis.py::test_valid_model_abstention_is_template_only` |
| §5 batched current checks pre/post model, access on every return, typed stale/denied | `synthesize.py::_read/_finish`, `citations.py::build_registry` | `test_synthesis.py` (revocation, tombstone, correction and eligibility cases); **real DB** `test_synthesis_db.py` (14 tests) |
| §6 D31 activation, endpoints, env, TLS/redirect/proxy, HTTP contract, typed codes, no hidden retry | `application/activation.py`, `adapters/openai_synthesis.py`, `adapters/trust.py`, `adapters/openai_embeddings.py` | `test_activation.py`, `test_activation_endpoint.py`, `test_openai_adapter.py` (zero outbound requests for every rejected config; exact request shape; one request per call) |
| §7 D32 ledger, full serialized counting, remaining budget to T5, caps, cost, drift | `application/ledger.py`, `PrepareSynthesis`, `_fit` | `test_synthesis_ledger.py`, `test_synthesis_budget.py`, `test_synthesis_integration.py::test_live_adapter_composes_with_service_and_maps_usage` (reported usage and drift through the HTTP adapter) |
| §8 shared limiter, pin while in flight, cancel/deadline semantics, outcome mapping | `application/concurrency.py`, `_run_pinned`, `_admit` | `test_synthesis_concurrency.py` (30/31 with barriers; no slot leak; timeouts cannot grow in-flight calls), `test_synthesis.py` (pre-cancelled, deadline vs provider) |
| §9.8 A+B composition over the mock transport | – | `test_synthesis_integration.py` (7 tests, network blocked) |
| §9.10 real PostgreSQL races and N+1 | – | `test_synthesis_db.py`: correction, tombstone and revocation during the model call **and** during the final batch fetch; abstention paths (no-evidence and model abstention); storage failure typed; telemetry carries no usage/metrics after denial; `test_source_reads_are_two_batched_statements_regardless_of_size[1,3,8]` asserts exactly 2 SQL statements |
| §9.11 E2E | – | `test_synthesis_e2e.py`: T4 ingest → lexical-only answer → T4 fake enrich (READY) → T5 hybrid filtered → T6 fake → validated cited answer; invalid model span → `synthesis_grounding_failed/unknown_span`, no raw text; the smoke runs inside the DB suite and a wrong expectation is reported as failed |
| §9.12 regressions, packaging | – | full `make check` / `rag-db-test`; wheel check (§3) |
| §10 smoke | `adapters/synthesis_smoke.py`, `cli.py synthesis-smoke` | step 05; exits 2 if any case fails |

**Red → green regressions (phase 3).** Each test below failed on the
pre-fix code and passes now (logs 02/03 of the dev run):

1. `test_typed_non_provider_adapter_error_keeps_code_and_settles`: before the
   fix, any non-provider `SynthesisError` from the port was relabelled
   `provider_unavailable`.
2. `test_provider_reporting_model_version_is_accepted_end_to_end`: before the
   fix, the application rejected a `model_version` that the adapter
   accepted.
3. `test_retrieval_counted_with_another_tokenizer_is_rejected_before_call`:
   before the fix, there was no check that T5 and T6 used the same counter.

The DB/E2E tests in `test_synthesis_db.py`/`test_synthesis_e2e.py` passed on
first run. They are **guard tests**, not red-green evidence.

## 6. Smoke output (summary; full JSON in the final run directory)

`synthesis-smoke.json` (safe summary: IDs, codes, counts, usage and
provenance; no article text):

| Case | Expected | Observed | Model calls | Mode | Citations |
| --- | --- | --- | --- | --- | --- |
| lexical_only | answered | answered | 1 | lexical_only | 2 |
| no_evidence_abstention | abstained_no_evidence | abstained_no_evidence (`no_matches`) | 0 | none | 0 |
| budget_failure (context_budget=8) | synthesis_budget_exceeded/context_too_small | same | 0 | (retrieval not run) | – |
| happy_cited_answer | answered | answered | 1 | hybrid | 5 |
| invalid_citation_blocked | synthesis_grounding_failed/unknown_span | same | 1 | hybrid | – |
| provider_timeout | synthesis_provider_failed/provider_timeout | same | 1 | hybrid | – |
| revoked_access (during model call) | synthesis_access_denied | same | 1 | hybrid | – |

- **Counted input.** Locally counted input tokens were 1,908 (lexical) and
  2,411 (happy), in `fixture-regex-tokens/v1` units.
- **Usage status.** The fake reports usage `missing`, so the full 1,500 output
  reservation is charged (a fixture artifact).
- **Timeout case.** `provider_timeout` is a scripted
  `SynthesisProviderError(provider_timeout)`, standing in for the adapter's
  transport-timeout mapping. The real HTTP-adapter timeout path is covered by
  `test_synthesis_integration.py::test_transport_timeout_settles_full_reservation_once`.

`synthesis-smoke-evidence.json` holds the fixture excerpts for reviewers:
the rendered answer, the claims, and citations with server-owned titles and
`https://fixture.example/rag/NN` URLs. Neither file contains credentials,
storage URIs or raw provider bodies.

## 7. Decisions and open items

Phase 3 decisions are in interfaces.md §7 (A-1…A-3 and B-3…B-5 accepted, B-4
and L-1/L-2 fixed). These remain OPEN:

- **Live activation** is unreachable by design. It needs an approved live
  pricing block (a future `synthesis-policy.v2`), a trusted issuer registry,
  corpus binding (B-2) and a real tokenizer mapping.
- **Late usage** reported after cancel or timeout is not recorded. The full
  reservation is charged instead, which is conservative.
- **The limiter is process-local.** There is no distributed enforcement of the
  30-request limit.
- **The current/auth guarantee ends at the T6 return boundary.** Policy epoch
  and response emission belong to T7, T12 and T13.

## 8. Limits not measured (do not relabel)

- Real model tokenizer, provider usage drift, provider cost and rates: **not
  measured**. Fixture regex counts and fixture pricing are mechanics only.
- Latency: the fake run latencies are recorded in the metrics only. The 8 s p95
  target is unproven, and there is no SLO claim.
- Semantic support and claim quality: **not evaluated**. T6 blocks mechanical
  linkage errors only (IDs, offsets, hashes, fingerprints, sent spans). The
  fake synthesizer copies span text, so its "answers" say nothing about
  quality. Semantic entailment belongs to T9.
- Vendor compatibility: only the frozen OpenAI-compatible request/response
  shape is tested against a mock transport. No claim is made that a real
  vendor supports it.
