# Spec: EventLens AI Briefing MVP

Status: APPROVED
Audience: Internal analyst/research team
Scope: First user-facing AI capability

## Decision Summary

EventLens MVP will answer analyst questions over a curated, normalized article corpus.
It will return a concise synthesis with claim-level citations and explicit uncertainty.

Event extraction is not the primary user-facing feature. Event records remain a supporting
knowledge layer for grouping related articles and preserving context. Article publication
time must not be treated as event occurrence time.

## Problem

Analysts currently need to read and compare many articles to answer one research question.
The work is slow because sources overlap, update one another, use different terminology,
and may disagree. A normal keyword search returns documents; it does not explain what is
known, what changed, or which source supports each claim.

EventLens should reduce reading and synthesis time without hiding uncertainty or inventing
facts.

## User And First Workflow

Primary user: internal analyst/researcher.

Initial workflow:

1. Analyst asks a question about one or more topics.
2. System retrieves relevant normalized articles.
3. System removes weak or duplicate context and assembles evidence.
4. AI writes an answer grounded only in retrieved evidence.
5. Analyst inspects citations and uncertainty before reusing the answer.

Example questions:

- What changed around company X in the last 24 hours?
- What do available sources agree on?
- Which claims are still unconfirmed?
- Summarize this topic and give me the supporting sources.

## MVP Output Contract

Every successful answer must contain:

- `answer`: readable synthesis.
- `claims`: material factual statements extracted from the answer.
- `citations`: source article ID, title, URL or source reference, and supporting span.
- `uncertainties`: missing evidence, ambiguity, or source conflict.
- `retrieved_at`: retrieval timestamp.
- `model_version`: synthesis model identifier.

`claims` use stable claim IDs. Each citation must reference a claim ID, article ID, and
stable evidence-span ID. An evidence span stores source content identity plus start/end
offsets or an equivalent immutable span reference. The validator rejects claims with a
missing, unknown, or non-matching span; article-level citation without claim binding is
not sufficient.

No factual claim may appear without at least one citation. If evidence is insufficient,
the system must say so instead of completing the answer from model knowledge.

## Time Semantics

Keep these timestamps separate:

- `source_published_at`: when publisher released an article.
- `ingested_at`: when EventLens received it.
- `processed_at`: when EventLens processed it.
- `event_occurred_at`: when the described event happened, only when supported by text.
- `first_observed_at`: when EventLens first observed evidence of the event.

`source_published_at` is provenance and freshness metadata. It is not a fallback for
`event_occurred_at`. MVP relative query filters such as "last 24 hours" use
`source_published_at` and the API response exposes this time basis explicitly.

The AI may extract temporal claims such as an exact time, date, range, relative expression,
or unknown value. Each temporal claim must retain evidence, precision, and confidence.

## Retrieval Scope

MVP input is the existing normalized `ArticleBundle` corpus. No crawler, RSS connector,
or new source integration is required.

Retrieval should support:

- lexical search;
- semantic search alongside lexical search;
- metadata filters for source, topic, entity, and time range;
- freshness weighting;
- duplicate or near-duplicate suppression;
- deterministic top-k ordering for reproducible tests, including explicit tie-breaking and
  index-version metadata.

Use a dedicated `ArticleDocument` boundary mapped from `ArticleBundle`. It owns searchable
text, immutable source provenance, evidence spans, timestamps, visibility metadata, and a
content fingerprint. Retrieval and indexing depend on `ArticleDocument`, not directly on
the ingestion contract. Use repository, index, embedding, and model ports so providers can
change without changing application code.

## AI Pipeline

```text
ArticleBundle
  -> article repository
  -> chunk/index adapter
  -> hybrid retriever (lexical + semantic)
  -> optional reranker within budget
  -> evidence context
  -> structured LLM synthesis
  -> citation validator
  -> BriefingAnswer
  -> API/UI
```

The model proposes text and claims. Deterministic validation controls output acceptance.
The model must not write directly to PostgreSQL or mutate canonical event data. Runtime
failures are fail-closed: bounded retries are optional only for transport failures, but
timeout, provider failure, malformed output, unsupported claim, or invalid citation yields
a typed error and no fabricated answer. The API returns stable error codes for these paths.

Article text is data, never instructions. The model boundary must preserve this rule and
include adversarial prompt-injection fixtures in evaluation.

## Event Layer Role

The current `event.v1` contract remains useful but is not the first product surface.

For MVP, event data may support:

- grouping articles with the same story;
- retrieving context across multiple articles;
- building a basic timeline when explicit event times exist;
- avoiding duplicate evidence in an answer.

Do not require full living-event updates, event merge decisions, or automatic event creation
before RAG Q&A demonstrates analyst value.

## Reuse From Current Project

- `packages/contracts-python`: validate ArticleBundle and preserve boundary contracts.
- New shared contracts: `ArticleDocument`, `EvidenceSpan`, `BriefingQuery`, `BriefingAnswer`,
  `Claim`, `Citation`, and `Uncertainty`, with machine-linkage invariants.
- `contracts/*.schema.json`: language-neutral input artifacts.
- `fixtures/`: deterministic corpus fixtures and test cases.
- `apps/api`: FastAPI adapter and framework-free application boundary.
- `apps/ai-worker/src/eventlens_worker/ports.py`: adapter boundary pattern.
- `apps/ai-worker/src/eventlens_worker/models.py`: typed output and invariant validation.
- `ProcessingTrace`: pattern for auditable processing outcomes.
- `apps/web`: existing React/Vite presentation scaffold and editorial design system.

The current `FakeEventExtractor` and `InMemoryEventWriter` are demonstration adapters.
They should not define MVP answer semantics.

## Phased Delivery

### Phase 0: Corpus And Evaluation Set

- Create a small representative corpus from normalized ArticleBundle fixtures.
- Include duplicate articles, follow-up articles, conflicting claims, delayed publication,
  relative dates, and articles with no actionable event.
- Define analyst questions and expected supporting article IDs.
- Include prompt-injection text, access-restricted fields, publication delays, and timezone
  boundary cases.
- Label expected claims, evidence spans, citation links, abstentions, and conflicts.

Exit: evaluation set is versioned and can score retrieval and citation coverage.

### Phase 1: Retrieval

- Implement article repository port.
- Implement lexical + semantic hybrid retrieval from the start, but score each stage
  independently against the lexical baseline.
- Add metadata and time filters.
- Add deduplication and deterministic ranking.
- Record retrieval basis, index version, tie-break rule, and query time basis.

Exit: top-k retrieval returns relevant evidence for the evaluation questions.

### Phase 2: Grounded Synthesis

- Implement model adapter behind a port.
- Require structured answer output.
- Require stable claim IDs and evidence-span IDs in model output.
- Reject answers with unsupported claims or missing citations.
- Return explicit insufficient-evidence responses.
- Use fail-closed typed errors for timeout, provider failure, malformed output, and
  citation validation failure.
- Enforce allowlisted fields before sending context to an external model provider.

Exit: analyst can ask a question and inspect grounded answer plus citations.

### Phase 3: Analyst UI And API

- Add query endpoint.
- Add answer, claim, citation, and uncertainty models.
- Add question input and result view in web app.
- Make source opening and citation inspection clear.
- Add loading, empty, no-evidence, model-error, and partial-result states.
- Expose query time basis (`source_published_at` for MVP) and stable error codes.

Exit: one analyst can complete the workflow without CLI access.

### Phase 4: What Changed

- Store answer or evidence snapshots by query/topic and time.
- Compare snapshots.
- Explain newly observed claims, changed values, and new sources.

Exit: analyst can ask what changed since a prior observation.

### Phase 5: Event Grouping

- Retrieve candidate existing events using time/entity/type signals.
- Group articles with evidence and ambiguity markers.
- Route uncertain matches to review.

Exit: grouped context improves answer quality without silently merging distinct events.

## Non-Goals

- Crawling or source acquisition.
- Real-time world-event detection.
- Treating article publication time as event time.
- Full event lifecycle automation.
- Automatic truth arbitration between conflicting sources.
- Direct LLM writes to canonical storage.
- Kafka, MCP, queues, retries, or production-scale deployment.
- Fine-tuning or multi-agent orchestration.
- Full RBAC and multi-tenant policy; MVP is internal-only with allowlisted fields.
- Provider-specific optimization beyond explicit budgets and telemetry.

## Success Criteria

MVP is successful when, on the evaluation set:

- retrieval returns at least one relevant source for each supported question;
- every material answer claim has a correct citation;
- unsupported claims are rejected or marked uncertain;
- duplicate articles do not dominate retrieved context;
- time filters exclude clearly out-of-range articles;
- relative or ambiguous dates remain marked with appropriate uncertainty;
- analyst can inspect source evidence without leaving the answer flow;
- analyst completes a briefing task faster than the existing manual workflow.

Initial quality targets should be measured on a labeled set before being treated as gates.
Do not claim real-time performance until source latency and pipeline latency are measured
separately. MVP also defines explicit budgets before implementation: p95 query latency,
maximum retrieved documents/tokens, maximum model context, and cost per query. Values are
recorded with the evaluation run and tightened after the baseline is known.

## Risks And Controls

| Risk | Control |
|---|---|
| Hallucinated synthesis | Claim-level citation validator and insufficient-evidence response |
| Retrieval misses relevant article | Hybrid retrieval, metadata filters, labeled retrieval evaluation |
| Duplicate context | Article fingerprinting and near-duplicate suppression |
| Source conflict hidden | Explicit uncertainty and conflict section |
| Publication time mistaken for event time | Separate timestamp fields and temporal evidence |
| Model/provider lock-in | Model and index ports |
| Untrusted article content influencing instructions | Treat article text as data, not executable instructions |
| Analyst over-trusts fluent answer | Visible citations, confidence/uncertainty, source inspection |
| Citation points to wrong text | Immutable content fingerprint plus stable evidence-span IDs and validator |
| Provider leaks restricted content | Allowlist/redaction before model boundary; internal-only policy |
| Query is too slow or expensive | p95, token, context, and cost budgets with telemetry |
| Index is stale or wrong version | Index version/freshness metadata and explicit rebuild/invalidation policy |
| Prompt injection in article | Treat article as data; adversarial fixtures and output validation |

## Implementation Inputs

Các mục dưới đây cần thu thập trước khi triển khai, nhưng không còn là quyết định kiến
trúc chưa chốt:

- Which internal article corpus and access policy will supply the first evaluation set?
- What analyst task and baseline time should be used for the speed comparison?
- Which model provider is approved for internal article content?
- Are URLs stable enough for citations, or does the system need an internal article viewer?
- Which fields are allowed to be shown to each analyst role?
- What are acceptable p95 latency, context-token, and cost-per-query budgets?
- What is the index refresh, correction, deletion, and visibility-lag policy?

## Failure Mode Matrix

| Path | Failure | Test | Handling | User sees |
|---|---|---|---|---|
| Retrieval | index unavailable/stale | adapter and freshness tests | typed retrieval error or stale marker | clear retry/stale-data state |
| Model call | timeout/provider error | transport tests | bounded retry then fail closed | stable model-unavailable error |
| Model parse | malformed structured output | contract tests | reject answer | invalid-answer error, no raw fallback |
| Citation | unknown claim/span or offset mismatch | citation validator tests | reject answer | evidence-validation error |
| Access | restricted field enters context | redaction tests | remove/reject field | answer with allowed evidence only |
| Query | empty/huge/ambiguous request | API boundary tests | stable validation/budget error | actionable input message |
| Content | prompt injection in article | adversarial eval | preserve data-only instruction | grounded answer or abstention |

## Explicit Budgets

Implementation must define and instrument these initial MVP budgets:

- p95 end-to-end query latency: 8 seconds;
- max retrieved articles: 20;
- max model input context: 12,000 tokens;
- max model output: 1,500 tokens;
- concurrent query limit: 30;
- cost per query and index freshness/visibility lag: measured and reported from first run,
  then converted into explicit gates after baseline data exists.

Budget values are an implementation decision recorded with the first evaluation run, not
an excuse to omit measurement.

## Test And Evaluation Coverage

```text
Query
  -> validate [empty, oversized, forbidden metadata]
  -> retrieve [lexical, semantic, hybrid, ties, stale index, zero hits]
  -> build context [dedup, allowlist, injection text, token budget]
  -> model [success, timeout, provider error, malformed output]
  -> validate answer [claim IDs, span IDs, offsets, unsupported claim]
  -> return [answer, abstention, stable error, citations]
```

Unit tests cover pure ranking, filtering, deduplication, span validation, redaction, and
DTO invariants. Integration tests cover repository/index/model adapter boundaries. An eval
suite covers Recall@k, citation precision/recall, claim support, abstention quality,
conflict handling, temporal extraction, prompt-injection resistance, latency, and cost.
The first user flow is an end-to-end test: analyst query -> API -> retrieval -> model ->
citation inspection.

## What Already Exists

- ArticleBundle validation and schema artifacts: reuse as ingestion boundary.
- Worker ports/models/trace invariants: reuse adapter and audit patterns, not event semantics.
- FastAPI health adapter: reuse HTTP/application split; add query use case rather than
  putting retrieval or model calls in the route.
- React/Vite foundation: reuse design tokens and accessibility constraints.
- Existing FakeEventExtractor/InMemoryEventWriter: retain only as local demonstration and
  regression fixtures; do not route RAG answers through them.

## Parallelization Strategy

| Step | Module | Depends on |
|---|---|---|
| Corpus/eval contracts | contracts-python, fixtures | - |
| Retrieval adapters | API application/search package | ArticleDocument contract |
| Model/citation adapters | AI worker/application package | BriefingAnswer contract |
| API endpoint | apps/api | retrieval + model contracts |
| Analyst UI | apps/web | API contract |

Launch corpus/contracts, retrieval, and model/citation work after agreeing shared DTOs;
then integrate API; then UI. Retrieval and model lanes must not duplicate contract types.

## NOT In Scope

- Crawling and source acquisition: corpus is supplied as normalized ArticleBundle.
- Full RBAC/multi-tenancy: internal-only allowlisted access is enough for MVP.
- Automatic truth arbitration: show conflict and provenance instead.
- Living-event lifecycle and silent event merges: validate RAG value first.
- Kafka/MCP/queue productionization: no user-facing need before MVP evidence.

## Next Action

Select 30-100 normalized articles and write 10 real analyst questions with expected source
articles. Do this before selecting an LLM or vector database. The result will show whether
retrieval and grounded synthesis solve a real workflow, rather than only producing a good
demo.

## Review Decisions

- Citation boundary: structured claim IDs and evidence-span IDs.
- Retrieval: hybrid lexical + semantic from MVP, with independent stage metrics.
- Runtime: fail closed with stable typed errors; no raw model fallback.
- Corpus boundary: dedicated ArticleDocument and repository.
- Relative time filters: `source_published_at` for MVP, explicit in response metadata.
- Contracts: add shared RAG DTOs before API implementation.
- Data policy: internal-only corpus with allowlisted fields before provider egress.
- Operations: explicit latency, token, context, cost, concurrency, and index freshness budgets.

## Implementation Tasks

- [ ] **T1 (P1)** — contracts — Add ArticleDocument, EvidenceSpan, BriefingQuery, BriefingAnswer, Claim, Citation, and Uncertainty models with linkage invariants.
- [ ] **T2 (P1)** — fixtures/eval — Create 30-100 article corpus and 10 labeled analyst questions with expected sources, claims, spans, abstentions, conflicts, time boundaries, access cases, and prompt-injection cases.
- [ ] **T3 (P1)** — retrieval — Build hybrid lexical/semantic adapters with deterministic ranking, publication-time filters, dedup, index metadata, and metrics.
- [ ] **T4 (P1)** — synthesis — Add model port, structured claim/span output, allowlist/redaction, citation validator, fail-closed typed errors, and provider telemetry.
- [ ] **T5 (P1)** — API — Add query use case and endpoint with stable DTOs, time-basis metadata, access boundary, errors, and budget enforcement.
- [ ] **T6 (P2)** — web — Add analyst query and grounded-answer UI with citations, uncertainty, source inspection, loading, empty, and error states.
- [ ] **T7 (P2)** — verification — Add unit/integration/E2E tests and evaluation reports for retrieval, grounding, abstention, security, latency, and cost.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| Eng Review | `/plan-eng-review` | Architecture, tests, performance | 1 | CLEAR | 9 decisions accepted; no critical gaps |
| Outside Voice | team reviewers | Independent architecture and test challenge | 2 | COMPLETE | Citation, time basis, failure modes, access, budgets identified |

**VERDICT:** Eng review cleared; spec ready to implement.

NO UNRESOLVED DECISIONS
