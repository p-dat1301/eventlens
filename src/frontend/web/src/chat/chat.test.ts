import { afterEach, describe, expect, test } from "bun:test"

import { createChatClient, createSessionManager } from "../api/chat"
import { createHttpClient } from "../api/http"
import {
  accepted,
  apiError,
  briefingSource,
  CITATION,
  CONVERSATION,
  citation,
  json,
  RUN_1,
  routedFetch,
  run,
  sourceRef,
} from "../test/news-fixtures"
import { openCitation } from "./citations"
import { activeRun, initialThread, isUncertain, threadReducer } from "./thread"

const session = () => ({
  schema_version: "session.v1",
  session_token: "secret-token-value",
  token_type: "bearer",
  cookie_name: "eventlens_session",
  expires_at: "2026-11-05T00:00:00Z",
})

const clients = (routes: Parameters<typeof routedFetch>[0]) => {
  const routed = routedFetch(routes)
  const http = createHttpClient({ fetch: routed.fetch })
  const sessions = createSessionManager(http)
  return { ...routed, chat: createChatClient(http, sessions), sessions }
}

afterEach(() => window.localStorage.clear())

describe("anonymous session", () => {
  test("concurrent 401s (StrictMode double effects) create exactly one session", async () => {
    let authed = false
    const { chat, count } = clients({
      "POST /api/v1/sessions": () => {
        authed = true
        return json(session(), 201)
      },
      "GET /api/v1/conversations": () =>
        authed
          ? json({ schema_version: "conversation_list.v1", items: [], next_cursor: null })
          : json(apiError("unauthenticated"), 401),
    })
    const [a, b, c] = await Promise.all([
      chat.listConversations(null),
      chat.listConversations(null),
      chat.listConversations(null),
    ])
    expect([a.ok, b.ok, c.ok]).toEqual([true, true, true])
    expect(count("POST /api/v1/sessions")).toBe(1)
  })

  test("a still-valid cookie session is reused: no POST /sessions on reload", async () => {
    const { chat, count } = clients({
      "GET /api/v1/conversations": () =>
        json({ schema_version: "conversation_list.v1", items: [], next_cursor: null }),
    })
    expect((await chat.listConversations(null)).ok).toBe(true)
    expect(count("POST /api/v1/sessions")).toBe(0)
  })

  test("a second 401 after renewing is returned, never retried in a loop", async () => {
    const { chat, count } = clients({
      "POST /api/v1/sessions": () => json(session(), 201),
      "GET /api/v1/conversations": () => json(apiError("unauthenticated"), 401),
    })
    const result = await chat.listConversations(null)
    expect(result.ok).toBe(false)
    expect(count("GET /api/v1/conversations")).toBe(2)
    expect(count("POST /api/v1/sessions")).toBe(1)
  })

  test("the token is never stored by the app; only a non-secret expiry marker", async () => {
    const { sessions } = clients({ "POST /api/v1/sessions": () => json(session(), 201) })
    const events: string[] = []
    sessions.subscribe((event) => events.push(event))
    await sessions.renew()
    await sessions.renew()
    const stored = JSON.stringify({ ...window.localStorage, ...window.sessionStorage })
    expect(stored).not.toContain("secret-token-value")
    expect(sessions.hadSession()).toBe(true)
    // The second renewal tells the UI that the old session (and its conversations) is gone.
    expect(events).toEqual(["created", "replaced"])
  })
})

describe("run submission", () => {
  test("the Idempotency-Key header carries the key given for this question", async () => {
    const { chat, calls } = clients({
      [`POST /api/v1/conversations/${CONVERSATION}/runs`]: () => json(accepted(), 202),
    })
    await chat.submit(CONVERSATION, "Câu hỏi?", "key-1")
    await chat.submit(CONVERSATION, "Câu hỏi?", "key-1")
    const keys = calls.map((call) => new Headers(call.init.headers).get("Idempotency-Key"))
    expect(keys).toEqual(["key-1", "key-1"])
  })

  test("timeouts and gateway failures are uncertain (retry with the same key); 4xx are not", () => {
    expect(isUncertain({ kind: "timeout" })).toBe(true)
    expect(isUncertain({ kind: "network" })).toBe(true)
    expect(isUncertain({ kind: "unavailable", status: 502 })).toBe(true)
    const api = (status: number, code: string) =>
      ({ kind: "api", status, code, reason: null, requestId: null }) as const
    expect(isUncertain(api(409, "run_in_progress"))).toBe(false)
    expect(isUncertain(api(503, "model_unavailable"))).toBe(false)
    expect(isUncertain(api(503, "conversation_storage_unavailable"))).toBe(true)
  })

  test("a late non-terminal poll never moves a finished run back", () => {
    let state = threadReducer(initialThread, {
      type: "accepted",
      accepted: accepted() as never,
      question: "q",
    })
    state = threadReducer(state, { type: "run", run: run("completed") as never })
    state = threadReducer(state, { type: "run", run: run("running") as never })
    expect(state.runs[0]?.run?.status).toBe("completed")
    expect(activeRun(state)).toBeNull()
  })

  test("cancel shows the server's state: running + cancel_requested is not 'cancelled'", () => {
    let state = threadReducer(initialThread, { type: "run", run: run("running") as never })
    state = threadReducer(state, {
      type: "run",
      run: run("running", { cancel_requested: true }) as never,
    })
    expect(state.runs[0]?.run?.status).toBe("running")
    expect(activeRun(state)?.runId).toBe(RUN_1)
    state = threadReducer(state, {
      type: "run",
      run: run("cancelled", { cancel_requested: true }) as never,
    })
    expect(state.runs[0]?.run?.status).toBe("cancelled")
    expect(activeRun(state)).toBeNull()
  })
})

describe("citations", () => {
  const routes = (status = 201) => ({
    [`POST /api/v1/runs/${RUN_1}/citations/${CITATION}/source-ref`]: ({
      call,
    }: {
      call: number
    }) =>
      status === 201 ? json(sourceRef(`ref-${call}`), 201) : json(apiError("source_stale"), status),
    "GET /briefing/sources/ref-1": () => json(briefingSource()),
    "GET /briefing/sources/ref-2": () => json(briefingSource()),
  })

  test("every open asks for a fresh source-ref (the old ref is not reused)", async () => {
    const { chat, calls } = clients(routes())
    const signal = new AbortController().signal
    const first = await openCitation(chat, RUN_1, citation() as never, signal)
    const second = await openCitation(chat, RUN_1, citation() as never, signal)
    expect(first.ok && second.ok).toBe(true)
    expect(calls.map((call) => `${call.method} ${call.url.pathname}`)).toEqual([
      `POST /api/v1/runs/${RUN_1}/citations/${CITATION}/source-ref`,
      "GET /briefing/sources/ref-1",
      `POST /api/v1/runs/${RUN_1}/citations/${CITATION}/source-ref`,
      "GET /briefing/sources/ref-2",
    ])
  })

  test.each([
    [410, "source_stale"],
    [403, "access_denied"],
    [404, "citation_not_found"],
  ])("HTTP %i is reported as %s, never replaced by newer text", async (status, code) => {
    const { chat, calls } = clients({
      [`POST /api/v1/runs/${RUN_1}/citations/${CITATION}/source-ref`]: () =>
        json(apiError(code), status),
    })
    const result = await openCitation(
      chat,
      RUN_1,
      citation() as never,
      new AbortController().signal,
    )
    expect(result).toMatchObject({ ok: false, failure: { kind: "api", status, code } })
    expect(calls).toHaveLength(1)
  })

  test("a source_ref_url pointing elsewhere is not followed", async () => {
    const { chat, calls } = clients({})
    const foreign = { ...citation(), source_ref_url: "/internal/v1/catalog/articles" }
    const result = await openCitation(chat, RUN_1, foreign as never, new AbortController().signal)
    expect(result.ok).toBe(false)
    expect(calls).toHaveLength(0)
  })

  test("a viewer URL outside /briefing/sources is not fetched", async () => {
    const { chat, calls } = clients({
      [`POST /api/v1/runs/${RUN_1}/citations/${CITATION}/source-ref`]: () =>
        json({ ...sourceRef(), viewer_url: "https://evil.example/x" }, 201),
    })
    const result = await openCitation(
      chat,
      RUN_1,
      citation() as never,
      new AbortController().signal,
    )
    expect(result.ok).toBe(false)
    expect(calls).toHaveLength(1)
  })
})
