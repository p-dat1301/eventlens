// Real-browser layout and keyboard checks against the built app (`dist/`).
// The API is stubbed in-process with the same fixtures as the unit suite.
// Run with `bun run test:e2e` (uses installed Google Chrome, or set
// EVENTLENS_CHROME to a Chromium executable).
import { afterAll, beforeAll, describe, expect, test } from "bun:test"
import { join } from "node:path"
import { type Browser, chromium, type Page } from "playwright-core"

import {
  answeredResponse,
  errorBody,
  modelAbstainedResponse,
  noEvidenceResponse,
  partialCoverageResponse,
  REF_CLOSE,
  REF_REOPEN,
  sourceResponse,
} from "../src/test/fixtures"

const DIST = join(import.meta.dir, "..", "dist")
let flakyCalls = 0

const reply = (body: unknown, status = 200) => Response.json(body, { status })

const briefingFor = (query: string): Response => {
  if (query.includes("nothing")) return reply(noEvidenceResponse())
  if (query.includes("abstain")) return reply(modelAbstainedResponse())
  if (query.includes("partial")) return reply(partialCoverageResponse())
  if (query.includes("flaky")) {
    flakyCalls += 1
    return flakyCalls === 1 ? reply(errorBody("model_unavailable"), 503) : reply(answeredResponse())
  }
  return reply(answeredResponse())
}

const server = Bun.serve({
  port: 0,
  async fetch(request) {
    const url = new URL(request.url)
    if (url.pathname === "/briefing" && request.method === "POST") {
      const body: unknown = await request.json()
      const query = typeof body === "object" && body !== null ? Reflect.get(body, "query") : ""
      return briefingFor(String(query))
    }
    if (url.pathname === `/briefing/sources/${REF_CLOSE}`) return reply(sourceResponse())
    if (url.pathname === `/briefing/sources/${REF_REOPEN}`) {
      return reply(errorBody("source_stale"), 410)
    }
    const file = Bun.file(join(DIST, url.pathname === "/" ? "index.html" : url.pathname))
    return (await file.exists())
      ? new Response(file)
      : new Response(Bun.file(join(DIST, "index.html")))
  },
})

let browser: Browser

beforeAll(async () => {
  const executablePath = process.env["EVENTLENS_CHROME"]
  browser = await chromium.launch(executablePath ? { executablePath } : { channel: "chrome" })
})

afterAll(async () => {
  await browser.close()
  server.stop(true)
})

const open = async (width: number): Promise<Page> => {
  const page = await browser.newPage({ viewport: { width, height: 900 } })
  await page.goto(server.url.href)
  return page
}

const ask = async (page: Page, question: string) => {
  await page.getByLabel("Question").fill(question)
  await page.getByRole("button", { name: "Request briefing" }).click()
}

const horizontalOverflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)

const box = async (page: Page, selector: string) => {
  const rect = await page.locator(selector).boundingBox()
  if (rect === null) throw new Error(`${selector} not visible`)
  return rect
}

describe("320px viewport", () => {
  test("no horizontal scroll in idle, validation, answered, source, error and abstention states", async () => {
    const page = await open(320)
    expect(await horizontalOverflow(page)).toBe(0)

    await page.getByRole("button", { name: "Request briefing" }).click()
    await page.getByText("Enter a question").waitFor()
    expect(await horizontalOverflow(page)).toBe(0)

    await ask(page, "When did East Pier close and reopen?")
    await page.getByRole("heading", { name: "Answer" }).waitFor()
    expect(await horizontalOverflow(page)).toBe(0)

    await page.getByRole("button", { name: "Inspect source" }).first().click()
    await page.locator("#source-viewer mark").waitFor()
    expect(await horizontalOverflow(page)).toBe(0)
    const result = await box(page, ".result-panel")
    const viewer = await box(page, "#source-viewer")
    expect(viewer.y).toBeGreaterThanOrEqual(result.y + result.height)
    expect(viewer.x + viewer.width).toBeLessThanOrEqual(320)

    await page.getByRole("button", { name: "Inspect source" }).nth(1).click()
    await page.locator("#source-viewer").getByText("Source changed").waitFor()
    expect(await horizontalOverflow(page)).toBe(0)

    for (const question of ["nothing here", "abstain please", "partial please", "flaky again"]) {
      await ask(page, question)
      await page.locator(".briefing-result, .notice-error").first().waitFor()
      expect(await horizontalOverflow(page)).toBe(0)
    }
    await page.close()
  })
})

describe("desktop viewport", () => {
  test("keeps the two-column query and result/source layout", async () => {
    const page = await open(1280)
    const copy = await box(page, ".query-copy")
    const form = await box(page, ".query-form")
    expect(form.x).toBeGreaterThan(copy.x + copy.width - 1)

    await ask(page, "When did East Pier close and reopen?")
    await page.getByRole("button", { name: "Inspect source" }).first().click()
    await page.locator("#source-viewer mark").waitFor()
    const result = await box(page, ".result-panel")
    const viewer = await box(page, "#source-viewer")
    expect(viewer.x).toBeGreaterThan(result.x + result.width - 1)
    expect(await horizontalOverflow(page)).toBe(0)
    await page.close()
  })
})

describe("keyboard", () => {
  test("skip link, form, citation inspection and retry work without a pointer", async () => {
    const page = await open(1280)
    flakyCalls = 0

    await page.keyboard.press("Tab")
    expect(await page.evaluate(() => document.activeElement?.textContent)).toBe("Skip to content")

    await page.keyboard.press("Tab")
    expect(await page.evaluate(() => document.activeElement?.tagName)).toBe("TEXTAREA")
    await page.keyboard.type("flaky question")
    await page.keyboard.press("Tab")
    const outline = await page.evaluate(
      () => getComputedStyle(document.activeElement ?? document.body).outlineStyle,
    )
    expect(outline).toBe("solid")
    await page.keyboard.press("Enter")

    const retry = page.getByRole("button", { name: "Retry briefing" })
    await retry.waitFor()
    expect(await page.getByRole("alert").textContent()).toContain("Model unavailable")
    await retry.focus()
    await page.keyboard.press("Enter")
    await page.getByRole("heading", { name: "Answer" }).waitFor()

    const inspect = page.getByRole("button", { name: "Inspect source" }).first()
    await inspect.focus()
    await page.keyboard.press("Enter")
    await page.locator("#source-viewer mark").waitFor()
    expect(await page.evaluate(() => document.activeElement?.id)).toBe("source-viewer-title")

    await page.keyboard.press("Escape")
    await page.locator("#source-viewer").waitFor({ state: "detached" })
    expect(await inspect.evaluate((node) => node === document.activeElement)).toBe(true)
    await page.close()
  })
})
