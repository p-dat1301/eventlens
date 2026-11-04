import { afterEach, describe, expect, test } from "bun:test"

import {
  ARTICLE_A,
  accepted,
  apiError,
  CONVERSATION,
  conversation,
  detail,
  json,
  messages,
  RUN_1,
  run,
  VERSION_A,
} from "../test/news-fixtures"
import { byText, click, mountApp, settle, text, typeInto, unmountApp } from "../test/render"

afterEach(unmountApp)

const taxonomy = {
  "GET /api/v1/topics": () =>
    json({
      schema_version: "topic_list.v1",
      taxonomy_version: "t",
      items: [{ id: "economy", name: "Kinh tế", name_en: "Economy" }],
    }),
  "GET /api/v1/locations": () =>
    json({ schema_version: "location_list.v1", gazetteer_version: "g", items: [] }),
  "GET /api/v1/sources": () => json({ schema_version: "source_list.v1", items: [] }),
}
const emptyList = () =>
  json({ schema_version: "conversation_list.v1", items: [], next_cursor: null })
const thread = {
  [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => json(messages([])),
  [`GET /api/v1/runs/${RUN_1}`]: () => json(run("completed")),
}
const QUESTION = "Các bài nói gì về cầu Rồng?"
const box = ".composer textarea"
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

describe("/chat — first question", () => {
  test("the composer is there at once; rendering, focusing and typing create nothing", async () => {
    const { count } = mountApp("/chat", { ...taxonomy, "GET /api/v1/conversations": emptyList })
    await settle()
    const textarea = document.querySelector<HTMLTextAreaElement>(box)
    expect(textarea).not.toBeNull()
    expect(text()).toContain("Bạn muốn tìm hiểu tin gì?")
    expect(byText(".scope-chip", "Tất cả tin")).toBeDefined()
    textarea?.focus()
    await typeInto(box, QUESTION)
    expect(count("POST /api/v1/conversations")).toBe(0)
  })

  test("one send (double click, StrictMode) creates one conversation and one run", async () => {
    const bodies: unknown[] = []
    const keys: (string | null)[] = []
    const { count } = mountApp(
      "/chat",
      {
        ...taxonomy,
        ...thread,
        "GET /api/v1/conversations": emptyList,
        "POST /api/v1/conversations": ({ init }) => {
          bodies.push(JSON.parse(String(init.body)))
          return json(conversation(), 201)
        },
        [`POST /api/v1/conversations/${CONVERSATION}/runs`]: ({ init }) => {
          keys.push(new Headers(init.headers).get("Idempotency-Key"))
          return json(accepted(), 202)
        },
      },
      { strict: true },
    )
    await settle()
    await typeInto(box, QUESTION)
    const send = byText<HTMLButtonElement>("button", "Gửi")
    const { act } = await import("react")
    await act(async () => {
      send.click()
      send.click()
    })
    await settle(12)
    expect(count("POST /api/v1/conversations")).toBe(1)
    expect(bodies[0]).toEqual({ scope: { type: "all" }, title: QUESTION })
    expect(window.location.pathname).toBe(`/chat/${CONVERSATION}`)
    expect(count(`POST /api/v1/conversations/${CONVERSATION}/runs`)).toBe(1)
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/)
    expect(text()).toContain("Cầu Rồng được nhắc trong bài.")
  })

  test("a failed create keeps the question and is not retried by itself", async () => {
    const { count } = mountApp("/chat", {
      ...taxonomy,
      "GET /api/v1/conversations": emptyList,
      "POST /api/v1/conversations": () => json(apiError("conversation_storage_unavailable"), 503),
    })
    await settle()
    await typeInto(box, QUESTION)
    await click(byText("button", "Gửi"))
    await wait(1_700)
    await settle()
    expect(count("POST /api/v1/conversations")).toBe(1)
    expect(document.querySelector<HTMLTextAreaElement>(box)?.value).toBe(QUESTION)
    expect(window.location.pathname).toBe("/chat")
  }, 10_000)

  test("created, then the submit fails: retries reuse the conversation and the key", async () => {
    const keys: (string | null)[] = []
    const { count } = mountApp("/chat", {
      ...taxonomy,
      ...thread,
      "GET /api/v1/conversations": emptyList,
      "POST /api/v1/conversations": () => json(conversation(), 201),
      [`POST /api/v1/conversations/${CONVERSATION}/runs`]: ({ init }) => {
        keys.push(new Headers(init.headers).get("Idempotency-Key"))
        throw new TypeError("Failed to fetch")
      },
    })
    await settle()
    await typeInto(box, QUESTION)
    await click(byText("button", "Gửi"))
    // One automatic retry after 1.5 s, then the reader's "Gửi lại".
    await wait(1_700)
    await settle()
    expect(text()).toContain("Chưa xác nhận được câu hỏi đã gửi")
    await click(byText("button", "Gửi lại"))
    expect(count("POST /api/v1/conversations")).toBe(1)
    expect(keys).toHaveLength(3)
    expect(new Set(keys).size).toBe(1)
    expect(window.location.pathname).toBe(`/chat/${CONVERSATION}`)
  }, 10_000)

  test("a filtered draft sends the whole filter definition, never page items", async () => {
    let body: unknown = null
    mountApp("/chat?scope=filtered&topic=economy&from=2026-10-29&to=2026-10-29", {
      ...taxonomy,
      ...thread,
      "GET /api/v1/conversations": emptyList,
      "POST /api/v1/conversations": ({ init }) => {
        body = JSON.parse(String(init.body))
        return json(apiError("conversation_storage_unavailable"), 503)
      },
    })
    await settle()
    expect(text()).toContain("Tin khớp bộ lọc")
    expect(text()).toContain("chủ đề Kinh tế")
    await typeInto(box, QUESTION)
    await click(byText("button", "Gửi"))
    expect(body).toEqual({
      scope: {
        type: "filtered",
        filters: {
          topic_ids: ["economy"],
          published_after: "2026-10-28T17:00:00Z",
          published_before: "2026-10-29T17:00:00Z",
        },
      },
      title: QUESTION,
    })
  })

  test("an article draft pins the version it came from and shows its title", async () => {
    let body: unknown = null
    mountApp(`/chat?article=${ARTICLE_A}&version=${VERSION_A}`, {
      ...taxonomy,
      "GET /api/v1/conversations": emptyList,
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      "POST /api/v1/conversations": ({ init }) => {
        body = JSON.parse(String(init.body))
        return json(apiError("conversation_storage_unavailable"), 503)
      },
    })
    await settle()
    expect(text()).toContain("Một bài")
    expect(text()).toContain("Tiêu đề 1111")
    await typeInto(box, QUESTION)
    await click(byText("button", "Gửi"))
    expect(body).toMatchObject({
      scope: {
        type: "articles",
        articles: [{ article_id: ARTICLE_A, article_version_id: VERSION_A }],
      },
    })
  })

  test("the session note is one line with details on demand, no deletion promise", async () => {
    mountApp("/chat", { ...taxonomy, "GET /api/v1/conversations": emptyList })
    await settle()
    const summary = byText("details.session-details summary", "Phiên ẩn danh")
    expect(summary.textContent).toBe("Phiên ẩn danh · truy cập tối đa 7 ngày")
    expect(text()).toContain("không cam kết dữ liệu bị xóa sau 7")
  })
})
