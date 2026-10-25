import type { ClientFailure } from "./client"
import {
  type BriefingCitation,
  type BriefingErrorCode,
  type BriefingRequest,
  BriefingRequestSchema,
  type BriefingResponse,
  type BriefingSource,
  type BriefingUncertainty,
  MAX_QUERY_CHARS,
} from "./contract"

export type QueryIssue = "empty" | "too_long"

export type QueryValidation =
  | { readonly ok: true; readonly request: BriefingRequest }
  | { readonly ok: false; readonly issue: QueryIssue }

export const buildBriefingRequest = (rawQuery: string): QueryValidation => {
  const query = rawQuery.trim()
  if (query.length === 0) {
    return { ok: false, issue: "empty" }
  }
  if (query.length > MAX_QUERY_CHARS) {
    return { ok: false, issue: "too_long" }
  }
  return {
    ok: true,
    request: BriefingRequestSchema.parse({
      schema_version: "briefing_request.v1",
      query,
      published_after: null,
      published_before: null,
      publication_time_basis: "source_published_at",
      source_domains: [],
      topic_filters: [],
      entity_ids: [],
      context_budget: 12_000,
      result_limit: 5,
    }),
  }
}

export const QUERY_ISSUE_MESSAGES: Readonly<Record<QueryIssue, string>> = {
  empty: "Enter a question before requesting a briefing.",
  too_long: `Keep the question under ${MAX_QUERY_CHARS.toLocaleString("en-US")} characters.`,
}

// --- Briefing request state ------------------------------------------------

export type RequestFailure = Exclude<ClientFailure, { readonly kind: "cancelled" }>

export type BriefingState =
  | { readonly status: "idle" }
  | { readonly status: "invalid"; readonly issue: QueryIssue }
  | {
      readonly status: "loading"
      readonly request: BriefingRequest
      readonly requestId: number
      readonly attempt: number
    }
  | {
      readonly status: "answered"
      readonly request: BriefingRequest
      readonly response: BriefingResponse
    }
  | {
      readonly status: "failed"
      readonly request: BriefingRequest
      readonly failure: RequestFailure
      readonly attempt: number
    }

export type BriefingAction =
  | { readonly type: "rejected"; readonly issue: QueryIssue }
  | { readonly type: "submitted"; readonly request: BriefingRequest; readonly requestId: number }
  | { readonly type: "retried"; readonly requestId: number }
  | { readonly type: "resolved"; readonly requestId: number; readonly response: BriefingResponse }
  | { readonly type: "failed"; readonly requestId: number; readonly failure: ClientFailure }

export const initialBriefingState: BriefingState = { status: "idle" }

export const briefingReducer = (state: BriefingState, action: BriefingAction): BriefingState => {
  switch (action.type) {
    case "rejected":
      return { status: "invalid", issue: action.issue }
    case "submitted":
      return {
        status: "loading",
        request: action.request,
        requestId: action.requestId,
        attempt: 1,
      }
    case "retried":
      return state.status === "failed"
        ? {
            status: "loading",
            request: state.request,
            requestId: action.requestId,
            attempt: state.attempt + 1,
          }
        : state
    case "resolved":
      return state.status === "loading" && state.requestId === action.requestId
        ? { status: "answered", request: state.request, response: action.response }
        : state
    case "failed":
      if (state.status !== "loading" || state.requestId !== action.requestId) {
        return state
      }
      return action.failure.kind === "cancelled"
        ? state
        : {
            status: "failed",
            request: state.request,
            failure: action.failure,
            attempt: state.attempt,
          }
  }
}

// --- Failure copy ------------------------------------------------------------

export type FailureCopy = {
  readonly title: string
  readonly message: string
  readonly retryable: boolean
}

export const describeFailure = (failure: RequestFailure): FailureCopy => {
  switch (failure.kind) {
    case "network":
      return {
        title: "Briefing service unreachable",
        message: "The request did not reach the briefing service. Check the connection and retry.",
        retryable: true,
      }
    case "malformed":
      return {
        title: "Unreadable response",
        message:
          "The service replied in a shape this page does not accept, so nothing is shown as an answer.",
        retryable: true,
      }
    case "api":
      return API_FAILURES[failure.code]
  }
}

const API_FAILURES: Readonly<Record<BriefingErrorCode, FailureCopy>> = {
  invalid_request: {
    title: "Request rejected",
    message: "The briefing service rejected the request format.",
    retryable: false,
  },
  invalid_query: {
    title: "Question rejected",
    message: "The briefing service could not use this question. Rephrase it and submit again.",
    retryable: false,
  },
  unauthenticated: {
    title: "Sign-in required",
    message: "The briefing service did not recognise this session.",
    retryable: false,
  },
  access_denied: {
    title: "Access denied",
    message: "This session is not allowed to read the requested sources.",
    retryable: false,
  },
  source_not_found: {
    title: "Source not found",
    message: "This source reference is unknown or no longer available to this session.",
    retryable: false,
  },
  source_stale: {
    title: "Source changed",
    message:
      "The source was revised after this briefing was produced. Request a new briefing to cite the current version.",
    retryable: false,
  },
  request_cancelled: {
    title: "Request cancelled",
    message: "The briefing request was cancelled before it finished.",
    retryable: true,
  },
  deadline_exceeded: {
    title: "Briefing timed out",
    message: "The briefing took longer than the service allows.",
    retryable: true,
  },
  evidence_stale: {
    title: "Evidence changed",
    message: "Sources changed while the briefing was being produced.",
    retryable: true,
  },
  budget_exceeded: {
    title: "Context budget exceeded",
    message: "The matching evidence does not fit the context budget. Narrow the question.",
    retryable: false,
  },
  capacity_exceeded: {
    title: "Service busy",
    message: "The briefing service is at capacity.",
    retryable: true,
  },
  invalid_grounded_answer: {
    title: "Answer failed grounding checks",
    message: "The model's draft could not be verified against its sources, so it was discarded.",
    retryable: true,
  },
  retrieval_unavailable: {
    title: "Retrieval unavailable",
    message: "The evidence store is unavailable.",
    retryable: true,
  },
  model_unavailable: {
    title: "Model unavailable",
    message: "The language model did not respond.",
    retryable: true,
  },
  configuration_invalid: {
    title: "Service misconfigured",
    message: "The briefing service configuration is invalid. Contact the operator.",
    retryable: false,
  },
  internal_error: {
    title: "Service error",
    message: "The briefing service failed unexpectedly.",
    retryable: true,
  },
}

// --- Presentation helpers ----------------------------------------------------

export const citationsForClaim = (
  response: BriefingResponse,
  claimId: string,
): readonly BriefingCitation[] =>
  response.citations.filter((citation) => citation.claim_id === claimId)

export const uncertaintiesForClaim = (
  response: BriefingResponse,
  claimId: string | null,
): readonly BriefingUncertainty[] =>
  response.uncertainties.filter((uncertainty) => uncertainty.claim_id === claimId)

export type CoverageAssessment = {
  readonly level: "complete" | "degraded"
  readonly notes: readonly string[]
}

const COVERAGE_NOTES: Readonly<Record<string, string | null>> = {
  complete: null,
  partial: "Metadata filters covered only part of the corpus; matching sources may be missing.",
  filter_unavailable: "Metadata filters were unavailable; the requested scope was not enforced.",
}

const VECTOR_NOTES: Readonly<Record<string, string | null>> = {
  executed: null,
  not_needed: null,
  disabled: "Semantic retrieval is disabled; only keyword matches were considered.",
  embedding_incompatible: "Semantic index is incompatible; only keyword matches were considered.",
  embedding_failed: "Semantic retrieval failed; only keyword matches were considered.",
}

const RERANK_NOTES: Readonly<Record<string, string | null>> = {
  applied: null,
  skipped: null,
  degraded: "Reranking degraded; evidence kept its fused order.",
}

const noteFor = (
  table: Readonly<Record<string, string | null>>,
  label: string,
  value: string,
): string | null => {
  const note = table[value]
  return note === undefined ? `Unrecognised ${label} status “${value}”.` : note
}

export const assessCoverage = (response: BriefingResponse): CoverageAssessment => {
  const { audit } = response
  const notes = [
    noteFor(COVERAGE_NOTES, "coverage", audit.coverage_status),
    noteFor(VECTOR_NOTES, "semantic retrieval", audit.vector_branch_status),
    noteFor(RERANK_NOTES, "rerank", audit.rerank_status),
  ].filter((note): note is string => note !== null)
  return { level: notes.length === 0 ? "complete" : "degraded", notes }
}

const RETRIEVAL_MODE_LABELS: Readonly<Record<string, string>> = {
  hybrid: "Hybrid (keyword + semantic)",
  lexical_only: "Keyword only",
  vector_only: "Semantic only",
  none: "No retrieval matches",
}

export const describeRetrievalMode = (mode: string): string => RETRIEVAL_MODE_LABELS[mode] ?? mode

export const humanize = (value: string): string => {
  const words = value.replaceAll("_", " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

export const formatTimestamp = (value: string | null): string => {
  if (value === null) {
    return "Publication time unknown"
  }
  return `${new Date(value).toISOString().slice(0, 16).replace("T", " ")} UTC`
}

export type SpanSegments = {
  readonly before: string
  readonly cited: string
  readonly after: string
}

// API offsets count Unicode code points (Python str), not UTF-16 units. The
// schema already guarantees the slice equals the cited excerpt.
export const segmentSource = (source: BriefingSource): SpanSegments => {
  const points = Array.from(source.normalized_content)
  const { start_offset: start, end_offset: end } = source.span
  return {
    before: points.slice(0, start).join(""),
    cited: points.slice(start, end).join(""),
    after: points.slice(end).join(""),
  }
}
