// Contract-shaped builders for the news/chat unit tests (mock data, not real
// articles). Shapes follow docs/openapi/*.json.
import type { FetchLike } from "../api/http"

export const ARTICLE_A = "11111111-1111-4111-8111-111111111111"
export const ARTICLE_B = "22222222-2222-4222-8222-222222222222"
export const SOURCE_ID = "33333333-3333-4333-8333-333333333333"
export const CONVERSATION = "44444444-4444-4444-8444-444444444444"
export const RUN_1 = "55555555-5555-4555-8555-555555555555"
export const RUN_2 = "66666666-6666-4666-8666-666666666666"
export const CLAIM = "77777777-7777-4777-8777-777777777777"
export const CITATION = "88888888-8888-4888-8888-888888888888"
export const VERSION_A = `sha256:${"a".repeat(64)}`
export const VERSION_A2 = `sha256:${"c".repeat(64)}`

export const card = (id = ARTICLE_A, overrides: Record<string, unknown> = {}) => ({
  article_id: id,
  article_version_id: VERSION_A,
  upstream_version_id: null,
  title: `Tiêu đề ${id.slice(0, 4)}`,
  source: { id: SOURCE_ID, name: "Báo Mẫu", domain: "bao-mau.example" },
  url: "https://bao-mau.example/bai",
  published_at: "2026-10-29T03:00:00Z",
  topics: [],
  locations: [],
  summary_preview: null,
  processing_status: { metadata: "unavailable", summary: "pending" },
  ...overrides,
})

export const articleList = (
  items: unknown[],
  next: string | null = null,
  overrides: Record<string, unknown> = {},
) => ({
  schema_version: "article_list.v1",
  items,
  next_cursor: next,
  has_more: next !== null,
  as_of: "2026-10-29T05:00:00Z",
  search_mode: "none",
  ...overrides,
})

export const facets = (total = 2) => ({
  schema_version: "article_facets.v1",
  semantics: "all_filters_applied",
  as_of: "2026-10-29T05:00:00Z",
  total,
  topics: [{ id: "economy", name: "Kinh tế", count: 1 }],
  locations: [],
  sources: [
    { id: SOURCE_ID, name: "Báo Mẫu", count: total, domain: "bao-mau.example", type: null },
  ],
  topics_unclassified: total - 1,
  locations_unknown: total,
})

// Content with a character outside the BMP before the cited span: code-point
// offsets differ from UTF-16 indices here.
export const CONTENT = "Mở đầu 𝔸 bài.\nĐoạn hai nói về cầu Rồng.\nĐoạn ba."
export const SPAN_START = Array.from("Mở đầu 𝔸 bài.\n").length
export const SPAN_TEXT = "Đoạn hai nói về cầu Rồng."

export const detail = (overrides: Record<string, unknown> = {}) => ({
  schema_version: "article_detail.v1",
  article: card(),
  content: CONTENT,
  content_policy: "full",
  ingested_at: "2026-10-29T04:00:00Z",
  updated_at_source: null,
  language: "vi",
  version: {
    article_version_id: VERSION_A,
    upstream_version_id: null,
    revision_count: 1,
    is_current: true,
  },
  labels: [],
  ...overrides,
})

export const summary = (status: string, overrides: Record<string, unknown> = {}) => ({
  schema_version: "article_summary.v1",
  article_id: ARTICLE_A,
  article_version_id: VERSION_A,
  status,
  source: "ai",
  text: status === "ready" ? "Tóm tắt mẫu." : null,
  key_points:
    status === "ready"
      ? [
          {
            text: "Điểm về cầu Rồng",
            supporting_spans: [
              {
                start_offset: SPAN_START,
                end_offset: SPAN_START + Array.from(SPAN_TEXT).length,
                excerpt: SPAN_TEXT,
                span_fingerprint: "fp",
              },
            ],
          },
        ]
      : [],
  generator:
    status === "ready"
      ? {
          model_id: "fixture-summarizer",
          model_version: "fixture-summarizer.v1",
          prompt_version: "p",
          schema_version: "s",
          summary_policy_id: "x",
        }
      : null,
  failure_code: null,
  publisher_summary: null,
  created_at: status === "ready" ? "2026-10-29T04:10:00Z" : null,
  ...overrides,
})

export const conversation = (
  scope: Record<string, unknown> = { type: "all", filters: null, articles: [] },
) => ({
  schema_version: "conversation.v1",
  conversation_id: CONVERSATION,
  title: "Hội thoại mẫu",
  scope,
  scope_sha256: "x".repeat(64),
  created_at: "2026-10-29T05:00:00Z",
  updated_at: "2026-10-29T05:00:00Z",
})

export const messages = (items: unknown[] = [], scope?: Record<string, unknown>) => ({
  schema_version: "message_list.v1",
  conversation: conversation(scope),
  items,
  next_after_sequence: null,
})

export const userMessage = (runId: string, sequence: number, content: string) => ({
  message_id: `9${runId.slice(1)}`,
  run_id: runId,
  role: "user",
  sequence,
  content,
  created_at: "2026-10-29T05:01:00Z",
})

export const accepted = (runId = RUN_1, replayed = false) => ({
  schema_version: "run_accepted.v1",
  run_id: runId,
  conversation_id: CONVERSATION,
  status: "queued",
  status_url: `/api/v1/runs/${runId}`,
  replayed,
})

export const citation = (runId = RUN_1) => ({
  citation_id: CITATION,
  claim_id: CLAIM,
  article_id: ARTICLE_A,
  article_version_id: VERSION_A,
  span: {
    start_offset: SPAN_START,
    end_offset: SPAN_START + Array.from(SPAN_TEXT).length,
    excerpt: SPAN_TEXT,
  },
  source_title: "Tiêu đề nguồn",
  source_name: "Báo Mẫu",
  source_domain: "bao-mau.example",
  source_url: "https://bao-mau.example/bai",
  source_published_at: "2026-10-29T03:00:00Z",
  source_ref_url: `/api/v1/runs/${runId}/citations/${CITATION}/source-ref`,
})

export const run = (status: string, overrides: Record<string, unknown> = {}) => {
  const runId = (overrides["run_id"] as string | undefined) ?? RUN_1
  const completed = status === "completed"
  return {
    schema_version: "query_run.v1",
    run_id: runId,
    conversation_id: CONVERSATION,
    sequence: 1,
    question: "Cầu Rồng thế nào?",
    status,
    outcome: completed ? "answered" : null,
    abstention_reason: null,
    error:
      status === "failed" ? { code: "model_unavailable", reason: "provider_not_configured" } : null,
    answer: completed
      ? {
          answer: "Cầu Rồng được nhắc trong bài.",
          claims: [
            { claim_id: CLAIM, text: "Cầu Rồng được nhắc trong bài.", temporal_claim: null },
          ],
          uncertainties: [],
          retrieved_at: "2026-10-29T05:02:00Z",
          model_version: "fixture",
          mode: "fixture",
          retrieval_mode: "lexical",
          audit: { mode: "fixture" },
        }
      : null,
    citations: completed ? [citation(runId)] : [],
    effective_scope: null,
    as_of: null,
    source_versions: null,
    usage: null,
    cancel_requested: false,
    created_at: "2026-10-29T05:01:00Z",
    started_at: null,
    finished_at: completed ? "2026-10-29T05:02:00Z" : null,
    ...overrides,
  }
}

export const sourceRef = (ref = "ref-1") => ({
  schema_version: "source_ref.v1",
  source_ref: ref,
  viewer_url: `/briefing/sources/${ref}`,
})

export const briefingSource = () => ({
  schema_version: "briefing_source.v1",
  title: "Tiêu đề nguồn",
  source_name: "Báo Mẫu",
  source_domain: "bao-mau.example",
  source_url: "https://bao-mau.example/bai",
  source_published_at: "2026-10-29T03:00:00Z",
  normalized_content: CONTENT,
  span: {
    start_offset: SPAN_START,
    end_offset: SPAN_START + Array.from(SPAN_TEXT).length,
    excerpt: SPAN_TEXT,
  },
})

export const apiError = (code: string, reason: string | null = null) => ({
  schema_version: "api_error.v1",
  error: { code, reason, request_id: "req-123456789" },
})

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })

type Handler = (request: {
  url: URL
  init: RequestInit
  call: number
}) => Response | Promise<Response>

export type RoutedCall = { readonly method: string; readonly url: URL; readonly init: RequestInit }

/**
 * Fetch routed by "METHOD /path" (exact path, query ignored). A handler may
 * return a pending promise to hold a response; unknown routes throw.
 */
export const routedFetch = (routes: Record<string, Handler>) => {
  const calls: RoutedCall[] = []
  const counts = new Map<string, number>()
  const fetch: FetchLike = async (input, init) => {
    const url = new URL(input, "http://localhost")
    const method = init.method ?? "GET"
    const key = `${method} ${url.pathname}`
    calls.push({ method, url, init })
    const handler = routes[key]
    if (handler === undefined) throw new Error(`unrouted ${key}`)
    const call = (counts.get(key) ?? 0) + 1
    counts.set(key, call)
    const signal = init.signal
    const response = handler({ url, init, call })
    if (!(response instanceof Promise)) return response
    return new Promise<Response>((resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))
      response.then(resolve, reject)
    })
  }
  return { fetch, calls, count: (key: string) => counts.get(key) ?? 0 }
}

export const hold = () => {
  let release: (response: Response) => void = () => undefined
  const promise = new Promise<Response>((resolve) => {
    release = resolve
  })
  return { promise, release }
}
