import { afterEach, describe, expect, test } from "bun:test"

import {
  ARTICLE_A,
  apiError,
  detail,
  json,
  SPAN_TEXT,
  summary,
  VERSION_A2,
} from "../test/news-fixtures"
import { byText, click, mountApp, settle, text, unmountApp } from "../test/render"

afterEach(unmountApp)

const path = `/articles/${ARTICLE_A}`

describe("article page", () => {
  test("a pending summary is re-read with GET until ready; the summary is never requested by POST", async () => {
    const { calls } = mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: ({ call }) =>
        json(call < 2 ? summary("pending") : summary("ready")),
    })
    await settle()
    expect(text()).toContain("Tóm tắt đang được xếp hàng")
    // First retry is scheduled 3.2 s later (2 s × 1.6 backoff); wait for it.
    await new Promise((resolve) => setTimeout(resolve, 3_400))
    await settle()
    expect(text()).toContain("Điểm về cầu Rồng")
    expect(document.querySelector("details.summary-more")?.textContent).toContain("Tóm tắt mẫu.")
    expect(text()).toContain("Dữ liệu AI mô phỏng")
    expect(calls.every((call) => call.method === "GET")).toBe(true)
  }, 10_000)

  test("leaving the page stops polling a pending summary", async () => {
    const { count } = mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(summary("pending")),
    })
    await settle()
    expect(count(`GET /api/v1/articles/${ARTICLE_A}/summary`)).toBe(1)
    unmountApp()
    await new Promise((resolve) => setTimeout(resolve, 3_600))
    expect(count(`GET /api/v1/articles/${ARTICLE_A}/summary`)).toBe(1)
  }, 10_000)

  test("a key point jumps to its exact code-point span (text after a non-BMP character)", async () => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(summary("ready")),
    })
    await settle()
    await click(byText("button", "Xem đoạn trong bài"))
    const mark = document.querySelector("mark.span-highlight")
    expect(mark?.textContent).toContain(SPAN_TEXT)
    expect(mark?.textContent?.replace(/Bắt đầu đoạn nguồn\. | Kết thúc đoạn nguồn\./g, "")).toBe(
      SPAN_TEXT,
    )
  })

  test("spans of a summary made for another version are not highlighted", async () => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () =>
        json(summary("ready", { article_version_id: VERSION_A2 })),
    })
    await settle()
    expect(text()).toContain("phiên bản khác của bài")
    expect(
      [...document.querySelectorAll("button")].some((b) => b.textContent === "Xem đoạn trong bài"),
    ).toBe(false)
  })

  test("withheld content links to the original instead of showing text", async () => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () =>
        json(detail({ content: null, content_policy: "withheld" })),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(summary("unavailable")),
    })
    await settle()
    expect(text()).toContain("Nội dung không hiển thị tại đây")
    expect(text()).toContain("Chưa có tóm tắt AI cho bài này")
  })

  test.each([
    [404, "article_not_found", "Không tìm thấy bài viết"],
    [410, "article_gone", "Bài viết đã bị gỡ"],
  ])("HTTP %i shows a final state without retry", async (status, code, title) => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(apiError(code), status),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(apiError(code), status),
    })
    await settle()
    expect(text()).toContain(title)
    expect(text()).not.toContain("Tải lại bài")
  })

  test("'ask about this article' links to a draft pinned to the version being read", async () => {
    const { count } = mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(summary("unavailable")),
    })
    await settle()
    const link = byText<HTMLAnchorElement>("a", "Hỏi về bài này")
    expect(link.getAttribute("href")).toBe(
      `/chat?article=${ARTICLE_A}&version=${encodeURIComponent(detail().version.article_version_id)}`,
    )
    expect(count("POST /api/v1/conversations")).toBe(0)
  })

  test("order: title, then Nắm nhanh, then the text; technical facts in a disclosure", async () => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () => json(summary("ready")),
    })
    await settle()
    const order = [...document.querySelectorAll("h1, h2")].map((h) => h.textContent)
    expect(order.indexOf("Nắm nhanh")).toBeGreaterThan(-1)
    expect(order.indexOf("Nắm nhanh")).toBeLessThan(order.indexOf("Nội dung bài"))
    expect(text()).toContain("Dữ liệu AI mô phỏng")
    const facts = document.querySelector<HTMLDetailsElement>("details.article-facts")
    expect(facts?.open).toBe(false)
    expect(facts?.textContent).toContain("fixture-summarizer")
    // The full summary text is one disclosure away, never a second visible block.
    const more = document.querySelector<HTMLDetailsElement>("details.summary-more")
    expect(more?.open).toBe(false)
    expect(more?.textContent).toContain("Tóm tắt mẫu.")
  })

  test("an extractive summary that only repeats its key points is not offered twice", async () => {
    mountApp(path, {
      [`GET /api/v1/articles/${ARTICLE_A}`]: () => json(detail()),
      [`GET /api/v1/articles/${ARTICLE_A}/summary`]: () =>
        json(summary("ready", { text: "Điểm về cầu Rồng." })),
    })
    await settle()
    expect(document.querySelector("details.summary-more")).toBeNull()
    expect(text().match(/Điểm về cầu Rồng/g)).toHaveLength(1)
  })
})
