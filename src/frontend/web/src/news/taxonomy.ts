import { useCallback, useEffect, useState } from "react"

import type {
  ArticleFacets,
  CatalogClient,
  Location,
  LocationList,
  SourceList,
  TopicList,
} from "../api/catalog"
import type { ApiFailure, ApiResult } from "../api/http"
import type { NewsFilters } from "../lib/filters"
import { toCatalogQuery } from "../lib/filters"

// Topic/location/source IDs and names always come from the API (never
// hardcoded). The three lists are small and change rarely: loaded once per
// page load and shared; a failure is retried on demand.

export type Taxonomy = {
  readonly topics: TopicList["items"]
  readonly locations: LocationList["items"]
  readonly sources: SourceList["items"]
}

export type TaxonomyState =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly taxonomy: Taxonomy }
  | { readonly status: "failed"; readonly failure: ApiFailure }

let shared: Promise<ApiResult<Taxonomy>> | null = null

const loadTaxonomy = (catalog: CatalogClient): Promise<ApiResult<Taxonomy>> => {
  if (shared !== null) return shared
  const attempt = Promise.all([catalog.topics(), catalog.locations(), catalog.sources()]).then(
    ([topics, locations, sources]): ApiResult<Taxonomy> => {
      if (!topics.ok) return topics
      if (!locations.ok) return locations
      if (!sources.ok) return sources
      return {
        ok: true,
        status: 200,
        data: {
          topics: topics.data.items,
          locations: locations.data.items,
          sources: sources.data.items,
        },
      }
    },
  )
  shared = attempt
  void attempt.then((result) => {
    if (!result.ok && shared === attempt) shared = null
  })
  return attempt
}

export const resetTaxonomyCache = () => {
  shared = null
}

export const useTaxonomy = (catalog: CatalogClient) => {
  const [state, setState] = useState<TaxonomyState>({ status: "loading" })
  const [attempt, setAttempt] = useState(0)
  // biome-ignore lint/correctness/useExhaustiveDependencies: attempt retries after a failure
  useEffect(() => {
    let active = true
    setState({ status: "loading" })
    void loadTaxonomy(catalog).then((result) => {
      if (!active) return
      setState(
        result.ok
          ? { status: "ready", taxonomy: result.data }
          : { status: "failed", failure: result.failure },
      )
    })
    return () => {
      active = false
    }
    // attempt: manual retry after a failure.
  }, [catalog, attempt])
  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { state, retry }
}

export const nameLookup = (taxonomy: Taxonomy | null) => {
  const topic = new Map(taxonomy?.topics.map((t) => [t.id, t.name]) ?? [])
  const location = new Map(taxonomy?.locations.map((l) => [l.id, l]) ?? [])
  const source = new Map(taxonomy?.sources.map((s) => [s.id, s.name]) ?? [])
  return {
    topic: (id: string) => topic.get(id) ?? id,
    location: (id: string) => location.get(id)?.name ?? id,
    locationEntry: (id: string): Location | undefined => location.get(id),
    // Unknown only once the list is loaded; before that, never claim it is gone.
    source: (id: string) =>
      source.get(id) ??
      (taxonomy === null ? "Đang tải tên nguồn…" : "Nguồn không còn trong danh mục"),
  }
}

export type FacetsState =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly facets: ArticleFacets }
  | { readonly status: "failed"; readonly failure: ApiFailure }

/** Facet counts for exactly the current filters (backend semantics: all filters applied). */
export const useFacets = (
  catalog: CatalogClient,
  filters: NewsFilters,
  key: string,
  invalid: boolean,
) => {
  const [state, setState] = useState<FacetsState>({ status: "loading" })
  const [attempt, setAttempt] = useState(0)
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed by the canonical filter key (filters read at request time); attempt retries
  useEffect(() => {
    if (invalid) return
    const controller = new AbortController()
    setState({ status: "loading" })
    void catalog.facets(toCatalogQuery(filters), controller.signal).then((result) => {
      if (controller.signal.aborted) return
      if (result.ok) setState({ status: "ready", facets: result.data })
      else if (result.failure.kind !== "cancelled") {
        setState({ status: "failed", failure: result.failure })
      }
    })
    return () => controller.abort()
    // `key` identifies the filter set; `filters` is read at request time only.
  }, [catalog, key, invalid, attempt])
  const retry = useCallback(() => setAttempt((n) => n + 1), [])
  return { state, retry }
}
