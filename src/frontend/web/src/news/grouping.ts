import type { ArticleCard, ArticleSort } from "../api/catalog"
import { hcmDayOf } from "../lib/dates"

// Day sections of the feed. Only chronological sorts are grouped, and only by
// cutting the server order into consecutive runs of the same Vietnam calendar
// day: nothing is re-sorted, so the order of every page (and of "Tải thêm") is
// exactly the API's. A run that continues on the next page joins the same
// section. Relevance keeps one uninterrupted ranking.

export const UNKNOWN_DAY = "unknown"

export type FeedEntry = { readonly article: ArticleCard; readonly index: number }

export type FeedGroup = {
  /** Vietnam calendar day `YYYY-MM-DD`, `unknown` (no publish time) or `ranked`. */
  readonly day: string
  readonly entries: readonly FeedEntry[]
}

export const dayOf = (article: ArticleCard): string => {
  if (article.published_at === null) return UNKNOWN_DAY
  const ms = Date.parse(article.published_at)
  return Number.isNaN(ms) ? UNKNOWN_DAY : hcmDayOf(new Date(ms))
}

export const isChronological = (sort: ArticleSort): boolean => sort !== "relevance"

export const groupFeed = (items: readonly ArticleCard[], sort: ArticleSort): FeedGroup[] => {
  const entries = items.map((article, index) => ({ article, index }))
  if (!isChronological(sort)) return entries.length === 0 ? [] : [{ day: "ranked", entries }]
  const groups: { day: string; entries: FeedEntry[] }[] = []
  for (const entry of entries) {
    const day = dayOf(entry.article)
    const last = groups.at(-1)
    if (last !== undefined && last.day === day) last.entries.push(entry)
    else groups.push({ day, entries: [entry] })
  }
  return groups
}
