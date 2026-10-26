import { describe, expect, test } from "bun:test"

import { scriptedFetch } from "../test/fake-fetch"
import { answeredResponse, errorBody, json, REF_CLOSE, sourceResponse } from "../test/fixtures"
import { createBriefingClient } from "./client"
import { buildBriefingRequest } from "./model"

const request = () => {
  const validation = buildBriefingRequest("When did East Pier close and reopen?")
  if (!validation.ok) {
    throw new Error("fixture query must be valid")
  }
  return validation.request
}

describe("createBriefingClient.submit", () => {
  test("posts the strict briefing_request.v1 body and parses the response", async () => {
    const { fetch, calls } = scriptedFetch(json(answeredResponse()))
    const client = createBriefingClient({ baseUrl: "http://api.test", fetch })

    const result = await client.submit(request())

    expect(result.ok).toBe(true)
    expect(calls[0]?.url).toBe("http://api.test/briefing")
    expect(calls[0]?.init.method).toBe("POST")
    expect(JSON.parse(String(calls[0]?.init.body))).toEqual({
      schema_version: "briefing_request.v1",
      query: "When did East Pier close and reopen?",
      published_after: null,
      published_before: null,
      publication_time_basis: "source_published_at",
      source_domains: [],
      topic_filters: [],
      entity_ids: [],
      context_budget: 12000,
      result_limit: 5,
    })
  })

  test("reports a 200 with an unknown field as malformed, never as data", async () => {
    const { fetch } = scriptedFetch(json({ ...answeredResponse(), graph_state: {} }))

    const result = await createBriefingClient({ fetch }).submit(request())

    expect(result).toEqual({ ok: false, failure: { kind: "malformed", status: 200 } })
  })

  test("reports a non-JSON 200 as malformed", async () => {
    const { fetch } = scriptedFetch(new Response("<html>proxy</html>", { status: 200 }))

    const result = await createBriefingClient({ fetch }).submit(request())

    expect(result).toEqual({ ok: false, failure: { kind: "malformed", status: 200 } })
  })

  test("maps a strict error envelope to its code", async () => {
    const { fetch } = scriptedFetch(json(errorBody("model_unavailable"), 503))

    const result = await createBriefingClient({ fetch }).submit(request())

    expect(result).toEqual({
      ok: false,
      failure: { kind: "api", status: 503, code: "model_unavailable" },
    })
  })

  test("never trusts a non-envelope error body", async () => {
    const { fetch } = scriptedFetch(json({ detail: "Traceback: secret" }, 500))

    const result = await createBriefingClient({ fetch }).submit(request())

    expect(result).toEqual({ ok: false, failure: { kind: "malformed", status: 500 } })
  })

  test("treats a bodiless gateway error as an unreachable service", async () => {
    const { fetch } = scriptedFetch(new Response("", { status: 502 }))

    const result = await createBriefingClient({ fetch }).submit(request())

    expect(result).toEqual({ ok: false, failure: { kind: "network" } })
  })

  test("maps a rejected fetch to a network failure and an abort to cancelled", async () => {
    const { fetch } = scriptedFetch(
      new TypeError("Failed to fetch"),
      new DOMException("aborted", "AbortError"),
    )
    const client = createBriefingClient({ fetch })

    expect(await client.submit(request())).toEqual({ ok: false, failure: { kind: "network" } })
    expect(await client.submit(request())).toEqual({ ok: false, failure: { kind: "cancelled" } })
  })
})

describe("createBriefingClient.inspectSource", () => {
  test("requests the encoded source reference and parses briefing_source.v1", async () => {
    const { fetch, calls } = scriptedFetch(json(sourceResponse()))

    const result = await createBriefingClient({ fetch }).inspectSource(REF_CLOSE)

    expect(calls[0]?.url).toBe(`/briefing/sources/${REF_CLOSE}`)
    expect(calls[0]?.init.method).toBe("GET")
    expect(result.ok && result.data.normalized_content.startsWith("Harbor Authority")).toBe(true)
  })

  test("escapes path characters in the reference", async () => {
    const { fetch, calls } = scriptedFetch(json(errorBody("source_not_found"), 404))

    const result = await createBriefingClient({ fetch }).inspectSource("../admin?x=1")

    expect(calls[0]?.url).toBe("/briefing/sources/..%2Fadmin%3Fx%3D1")
    expect(result).toEqual({
      ok: false,
      failure: { kind: "api", status: 404, code: "source_not_found" },
    })
  })
})
