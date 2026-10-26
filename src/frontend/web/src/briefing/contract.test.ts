import { describe, expect, test } from "bun:test"

import {
  answeredResponse,
  CLAIM_CLOSE,
  CLAIM_REOPEN,
  errorBody,
  modelAbstainedResponse,
  noEvidenceResponse,
  sourceResponse,
} from "../test/fixtures"
import { BriefingErrorSchema, BriefingResponseSchema, BriefingSourceSchema } from "./contract"

describe("BriefingResponseSchema", () => {
  test("parses a valid briefing_response.v1", () => {
    const parsed = BriefingResponseSchema.parse(answeredResponse())

    expect(parsed.outcome).toBe("answered")
    expect(parsed.claims.map((claim) => claim.claim_id)).toEqual([CLAIM_CLOSE, CLAIM_REOPEN])
    expect(parsed.citations[1]?.claim_id).toBe(CLAIM_REOPEN)
    expect(parsed.query_time_basis).toBe("source_published_at")
  })

  test("rejects an unknown top-level field", () => {
    const result = BriefingResponseSchema.safeParse({ ...answeredResponse(), prompt: "leak" })

    expect(result.success).toBe(false)
  })

  test("rejects an unknown nested citation field", () => {
    const response = answeredResponse()
    const [first, ...rest] = response.citations
    const tampered = { ...response, citations: [{ ...first, storage_uri: "s3://x" }, ...rest] }

    expect(BriefingResponseSchema.safeParse(tampered).success).toBe(false)
  })

  test("rejects an unknown audit field", () => {
    const response = answeredResponse()
    const tampered = { ...response, audit: { ...response.audit, raw_model_output: "{}" } }

    expect(BriefingResponseSchema.safeParse(tampered).success).toBe(false)
  })

  test("rejects a citation pointing at a claim that is not in the response", () => {
    const response = answeredResponse()
    const tampered = { ...response, claims: response.claims.slice(0, 1) }

    expect(BriefingResponseSchema.safeParse(tampered).success).toBe(false)
  })

  test("rejects an answered outcome with no citations", () => {
    const tampered = { ...answeredResponse(), citations: [] }

    expect(BriefingResponseSchema.safeParse(tampered).success).toBe(false)
  })

  test("rejects a wrong schema version", () => {
    const tampered = { ...answeredResponse(), schema_version: "briefing_response.v2" }

    expect(BriefingResponseSchema.safeParse(tampered).success).toBe(false)
  })
})

const SPAN_A = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
const SPAN_B = "9b2f4e1a-1d3c-4f5e-8a7b-6c5d4e3f2a1b"

const knownTemporal = (overrides: Record<string, unknown> = {}) => ({
  value_state: "known",
  start_at: "2026-03-01T08:00:00Z",
  end_at: "2026-03-01T14:30:00Z",
  precision: "minute",
  confidence: 0.9,
  evidence_span_ids: [SPAN_A],
  relative_expression: null,
  reference_at: null,
  timezone_basis: null,
  ...overrides,
})

const withTemporal = (temporal: Record<string, unknown>) => {
  const response = answeredResponse()
  const [first, ...rest] = response.claims
  return { ...response, claims: [{ ...first, temporal_claim: temporal }, ...rest] }
}

const withFirstSpan = (span: Record<string, unknown>) => {
  const response = answeredResponse()
  const [first, ...rest] = response.citations
  return { ...response, citations: [{ ...first, span }, ...rest] }
}

const accepts = (input: unknown) => BriefingResponseSchema.safeParse(input).success

describe("grounding invariants", () => {
  test("rejects an answered response with a claim that has no citation", () => {
    const response = answeredResponse()

    expect(accepts({ ...response, citations: response.citations.slice(0, 1) })).toBe(false)
  })

  test("rejects an answered response with no claims", () => {
    const response = answeredResponse()

    expect(accepts({ ...response, claims: [], citations: [] })).toBe(false)
  })

  test("rejects duplicate claim and citation ids", () => {
    const response = answeredResponse()
    const [firstClaim] = response.claims
    const [firstCitation] = response.citations

    expect(accepts({ ...response, claims: [...response.claims, firstClaim] })).toBe(false)
    expect(accepts({ ...response, citations: [...response.citations, firstCitation] })).toBe(false)
  })

  test("rejects an uncertainty bound to an unknown claim or with an unknown category", () => {
    const uncertainty = {
      uncertainty_id: "1f2e3d4c-5b6a-4987-a654-3210fedcba98",
      category: "ambiguity",
      explanation: "Wording is ambiguous.",
      claim_id: "00000000-0000-4000-8000-000000000000",
    }

    expect(accepts({ ...answeredResponse(), uncertainties: [uncertainty] })).toBe(false)
    expect(
      accepts({
        ...answeredResponse(),
        uncertainties: [{ ...uncertainty, claim_id: null, category: "vibes" }],
      }),
    ).toBe(false)
  })

  test("rejects an abstention without an insufficient_evidence uncertainty", () => {
    expect(accepts({ ...noEvidenceResponse(), uncertainties: [] })).toBe(false)
  })

  test("rejects an empty span (start_offset === end_offset)", () => {
    expect(accepts(withFirstSpan({ start_offset: 4, end_offset: 4, excerpt: "" }))).toBe(false)
    expect(accepts(withFirstSpan({ start_offset: 4, end_offset: 4, excerpt: "x" }))).toBe(false)
  })

  test("rejects a span whose excerpt length disagrees with its offsets", () => {
    expect(accepts(withFirstSpan({ start_offset: 0, end_offset: 10, excerpt: "short" }))).toBe(
      false,
    )
  })

  test("accepts valid known and relative temporal claims", () => {
    expect(accepts(withTemporal(knownTemporal()))).toBe(true)
    expect(
      accepts(
        withTemporal(
          knownTemporal({
            relative_expression: "this morning",
            reference_at: "2026-03-01T10:00:00Z",
            timezone_basis: "UTC",
          }),
        ),
      ),
    ).toBe(true)
    expect(
      accepts(
        withTemporal(
          knownTemporal({
            value_state: "unknown",
            start_at: null,
            end_at: null,
            precision: "unknown",
          }),
        ),
      ),
    ).toBe(true)
  })

  test.each([
    ["missing evidence_span_ids", { evidence_span_ids: [] }],
    ["duplicate evidence_span_ids", { evidence_span_ids: [SPAN_A, SPAN_A] }],
    ["known without start_at", { start_at: null, end_at: null }],
    ["unknown with a timestamp", { value_state: "unknown", end_at: null }],
    ["unknown with only end_at", { value_state: "unknown", start_at: null }],
    ["end_at before start_at", { end_at: "2026-02-28T00:00:00Z" }],
    [
      "relative expression without reference_at",
      { relative_expression: "yesterday", timezone_basis: "UTC" },
    ],
    [
      "relative expression without timezone_basis",
      { relative_expression: "yesterday", reference_at: "2026-03-02T00:00:00Z" },
    ],
    ["clock basis without relative expression", { reference_at: "2026-03-02T00:00:00Z" }],
    ["timezone basis without relative expression", { timezone_basis: "UTC" }],
    [
      "blank relative expression",
      { relative_expression: " ", reference_at: "2026-03-02T00:00:00Z", timezone_basis: "UTC" },
    ],
    ["unknown span id field", { evidence_span_ids: [SPAN_B], span_fingerprint: "sha256:x" }],
  ])("rejects a temporal claim with %s", (_, overrides) => {
    expect(accepts(withTemporal(knownTemporal(overrides)))).toBe(false)
  })
})

describe("fixture outcomes", () => {
  test("no-evidence and model abstention fixtures satisfy the contract", () => {
    expect(accepts(noEvidenceResponse())).toBe(true)
    expect(accepts(modelAbstainedResponse())).toBe(true)
  })
})

describe("BriefingSourceSchema", () => {
  test("parses briefing_source.v1", () => {
    const source = BriefingSourceSchema.parse(sourceResponse())

    expect(source.title).toBe("Duplicate harbor closure bulletin")
    expect(source.span.start_offset).toBe(0)
  })

  test("rejects a span whose offsets do not select the excerpt", () => {
    const source = sourceResponse()

    expect(
      BriefingSourceSchema.safeParse({
        ...source,
        span: { start_offset: 1, end_offset: 11, excerpt: "Harbor Aut" },
      }).success,
    ).toBe(false)
  })

  test("rejects a span beyond the content and unknown fields", () => {
    const source = sourceResponse()

    expect(
      BriefingSourceSchema.safeParse({ ...source, span: { ...source.span, end_offset: 9_999 } })
        .success,
    ).toBe(false)
    expect(BriefingSourceSchema.safeParse({ ...source, content_hash: "abc" }).success).toBe(false)
  })
})

describe("BriefingErrorSchema", () => {
  test("parses a known error code", () => {
    expect(BriefingErrorSchema.parse(errorBody("source_stale")).error.code).toBe("source_stale")
  })

  test("rejects unknown codes and extra fields", () => {
    expect(BriefingErrorSchema.safeParse(errorBody("teapot")).success).toBe(false)
    expect(
      BriefingErrorSchema.safeParse({ ...errorBody("internal_error"), detail: "trace" }).success,
    ).toBe(false)
  })
})
