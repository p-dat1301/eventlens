// Live browser suite against a running stack (real crud-api, rag-api, worker,
// PostgreSQL and the production Nginx web image), e.g. the demo started by
// scripts/news/demo.sh. Nothing here is stubbed except the two cases marked
// MOCK, which use Playwright routing to force a network failure.
//
//   EVENTLENS_E2E_BASE_URL   default http://127.0.0.1:18080
//   EVENTLENS_E2E_AI_MODE    none | fixture (what the stack runs; asserted, not changed)
//   EVENTLENS_BROWSER=chromium for Playwright's pinned Chromium (CI), else Chrome.
import { afterAll, beforeAll, describe, expect, test } from "bun:test"
import { type Browser, type BrowserContext, chromium, type Page } from "playwright-core"

const BASE = process.env["EVENTLENS_E2E_BASE_URL"] ?? "http://127.0.0.1:18080"
const MODE = process.env["EVENTLENS_E2E_AI_MODE"] ?? "fixture"

let browser: Browser

beforeAll(async () => {
  const executablePath = process.env["EVENTLENS_CHROME"]
  const bundled = process.env["EVENTLENS_BROWSER"] === "chromium"
  browser = await chromium.launch(
    executablePath ? { executablePath } : bundled ? {} : { channel: "chrome" },
  )
})

afterAll(async () => {
  await browser.close()
})

const newPage = async (width = 1280, context?: BrowserContext): Promise<Page> => {
  const page = context
    ? await context.newPage()
    : await browser.newPage({ viewport: { width, height: 900 } })
  return page
}

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)

const firstArticleId = async (): Promise<string> => {
  const response = await fetch(`${BASE}/api/v1/articles?limit=1`)
  const body = (await response.json()) as { items: { article_id: string }[] }
  const id = body.items[0]?.article_id
  if (id === undefined) throw new Error("corpus is empty: import articles first")
  return id
}

const ask = async (page: Page, question: string) => {
  await page.locator(".composer textarea").fill(question)
  await page.getByRole("button", { name: "Gửi" }).click()
}

const CONVERSATION_URL = /\/chat\/[0-9a-f-]{36}$/

/**
 * Article → "Hỏi về bài này" (a draft: nothing is created yet) → first
 * question, which creates the conversation and opens its thread.
 */
const startArticleConversation = async (page: Page, articleId: string, question: string) => {
  await page.goto(`${BASE}/articles/${articleId}`)
  await page.getByRole("link", { name: "Hỏi về bài này" }).click()
  await page.waitForURL(/\/chat\?article=/)
  await page.locator(".scope-chip").waitFor()
  await ask(page, question)
  await page.waitForURL(CONVERSATION_URL)
  await page.locator(".scope-box").waitFor()
}

const firstStory = (page: Page) => page.locator(".story-list .story-link").first()

describe("production proxy (real Nginx container)", () => {
  test.each([
    ["/api/v1/articles?limit=1", 200, "article_list.v1"],
    ["/api/v1/article-facets", 200, "article_facets.v1"],
    ["/api/v1/topics", 200, "topic_list.v1"],
    ["/api/v1/locations?limit=5", 200, "location_list.v1"],
    ["/api/v1/sources", 200, "source_list.v1"],
    ["/api/v1/conversations", 401, "api_error.v1"],
    ["/internal/v1/catalog/topics", 404, "api_error.v1"],
    ["/api/v1/not-a-route", 404, "api_error.v1"],
  ])("%s → %i JSON (never index.html)", async (path, status, schema) => {
    const response = await fetch(`${BASE}${path}`)
    expect(response.status).toBe(status)
    expect(response.headers.get("content-type")).toContain("application/json")
    expect(((await response.json()) as { schema_version: string }).schema_version).toBe(schema)
  })

  test("deep links load the app and survive a reload", async () => {
    const id = await firstArticleId()
    const page = await newPage()
    for (const path of [`/articles/${id}`, "/chat", "/news?sort=oldest"]) {
      await page.goto(`${BASE}${path}`)
      await page.reload()
      await page.locator("main#main-content h1").first().waitFor()
      expect(page.url()).toBe(`${BASE}${path}`)
    }
    await page.close()
  })
})

describe("news → filter → article", () => {
  test("filter by a source taken from the API, open an article, see its summary status", async () => {
    const page = await newPage()
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    await page.getByRole("button", { name: /^Bộ lọc/ }).click()
    const dialog = page.getByRole("dialog", { name: "Bộ lọc" })
    await dialog.waitFor()
    const firstSource = dialog
      .locator("fieldset", { hasText: "Nguồn" })
      .locator("label.check")
      .first()
    const sourceName = (await firstSource.locator(".check-label").textContent()) ?? ""
    await firstSource.click()
    await page.waitForURL(/source=/)
    await dialog.getByRole("button", { name: /^Xem/ }).click()
    await firstStory(page).waitFor()
    // Every listed story is from the chosen source.
    const names = await page.locator(".story .source-name").allTextContents()
    expect(names.length).toBeGreaterThan(0)
    expect(new Set(names)).toEqual(new Set([sourceName]))
    // The URL keeps the filter; reload reproduces it.
    await page.reload()
    await firstStory(page).waitFor()
    expect(await page.locator(".chip").allTextContents()).toEqual([`${sourceName}×`])

    await firstStory(page).click()
    await page.waitForURL(/\/articles\//)
    await page.locator("#summary-title").waitFor()
    const summary = (await page.locator(".quick-read").textContent()) ?? ""
    const facts = (await page.locator("details.article-facts").textContent()) ?? ""
    // Whatever the mode, a stored summary from the fixture generator is always
    // labelled as simulated; without one the status is pending/unavailable.
    if (facts.includes("fixture-summarizer")) expect(summary).toContain("Dữ liệu AI mô phỏng")
    else expect(summary).toMatch(/xếp hàng|đang chờ|Chưa có tóm tắt/)
    if (MODE === "fixture") expect(summary).toContain("Dữ liệu AI mô phỏng")
    expect(await overflow(page)).toBe(0)

    // Back restores the filtered feed.
    await page.goBack()
    await firstStory(page).waitFor()
    expect(page.url()).toContain("source=")
    await page.close()
  })

  test("Back from an article restores the feed position after 'load more'", async () => {
    const page = await newPage(1280)
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    await page.getByRole("button", { name: "Tải thêm tin" }).click()
    await page.locator(".story-list > li").nth(25).waitFor()
    const target = page.locator(".story-list > li").nth(22).locator(".story-link")
    await target.scrollIntoViewIfNeeded()
    const before = await page.evaluate(() => window.scrollY)
    await target.click()
    await page.waitForURL(/\/articles\//)
    await page.goBack()
    await page.locator(".story-list > li").nth(25).waitFor()
    await page.waitForTimeout(200)
    const after = await page.evaluate(() => window.scrollY)
    expect(Math.abs(after - before)).toBeLessThan(40)
    await page.close()
  })

  test("filters live in the URL: Back and Forward switch the feed", async () => {
    const page = await newPage()
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    await page.getByRole("combobox", { name: "Sắp xếp" }).selectOption("oldest")
    await page.waitForURL(/sort=oldest/)
    const oldest = await firstStory(page).textContent()
    await page.goBack()
    await page.waitForURL((url) => !url.search.includes("sort="))
    await firstStory(page).waitFor()
    expect(await firstStory(page).textContent()).not.toBe(oldest)
    await page.goForward()
    await page.waitForURL(/sort=oldest/)
    await page
      .locator(".story-list .story-link", { hasText: oldest ?? "" })
      .first()
      .waitFor()
    await page.close()
  })

  test("MOCK fixture catalog: topic + location + date combine into one request and labelled results", async () => {
    // Separate labelled test fixture (the real corpus has no trusted labels yet).
    const page = await newPage(1280)
    const seen: URL[] = []
    const labelled = {
      article_id: "11111111-1111-4111-8111-111111111111",
      article_version_id: `sha256:${"a".repeat(64)}`,
      upstream_version_id: null,
      title: "Bài mẫu có nhãn: giao thông Đà Nẵng",
      source: {
        id: "33333333-3333-4333-8333-333333333333",
        name: "Nguồn mẫu",
        domain: "mau.example",
      },
      url: "https://mau.example/a",
      published_at: "2026-10-29T02:00:00Z",
      topics: [{ id: "transportation", name: "Giao thông" }],
      locations: [{ id: "vn-da-nang", name: "Đà Nẵng", type: "municipality" }],
      summary_preview: null,
      processing_status: { metadata: "ready", summary: "unavailable" },
    }
    await page.route("**/api/v1/topics", (route) =>
      route.fulfill({
        json: {
          schema_version: "topic_list.v1",
          taxonomy_version: "t",
          items: [{ id: "transportation", name: "Giao thông", name_en: "Transportation" }],
        },
      }),
    )
    await page.route("**/api/v1/locations?*", (route) =>
      route.fulfill({
        json: {
          schema_version: "location_list.v1",
          gazetteer_version: "g",
          items: [
            {
              id: "vn-da-nang",
              name: "Đà Nẵng",
              type: "municipality",
              parent_id: "vn",
              aliases: ["da nang"],
              valid_until: null,
            },
          ],
        },
      }),
    )
    await page.route("**/api/v1/sources", (route) =>
      route.fulfill({ json: { schema_version: "source_list.v1", items: [] } }),
    )
    await page.route("**/api/v1/article-facets*", (route) =>
      route.fulfill({
        json: {
          schema_version: "article_facets.v1",
          semantics: "all_filters_applied",
          as_of: "2026-10-29T05:00:00Z",
          total: 1,
          topics: [{ id: "transportation", name: "Giao thông", count: 1 }],
          locations: [{ id: "vn-da-nang", name: "Đà Nẵng", count: 1, type: "municipality" }],
          sources: [],
          topics_unclassified: 0,
          locations_unknown: 0,
        },
      }),
    )
    await page.route("**/api/v1/articles?*", (route) => {
      seen.push(new URL(route.request().url()))
      return route.fulfill({
        json: {
          schema_version: "article_list.v1",
          items: [labelled],
          next_cursor: null,
          has_more: false,
          as_of: "2026-10-29T05:00:00Z",
          search_mode: "none",
        },
      })
    })
    await page.goto(
      `${BASE}/news?topic=transportation&location=vn-da-nang&from=2026-10-29&to=2026-10-29`,
    )
    await firstStory(page).waitFor()
    const last = seen.at(-1)
    expect(last?.searchParams.getAll("topic_ids")).toEqual(["transportation"])
    expect(last?.searchParams.getAll("location_ids")).toEqual(["vn-da-nang"])
    expect(last?.searchParams.get("published_after")).toBe("2026-10-28T17:00:00Z")
    expect(last?.searchParams.get("published_before")).toBe("2026-10-29T17:00:00Z")
    expect(await page.locator(".chip").allTextContents()).toEqual([
      "Giao thông×",
      "Đà Nẵng×",
      "Ngày 29/10/2026×",
    ])
    expect(await page.locator(".story .label-row").first().textContent()).toContain("Đà Nẵng")
    expect(await page.locator(".result-count").textContent()).toBe("1 bài")
    await page.close()
  })

  test("MOCK: catalog unreachable shows an error with retry, then recovers", async () => {
    const page = await newPage(390)
    let fail = true
    await page.route("**/api/v1/articles?*", (route) =>
      fail ? route.abort("connectionrefused") : route.continue(),
    )
    await page.goto(`${BASE}/news`)
    await page.getByRole("alert").getByText("Mất kết nối").waitFor()
    fail = false
    await page.getByRole("button", { name: "Tải lại danh sách" }).click()
    await firstStory(page).waitFor()
    await page.close()
  })
})

describe("chat on the real API", () => {
  test(`session → conversation → submit → poll → ${MODE === "fixture" ? "answer + citation" : "AI unavailable"}`, async () => {
    const id = await firstArticleId()
    const page = await newPage()
    await startArticleConversation(page, id, "Bài này nói về điều gì?")
    expect(await page.locator(".scope-box").textContent()).toContain("Phạm vi: Một bài")
    if (MODE === "fixture") {
      await page.locator(".answer").waitFor({ timeout: 30_000 })
      expect(await page.locator(".answer").textContent()).toContain("Dữ liệu AI mô phỏng")
      const cite = page.locator(".cite").first()
      await cite.click()
      await page.locator(".source-pane mark").waitFor()
      expect(await page.evaluate(() => document.activeElement?.id)).toBe("source-pane-title")
      await page.keyboard.press("Escape")
      await page.locator(".source-pane").waitFor({ state: "detached" })
      expect(await cite.evaluate((node) => node === document.activeElement)).toBe(true)
    } else {
      await page.getByText("AI chưa khả dụng").first().waitFor({ timeout: 30_000 })
    }
    await page.close()
  }, 60_000)

  test("mobile: citation opens a dialog, Escape closes it and focus returns", async () => {
    if (MODE !== "fixture") return
    const id = await firstArticleId()
    const page = await newPage(390)
    await startArticleConversation(page, id, "Bài này nói về điều gì?")
    await page.locator(".answer").waitFor({ timeout: 30_000 })
    // Keyboard activation: Playwright's click would re-scroll a target that sits
    // under the sticky composer, which is test-tool movement, not the app's.
    const source = page.locator(".source-item").first()
    await source.focus()
    const before = await page.evaluate(() => window.scrollY)
    await page.keyboard.press("Enter")
    const dialog = page.locator("dialog.source-dialog")
    await dialog.locator("mark").waitFor()
    expect(await overflow(page)).toBe(0)
    await page.keyboard.press("Escape")
    await dialog.waitFor({ state: "detached" })
    expect(await source.evaluate((node) => node === document.activeElement)).toBe(true)
    expect(Math.abs((await page.evaluate(() => window.scrollY)) - before)).toBeLessThan(40)
    await page.close()
  }, 60_000)

  test("the composer stays visible when the on-screen keyboard shrinks the viewport", async () => {
    const id = await firstArticleId()
    const page = await newPage(390)
    await startArticleConversation(page, id, "Bài này nói về điều gì?")
    // interactive-widget=resizes-content: the keyboard shrinks the layout viewport.
    await page.setViewportSize({ width: 390, height: 420 })
    await page.locator(".composer textarea").focus()
    const box = await page.locator(".composer").boundingBox()
    expect(box).not.toBeNull()
    expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual(421)
    await page.close()
  })

  test("reloading during a run resumes it from the API", async () => {
    const id = await firstArticleId()
    const page = await newPage()
    await startArticleConversation(page, id, "Tóm tắt các ý chính của bài?")
    if (MODE === "fixture") {
      await page.locator(".turn").first().waitFor()
      await page.reload()
      await page.locator(".turn-question", { hasText: "Tóm tắt các ý chính của bài?" }).waitFor()
      await page.locator(".answer").waitFor({ timeout: 30_000 })
    } else {
      // Mode none: the server refuses the run (503 model_unavailable), so a
      // reload shows no run — nothing was stored, nothing is faked.
      await page.getByText("AI chưa khả dụng").first().waitFor()
      // The rejected question went back into the box (not lost).
      expect(await page.locator(".composer textarea").inputValue()).toBe(
        "Tóm tắt các ý chính của bài?",
      )
      await page.reload()
      await page.locator(".scope-box").waitFor()
      expect(await page.locator(".turn").count()).toBe(0)
    }
    await page.close()
  }, 60_000)

  test("direct /chat: composer at once, first question creates exactly one conversation", async () => {
    const page = await newPage()
    const creates: string[] = []
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/v1/conversations"
      )
        creates.push(request.url())
    })
    await page.goto(`${BASE}/chat`)
    const box = page.locator(".composer textarea")
    await box.waitFor()
    expect(await page.locator(".scope-chip").textContent()).toContain("Tất cả tin")
    await box.focus()
    await box.fill("Có tin nào về giao thông?")
    expect(creates).toHaveLength(0)
    await page.getByRole("button", { name: "Gửi" }).dblclick()
    await page.waitForURL(CONVERSATION_URL)
    await page.locator(".scope-box").waitFor()
    expect(creates).toHaveLength(1)
    expect(await page.locator(".scope-box").textContent()).toContain("Phạm vi: Tất cả tin")
    if (MODE === "fixture") await page.locator(".answer").waitFor({ timeout: 30_000 })
    else await page.getByText("AI chưa khả dụng").first().waitFor({ timeout: 30_000 })
    await page.close()
  }, 60_000)

  test("ask about the whole filtered set: the scope is the filter, not the visible page", async () => {
    const page = await newPage()
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    const source = page.locator(".news-aside .source-filter").first()
    const name = (await source.locator(".source-filter-name").textContent()) ?? ""
    await source.click()
    await page.waitForURL(/source=/)
    await page.locator(".result-count", { hasText: "bài" }).waitFor()
    const link = page.locator(".news-aside a.ask-link")
    expect(await link.textContent()).toMatch(/Hỏi về [\d.]+ bài đang lọc/)
    let body: { scope?: { type?: string; filters?: Record<string, unknown> } } = {}
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname === "/api/v1/conversations"
      )
        body = request.postDataJSON()
    })
    await link.click()
    await page.waitForURL(/\/chat\?scope=filtered/)
    await ask(page, "Các bài của nguồn này nói gì?")
    await page.waitForURL(CONVERSATION_URL)
    await page.locator(".scope-box").waitFor()
    expect(body.scope?.type).toBe("filtered")
    expect(Object.keys(body.scope?.filters ?? {})).toEqual(["source_ids"])
    await page.locator(".scope-box", { hasText: name }).waitFor()
    // Changing the feed afterwards never changes this conversation's scope.
    await page.goto(`${BASE}/news`)
    await page.goBack()
    await page.locator(".scope-box", { hasText: name }).waitFor()
    await page.close()
  }, 60_000)

  test("two browser contexts cannot read each other's conversations", async () => {
    const id = await firstArticleId()
    const alice = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const bob = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const pageA = await newPage(1280, alice)
    await startArticleConversation(pageA, id, "Bài này nói về điều gì?")
    const url = pageA.url()
    const pageB = await newPage(1280, bob)
    await pageB.goto(url)
    await pageB.getByText("Không mở được hội thoại").first().waitFor()
    const listed = await pageB
      .locator(".conversation-link")
      .evaluateAll((links) => links.map((link) => link.getAttribute("href")))
    expect(listed).not.toContain(new URL(url).pathname)
    await alice.close()
    await bob.close()
  })
})

describe("layout, keyboard and motion", () => {
  test.each([320, 390, 768, 1280, 1440])("no horizontal page scroll at %ipx", async (width) => {
    const id = await firstArticleId()
    const page = await newPage(width)
    for (const path of ["/news", `/articles/${id}`, "/chat"]) {
      await page.goto(`${BASE}${path}`)
      await page.locator("main#main-content h1").first().waitFor()
      expect(await overflow(page)).toBe(0)
    }
    await page.goto(`${BASE}/news`)
    await page.getByRole("button", { name: /^Bộ lọc/ }).click()
    await page.getByRole("dialog", { name: "Bộ lọc" }).waitFor()
    expect(await overflow(page)).toBe(0)
    await page.close()
  })

  test("skip link, filter dialog Escape and focus return work from the keyboard", async () => {
    const page = await newPage(1280)
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    await page.keyboard.press("Tab")
    expect(await page.evaluate(() => document.activeElement?.textContent)).toBe(
      "Bỏ qua, đến nội dung chính",
    )
    await page.keyboard.press("Enter")
    expect(await page.evaluate(() => document.activeElement?.id)).toBe("main-content")
    const filters = page.getByRole("button", { name: /^Bộ lọc/ })
    await filters.focus()
    await page.keyboard.press("Enter")
    await page.getByRole("dialog", { name: "Bộ lọc" }).waitFor()
    await page.keyboard.press("Escape")
    await page.getByRole("dialog", { name: "Bộ lọc" }).waitFor({ state: "hidden" })
    expect(await filters.evaluate((node) => node === document.activeElement)).toBe(true)
    await page.close()
  })

  test("sticky nav and filter bar never cover a focused story link", async () => {
    const page = await newPage(390)
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    await page.mouse.wheel(0, 1600)
    const link = page.locator(".story-list .story-link").nth(8)
    await link.focus()
    const covered = await page.evaluate(() => {
      const active = document.activeElement as HTMLElement
      const rect = active.getBoundingClientRect()
      const bar = document.querySelector(".filter-bar")?.getBoundingClientRect()
      return bar !== undefined && rect.top < bar.bottom
    })
    expect(covered).toBe(false)
    await page.close()
  })

  test("reduced motion: content is visible immediately, no reveal animation runs", async () => {
    const context = await browser.newContext({
      reducedMotion: "reduce",
      viewport: { width: 1280, height: 900 },
    })
    const page = await context.newPage()
    await page.goto(`${BASE}/news`)
    await firstStory(page).waitFor()
    const animation = await page.evaluate(() => {
      const first = document.querySelector(".story-list > li")
      return first === null ? "missing" : getComputedStyle(first).animationName
    })
    expect(animation).toBe("none")
    expect(await firstStory(page).isVisible()).toBe(true)
    await context.close()
  })
})
