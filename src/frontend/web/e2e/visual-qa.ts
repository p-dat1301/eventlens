// Screenshot sweep for manual visual review (not an assertion suite).
// Output goes to a gitignored folder because it contains real article text.
//   bun e2e/visual-qa.ts <baseUrl> <outDir> <label>
import { mkdirSync } from "node:fs"
import { chromium, type Page } from "playwright-core"

const [base = "http://127.0.0.1:18080", out = "../../../.private/news-frontend-qa", label = "run"] =
  process.argv.slice(2)
const dir = `${out}/${label}`
mkdirSync(dir, { recursive: true })
const browser = await chromium.launch()
const results: string[] = []

const shot = async (page: Page, name: string) => {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  await page.screenshot({ path: `${dir}/${name}.png` })
  results.push(`${name}\toverflow=${overflow}`)
}

const open = async (width: number, path: string, height = 900, reducedMotion = false) => {
  const context = await browser.newContext({
    viewport: { width, height },
    reducedMotion: reducedMotion ? "reduce" : "no-preference",
  })
  const page = await context.newPage()
  await page.goto(base + path)
  return page
}

const articles = (await (await fetch(`${base}/api/v1/articles?limit=50`)).json()) as {
  items: { article_id: string; title: string }[]
}
const longest = [...articles.items].sort((a, b) => b.title.length - a.title.length)[0]
const first = articles.items[0]
if (first === undefined || longest === undefined) throw new Error("empty corpus")

for (const width of [320, 390, 768, 1280, 1440]) {
  const page = await open(width, "/news")
  await page.locator(".story-lead").waitFor()
  await shot(page, `news-${width}`)
  await page.getByRole("button", { name: /^Bộ lọc/ }).click()
  await page.getByRole("dialog").waitFor()
  await page.waitForTimeout(300)
  await shot(page, `news-filters-${width}`)
  await page.context().close()
}

{
  const page = await open(390, "/news?q=zzqqxxnotfound")
  await page.getByText("Không có bài nào khớp bộ lọc").waitFor()
  await shot(page, "news-noresults-390")
  await page.context().close()
}
{
  // MOCK: held response to capture the loading state.
  const context = await browser.newContext({ viewport: { width: 390, height: 900 } })
  const page = await context.newPage()
  await page.route("**/api/v1/articles?*", () => undefined)
  await page.goto(`${base}/news`)
  await page.waitForTimeout(400)
  await shot(page, "news-loading-390")
  await context.close()
}
{
  // MOCK: network failure to capture the error state.
  const context = await browser.newContext({ viewport: { width: 390, height: 900 } })
  const page = await context.newPage()
  await page.route("**/api/v1/articles?*", (route) => route.abort("connectionrefused"))
  await page.goto(`${base}/news`)
  await page.getByRole("alert").waitFor()
  await shot(page, "news-error-390")
  await context.close()
}
{
  const page = await open(1280, "/news", 900, true)
  await page.locator(".story-lead").waitFor()
  await page.keyboard.press("Tab")
  await page.keyboard.press("Enter")
  for (let i = 0; i < 5; i += 1) await page.keyboard.press("Tab")
  await shot(page, "news-reduced-motion-focus-1280")
  await page.context().close()
}

for (const width of [390, 768, 1280]) {
  const page = await open(width, `/articles/${longest.article_id}`)
  await page.locator("#summary-title").waitFor()
  await page.waitForTimeout(600)
  await shot(page, `article-long-title-${width}`)
  await page.context().close()
}
{
  // 200% zoom of a 1280px window = 640 CSS px.
  const page = await open(640, `/articles/${first.article_id}`)
  await page.locator("#summary-title").waitFor()
  await shot(page, "article-zoom200")
  const point = page.getByRole("button", { name: "Xem đoạn trong bài" }).first()
  if ((await point.count()) > 0) {
    await point.click()
    await page.waitForTimeout(800)
    await shot(page, "article-keypoint-highlight-640")
  }
  await page.mouse.wheel(0, 1200)
  await page.waitForTimeout(300)
  await shot(page, "article-reading-progress-640")
  await page.context().close()
}

for (const width of [390, 1280]) {
  const page = await open(width, "/chat")
  await page.locator("main h1").waitFor()
  await page.waitForTimeout(500)
  await shot(page, `chat-home-${width}`)
  await page.context().close()
}

for (const width of [390, 1280]) {
  const page = await open(width, `/articles/${first.article_id}`)
  await page.getByRole("button", { name: "Hỏi về bài này" }).click()
  await page.waitForURL(/\/chat\//)
  await page.getByLabel("Câu hỏi").fill("Bài này nói về điều gì?")
  await page.getByRole("button", { name: "Gửi" }).click()
  await page.locator(".answer, .callout").first().waitFor({ timeout: 30_000 })
  await page.waitForTimeout(400)
  await shot(page, `chat-thread-${width}`)
  const source = page.locator(".source-item").first()
  if ((await source.count()) > 0) {
    await source.focus()
    await page.keyboard.press("Enter")
    await page.locator("mark.cited-mark").waitFor()
    await page.waitForTimeout(300)
    await shot(page, `chat-source-${width}`)
  }
  await page.context().close()
}

await browser.close()
console.log(results.join("\n"))
