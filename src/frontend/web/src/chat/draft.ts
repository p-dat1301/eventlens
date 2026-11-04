import { VERSION_ID } from "../api/catalog"
import type { ScopeRequest } from "../api/chat"
import {
  EMPTY_FILTERS,
  hasActiveFilters,
  type NewsFilters,
  parseFilters,
  serializeFilters,
  toScopeFilters,
} from "../lib/filters"

// Scope of a conversation that does not exist yet. Entry points (feed, article)
// link to `/chat?…` with the scope in the URL; the conversation is created only
// when the first question is sent, so opening, reloading or leaving /chat never
// creates an empty conversation. Once created, the scope shown is always the
// server's (conversation.scope), never this draft.
//
//   /chat                                   → all
//   /chat?scope=filtered&topic=…&from=…     → filtered (same keys as /news)
//   /chat?article=<uuid>[&version=sha256:…] → one article (pinned version if given)

export type DraftScope =
  | { readonly type: "all" }
  | { readonly type: "filtered"; readonly filters: NewsFilters }
  | { readonly type: "article"; readonly articleId: string; readonly versionId: string | null }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export const parseDraftScope = (search: string): DraftScope => {
  const params = new URLSearchParams(search)
  const article = params.get("article")
  if (article !== null && UUID.test(article)) {
    const version = params.get("version")
    return {
      type: "article",
      articleId: article.toLowerCase(),
      versionId: version !== null && VERSION_ID.test(version) ? version : null,
    }
  }
  if (params.get("scope") === "filtered") {
    const filters = { ...parseFilters(search), sort: null }
    if (hasActiveFilters(filters)) return { type: "filtered", filters }
  }
  return { type: "all" }
}

export const draftHref = (scope: DraftScope): string => {
  if (scope.type === "all") return "/chat"
  if (scope.type === "article") {
    const params = new URLSearchParams({ article: scope.articleId })
    if (scope.versionId !== null) params.set("version", scope.versionId)
    return `/chat?${params.toString()}`
  }
  const filters = serializeFilters({ ...scope.filters, sort: null })
  return filters === "" ? "/chat" : `/chat?scope=filtered&${filters.slice(1)}`
}

/** Feed entry point: the whole filter set (never the loaded page), or everything. */
export const feedDraft = (filters: NewsFilters): DraftScope =>
  hasActiveFilters(filters)
    ? { type: "filtered", filters: { ...filters, sort: null } }
    : { type: "all" }

export const toScopeRequest = (scope: DraftScope): ScopeRequest => {
  if (scope.type === "all") return { type: "all" }
  if (scope.type === "filtered") {
    return { type: "filtered", filters: toScopeFilters({ ...EMPTY_FILTERS, ...scope.filters }) }
  }
  return {
    type: "articles",
    articles: [
      scope.versionId === null
        ? { article_id: scope.articleId }
        : { article_id: scope.articleId, article_version_id: scope.versionId },
    ],
  }
}
