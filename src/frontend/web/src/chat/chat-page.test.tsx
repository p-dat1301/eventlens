import { afterEach, describe, expect, test } from "bun:test"

import {
  accepted,
  apiError,
  CONVERSATION,
  conversation,
  hold,
  json,
  messages,
  RUN_1,
  RUN_2,
  run,
  userMessage,
} from "../test/news-fixtures"
import { byText, click, go, mountApp, settle, text, unmountApp } from "../test/render"

afterEach(unmountApp)

const OTHER = "99999999-9999-4999-8999-999999999999"
const list = () =>
  json({ schema_version: "conversation_list.v1", items: [conversation()], next_cursor: null })
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
const base = { ...taxonomy, "GET /api/v1/conversations": list }
const path = `/chat/${CONVERSATION}`
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

const type = async (value: string) => {
  const box = document.querySelector<HTMLTextAreaElement>(".composer textarea")
  if (box === null) throw new Error("composer missing")
  const { act } = await import("react")
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set
    setter?.call(box, value)
    box.dispatchEvent(new Event("input", { bubbles: true }))
  })
}

describe("conversation thread", () => {
  test("after a reload the active run is found from history and polled to its answer", async () => {
    mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () =>
        json(messages([userMessage(RUN_1, 1, "Cầu Rồng thế nào?")])),
      [`GET /api/v1/runs/${RUN_1}`]: ({ call }) => json(run(call === 1 ? "running" : "completed")),
    })
    await settle()
    expect(text()).toContain("Đang tìm nguồn và soạn câu trả lời")
    await wait(1_200)
    await settle()
    expect(text()).toContain("Cầu Rồng được nhắc trong bài.")
    expect(text()).toContain("Dữ liệu AI mô phỏng")
  }, 10_000)

  test("an unconfirmed submit is retried with the same key; a new question gets a new key", async () => {
    const keys: (string | null)[] = []
    mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => json(messages([])),
      [`POST /api/v1/conversations/${CONVERSATION}/runs`]: ({ init, call }) => {
        keys.push(new Headers(init.headers).get("Idempotency-Key"))
        if (call === 1) throw new TypeError("Failed to fetch")
        return json(accepted(call === 2 ? RUN_1 : RUN_2), 202)
      },
      [`GET /api/v1/runs/${RUN_1}`]: () => json(run("completed")),
      [`GET /api/v1/runs/${RUN_2}`]: () => json(run("completed", { run_id: RUN_2, sequence: 3 })),
    })
    await settle()
    await type("Cầu Rồng thế nào?")
    await click(byText("button", "Gửi"))
    expect(text()).toContain("Chưa xác nhận được câu hỏi đã gửi")
    await wait(1_700)
    await settle()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
    expect(text()).toContain("Cầu Rồng được nhắc trong bài.")
    await type("Câu hỏi khác?")
    await click(byText("button", "Gửi"))
    expect(keys).toHaveLength(3)
    expect(keys[2]).not.toBe(keys[0])
  }, 10_000)

  test("409 run_in_progress shows the run the server is actually working on", async () => {
    let history = [] as unknown[]
    const { count } = mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => json(messages(history)),
      [`POST /api/v1/conversations/${CONVERSATION}/runs`]: () => {
        history = [userMessage(RUN_1, 1, "Câu đang chạy")]
        return json(apiError("run_in_progress"), 409)
      },
      [`GET /api/v1/runs/${RUN_1}`]: () => json(run("running", { question: "Câu đang chạy" })),
    })
    await settle()
    await type("Câu mới")
    await click(byText("button", "Gửi"))
    expect(count(`GET /api/v1/conversations/${CONVERSATION}/messages`)).toBe(2)
    expect(text()).toContain("Câu đang chạy")
    expect(text()).toContain("Đang có câu hỏi chưa xong")
  })

  test("cancel is shown as done only when the server confirms it", async () => {
    mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () =>
        json(messages([userMessage(RUN_1, 1, "Cầu Rồng thế nào?")])),
      [`GET /api/v1/runs/${RUN_1}`]: () => json(run("running")),
      [`POST /api/v1/runs/${RUN_1}/cancel`]: () =>
        json(run("running", { cancel_requested: true }), 202),
    })
    await settle()
    await click(byText("button", "Hủy"))
    expect(text()).toContain("Đang yêu cầu hủy")
    expect(text()).not.toContain("Đã hủy theo yêu cầu")
  })

  test("answered, abstained and failed runs render differently", async () => {
    const R3 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () =>
        json(
          messages([
            userMessage(RUN_1, 1, "Một"),
            userMessage(RUN_2, 3, "Hai"),
            userMessage(R3, 5, "Ba"),
          ]),
        ),
      [`GET /api/v1/runs/${RUN_1}`]: () => json(run("completed")),
      [`GET /api/v1/runs/${RUN_2}`]: () =>
        json(
          run("completed", {
            run_id: RUN_2,
            sequence: 3,
            outcome: "abstained",
            abstention_reason: "abstained_no_evidence",
            answer: null,
            citations: [],
          }),
        ),
      [`GET /api/v1/runs/${R3}`]: () => json(run("failed", { run_id: R3, sequence: 5 })),
    })
    await settle()
    expect(text()).toContain("EventLens trả lời")
    expect(text()).toContain("Không đủ nguồn để trả lời")
    expect(text()).toContain("AI chưa khả dụng")
  })

  test("the scope shown is the conversation's stored scope, not the feed's filters", async () => {
    const filtered = {
      type: "filtered",
      filters: {
        q: null,
        topic_ids: ["economy"],
        location_ids: [],
        source_ids: [],
        published_after: null,
        published_before: null,
        include_descendants: true,
      },
      articles: [],
    }
    mountApp(`${path}?topic=health`, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => json(messages([], filtered)),
    })
    await settle()
    expect(text()).toContain("Phạm vi: Tin khớp bộ lọc")
    expect(text()).toContain("chủ đề Kinh tế")
    expect(text()).toContain("toàn bộ bài khớp")
  })

  test("a lost session is explained: the old conversation is not reachable", async () => {
    window.localStorage.setItem("eventlens.session.marker", "2026-10-01T00:00:00Z")
    let authed = false
    mountApp(path, {
      ...taxonomy,
      "POST /api/v1/sessions": () => {
        authed = true
        return json(
          {
            schema_version: "session.v1",
            session_token: "t",
            token_type: "bearer",
            cookie_name: "eventlens_session",
            expires_at: "2026-11-10T00:00:00Z",
          },
          201,
        )
      },
      "GET /api/v1/conversations": () =>
        authed
          ? json({ schema_version: "conversation_list.v1", items: [], next_cursor: null })
          : json(apiError("unauthenticated"), 401),
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () =>
        authed
          ? json(apiError("conversation_not_found"), 404)
          : json(apiError("unauthenticated"), 401),
    })
    await settle()
    expect(text()).toContain("Không mở được hội thoại")
    expect(text()).toContain("Phiên cũ đã hết hạn")
  })

  test("a slow conversation A never overwrites conversation B", async () => {
    const slow = hold()
    mountApp(path, {
      ...base,
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => slow.promise,
      [`GET /api/v1/conversations/${OTHER}/messages`]: () =>
        json({
          ...messages([]),
          conversation: { ...conversation(), conversation_id: OTHER, title: "Hội thoại B" },
        }),
    })
    await settle()
    await go(`/chat/${OTHER}`)
    expect(document.querySelector(".thread-title")?.textContent).toBe("Hội thoại B")
    slow.release(json(messages([])))
    await settle()
    expect(document.querySelector(".thread-title")?.textContent).toBe("Hội thoại B")
  })

  test("deleting a conversation asks for confirmation, calls DELETE and leaves the thread", async () => {
    let deleted = false
    const { calls } = mountApp(path, {
      ...taxonomy,
      "GET /api/v1/conversations": () =>
        json({
          schema_version: "conversation_list.v1",
          items: deleted ? [] : [conversation()],
          next_cursor: null,
        }),
      [`GET /api/v1/conversations/${CONVERSATION}/messages`]: () => json(messages([])),
      [`DELETE /api/v1/conversations/${CONVERSATION}`]: () => {
        deleted = true
        return new Response(null, { status: 204 })
      },
    })
    await settle()
    await click(byText("button", "Xóa"))
    expect(calls.some((call) => call.method === "DELETE")).toBe(false)
    await click(byText("button", "Xóa hẳn"))
    expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(1)
    expect(window.location.pathname).toBe("/chat")
    expect(document.querySelector(".conversation-link")).toBeNull()
  })
})
