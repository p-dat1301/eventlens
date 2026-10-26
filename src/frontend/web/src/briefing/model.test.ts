import { describe, expect, test } from "bun:test"

import {
  answeredResponse,
  CLAIM_CLOSE,
  CLAIM_REOPEN,
  noEvidenceResponse,
  partialCoverageResponse,
  sourceResponse,
} from "../test/fixtures"
import { BriefingResponseSchema, BriefingSourceSchema } from "./contract"
import {
  assessCoverage,
  type BriefingState,
  briefingReducer,
  buildBriefingRequest,
  citationsForClaim,
  describeFailure,
  initialBriefingState,
  segmentSource,
} from "./model"

const request = () => {
  const validation = buildBriefingRequest("East Pier")
  if (!validation.ok) {
    throw new Error("fixture query must be valid")
  }
  return validation.request
}

describe("buildBriefingRequest", () => {
  test("rejects blank and oversized questions without building a request", () => {
    expect(buildBriefingRequest("   \n")).toEqual({ ok: false, issue: "empty" })
    expect(buildBriefingRequest("x".repeat(2_001))).toEqual({ ok: false, issue: "too_long" })
  })

  test("trims the question", () => {
    const validation = buildBriefingRequest("  East Pier  ")
    expect(validation.ok && validation.request.query).toBe("East Pier")
  })
})

describe("briefingReducer", () => {
  test("moves loading -> failed -> loading(attempt 2) -> answered", () => {
    const response = BriefingResponseSchema.parse(answeredResponse())
    let state: BriefingState = briefingReducer(initialBriefingState, {
      type: "submitted",
      request: request(),
      requestId: 1,
    })
    expect(state).toMatchObject({ status: "loading", attempt: 1 })

    state = briefingReducer(state, { type: "failed", requestId: 1, failure: { kind: "network" } })
    expect(state).toMatchObject({ status: "failed", attempt: 1 })

    state = briefingReducer(state, { type: "retried", requestId: 2 })
    expect(state).toMatchObject({ status: "loading", attempt: 2, requestId: 2 })

    state = briefingReducer(state, { type: "resolved", requestId: 2, response })
    expect(state.status).toBe("answered")
  })

  test("ignores late results from superseded and cancelled requests", () => {
    const response = BriefingResponseSchema.parse(answeredResponse())
    const loading = briefingReducer(initialBriefingState, {
      type: "submitted",
      request: request(),
      requestId: 5,
    })

    expect(briefingReducer(loading, { type: "resolved", requestId: 4, response })).toBe(loading)
    expect(
      briefingReducer(loading, { type: "failed", requestId: 5, failure: { kind: "cancelled" } }),
    ).toBe(loading)
  })
})

describe("presentation helpers", () => {
  test("groups citations under their own claim", () => {
    const response = BriefingResponseSchema.parse(answeredResponse())

    expect(citationsForClaim(response, CLAIM_CLOSE).map((c) => c.source_url)).toEqual([
      "https://fixture.example/rag/01",
    ])
    expect(citationsForClaim(response, CLAIM_REOPEN).map((c) => c.source_url)).toEqual([
      "https://fixture.example/rag/02",
    ])
  })

  test("reports complete coverage only when every branch is healthy", () => {
    expect(assessCoverage(BriefingResponseSchema.parse(answeredResponse())).level).toBe("complete")
    expect(assessCoverage(BriefingResponseSchema.parse(noEvidenceResponse())).level).toBe(
      "complete",
    )

    const partial = assessCoverage(BriefingResponseSchema.parse(partialCoverageResponse()))
    expect(partial.level).toBe("degraded")
    expect(partial.notes).toHaveLength(2)
  })

  test("treats unrecognised statuses as degraded instead of healthy", () => {
    const response = answeredResponse()
    const parsed = BriefingResponseSchema.parse({
      ...response,
      audit: { ...response.audit, coverage_status: "sampled" },
    })

    expect(assessCoverage(parsed)).toEqual({
      level: "degraded",
      notes: ["Unrecognised coverage status “sampled”."],
    })
  })

  test("segments the cited span by code point offsets", () => {
    const base = sourceResponse()
    const source = BriefingSourceSchema.parse({
      ...base,
      normalized_content: "🌊 Pier shut. Open later.",
      span: { start_offset: 2, end_offset: 12, excerpt: "Pier shut." },
    })

    expect(segmentSource(source)).toEqual({
      before: "🌊 ",
      cited: "Pier shut.",
      after: " Open later.",
    })
  })

  test("marks retryable failures", () => {
    expect(describeFailure({ kind: "network" }).retryable).toBe(true)
    expect(describeFailure({ kind: "api", status: 503, code: "model_unavailable" }).retryable).toBe(
      true,
    )
    expect(describeFailure({ kind: "api", status: 410, code: "source_stale" }).retryable).toBe(
      false,
    )
    expect(describeFailure({ kind: "api", status: 400, code: "invalid_request" }).retryable).toBe(
      false,
    )
  })
})
