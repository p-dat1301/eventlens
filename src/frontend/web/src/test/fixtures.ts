// Shapes captured from the T7 fixture API (`POST /briefing`, East Pier question).

export const CLAIM_CLOSE = "49e0469f-9d8c-4e07-82f8-560f356e54cf"
export const CLAIM_REOPEN = "43eaab54-6c10-454c-abec-4ae2de3be17c"
export const REF_CLOSE = "Fep0TMwAb-pt4tgMAXnJUVOwexALHynh"
export const REF_REOPEN = "QqCOckaEzgHCtL8ugtfs6hCcYxONAOHq"

const CLOSE_TEXT =
  "Harbor Authority bulletin: East Pier gate closes at 08:00 UTC on 2026-03-01 for crane inspection. Cargo trucks use North Gate."
const REOPEN_TEXT =
  "Harbor Authority follow-up: East Pier gate reopened at 14:30 UTC on 2026-03-01 after crane inspection finished. North Gate detour ends."

export const audit = (overrides: Record<string, unknown> = {}) => ({
  retrieval_policy_id: "retrieval-policy.v3",
  fusion_policy_id: "rrf-k60.v1",
  index_version: "retrieval-policy.v3|rag-fixture-corpus.v1",
  corpus_generation: "rag-fixture-corpus.v1",
  chunker_version: "codepoint-1200-200.v1",
  embedding_model: "fixture-embedding",
  embedding_version: "fixture-embedding.v1",
  reranker_id: "identity-reranker",
  vector_branch_status: "executed",
  rerank_status: "applied",
  coverage_status: "complete",
  synthesis_policy_id: "synthesis-policy.v1",
  activation_policy_id: "provider-activation.fixture.v1",
  mode: "fixture",
  prompt_version: "synthesis-prompt.v1",
  draft_schema_version: "synthesis_draft.v1",
  template_version: "uncertainty-templates.v1",
  tokenizer_id: "fixture-regex-tokenizer",
  tokenizer_version: "fixture-regex.v1",
  model_calls: 1,
  input_tokens_charged: 0,
  output_tokens_charged: 0,
  usage_status: "missing",
  ...overrides,
})

const citation = (id: string, claimId: string, ref: string, n: string, text: string) => ({
  citation_id: id,
  claim_id: claimId,
  source_ref: ref,
  source_title:
    n === "01" ? "Duplicate harbor closure bulletin" : "Follow-up reopens East Pier gate",
  source_name: "Synthetic EventLens Desk",
  source_domain: "fixture.example",
  source_url: `https://fixture.example/rag/${n}`,
  source_published_at: n === "01" ? "2026-03-01T10:00:00Z" : "2026-03-02T10:00:00Z",
  span: { start_offset: 0, end_offset: Array.from(text).length, excerpt: text },
})

export const answeredResponse = (overrides: Record<string, unknown> = {}) => ({
  schema_version: "briefing_response.v1",
  outcome: "answered",
  abstention_reason: null,
  answer: `${CLOSE_TEXT}\n\n${REOPEN_TEXT}`,
  claims: [
    { claim_id: CLAIM_CLOSE, text: CLOSE_TEXT, temporal_claim: null },
    { claim_id: CLAIM_REOPEN, text: REOPEN_TEXT, temporal_claim: null },
  ],
  citations: [
    citation("680e2264-766d-4c1b-afda-1a6463b982b1", CLAIM_CLOSE, REF_CLOSE, "01", CLOSE_TEXT),
    citation("98dcab7e-cadc-4ac9-9a95-c9460cb8db30", CLAIM_REOPEN, REF_REOPEN, "02", REOPEN_TEXT),
  ],
  uncertainties: [],
  retrieved_at: "2026-10-25T08:35:59.396095Z",
  model_version: "fixture-chat-model.v1",
  retrieval_mode: "lexical_only",
  query_time_basis: "source_published_at",
  audit: audit(),
  ...overrides,
})

const NO_MATCH = "No authorized source in the requested scope matched this question."

export const noEvidenceResponse = () =>
  answeredResponse({
    outcome: "abstained_no_evidence",
    abstention_reason: "no_matches",
    answer: NO_MATCH,
    claims: [],
    citations: [],
    uncertainties: [
      {
        uncertainty_id: "0c3aa998-f050-47b8-a65c-8c42fe885044",
        category: "insufficient_evidence",
        explanation: NO_MATCH,
        claim_id: null,
      },
    ],
    retrieval_mode: "none",
    audit: audit({ vector_branch_status: "not_needed", rerank_status: "skipped", model_calls: 0 }),
  })

export const MODEL_DOUBT = "Sources disagree on whether the gate reopened the same day."

export const modelAbstainedResponse = () =>
  answeredResponse({
    outcome: "abstained_by_model",
    abstention_reason: "insufficient_evidence",
    answer: "The model could not reconcile the available sources.",
    claims: [],
    citations: [],
    uncertainties: [
      {
        uncertainty_id: "5a0d4f0e-3a5c-4b8f-9d61-0d1f7a2b6c11",
        category: "source_conflict",
        explanation: MODEL_DOUBT,
        claim_id: null,
      },
      {
        uncertainty_id: "6b1e5a1f-4b6d-4c90-8e72-1e2a8b3c7d22",
        category: "insufficient_evidence",
        explanation: "The retrieved sources do not settle the question.",
        claim_id: null,
      },
    ],
  })

export const partialCoverageResponse = () =>
  answeredResponse({
    retrieval_mode: "lexical_only",
    audit: audit({ coverage_status: "partial", vector_branch_status: "embedding_failed" }),
  })

export const sourceResponse = () => ({
  schema_version: "briefing_source.v1",
  title: "Duplicate harbor closure bulletin",
  source_name: "Synthetic EventLens Desk",
  source_domain: "fixture.example",
  source_url: "https://fixture.example/rag/01",
  source_published_at: "2026-03-01T10:00:00Z",
  normalized_content: `${CLOSE_TEXT} Later paragraph without citation.`,
  span: { start_offset: 0, end_offset: Array.from(CLOSE_TEXT).length, excerpt: CLOSE_TEXT },
})

export const errorBody = (code: string) => ({
  schema_version: "briefing_error.v1",
  error: { code, reason: null },
})

export const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  })
