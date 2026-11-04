import type { ArticleSort, CatalogQuery } from "../api/catalog"
import type { ScopeFiltersRequest } from "../api/chat"
import { dayEndExclusiveUtc, dayStartUtc, parseDay } from "./dates"

// Feed filters live in the URL (`/news?q=&topic=&location=&source=&from=&to=&sort=`)
// so reload, Back/Forward and shared links reproduce the same feed. Dates are
// calendar days in Asia/Ho_Chi_Minh; the cursor is never part of the URL.

export type NewsFilters = {
  readonly q: string
  readonly topics: readonly string[]
  readonly locations: readonly string[]
  readonly sources: readonly string[]
  readonly from: string
  readonly to: string
  readonly sort: ArticleSort | null
}

export const EMPTY_FILTERS: NewsFilters = {
  q: "",
  topics: [],
  locations: [],
  sources: [],
  from: "",
  to: "",
  sort: null,
}

export const MAX_IDS = 20
export const MAX_QUERY = 200

const ID = /^[a-z0-9_-]{1,80}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const ids = (values: readonly string[], pattern: RegExp): readonly string[] =>
  [...new Set(values.filter((value) => pattern.test(value)))].sort().slice(0, MAX_IDS)

export const parseFilters = (search: string): NewsFilters => {
  const params = new URLSearchParams(search)
  const sortParam = params.get("sort")
  const q = (params.get("q") ?? "").slice(0, MAX_QUERY)
  const sort: ArticleSort | null =
    sortParam === "latest" || sortParam === "oldest" || sortParam === "relevance" ? sortParam : null
  const day = (value: string | null) => (value !== null && parseDay(value) !== null ? value : "")
  return {
    q,
    topics: ids(params.getAll("topic"), ID),
    locations: ids(params.getAll("location"), ID),
    sources: ids(params.getAll("source"), UUID),
    from: day(params.get("from")),
    to: day(params.get("to")),
    // relevance needs a query (backend: relevance_requires_query).
    sort: sort === "relevance" && q.trim() === "" ? null : sort,
  }
}

export const serializeFilters = (filters: NewsFilters): string => {
  const params = new URLSearchParams()
  if (filters.q.trim() !== "") params.set("q", filters.q.trim())
  for (const id of filters.topics) params.append("topic", id)
  for (const id of filters.locations) params.append("location", id)
  for (const id of filters.sources) params.append("source", id)
  if (filters.from !== "") params.set("from", filters.from)
  if (filters.to !== "") params.set("to", filters.to)
  if (filters.sort !== null) params.set("sort", filters.sort)
  const text = params.toString()
  return text === "" ? "" : `?${text}`
}

/** Sort sent to the API: explicit choice, else the backend default. */
export const effectiveSort = (filters: NewsFilters): ArticleSort =>
  filters.sort ?? (filters.q.trim() !== "" ? "relevance" : "latest")

export type DateRangeIssue = "inverted" | null

export const dateRangeIssue = (filters: NewsFilters): DateRangeIssue =>
  filters.from !== "" && filters.to !== "" && filters.from > filters.to ? "inverted" : null

/** Wire query for `/articles` and `/article-facets`. */
export const toCatalogQuery = (filters: NewsFilters): CatalogQuery => {
  const after = filters.from === "" ? null : dayStartUtc(filters.from)
  const before = filters.to === "" ? null : dayEndExclusiveUtc(filters.to)
  return {
    q: filters.q.trim() === "" ? undefined : filters.q.trim(),
    topic_ids: filters.topics,
    location_ids: filters.locations,
    source_ids: filters.sources,
    published_after: after ?? undefined,
    published_before: before ?? undefined,
  }
}

export const hasActiveFilters = (filters: NewsFilters): boolean =>
  filters.q.trim() !== "" ||
  filters.topics.length > 0 ||
  filters.locations.length > 0 ||
  filters.sources.length > 0 ||
  filters.from !== "" ||
  filters.to !== ""

/**
 * Scope filters for "ask about these articles": the same definition as the
 * feed (the backend resolves the *whole* matching set, never one page).
 */
export const toScopeFilters = (filters: NewsFilters): ScopeFiltersRequest => {
  const query = toCatalogQuery(filters)
  return {
    ...(query.q === undefined ? {} : { q: query.q }),
    ...(filters.topics.length > 0 ? { topic_ids: filters.topics } : {}),
    ...(filters.locations.length > 0 ? { location_ids: filters.locations } : {}),
    ...(filters.sources.length > 0 ? { source_ids: filters.sources } : {}),
    ...(query.published_after === undefined ? {} : { published_after: query.published_after }),
    ...(query.published_before === undefined ? {} : { published_before: query.published_before }),
  }
}

/** Stable key of the request a filter state produces (cache + stale-response guard). */
export const feedKey = (filters: NewsFilters): string =>
  `${serializeFilters({ ...filters, sort: effectiveSort(filters) })}`
