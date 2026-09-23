---
slug: eventlens-ai-rag-mvp
status: plan-complete
intent: clear
review_required: false
pending-action: hand off .omo/plans/eventlens-ai-rag-mvp.md to a separate `$start-work` session or run optional high-accuracy review
approach: Contract-first grounded RAG Q&A: shared ArticleDocument/Briefing DTOs, labeled corpus, hybrid retrieval, structured synthesis with claim-bound evidence spans, fail-closed API, then analyst UI.
---

# Draft: eventlens-ai-rag-mvp

## Components (topology ledger)
<!-- Lock the SHAPE before depth. One row per top-level component that can succeed or fail independently. -->
<!-- id | outcome (one line) | status: active|deferred | evidence path -->

| ID | Outcome | Status | Evidence |
|---|---|---|---|
| C1 | Shared RAG contracts validate corpus, claims, citations, spans, uncertainty | active | `packages/contracts-python/src/eventlens_contracts/base.py`, `article.py`, spec T1 |
| C2 | Labeled corpus/eval set measures retrieval and grounding | active | `fixtures/article_bundle.v1.json`, spec Phase 0/T2 |
| C3 | Hybrid retrieval returns bounded, deduplicated, publication-time-filtered evidence | active | No current retrieval implementation; worker ports pattern at `apps/ai-worker/src/eventlens_worker/ports.py` |
| C4 | Structured model synthesis validates access, citations, errors, and budgets | active | No current model adapter; spec failure matrix and T4 |
| C5 | API and web deliver analyst query-to-citation workflow | active | `apps/api/src/eventlens_api/application.py`, `http.py`, `apps/web/src/App.tsx` |
| C6 | Event/living-event automation, crawling, production infra remain deferred | deferred | `README.md:49-52`, spec NOT In Scope |

## Open assumptions (announced defaults)
<!-- Record any default you adopt instead of asking, so the user can veto it at the gate. -->
<!-- assumption | adopted default | rationale | reversible? -->

| Assumption | Default | Rationale | Reversible? |
|---|---|---|---|
| Corpus input | Existing normalized `ArticleBundle` only | Removes crawler/source integration from first delivery | yes |
| Time filter | `source_published_at` | Explicit, available, testable; never infer event time | yes |
| Retrieval | Hybrid lexical + semantic, stage metrics independent | User approved; avoids black-box retrieval | yes |
| Runtime safety | Fail closed; typed errors; no raw model fallback | Protects grounded-answer guarantee | no for MVP contract |
| Access | Internal-only, allowlisted fields before provider egress | MVP scope; limits data leakage | yes |
| Budgets | p95 8s; 20 articles; 12k input; 1.5k output; 30 concurrent | Approved balanced defaults | yes |
| Retrieval backend | PostgreSQL + pgvector; PostgreSQL full-text plus cosine vector search | Existing local DB placeholder; one persistence surface | yes |
| Model boundary | OpenAI-compatible HTTP adapter behind protocol; deterministic fake adapter in CI | Provider choice isolated; CI stays no-network | yes |
| Chunking | 1,200 Unicode code points with 200-code-point overlap; offsets against normalized stored text | Stable citation offsets without byte/Unicode ambiguity | yes |
| Index lifecycle | Versioned local corpus; explicit manual rebuild; correction/deletion requires rebuild | Reproducible MVP, no hidden freshness worker | yes |
| Event usage | ArticleDocument only; no event grouping/create/merge/write path | Keeps event semantics out of first RAG value test | yes |
| Test strategy | TDD with pytest/Bun; deterministic CI path; mocked provider transport tests | Existing repo conventions and no external secrets | yes |
| Metadata enrichment | Full persist-first asynchronous phase before metadata/vector indexing | Model failure must not block canonical storage or lexical search | yes |
| Enrichment taxonomy | Controlled topics/content type/event relevance + canonical entities/locations + suggested tags | Stable filters plus discovery without taxonomy drift | yes |
| Enrichment identity | `(article_id, content_hash, enrichment_version)`; model/prompt versions recorded | Idempotent retries and explicit stale/supersede behavior | no for MVP contract |
| Dirty worktree | Preserve all existing WIP; no overwrite or cleanup | Current worker WIP is unrelated/possibly user-owned | no |

## Findings (cited - path:lines)

- RAG flow does not exist in current code; existing worker is deterministic demo: `README.md:23-34`, `apps/ai-worker/src/eventlens_worker/fake_extractor.py:13-51`.
- Contracts are strict/frozen/extra-forbid: `packages/contracts-python/src/eventlens_contracts/base.py:41-46`.
- Article already has content, URL, publication time, hash, source and provenance: `packages/contracts-python/src/eventlens_contracts/article.py:61-85`.
- API only has framework-free health use case and `/health`: `apps/api/src/eventlens_api/application.py:7-16`, `apps/api/src/eventlens_api/http.py:10-25`.
- Web has no API request or briefing flow: `apps/web/src/App.tsx:3-81`.
- Canonical checks are `make check`, Python pytest, Bun tests/build: `Makefile:7-23`, `pyproject.toml:54-62`, `apps/web/package.json:7-11`.
- Worktree contains modified and untracked AI-worker WIP; plan must not overwrite it: current `git status` evidence collected during exploration.

## Decisions (with rationale)

- Add shared RAG DTOs before API: executor needs one contract for Python and frontend boundaries.
- Use dedicated `ArticleDocument` mapped from `ArticleBundle`: prevents ingestion contract becoming accidental search storage.
- Bind `Claim` to `Citation` and immutable `EvidenceSpan`: source-level citation alone cannot prove support.
- Implement hybrid retrieval from MVP, but measure lexical, semantic, and combined stages independently.
- Query relative time uses `source_published_at`; response exposes time basis.
- Model output is structured; parse/provider/timeout/citation failures return typed errors and no raw fallback.
- Redact to allowlisted fields before external provider; article text is data, never instructions.
- Add evaluation fixtures before provider-specific optimization.
- Use PostgreSQL + pgvector for hybrid retrieval; provision extension, FTS/vector indexes, migration, healthcheck, and deterministic rebuild command.
- Use OpenAI-compatible provider adapter only behind a model port; CI uses fake model and mocked HTTP transport.
- Resolve implementation inputs before provider integration: local versioned corpus, manual rebuild, internal-only allowlist, URL plus immutable span citation.
- Persist ArticleDocument and lexical index before enrichment; enrichment state is PENDING/PROCESSING/READY/FAILED/STALE and never mutates `article.v1`.
- Retry transport/timeout failures only; schema/evidence failures become FAILED; content/model/prompt/enrichment version drift creates a new PENDING version and marks prior output STALE.

## Scope IN

- Shared contracts and JSON/schema policy for ArticleDocument, EvidenceSpan, BriefingQuery, BriefingAnswer, Claim, Citation, Uncertainty.
- 30-100 normalized article corpus and 10 labeled analyst questions.
- Hybrid retrieval, metadata filters, dedup, deterministic ties, index metadata and freshness.
- Structured synthesis adapter, claim/span validator, redaction, fail-closed errors, budgets and telemetry.
- FastAPI query use case/endpoint and React analyst query/result/citation states.
- Unit, integration, E2E and quality-eval verification.

## Scope OUT (Must NOT have)

- Do not modify or delete current dirty worker WIP, `.omo` artifacts, or unrelated docs.
- Do not route RAG through `FakeEventExtractor` or `InMemoryEventWriter`.
- No crawler/RSS/source acquisition, full RBAC, multi-tenancy, living-event automation, silent event merge, Kafka/MCP/queue, fine-tuning, multi-agent orchestration, or production deployment.
- No direct LLM writes to canonical storage.

## Open questions

- Collect implementation inputs: approved internal corpus/access policy, analyst baseline task/time, citation URL/internal viewer choice, and final field visibility list.
- Provider, chunking, index lifecycle, and test strategy are resolved defaults above; only corpus/access facts remain implementation inputs.

## Approval gate
status: approved-for-plan-writing
approach: Contract-first grounded RAG Q&A with five active components (contracts, evaluation corpus, retrieval, synthesis, API/UI) and event automation explicitly deferred.
next-action: Plan exists at `.omo/plans/eventlens-ai-rag-mvp.md`; hand off to `$start-work` or run optional dual high-accuracy review first.
approval-needed: SẾP approved plan creation; this does not authorize product-code implementation.
<!-- When exploration is exhausted and unknowns are answered, set status: awaiting-approval. -->
<!-- That durable record is the loop guard: on a later turn read it and resume at the gate instead of re-running exploration. -->
