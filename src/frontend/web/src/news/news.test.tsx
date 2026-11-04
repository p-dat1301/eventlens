import { afterEach, describe, expect, test } from "bun:test"

import {
  ARTICLE_A,
  ARTICLE_B,
  apiError,
  articleList,
  card,
  facets,
  hold,
  json,
  SOURCE_ID,
} from "../test/news-fixtures"
import { byText, click, go, mountApp, settle, text, unmountApp } from "../test/render"

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

describe("news feed", () => {
  test("a slow response for old filters never replaces the newer list", async () => {
    const slow = hold()
    const { calls } = mountApp("/news?q=alpha", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": ({ url }) =>
        url.searchParams.get("q") === "alpha"
          ? slow.promise
          : json(articleList([card(ARTICLE_B, { title: "Bài của bộ lọc B" })])),
    })
    await settle()
    await go("/news?q=beta")
    expect(text()).toContain("Bài của bộ lọc B")
    slow.release(json(articleList([card(ARTICLE_A, { title: "Bài muộn của A" })])))
    await settle()
    expect(text()).toContain("Bài của bộ lọc B")
    expect(text()).not.toContain("Bài muộn của A")
    // The old request was aborted, not just ignored.
    const alpha = calls.find((call) => call.url.searchParams.get("q") === "alpha")
    expect(alpha?.init.signal?.aborted).toBe(true)
  })

  test("load more sends the cursor; changing filters drops it and restarts at page one", async () => {
    const { calls } = mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": ({ url }) =>
        url.searchParams.get("cursor") === "page-2"
          ? json(articleList([card(ARTICLE_B, { title: "Trang hai" })]))
          : json(articleList([card(ARTICLE_A, { title: "Trang một" })], "page-2")),
    })
    await settle()
    await click(byText("button", "Tải thêm tin"))
    expect(text()).toContain("Trang hai")
    await go("/news?sort=oldest")
    const listCalls = calls.filter((call) => call.url.pathname === "/api/v1/articles")
    expect(listCalls.map((call) => call.url.searchParams.get("cursor"))).toEqual([
      null,
      "page-2",
      null,
    ])
    expect(listCalls.at(-1)?.url.searchParams.get("sort")).toBe("oldest")
  })

  test("a rejected cursor reloads the feed from page one instead of looping", async () => {
    let first = 0
    const { calls } = mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": ({ url }) => {
        if (url.searchParams.get("cursor") !== null) return json(apiError("invalid_cursor"), 400)
        first += 1
        return json(articleList([card(ARTICLE_A, { title: `Lần ${first}` })], "stale"))
      },
    })
    await settle()
    await click(byText("button", "Tải thêm tin"))
    expect(text()).toContain("Lần 2")
    expect(calls.filter((call) => call.url.searchParams.get("cursor") === "stale")).toHaveLength(1)
  })

  test("empty corpus, no results and errors are distinct states; retry recovers", async () => {
    let broken = true
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets(0)),
      "GET /api/v1/articles": () =>
        broken ? json(apiError("catalog_unavailable"), 503) : json(articleList([])),
    })
    await settle()
    expect(text()).toContain("Kho tin tạm thời không phản hồi")
    broken = false
    await click(byText("button", "Tải lại danh sách"))
    expect(text()).toContain("Kho tin chưa có bài nào")
    await go("/news?topic=economy")
    expect(text()).toContain("Không có bài nào khớp bộ lọc")
  })

  test("facet counts come from the API, not from the loaded page", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets(137)),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    expect(text()).toContain("137 bài")
    await click(byText("button", "Bộ lọc"))
    const economy = byText("label.check", "Kinh tế")
    expect(economy.querySelector(".check-count")?.textContent).toContain("1")
  })

  test("calendar-day filters are sent as UTC instants with an offset", async () => {
    const { calls } = mountApp("/news?from=2026-10-29&to=2026-10-29", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    const list = calls.find((call) => call.url.pathname === "/api/v1/articles")
    expect(list?.url.searchParams.get("published_after")).toBe("2026-10-28T17:00:00Z")
    expect(list?.url.searchParams.get("published_before")).toBe("2026-10-29T17:00:00Z")
  })

  test("the feed's chat entry carries the whole filter and the API count, and creates nothing", async () => {
    const { count } = mountApp("/news?topic=economy&sort=oldest", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets(137)),
      "GET /api/v1/articles": () => json(articleList([card()], "next-page")),
    })
    await settle()
    const links = [...document.querySelectorAll<HTMLAnchorElement>("a.ask-link")]
    expect(links.length).toBeGreaterThan(0)
    for (const link of links) {
      expect(link.textContent).toContain("Hỏi về 137 bài đang lọc")
      expect(link.getAttribute("href")).toBe("/chat?scope=filtered&topic=economy")
    }
    expect(count("POST /api/v1/conversations")).toBe(0)
  })

  test("without a count the chat entry uses no number (never a guessed 0)", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(apiError("catalog_unavailable"), 503),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    expect(byText("a.ask-link", "Hỏi về toàn bộ tin").getAttribute("href")).toBe("/chat")
    expect(text()).toContain("Chưa đếm được")
    expect(text()).not.toContain("0 bài")
  })

  test("previews without provenance are not shown; no hero, no fetch-time line", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": () =>
        json(articleList([card(ARTICLE_A, { summary_preview: "Đoạn tóm tắt mô phỏng." })])),
    })
    await settle()
    expect(text()).not.toContain("Đoạn tóm tắt mô phỏng.")
    expect(text()).not.toContain("Tóm tắt tự động")
    expect(text()).not.toContain("Danh sách lấy lúc")
    expect(text()).not.toContain("Bản tin")
    expect(document.querySelector(".story-lead")).toBeNull()
  })

  test("stories are grouped by Vietnam day in server order; load more joins the same day", async () => {
    const ARTICLE_C = "33333333-3333-4333-8333-3333333333cc"
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets(3)),
      "GET /api/v1/articles": ({ url }) =>
        url.searchParams.get("cursor") === "p2"
          ? json(
              articleList([
                card(ARTICLE_B, { title: "Ba", published_at: "2026-10-28T18:00:00Z" }),
                card(ARTICLE_C, { title: "Bốn", published_at: "2026-10-27T10:00:00Z" }),
              ]),
            )
          : json(
              articleList(
                [card(ARTICLE_A, { title: "Một", published_at: "2026-10-29T03:00:00Z" })],
                "p2",
              ),
            ),
    })
    await settle()
    await click(byText("button", "Tải thêm tin"))
    const headings = [...document.querySelectorAll(".day-heading")].map((h) => h.textContent)
    // 28/10 18:00 UTC is already 29/10 in Vietnam: it joins the first section.
    expect(headings).toHaveLength(2)
    expect(headings[0]).toContain("29/10/2026")
    expect(headings[1]).toContain("27/10/2026")
    const titles = [...document.querySelectorAll(".story-link")].map((a) => a.textContent)
    expect(titles).toEqual(["Một", "Ba", "Bốn"])
  })

  test("relevance keeps one continuous ranking without day sections", async () => {
    mountApp("/news?q=cau", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": () =>
        json(
          articleList([
            card(ARTICLE_A, { title: "Cũ hơn", published_at: "2026-10-20T03:00:00Z" }),
            card(ARTICLE_B, { title: "Mới hơn", published_at: "2026-10-29T03:00:00Z" }),
          ]),
        ),
    })
    await settle()
    expect(document.querySelectorAll(".day-heading")).toHaveLength(0)
    const titles = [...document.querySelectorAll(".story-link")].map((a) => a.textContent)
    expect(titles).toEqual(["Cũ hơn", "Mới hơn"])
  })

  test("a filter that matches nothing keeps its chip and a way out", async () => {
    mountApp("/news?topic=economy", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets(0)),
      "GET /api/v1/articles": () => json(articleList([])),
    })
    await settle()
    expect(byText("button.chip", "Kinh tế").getAttribute("aria-label")).toBe("Bỏ lọc Kinh tế")
    await click(byText("button", "Bộ lọc"))
    // Inside the drawer the selection is listed even though the group is collapsed.
    expect(byText(".selected-chips button", "Kinh tế")).toBeDefined()
    await click(byText(".selected-chips button", "Kinh tế"))
    expect(window.location.search).toBe("")
  })

  test("the drawer puts time and sources first; labels start as one status line", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () =>
        json({
          ...facets(5),
          topics: [{ id: "economy", name: "Kinh tế", count: 0 }],
          topics_unclassified: 5,
        }),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    await click(byText("button", "Bộ lọc"))
    const legends = [...document.querySelectorAll(".drawer legend")].map((l) => l.textContent)
    expect(legends).toEqual(["Thời gian đăng", "Nguồn", "Chủ đề", "Địa phương"])
    const details = document.querySelector<HTMLDetailsElement>(".group-details")
    expect(details?.open).toBe(false)
    expect(text()).toContain("Chưa bài nào trong kết quả hiện tại có nhãn chủ đề")
    expect(text()).not.toContain("toàn kho")
  })

  test("sidebar source counts are the current result's facets, labelled as such", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/sources": () =>
        json({
          schema_version: "source_list.v1",
          items: [
            { id: SOURCE_ID, name: "Báo Mẫu", domain: "bao-mau.example", article_count: 999 },
          ],
        }),
      "GET /api/v1/article-facets": () => json(facets(42)),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    const button = byText(".source-filter", "Báo Mẫu")
    expect(button.textContent).toContain("42")
    expect(button.textContent).not.toContain("999")
    expect(text()).toContain("Số bài trong kết quả đang xem")
    await click(button)
    expect(window.location.search).toBe(`?source=${SOURCE_ID}`)
  })

  test("primary nav is Tin tức / Hỏi đáp; the analyst tool stays reachable from the footer", async () => {
    mountApp("/news", {
      ...taxonomy,
      "GET /api/v1/article-facets": () => json(facets()),
      "GET /api/v1/articles": () => json(articleList([card()])),
    })
    await settle()
    const nav = [...document.querySelectorAll(".site-nav a")].map((a) => a.textContent)
    expect(nav).toEqual(["Tin tức", "Hỏi đáp"])
    await click(byText("footer a", "Phân tích nâng cao"))
    expect(window.location.pathname).toBe("/analyst")
    expect(text()).toContain("Ask the evidence.")
  })
})
