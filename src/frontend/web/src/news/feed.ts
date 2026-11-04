import { useCallback, useEffect, useReducer, useRef } from "react"

import type { ArticleCard, ArticleList, CatalogClient } from "../api/catalog"
import type { ApiFailure, ApiResult } from "../api/http"
import { effectiveSort, feedKey, type NewsFilters, toCatalogQuery } from "../lib/filters"

// Feed state for one filter key. Changing the key aborts every in-flight
// request of the old key and drops the cursor; a response is applied only if it
// belongs to the current key and the current request (A slow / B fast safe).
// The last few feeds are cached in memory so Back from an article restores the
// loaded pages (and then the scroll position) without refetching.

export const PAGE_SIZE = 20

export type FeedState = {
  readonly key: string
  readonly status: "loading" | "ready" | "failed"
  readonly items: readonly ArticleCard[]
  readonly nextCursor: string | null
  readonly hasMore: boolean
  readonly asOf: string | null
  readonly searchMode: ArticleList["search_mode"] | null
  readonly failure: ApiFailure | null
  readonly more: "idle" | "loading" | "failed"
  readonly moreFailure: ApiFailure | null
  /** Index of the first item of the last appended page (focus/reveal target). */
  readonly appendedAt: number | null
  readonly restored: boolean
}

type Action =
  | { readonly type: "start"; readonly key: string }
  | { readonly type: "restore"; readonly state: FeedState }
  | { readonly type: "loaded"; readonly key: string; readonly page: ArticleList }
  | { readonly type: "failed"; readonly key: string; readonly failure: ApiFailure }
  | { readonly type: "more"; readonly key: string }
  | { readonly type: "more_loaded"; readonly key: string; readonly page: ArticleList }
  | { readonly type: "more_failed"; readonly key: string; readonly failure: ApiFailure }

export const initialFeed = (key: string): FeedState => ({
  key,
  status: "loading",
  items: [],
  nextCursor: null,
  hasMore: false,
  asOf: null,
  searchMode: null,
  failure: null,
  more: "idle",
  moreFailure: null,
  appendedAt: null,
  restored: false,
})

export const feedReducer = (state: FeedState, action: Action): FeedState => {
  switch (action.type) {
    case "start":
      return initialFeed(action.key)
    case "restore":
      return { ...action.state, restored: true }
    case "loaded":
      if (action.key !== state.key) return state
      return {
        ...state,
        status: "ready",
        items: action.page.items,
        nextCursor: action.page.next_cursor,
        hasMore: action.page.has_more && action.page.next_cursor !== null,
        asOf: action.page.as_of,
        searchMode: action.page.search_mode,
        failure: null,
      }
    case "failed":
      if (action.key !== state.key) return state
      return { ...state, status: "failed", failure: action.failure }
    case "more":
      if (action.key !== state.key) return state
      return { ...state, more: "loading", moreFailure: null }
    case "more_loaded": {
      if (action.key !== state.key) return state
      // Pages are not snapshots: an article can move across a page boundary.
      const seen = new Set(state.items.map((item) => item.article_id))
      const fresh = action.page.items.filter((item) => !seen.has(item.article_id))
      return {
        ...state,
        items: [...state.items, ...fresh],
        nextCursor: action.page.next_cursor,
        hasMore: action.page.has_more && action.page.next_cursor !== null,
        more: "idle",
        appendedAt: fresh.length > 0 ? state.items.length : state.appendedAt,
        restored: false,
      }
    }
    case "more_failed":
      if (action.key !== state.key) return state
      return { ...state, more: "failed", moreFailure: action.failure }
  }
}

const CACHE_LIMIT = 6
const cache = new Map<string, FeedState>()

const remember = (state: FeedState) => {
  if (state.status !== "ready") return
  cache.delete(state.key)
  cache.set(state.key, state)
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value
    if (oldest === undefined) break
    cache.delete(oldest)
  }
}

export const clearFeedCache = () => cache.clear()

const isCursorRejected = (result: ApiResult<unknown>) =>
  !result.ok &&
  result.failure.kind === "api" &&
  (result.failure.code === "cursor_mismatch" || result.failure.code === "invalid_cursor")

export const useFeed = (catalog: CatalogClient, filters: NewsFilters, invalid: boolean) => {
  const key = feedKey(filters)
  const [state, dispatch] = useReducer(feedReducer, key, (k) => cache.get(k) ?? initialFeed(k))
  const pageAbort = useRef<AbortController | null>(null)
  const moreAbort = useRef<AbortController | null>(null)
  const filtersRef = useRef(filters)
  filtersRef.current = filters
  const [reloadToken, bumpReload] = useReducer((n: number) => n + 1, 0)
  const forceFresh = useRef(false)
  const reload = useCallback(() => {
    forceFresh.current = true
    bumpReload()
  }, [])

  useEffect(() => {
    remember(state)
  }, [state])

  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadToken re-runs page one for the same key (retry, cursor recovery)
  useEffect(() => {
    pageAbort.current?.abort()
    moreAbort.current?.abort()
    if (invalid) return
    const cached = forceFresh.current ? undefined : cache.get(key)
    forceFresh.current = false
    if (cached !== undefined) {
      dispatch({ type: "restore", state: cached })
      return
    }
    dispatch({ type: "start", key })
    const controller = new AbortController()
    pageAbort.current = controller
    const current = filtersRef.current
    void catalog
      .listArticles(
        toCatalogQuery(current),
        { sort: effectiveSort(current), cursor: null, limit: PAGE_SIZE },
        controller.signal,
      )
      .then((result) => {
        if (controller.signal.aborted) return
        if (result.ok) dispatch({ type: "loaded", key, page: result.data })
        else if (result.failure.kind !== "cancelled") {
          dispatch({ type: "failed", key, failure: result.failure })
        }
      })
    return () => controller.abort()
    // reloadToken: retry/cursor recovery re-runs this effect for the same key.
  }, [catalog, key, invalid, reloadToken])

  const loadMore = useCallback(() => {
    if (state.nextCursor === null || state.more === "loading") return
    moreAbort.current?.abort()
    const controller = new AbortController()
    moreAbort.current = controller
    const current = filtersRef.current
    const requestKey = state.key
    dispatch({ type: "more", key: requestKey })
    void catalog
      .listArticles(
        toCatalogQuery(current),
        { sort: effectiveSort(current), cursor: state.nextCursor, limit: PAGE_SIZE },
        controller.signal,
      )
      .then((result) => {
        if (controller.signal.aborted) return
        if (result.ok) dispatch({ type: "more_loaded", key: requestKey, page: result.data })
        else if (isCursorRejected(result)) {
          // The cursor no longer fits (filters changed, or the server restarted):
          // start this feed again from page one instead of looping on the error.
          cache.delete(requestKey)
          reload()
        } else if (result.failure.kind !== "cancelled") {
          dispatch({ type: "more_failed", key: requestKey, failure: result.failure })
        }
      })
  }, [catalog, reload, state.key, state.more, state.nextCursor])

  useEffect(
    () => () => {
      pageAbort.current?.abort()
      moreAbort.current?.abort()
    },
    [],
  )

  const retry = useCallback(() => {
    cache.delete(key)
    reload()
  }, [key, reload])

  return { state: state.key === key ? state : initialFeed(key), loadMore, retry }
}
