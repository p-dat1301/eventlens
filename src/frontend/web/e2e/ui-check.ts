// Hands-on UI check for the 2026-11 remediation (not a CI suite): drives the
// real stack, clicks through the main flows and saves viewport screenshots to a
// gitignored folder (they contain article text).
//   bun e2e/ui-check.ts <baseUrl> <outDir>
import { mkdirSync } from "node:fs"
import { type Browser, chromium, type Page } from "playwright-core"

const [
  base = "http://127.0.0.1:18080",
  out = "../../../.private/news-frontend-qa/ui-remediation/after",
] = process.argv.slice(2)
mkdirSync(out, { recursive: true })
const browser: Browser = await chromium.launch()
const rows: string[] = []
const log = (name: string, ok: boolean, note = "") =>
  rows.push(`${ok ? "OK  " : "FAIL"} ${name}${note ? ` — ${note}` : ""}`)

const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
const shot = (page: Page, name: string) => page.screenshot({ path: `${out}/${name}.png` })

const open = async (
  width: number,
  path: string,
  opts: { scale?: number; reduced?: boolean; height?: number } = {},
) => {
  const context = await browser.newContext({
    viewport: { width, height: opts.height ?? 900 },
    deviceScaleFactor: opts.scale ?? 1,
    reducedMotion: opts.reduced ? "reduce" : "no-preference",
  })
  const page = await context.newPage()
  await page.goto(base + path)
  return page
}

const list = (await (await fetch(`${base}/api/v1/articles?limit=50`)).json()) as {
  items: { article_id: string; title: string }[]
}
const longest = [...list.items].sort((a, b) => b.title.length - a.title.length)[0]
if (longest === undefined) throw new Error("empty corpus")

// 1. Feed at each width (+ 200% zoom of 1280 = 640 CSS px at scale 2).
for (const [width, scale, label] of [
  [390, 1, "390"],
  [768, 1, "768"],
  [1280, 1, "1280"],
  [640, 2, "zoom200"],
] as const) {
  const page = await open(width, "/news", { scale })
  await page.locator(".story-list .story-link").first().waitFor()
  const firstTop = await page.locator(".story-list .story-link").first().boundingBox()
  const visible = await page.evaluate(
    () =>
      [...document.querySelectorAll(".story-list .story-link")].filter((node) => {
        const rect = node.getBoundingClientRect()
        return rect.top >= 0 && rect.bottom <= window.innerHeight
      }).length,
  )
  const titleSize = await page
    .locator(".story-title")
    .first()
    .evaluate((node) => getComputedStyle(node).fontSize)
  log(`feed ${label}: no horizontal scroll`, (await overflow(page)) === 0)
  log(
    `feed ${label}: stories fully visible on first screen`,
    visible >= 2,
    `${visible}, title ${titleSize}, first at y=${Math.round(firstTop?.y ?? 0)}`,
  )
  await shot(page, `feed-${label}`)
  // Drawer: time and sources first; topic collapsed.
  await page.getByRole("button", { name: /^Bộ lọc/ }).click()
  const dialog = page.getByRole("dialog", { name: "Bộ lọc" })
  await dialog.waitFor()
  const legends = await dialog.locator("legend").allTextContents()
  log(
    `drawer ${label}: order`,
    legends.join("|") === "Thời gian đăng|Nguồn|Chủ đề|Địa phương",
    legends.join("|"),
  )
  log(`drawer ${label}: no horizontal scroll`, (await overflow(page)) === 0)
  await shot(page, `drawer-${label}`)
  await page.keyboard.press("Escape")
  await page.context().close()
}

// 2. Quick "Nguồn" button focuses the sources group; pick one; chips; ask link.
{
  const page = await open(1280, "/news")
  await page.locator(".story-list .story-link").first().waitFor()
  await page.getByRole("button", { name: /^Nguồn/ }).click()
  await page.getByRole("dialog", { name: "Bộ lọc" }).waitFor()
  await page.waitForTimeout(100)
  const focusedIn = await page.evaluate(() =>
    document.activeElement?.closest("[data-section]")?.getAttribute("data-section"),
  )
  log("quick Nguồn focuses sources group", focusedIn === "sources", String(focusedIn))
  await page.keyboard.press("Space")
  await page.waitForURL(/source=/)
  await page.keyboard.press("Escape")
  const chips = await page.locator(".chip-row .chip").allTextContents()
  const ask = await page.locator(".news-aside a.ask-link").textContent()
  log(
    "source filter: chip + counted ask link",
    chips.length === 1 && /đang lọc/.test(ask ?? ""),
    `${chips} / ${ask}`,
  )
  // Topic filter with 0 results keeps its chip and a way out.
  await page.goto(`${base}/news?topic=economy`)
  await page.locator(".empty-state").waitFor()
  const chip = page.getByRole("button", { name: /^Bỏ lọc / })
  log(
    "0-result topic: chip + clear stay",
    (await chip.count()) === 1 &&
      (await page.getByRole("button", { name: "Xóa toàn bộ bộ lọc" }).count()) === 1,
  )
  await shot(page, "zero-topic-1280")
  await chip.click()
  await page.waitForURL((url) => url.search === "")
  await page.locator(".story-list .story-link").first().waitFor()
  log(
    "removing the chip restores the feed",
    (await page.locator(".story-list .story-link").count()) > 0,
  )
  await page.context().close()
}

// 3. Facets unavailable: no guessed counts.
{
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
  const page = await context.newPage()
  await page.route("**/api/v1/article-facets*", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        schema_version: "api_error.v1",
        error: { code: "upstream_unavailable", reason: null, request_id: "qa" },
      }),
    }),
  )
  await page.goto(`${base}/news`)
  await page.locator(".story-list .story-link").first().waitFor()
  await page.waitForTimeout(300)
  const count = await page.locator(".result-count").textContent()
  const ask = await page.locator(".news-aside a.ask-link").textContent()
  const sourceCounts = await page.locator(".source-filter-count").count()
  log(
    "facets down: no number claimed",
    count === "Chưa đếm được" && ask?.includes("toàn bộ tin") === true && sourceCounts === 0,
    `${count} / ${ask} / ${sourceCounts}`,
  )
  await shot(page, "facets-down-1280")
  await context.close()
}

// 4. Long title article: reading column, Nắm nhanh, disclosure, highlight below sticky nav.
for (const width of [390, 1280]) {
  const page = await open(width, `/articles/${longest.article_id}`)
  await page.locator("#summary-title").waitFor()
  await page.waitForTimeout(400)
  log(`article ${width}: no horizontal scroll`, (await overflow(page)) === 0)
  const measure = await page
    .locator(".article-text")
    .evaluate((node) => {
      const style = getComputedStyle(node)
      return `${Math.round(node.getBoundingClientRect().width)}px @ ${style.fontSize}`
    })
    .catch(() => "no text")
  log(`article ${width}: reading width`, true, measure)
  await shot(page, `article-long-${width}`)
  const jump = page.getByRole("button", { name: "Xem đoạn trong bài" }).first()
  if ((await jump.count()) > 0) {
    await jump.click()
    await page.waitForTimeout(900)
    const covered = await page.evaluate(() => {
      const mark = document.querySelector("mark.span-highlight")?.getBoundingClientRect()
      const nav = document.querySelector(".site-nav")?.getBoundingClientRect()
      return mark === undefined || nav === undefined
        ? "missing"
        : mark.top < nav.bottom
          ? "covered"
          : "visible"
    })
    log(`article ${width}: source highlight visible below nav`, covered === "visible", covered)
    await shot(page, `article-highlight-${width}`)
  }
  await page.locator("details.article-facts summary").click()
  log(
    `article ${width}: Thông tin bài opens`,
    await page.locator("details.article-facts").evaluate((d) => (d as HTMLDetailsElement).open),
  )
  await page.context().close()
}

// 5. Keyboard: skip link → feed link → article → back.
{
  const page = await open(1280, "/news")
  await page.locator(".story-list .story-link").first().waitFor()
  await page.keyboard.press("Tab")
  await page.keyboard.press("Enter")
  let tabs = 0
  while (tabs < 30) {
    await page.keyboard.press("Tab")
    tabs += 1
    const isStory = await page.evaluate(
      () => document.activeElement?.classList.contains("story-link") === true,
    )
    if (isStory) break
  }
  const ring = await page.evaluate(
    () => getComputedStyle(document.activeElement as Element).outlineStyle,
  )
  log(
    "keyboard reaches first story with a focus ring",
    ring !== "none",
    `${tabs} tabs, outline ${ring}`,
  )
  await page.keyboard.press("Enter")
  await page.waitForURL(/\/articles\//)
  await page.goBack()
  await page.locator(".story-list .story-link").first().waitFor()
  log("Back returns to the feed", new URL(page.url()).pathname === "/news")
  await page.context().close()
}

// 6. Reduced motion: no reveal animation, content visible.
{
  const page = await open(1280, "/news", { reduced: true })
  await page.locator(".story-list .story-link").first().waitFor()
  const anim = await page
    .locator(".story-list > li")
    .first()
    .evaluate((li) => getComputedStyle(li).animationName)
  log("reduced motion: no reveal", anim === "none", anim)
  await page.context().close()
}

// 7. /chat at 390 and 1280: composer on screen at once, not covered.
for (const width of [390, 1280]) {
  const page = await open(width, "/chat", { height: width === 390 ? 844 : 900 })
  const box = page.locator(".composer textarea")
  await box.waitFor()
  const rect = await box.boundingBox()
  const inView = rect !== null && rect.y + rect.height <= (width === 390 ? 844 : 900)
  log(`/chat ${width}: composer in first screen`, inView, `y=${Math.round(rect?.y ?? -1)}`)
  log(`/chat ${width}: no horizontal scroll`, (await overflow(page)) === 0)
  await page.getByText("Phạm vi và trích dẫn").click()
  await shot(page, `chat-home-${width}`)
  await page.context().close()
}

console.log(rows.join("\n"))
await browser.close()
