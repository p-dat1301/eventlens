import { describe, expect, test } from "bun:test"

import type { ArticleCard } from "../api/catalog"
import { ARTICLE_A, ARTICLE_B, card } from "../test/news-fixtures"
import { groupFeed, UNKNOWN_DAY } from "./grouping"

const at = (id: string, published_at: string | null) =>
  card(id, { published_at }) as unknown as ArticleCard

describe("groupFeed", () => {
  test("cuts server order into consecutive Vietnam days without reordering", () => {
    const items = [
      at(ARTICLE_A, "2026-10-29T16:59:00Z"), // 23:59 on the 29th
      at(ARTICLE_B, "2026-10-29T17:00:00Z"), // 00:00 on the 30th — server order kept anyway
      at("33333333-3333-4333-8333-333333333333", null),
    ]
    const groups = groupFeed(items, "oldest")
    expect(groups.map((g) => g.day)).toEqual(["2026-10-29", "2026-10-30", UNKNOWN_DAY])
    expect(groups.flatMap((g) => g.entries.map((e) => e.index))).toEqual([0, 1, 2])
  })

  test("an appended page continuing the same day joins its section", () => {
    const page1 = [at(ARTICLE_A, "2026-10-29T05:00:00Z")]
    const page2 = [at(ARTICLE_B, "2026-10-29T01:00:00Z")]
    expect(groupFeed([...page1, ...page2], "latest")).toHaveLength(1)
  })

  test("relevance is one ranked group", () => {
    const groups = groupFeed(
      [at(ARTICLE_A, "2026-10-20T00:00:00Z"), at(ARTICLE_B, "2026-10-29T00:00:00Z")],
      "relevance",
    )
    expect(groups).toHaveLength(1)
    expect(groups[0]?.entries.map((e) => e.article.article_id)).toEqual([ARTICLE_A, ARTICLE_B])
  })
})
