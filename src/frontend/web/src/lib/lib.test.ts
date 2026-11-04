import { describe, expect, test } from "bun:test"

import { codePoints, paragraphs, segmentParagraph, spanMatches } from "./code-points"
import { dayEndExclusiveUtc, dayStartUtc, formatDateTime, hcmDayFromUtc, parseDay } from "./dates"
import {
  EMPTY_FILTERS,
  effectiveSort,
  feedKey,
  parseFilters,
  serializeFilters,
  toCatalogQuery,
  toScopeFilters,
} from "./filters"
import { safeExternalUrl } from "./safe-url"

describe("Asia/Ho_Chi_Minh calendar days → UTC [after, before)", () => {
  test("a day starts at 17:00Z the previous day and ends exclusive at the next start", () => {
    expect(dayStartUtc("2026-10-29")).toBe("2026-10-28T17:00:00Z")
    expect(dayEndExclusiveUtc("2026-10-29")).toBe("2026-10-29T17:00:00Z")
  })

  test("month and year boundaries roll over", () => {
    expect(dayEndExclusiveUtc("2026-12-31")).toBe("2026-12-31T17:00:00Z")
    expect(dayStartUtc("2027-01-01")).toBe("2026-12-31T17:00:00Z")
  })

  test("impossible or malformed days are rejected", () => {
    expect(parseDay("2026-02-30")).toBeNull()
    expect(parseDay("29/10/2026")).toBeNull()
    expect(dayStartUtc("2026-13-01")).toBeNull()
  })

  test("stored UTC bounds map back to the chosen calendar days", () => {
    expect(hcmDayFromUtc("2026-10-28T17:00:00Z", false)).toBe("2026-10-29")
    expect(hcmDayFromUtc("2026-10-29T17:00:00Z", true)).toBe("2026-10-29")
  })

  test("display uses Vietnam time", () => {
    expect(formatDateTime("2026-10-28T17:30:00Z")).toBe("00:30, 29/10/2026")
    expect(formatDateTime(null)).toBe("Chưa rõ thời gian đăng")
  })
})

describe("feed filters in the URL", () => {
  test("round trip keeps every filter and repeats ids", () => {
    const search =
      "?q=c%E1%BA%A7u+R%E1%BB%93ng&topic=economy&topic=health&location=vn-da-nang&from=2026-10-01&to=2026-10-29&sort=oldest"
    const filters = parseFilters(search)
    expect(filters).toEqual({
      q: "cầu Rồng",
      topics: ["economy", "health"],
      locations: ["vn-da-nang"],
      sources: [],
      from: "2026-10-01",
      to: "2026-10-29",
      sort: "oldest",
    })
    expect(parseFilters(serializeFilters(filters))).toEqual(filters)
  })

  test("invalid ids, days and relevance without a query are dropped", () => {
    const filters = parseFilters(
      "?topic=Bad%20Id&source=not-a-uuid&from=2026-02-30&sort=relevance&location=vn-ha-noi",
    )
    expect(filters).toEqual({ ...EMPTY_FILTERS, locations: ["vn-ha-noi"] })
  })

  test("the request key ignores id order and makes the default sort explicit", () => {
    expect(feedKey(parseFilters("?topic=b&topic=a"))).toBe(
      feedKey(parseFilters("?topic=a&topic=b")),
    )
    expect(effectiveSort(parseFilters("?q=x"))).toBe("relevance")
    expect(effectiveSort(parseFilters(""))).toBe("latest")
  })

  test("calendar days become UTC instants with an offset on the wire", () => {
    expect(toCatalogQuery(parseFilters("?from=2026-10-29&to=2026-10-29"))).toMatchObject({
      published_after: "2026-10-28T17:00:00Z",
      published_before: "2026-10-29T17:00:00Z",
    })
  })

  test("a filtered chat scope is the filter definition only — no cursor, page or limit", () => {
    const scope = toScopeFilters(parseFilters("?topic=economy&from=2026-10-29&sort=oldest"))
    expect(scope).toEqual({ topic_ids: ["economy"], published_after: "2026-10-28T17:00:00Z" })
  })
})

describe("code-point offsets", () => {
  const text = "a😀b 𝔸 cầu Rồng"
  const points = codePoints(text)

  test("offsets after a non-BMP character differ from UTF-16 indices", () => {
    const start = points.indexOf("c")
    expect(start).toBe(6)
    expect(text.indexOf("c")).toBe(8)
    expect(
      spanMatches(points, { start_offset: start, end_offset: start + 3, excerpt: "cầu" }),
    ).toBe(true)
    // The same offsets applied with String.slice would miss the excerpt.
    expect(text.slice(start, start + 3)).not.toBe("cầu")
  })

  test("a span that does not match its excerpt or overruns is refused", () => {
    expect(spanMatches(points, { start_offset: 6, end_offset: 9, excerpt: "cau" })).toBe(false)
    expect(spanMatches(points, { start_offset: 6, end_offset: 99, excerpt: "cầu" })).toBe(false)
  })

  test("paragraph offsets and highlighted segments stay in code points", () => {
    const content = codePoints("Một 😀.\nHai ba bốn.")
    const blocks = paragraphs(content)
    expect(blocks.map((b) => b.text)).toEqual(["Một 😀.", "Hai ba bốn."])
    const second = blocks[1]
    if (second === undefined) throw new Error("missing paragraph")
    expect(second.start).toBe(7)
    expect(segmentParagraph(content, second, { start_offset: 11, end_offset: 13 })).toEqual([
      { text: "Hai ", marked: false },
      { text: "ba", marked: true },
      { text: " bốn.", marked: false },
    ])
  })
})

describe("external links", () => {
  test("only http(s) URLs are rendered as links", () => {
    expect(safeExternalUrl("https://vnexpress.net/a")).toBe("https://vnexpress.net/a")
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull()
    expect(safeExternalUrl("data:text/html,x")).toBeNull()
    expect(safeExternalUrl("/relative")).toBeNull()
  })
})
