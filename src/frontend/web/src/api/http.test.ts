import { describe, expect, test } from "bun:test"
import { z } from "zod"

import { apiError, json } from "../test/news-fixtures"
import { createHttpClient, errorFromBody, isAllowedApiPath } from "./http"

const Ok = z.strictObject({ ok: z.literal(true) })

describe("path allowlist", () => {
  test.each([
    "/api/v1/articles",
    "/api/v1/articles/11111111-1111-4111-8111-111111111111/summary",
    "/api/v1/runs/abc/citations/def/source-ref",
    "/briefing/sources/ref_ABC-123",
    "/briefing",
  ])("allows %s", (path) => {
    expect(isAllowedApiPath(path)).toBe(true)
  })

  test.each([
    "/internal/v1/catalog/articles",
    "https://evil.example/api/v1/articles",
    "//evil.example/api/v1/articles",
    "/api/v1/runs/../../internal/v1/catalog",
    "/api/v1/articles?cursor=x",
    "/api/v2/articles",
    "/api/v1/admin",
    "javascript:alert(1)",
  ])("refuses %s", (path) => {
    expect(isAllowedApiPath(path)).toBe(false)
  })

  test("a backend URL outside the allowlist is never fetched", async () => {
    let called = false
    const http = createHttpClient({
      fetch: async () => {
        called = true
        return json({ ok: true })
      },
    })
    const result = await http.request("/internal/v1/catalog/topics", Ok)
    expect(called).toBe(false)
    expect(result.ok).toBe(false)
  })
})

describe("error envelopes", () => {
  test("api_error.v1 keeps only code, safe reason and request id", () => {
    expect(errorFromBody(400, apiError("cursor_mismatch", "filters_changed"))).toEqual({
      kind: "api",
      status: 400,
      code: "cursor_mismatch",
      reason: "filters_changed",
      requestId: "req-123456789",
    })
  })

  test("an unexpected code or free-text reason is not echoed", () => {
    const failure = errorFromBody(500, apiError("<script>alert(1)</script>", "SELECT * FROM x"))
    expect(failure).toMatchObject({ kind: "api", code: "unknown_error", reason: null })
  })

  test("legacy briefing_error.v1 (viewer) is understood", () => {
    const body = {
      schema_version: "briefing_error.v1",
      error: { code: "source_stale", reason: null },
    }
    expect(errorFromBody(410, body)).toMatchObject({
      kind: "api",
      status: 410,
      code: "source_stale",
    })
  })

  test("a gateway page without the envelope means unavailable, not an API answer", () => {
    expect(errorFromBody(502, undefined)).toEqual({ kind: "unavailable", status: 502 })
    expect(errorFromBody(503, { detail: "x" })).toEqual({ kind: "unavailable", status: 503 })
  })

  test("any other non-envelope error body is malformed", () => {
    expect(errorFromBody(400, { detail: "raw" })).toEqual({ kind: "malformed", status: 400 })
  })
})

describe("request lifecycle", () => {
  test("HTML returned with 200 (SPA fallback) is rejected as malformed", async () => {
    const http = createHttpClient({
      fetch: async () => new Response("<!doctype html><html></html>", { status: 200 }),
    })
    expect(await http.request("/api/v1/topics", Ok)).toEqual({
      ok: false,
      failure: { kind: "malformed", status: 200 },
    })
  })

  test("a body with an unknown field is rejected", async () => {
    const http = createHttpClient({ fetch: async () => json({ ok: true, extra: 1 }) })
    const result = await http.request("/api/v1/topics", Ok)
    expect(result.ok).toBe(false)
  })

  test("caller abort is 'cancelled', the client deadline is 'timeout'", async () => {
    const never = (_: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("x", "AbortError")))
      })
    const http = createHttpClient({ fetch: never })
    const controller = new AbortController()
    const cancelled = http.request("/api/v1/topics", Ok, { signal: controller.signal })
    controller.abort()
    expect(await cancelled).toEqual({ ok: false, failure: { kind: "cancelled" } })
    expect(await http.request("/api/v1/topics", Ok, { timeoutMs: 5 })).toEqual({
      ok: false,
      failure: { kind: "timeout" },
    })
  })

  test("network errors are reported as such and requests use same-origin credentials", async () => {
    let init: RequestInit | null = null
    const http = createHttpClient({
      fetch: async (_url, given) => {
        init = given
        throw new TypeError("Failed to fetch")
      },
    })
    expect(await http.request("/api/v1/topics", Ok)).toEqual({
      ok: false,
      failure: { kind: "network" },
    })
    expect(init).toMatchObject({ credentials: "same-origin" })
  })

  test("repeated parameters are sent as repeated keys", async () => {
    let seen = ""
    const http = createHttpClient({
      fetch: async (url) => {
        seen = url
        return json({ ok: true })
      },
    })
    await http.request("/api/v1/articles", Ok, {
      query: { topic_ids: ["economy", "health"], q: undefined, cursor: null, limit: 20 },
    })
    expect(seen).toBe("/api/v1/articles?topic_ids=economy&topic_ids=health&limit=20")
  })
})
