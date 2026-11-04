import { z } from "zod"

// One HTTP boundary for the news, article and chat clients. Every response body
// is parsed with a schema before the UI sees it; error bodies are reduced to a
// stable code (never rendered raw); every path, including URLs the backend
// hands back (status_url, source_ref_url, viewer_url), must be an allowed
// same-origin API path.

export type ApiFailure =
  | {
      readonly kind: "api"
      readonly status: number
      readonly code: string
      readonly reason: string | null
      readonly requestId: string | null
    }
  /** No response: offline, DNS, connection refused. */
  | { readonly kind: "network" }
  /** The gateway answered without an API body (service down / restarting). */
  | { readonly kind: "unavailable"; readonly status: number }
  /** A response arrived but did not match the contract. */
  | { readonly kind: "malformed"; readonly status: number }
  /** The client gave up waiting; the server may still have processed it. */
  | { readonly kind: "timeout" }
  /** Aborted by the caller (navigation, newer request). Never shown. */
  | { readonly kind: "cancelled" }

export type ApiResult<T> =
  | { readonly ok: true; readonly data: T; readonly status: number }
  | { readonly ok: false; readonly failure: ApiFailure }

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>

const SAFE_CODE = /^[a-z0-9_]{1,64}$/

const ApiErrorEnvelope = z.strictObject({
  schema_version: z.literal("api_error.v1"),
  error: z.strictObject({
    code: z.string(),
    reason: z.string().nullable().optional(),
    request_id: z.string().nullable().optional(),
  }),
})

// Legacy envelope of /briefing and /briefing/sources/{ref}.
const BriefingErrorEnvelope = z.strictObject({
  schema_version: z.literal("briefing_error.v1"),
  error: z.strictObject({ code: z.string(), reason: z.string().nullable().optional() }),
})

const ALLOWED_PATH =
  /^\/(?:api\/v1\/(?:articles|article-facets|topics|locations|sources|sessions|conversations|runs)(?:\/[A-Za-z0-9._~:@%-]+)*|briefing(?:\/[A-Za-z0-9._~%-]+)*)$/

/**
 * True when `path` is a relative, same-origin path of the public API. Rejects
 * absolute URLs, protocol-relative URLs, dot segments, `/internal` and query
 * strings (queries are always built by the client).
 */
export const isAllowedApiPath = (path: string): boolean =>
  ALLOWED_PATH.test(path) && !path.split("/").some((part) => part === "." || part === "..")

export type QueryValue = string | number | boolean | readonly string[] | null | undefined

export const buildQuery = (params: Readonly<Record<string, QueryValue>>): string => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, item)
    } else {
      search.append(key, String(value))
    }
  }
  const text = search.toString()
  return text.length > 0 ? `?${text}` : ""
}

export type RequestOptions = {
  readonly method?: "GET" | "POST" | "DELETE"
  readonly query?: Readonly<Record<string, QueryValue>>
  readonly body?: unknown
  readonly headers?: Readonly<Record<string, string>>
  readonly signal?: AbortSignal | undefined
  readonly timeoutMs?: number
}

export type HttpClient = {
  readonly request: <T>(
    path: string,
    schema: z.ZodType<T> | null,
    options?: RequestOptions,
  ) => Promise<ApiResult<T>>
}

const GATEWAY_STATUSES = new Set([502, 503, 504])
const DEFAULT_TIMEOUT_MS = 15_000

const isAbort = (error: unknown): boolean =>
  (error instanceof DOMException || error instanceof Error) &&
  (error.name === "AbortError" || error.name === "TimeoutError")

const failure = (value: ApiFailure): ApiResult<never> => ({ ok: false, failure: value })

const readBody = async (response: Response): Promise<unknown> => {
  if (response.status === 204) return null
  const text = await response.text()
  if (text.length === 0) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

export const errorFromBody = (status: number, body: unknown): ApiFailure => {
  const api = ApiErrorEnvelope.safeParse(body)
  if (api.success) {
    const code = SAFE_CODE.test(api.data.error.code) ? api.data.error.code : "unknown_error"
    const reason = api.data.error.reason ?? null
    return {
      kind: "api",
      status,
      code,
      reason: reason !== null && SAFE_CODE.test(reason) ? reason : null,
      requestId: api.data.error.request_id ?? null,
    }
  }
  const legacy = BriefingErrorEnvelope.safeParse(body)
  if (legacy.success) {
    const code = SAFE_CODE.test(legacy.data.error.code) ? legacy.data.error.code : "unknown_error"
    return { kind: "api", status, code, reason: null, requestId: null }
  }
  // A gateway status without our envelope means the service itself is unreachable.
  if (GATEWAY_STATUSES.has(status)) return { kind: "unavailable", status }
  return { kind: "malformed", status }
}

type ClientOptions = {
  readonly baseUrl?: string
  readonly fetch?: FetchLike
}

export const createHttpClient = ({
  baseUrl = "",
  fetch: fetchImpl = (input, init) => fetch(input, init),
}: ClientOptions = {}): HttpClient => ({
  request: async <T>(
    path: string,
    schema: z.ZodType<T> | null,
    options: RequestOptions = {},
  ): Promise<ApiResult<T>> => {
    if (!isAllowedApiPath(path)) {
      // A backend-provided URL outside the allowlist is a contract violation.
      return failure({ kind: "malformed", status: 0 })
    }
    const external = options.signal
    if (external?.aborted) return failure({ kind: "cancelled" })

    const controller = new AbortController()
    let timedOut = false
    const onAbort = () => controller.abort()
    external?.addEventListener("abort", onAbort, { once: true })
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS)

    const headers: Record<string, string> = { accept: "application/json", ...options.headers }
    if (options.body !== undefined) headers["content-type"] = "application/json"

    try {
      let response: Response
      try {
        response = await fetchImpl(`${baseUrl}${path}${buildQuery(options.query ?? {})}`, {
          method: options.method ?? "GET",
          headers,
          body: options.body === undefined ? null : JSON.stringify(options.body),
          credentials: "same-origin",
          signal: controller.signal,
        })
      } catch (error) {
        if (timedOut) return failure({ kind: "timeout" })
        if (external?.aborted || isAbort(error)) return failure({ kind: "cancelled" })
        return failure({ kind: "network" })
      }

      let body: unknown
      try {
        body = await readBody(response)
      } catch {
        if (timedOut) return failure({ kind: "timeout" })
        if (external?.aborted) return failure({ kind: "cancelled" })
        return failure({ kind: "network" })
      }

      if (!response.ok) return failure(errorFromBody(response.status, body))
      if (schema === null) return { ok: true, data: undefined as T, status: response.status }
      const parsed = schema.safeParse(body)
      return parsed.success
        ? { ok: true, data: parsed.data, status: response.status }
        : failure({ kind: "malformed", status: response.status })
    } finally {
      clearTimeout(timer)
      external?.removeEventListener("abort", onAbort)
    }
  },
})
