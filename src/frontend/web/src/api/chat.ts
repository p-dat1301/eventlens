import { z } from "zod"

import { BriefingSourceSchema } from "../briefing/contract"
import { VERSION_ID } from "./catalog"
import type { ApiFailure, ApiResult, HttpClient } from "./http"

// Mirrors src/backend/rag/presentation/conversations_http.py (chat API of
// rag-api). Opaque blobs the UI never renders (usage, effective_scope,
// source_versions, temporal claims, audit) are accepted but not interpreted.

const Id = z.guid()
const Timestamp = z.iso.datetime({ offset: true })

export const SessionSchema = z.strictObject({
  schema_version: z.literal("session.v1"),
  session_token: z.string(),
  token_type: z.literal("bearer"),
  cookie_name: z.string(),
  expires_at: Timestamp,
})

export const ConversationSchema = z.strictObject({
  schema_version: z.literal("conversation.v1"),
  conversation_id: Id,
  title: z.string().nullable(),
  // Parsed separately with ScopeSchema so one odd stored scope never hides the conversation.
  scope: z.record(z.string(), z.unknown()),
  scope_sha256: z.string(),
  created_at: Timestamp,
  updated_at: Timestamp,
})

export const ConversationListSchema = z.strictObject({
  schema_version: z.literal("conversation_list.v1"),
  items: z.array(ConversationSchema),
  next_cursor: z.string().nullable(),
})

export const MessageSchema = z.strictObject({
  message_id: Id,
  run_id: Id,
  role: z.enum(["user", "assistant"]),
  sequence: z.int().min(0),
  content: z.string(),
  created_at: Timestamp,
})

export const MessageListSchema = z.strictObject({
  schema_version: z.literal("message_list.v1"),
  conversation: ConversationSchema,
  items: z.array(MessageSchema),
  next_after_sequence: z.int().nullable(),
})

export const RunAcceptedSchema = z.strictObject({
  schema_version: z.literal("run_accepted.v1"),
  run_id: Id,
  conversation_id: Id,
  status: z.string(),
  status_url: z.string(),
  replayed: z.boolean(),
})

const CitedSpan = z
  .strictObject({ start_offset: z.int().min(0), end_offset: z.int().min(1), excerpt: z.string() })
  .refine((span) => span.end_offset > span.start_offset, "span must be ordered")

export const CitationSchema = z.strictObject({
  citation_id: Id,
  claim_id: Id,
  article_id: Id,
  article_version_id: z.string(),
  span: CitedSpan,
  source_title: z.string(),
  source_name: z.string(),
  source_domain: z.string(),
  source_url: z.string(),
  source_published_at: Timestamp.nullable(),
  source_ref_url: z.string(),
})

const AnswerSchema = z.strictObject({
  answer: z.string(),
  claims: z.array(
    z.strictObject({ claim_id: Id, text: z.string(), temporal_claim: z.unknown().nullable() }),
  ),
  uncertainties: z.array(
    z.strictObject({
      uncertainty_id: Id,
      category: z.string(),
      explanation: z.string(),
      claim_id: Id.nullable(),
    }),
  ),
  retrieved_at: Timestamp,
  model_version: z.string(),
  mode: z.string(),
  retrieval_mode: z.string(),
  audit: z.record(z.string(), z.string()),
})

export const RUN_STATUSES = ["queued", "running", "completed", "failed", "cancelled"] as const

export const RunSchema = z.strictObject({
  schema_version: z.literal("query_run.v1"),
  run_id: Id,
  conversation_id: Id,
  sequence: z.int().min(0),
  question: z.string(),
  status: z.enum(RUN_STATUSES),
  outcome: z.enum(["answered", "abstained"]).nullable(),
  abstention_reason: z.string().nullable(),
  error: z.strictObject({ code: z.string(), reason: z.string().nullable() }).nullable(),
  answer: AnswerSchema.nullable(),
  citations: z.array(CitationSchema),
  effective_scope: z.unknown().nullable(),
  as_of: Timestamp.nullable(),
  source_versions: z.unknown().nullable(),
  usage: z.unknown().nullable(),
  cancel_requested: z.boolean(),
  created_at: Timestamp,
  started_at: Timestamp.nullable(),
  finished_at: Timestamp.nullable(),
})

export const SourceRefSchema = z.strictObject({
  schema_version: z.literal("source_ref.v1"),
  source_ref: z.string(),
  viewer_url: z.string(),
})

// Canonical stored scope definition (backend.rag.domain.conversation.ConversationScope.canonical).
export const ScopeFiltersSchema = z.object({
  q: z.string().nullable().optional(),
  topic_ids: z.array(z.string()).optional(),
  location_ids: z.array(z.string()).optional(),
  source_ids: z.array(z.string()).optional(),
  published_after: z.string().nullable().optional(),
  published_before: z.string().nullable().optional(),
  include_descendants: z.boolean().optional(),
})

export const ScopeSchema = z.object({
  type: z.enum(["all", "filtered", "articles"]),
  filters: ScopeFiltersSchema.nullable().optional(),
  articles: z
    .array(
      z.object({
        article_id: z.string(),
        article_version_id: z.string().regex(VERSION_ID).nullable().optional(),
      }),
    )
    .optional(),
})

export type Session = z.infer<typeof SessionSchema>
export type Conversation = z.infer<typeof ConversationSchema>
export type ConversationList = z.infer<typeof ConversationListSchema>
export type Message = z.infer<typeof MessageSchema>
export type MessageList = z.infer<typeof MessageListSchema>
export type RunAccepted = z.infer<typeof RunAcceptedSchema>
export type Run = z.infer<typeof RunSchema>
export type RunStatus = Run["status"]
export type Citation = z.infer<typeof CitationSchema>
export type SourceRef = z.infer<typeof SourceRefSchema>
export type Scope = z.infer<typeof ScopeSchema>
export type ScopeFilters = z.infer<typeof ScopeFiltersSchema>
export type BriefingSource = z.infer<typeof BriefingSourceSchema>

export const isTerminal = (status: RunStatus): boolean =>
  status === "completed" || status === "failed" || status === "cancelled"

/** Request body of `POST /api/v1/conversations`. */
export type ScopeRequest =
  | { readonly type: "all" }
  | { readonly type: "filtered"; readonly filters: ScopeFiltersRequest }
  | {
      readonly type: "articles"
      readonly articles: readonly {
        readonly article_id: string
        readonly article_version_id?: string
      }[]
    }

export type ScopeFiltersRequest = {
  readonly q?: string
  readonly topic_ids?: readonly string[]
  readonly location_ids?: readonly string[]
  readonly source_ids?: readonly string[]
  readonly published_after?: string
  readonly published_before?: string
}

// --- Session ----------------------------------------------------------------

export type SessionEvent = "created" | "replaced"

/**
 * Anonymous browser session. The token lives only in the HttpOnly cookie set
 * by the server; nothing secret is stored by the app. `ensure` is
 * single-flight: StrictMode double effects or concurrent 401s share one POST.
 * A non-secret marker (expiry time) remembers that this browser had a session,
 * so losing it can be explained to the user instead of silently starting over.
 */
export type SessionManager = {
  readonly renew: () => Promise<ApiResult<Session>>
  readonly hadSession: () => boolean
  readonly subscribe: (listener: (event: SessionEvent) => void) => () => void
}

const MARKER_KEY = "eventlens.session.marker"

const readMarker = (): string | null => {
  try {
    return window.localStorage.getItem(MARKER_KEY)
  } catch {
    return null
  }
}

const writeMarker = (value: string) => {
  try {
    window.localStorage.setItem(MARKER_KEY, value)
  } catch {
    // Storage disabled: the app still works, only the "session lost" hint is lost.
  }
}

export const createSessionManager = (http: HttpClient): SessionManager => {
  let inflight: Promise<ApiResult<Session>> | null = null
  const listeners = new Set<(event: SessionEvent) => void>()
  return {
    renew: () => {
      if (inflight !== null) return inflight
      const had = readMarker() !== null
      inflight = http
        .request("/api/v1/sessions", SessionSchema, { method: "POST" })
        .then((result) => {
          if (result.ok) {
            // Only the expiry is kept; the token itself is never stored by the app.
            writeMarker(result.data.expires_at)
            for (const listener of listeners) listener(had ? "replaced" : "created")
          }
          return result
        })
        .finally(() => {
          inflight = null
        })
      return inflight
    },
    hadSession: () => readMarker() !== null,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
}

// --- Client -----------------------------------------------------------------

export type ChatClient = {
  readonly listConversations: (
    cursor: string | null,
    signal?: AbortSignal,
  ) => Promise<ApiResult<ConversationList>>
  readonly createConversation: (
    scope: ScopeRequest,
    title: string | null,
    signal?: AbortSignal,
  ) => Promise<ApiResult<Conversation>>
  readonly deleteConversation: (id: string, signal?: AbortSignal) => Promise<ApiResult<undefined>>
  readonly messages: (
    id: string,
    afterSequence: number,
    signal?: AbortSignal,
  ) => Promise<ApiResult<MessageList>>
  readonly submit: (
    conversationId: string,
    question: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<ApiResult<RunAccepted>>
  readonly run: (statusPath: string, signal?: AbortSignal) => Promise<ApiResult<Run>>
  readonly cancel: (runId: string, signal?: AbortSignal) => Promise<ApiResult<Run>>
  readonly sourceRef: (path: string, signal?: AbortSignal) => Promise<ApiResult<SourceRef>>
  readonly viewSource: (path: string, signal?: AbortSignal) => Promise<ApiResult<BriefingSource>>
}

const isUnauthenticated = (failure: ApiFailure) => failure.kind === "api" && failure.status === 401

/**
 * Chat client with session recovery: a `401` creates a session (once, shared)
 * and repeats the call once. A second `401` is returned to the caller; there
 * is no retry loop.
 */
export const createChatClient = (http: HttpClient, session: SessionManager): ChatClient => {
  const authed = async <T>(call: () => Promise<ApiResult<T>>): Promise<ApiResult<T>> => {
    const first = await call()
    if (first.ok || !isUnauthenticated(first.failure)) return first
    const renewed = await session.renew()
    if (!renewed.ok) return renewed
    return call()
  }
  const id = (value: string) => encodeURIComponent(value)
  return {
    listConversations: (cursor, signal) =>
      authed(() =>
        http.request("/api/v1/conversations", ConversationListSchema, {
          query: { cursor, limit: 30 },
          signal,
        }),
      ),
    createConversation: (scope, title, signal) =>
      authed(() =>
        http.request("/api/v1/conversations", ConversationSchema, {
          method: "POST",
          body: title === null ? { scope } : { scope, title },
          signal,
        }),
      ),
    deleteConversation: (conversationId, signal) =>
      authed(() =>
        http.request(`/api/v1/conversations/${id(conversationId)}`, null, {
          method: "DELETE",
          signal,
        }),
      ),
    messages: (conversationId, afterSequence, signal) =>
      authed(() =>
        http.request(`/api/v1/conversations/${id(conversationId)}/messages`, MessageListSchema, {
          query: { after_sequence: afterSequence, limit: 100 },
          signal,
        }),
      ),
    submit: (conversationId, question, idempotencyKey, signal) =>
      authed(() =>
        http.request(`/api/v1/conversations/${id(conversationId)}/runs`, RunAcceptedSchema, {
          method: "POST",
          body: { question },
          headers: { "Idempotency-Key": idempotencyKey },
          signal,
          timeoutMs: 20_000,
        }),
      ),
    // status_url comes from the backend; the HTTP layer rejects anything outside the allowlist.
    run: (statusPath, signal) => authed(() => http.request(statusPath, RunSchema, { signal })),
    cancel: (runId, signal) =>
      authed(() =>
        http.request(`/api/v1/runs/${id(runId)}/cancel`, RunSchema, { method: "POST", signal }),
      ),
    sourceRef: (path, signal) =>
      authed(() => http.request(path, SourceRefSchema, { method: "POST", signal })),
    viewSource: (path, signal) => http.request(path, BriefingSourceSchema, { signal }),
  }
}

export const RUN_PATH = (runId: string) => `/api/v1/runs/${encodeURIComponent(runId)}`

/** source_ref_url must point at this run's citation; anything else is refused. */
export const isCitationRefPath = (path: string, runId: string, citationId: string): boolean =>
  path === `${RUN_PATH(runId)}/citations/${encodeURIComponent(citationId)}/source-ref`

export const isViewerPath = (path: string): boolean => /^\/briefing\/sources\/[^/?#]+$/.test(path)
