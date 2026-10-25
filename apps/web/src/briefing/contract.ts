import { z } from "zod"

// Mirrors apps/api/src/eventlens_api/briefing_http.py. Every boundary object is
// strict: an unknown field means the contract drifted, and the UI refuses it.

export const MAX_QUERY_CHARS = 2_000

const Uuid = z.uuid()
const Timestamp = z.iso.datetime({ offset: true })
const Text = z.string()
const Status = z.string().min(1)
const NonEmptyText = z.string().regex(/\S/)

export const BriefingRequestSchema = z
  .strictObject({
    schema_version: z.literal("briefing_request.v1"),
    query: z.string().min(1).max(MAX_QUERY_CHARS).regex(/\S/),
    published_after: Timestamp.nullable(),
    published_before: Timestamp.nullable(),
    publication_time_basis: z.literal("source_published_at"),
    source_domains: z.array(z.string()).max(20).readonly(),
    topic_filters: z.array(z.string()).max(20).readonly(),
    entity_ids: z.array(Uuid).max(20).readonly(),
    context_budget: z.int().min(1).max(12_000),
    result_limit: z.int().min(1).max(20),
  })
  .readonly()

const codePoints = (text: string): number => Array.from(text).length

const unique = (values: readonly string[]): boolean => new Set(values).size === values.length

// Mirrors eventlens_rag.domain.temporal.TemporalClaim.enforce_temporal_basis.
const TemporalClaimSchema = z
  .strictObject({
    value_state: z.enum(["known", "unknown"]),
    start_at: Timestamp.nullable(),
    end_at: Timestamp.nullable(),
    precision: z.enum(["minute", "hour", "day", "month", "year", "unknown"]),
    confidence: z.number().min(0).max(1),
    evidence_span_ids: z.array(Uuid).min(1).readonly(),
    relative_expression: NonEmptyText.nullable(),
    reference_at: Timestamp.nullable(),
    timezone_basis: z.literal("UTC").nullable(),
  })
  .readonly()
  .superRefine((claim, context) => {
    const issue = (message: string) => context.addIssue({ code: "custom", message })
    if (!unique(claim.evidence_span_ids)) {
      issue("temporal evidence span ids must be unique")
    }
    if (claim.value_state === "known" && claim.start_at === null) {
      issue("known temporal value needs start_at")
    }
    if (claim.value_state === "unknown" && (claim.start_at !== null || claim.end_at !== null)) {
      issue("unknown temporal value cannot carry timestamps")
    }
    if (
      claim.start_at !== null &&
      claim.end_at !== null &&
      Date.parse(claim.end_at) < Date.parse(claim.start_at)
    ) {
      issue("end_at must not precede start_at")
    }
    const hasRelativeBasis = claim.reference_at !== null && claim.timezone_basis === "UTC"
    if (claim.relative_expression !== null && !hasRelativeBasis) {
      issue("relative temporal values require reference_at and UTC timezone_basis")
    }
    if (
      claim.relative_expression === null &&
      (claim.reference_at !== null || claim.timezone_basis !== null)
    ) {
      issue("only relative temporal values carry a clock basis")
    }
  })

const ClaimSchema = z
  .strictObject({
    claim_id: Uuid,
    text: NonEmptyText,
    temporal_claim: TemporalClaimSchema.nullable(),
  })
  .readonly()

// Mirrors eventlens_contracts.evidence.EvidenceSpan: non-empty, half-open,
// code-point range whose excerpt length equals the range length.
const CitedSpanSchema = z
  .strictObject({
    start_offset: z.int().min(0),
    end_offset: z.int().min(1),
    excerpt: NonEmptyText,
  })
  .readonly()
  .refine((span) => span.end_offset > span.start_offset, "span must be non-empty and ordered")
  .refine(
    (span) => codePoints(span.excerpt) === span.end_offset - span.start_offset,
    "span excerpt length must match its offsets",
  )

const CitationSchema = z
  .strictObject({
    citation_id: Uuid,
    claim_id: Uuid,
    source_ref: z.string().min(1),
    source_title: NonEmptyText,
    source_name: Text,
    source_domain: Text,
    source_url: z.url({ protocol: /^https?$/ }),
    source_published_at: Timestamp.nullable(),
    span: CitedSpanSchema,
  })
  .readonly()

const UncertaintySchema = z
  .strictObject({
    uncertainty_id: Uuid,
    category: z.enum([
      "insufficient_evidence",
      "ambiguity",
      "source_conflict",
      "temporal_uncertainty",
    ]),
    explanation: NonEmptyText,
    claim_id: Uuid.nullable(),
  })
  .readonly()

// Audit is parsed in full so drift is caught, but only retrieval/coverage
// fields are rendered; prompt and tokenizer metadata stay internal.
const AuditSchema = z
  .strictObject({
    retrieval_policy_id: Status,
    fusion_policy_id: Status,
    index_version: Status,
    corpus_generation: Status,
    chunker_version: Status,
    embedding_model: Status,
    embedding_version: Status,
    reranker_id: Status.nullable(),
    vector_branch_status: Status,
    rerank_status: Status,
    coverage_status: Status,
    synthesis_policy_id: Status,
    activation_policy_id: Status,
    mode: z.enum(["fixture", "live"]),
    prompt_version: Status,
    draft_schema_version: Status,
    template_version: Status,
    tokenizer_id: Status,
    tokenizer_version: Status,
    model_calls: z.int().min(0),
    input_tokens_charged: z.int().min(0),
    output_tokens_charged: z.int().min(0),
    usage_status: Status,
  })
  .readonly()

export const BriefingResponseSchema = z
  .strictObject({
    schema_version: z.literal("briefing_response.v1"),
    outcome: z.enum(["answered", "abstained_no_evidence", "abstained_by_model"]),
    abstention_reason: Status.nullable(),
    answer: Text,
    claims: z.array(ClaimSchema).readonly(),
    citations: z.array(CitationSchema).readonly(),
    uncertainties: z.array(UncertaintySchema).readonly(),
    retrieved_at: Timestamp,
    model_version: Status,
    retrieval_mode: Status,
    query_time_basis: z.literal("source_published_at"),
    audit: AuditSchema,
  })
  .readonly()
  // Mirrors eventlens_rag.domain.answer.BriefingAnswer.enforce_grounding as far
  // as the public DTO allows (citation span ids are not exposed).
  .superRefine((response, context) => {
    const issue = (message: string) => context.addIssue({ code: "custom", message })
    const claimIds = response.claims.map((claim) => claim.claim_id)
    const known = new Set(claimIds)
    const cited = new Set(response.citations.map((citation) => citation.claim_id))
    if (!unique(claimIds)) {
      issue("claim ids must be unique")
    }
    if (!unique(response.citations.map((citation) => citation.citation_id))) {
      issue("citation ids must be unique")
    }
    if (!unique(response.uncertainties.map((item) => item.uncertainty_id))) {
      issue("uncertainty ids must be unique")
    }
    if (response.citations.some((citation) => !known.has(citation.claim_id))) {
      issue("citation references unknown claim")
    }
    if (claimIds.some((claimId) => !cited.has(claimId))) {
      issue("every claim must have at least one citation")
    }
    if (
      response.uncertainties.some((item) => item.claim_id !== null && !known.has(item.claim_id))
    ) {
      issue("uncertainty references unknown claim")
    }
    if (
      response.claims.length === 0 &&
      !response.uncertainties.some((item) => item.category === "insufficient_evidence")
    ) {
      issue("a briefing without claims needs an insufficient_evidence uncertainty")
    }
    if (response.outcome === "answered" && response.claims.length === 0) {
      issue("answered briefing without claims")
    }
  })

export const BriefingSourceSchema = z
  .strictObject({
    schema_version: z.literal("briefing_source.v1"),
    title: Text,
    source_name: Text,
    source_domain: Text,
    source_url: z.url({ protocol: /^https?$/ }),
    source_published_at: Timestamp.nullable(),
    normalized_content: Text,
    span: CitedSpanSchema,
  })
  .readonly()
  .refine(
    (source) =>
      Array.from(source.normalized_content)
        .slice(source.span.start_offset, source.span.end_offset)
        .join("") === source.span.excerpt,
    "span excerpt must match normalized_content offsets",
  )

export const BRIEFING_ERROR_CODES = [
  "invalid_request",
  "invalid_query",
  "unauthenticated",
  "access_denied",
  "source_not_found",
  "source_stale",
  "request_cancelled",
  "deadline_exceeded",
  "evidence_stale",
  "budget_exceeded",
  "capacity_exceeded",
  "invalid_grounded_answer",
  "retrieval_unavailable",
  "model_unavailable",
  "configuration_invalid",
  "internal_error",
] as const

export const BriefingErrorSchema = z
  .strictObject({
    schema_version: z.literal("briefing_error.v1"),
    error: z
      .strictObject({
        code: z.enum(BRIEFING_ERROR_CODES),
        reason: z.string().nullable().optional(),
      })
      .readonly(),
  })
  .readonly()

export type BriefingRequest = z.infer<typeof BriefingRequestSchema>
export type BriefingResponse = z.infer<typeof BriefingResponseSchema>
export type BriefingClaim = BriefingResponse["claims"][number]
export type BriefingCitation = BriefingResponse["citations"][number]
export type BriefingUncertainty = BriefingResponse["uncertainties"][number]
export type BriefingSource = z.infer<typeof BriefingSourceSchema>
export type BriefingErrorCode = (typeof BRIEFING_ERROR_CODES)[number]
