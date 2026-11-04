import { z } from "zod"

import type { ApiResult, HttpClient } from "./http"

// Mirrors src/backend/shared/contracts/catalog.py (catalog.v1, served by
// crud-api). Objects are strict: an unknown field means the contract drifted.

const Id = z.guid()
const Timestamp = z.iso.datetime({ offset: true })
const Status = z.enum(["ready", "pending", "failed", "unavailable"])
export const VERSION_ID = /^sha256:[0-9a-f]{64}$/
const VersionId = z.string().regex(VERSION_ID)

const TopicLabel = z.strictObject({ id: z.string(), name: z.string() })
const LocationLabel = z.strictObject({ id: z.string(), name: z.string(), type: z.string() })
const SourceInfo = z.strictObject({ id: Id, name: z.string(), domain: z.string() })

export const ArticleCardSchema = z.strictObject({
  article_id: Id,
  article_version_id: VersionId,
  upstream_version_id: Id.nullable(),
  title: z.string(),
  source: SourceInfo,
  url: z.string(),
  published_at: Timestamp.nullable(),
  topics: z.array(TopicLabel),
  locations: z.array(LocationLabel),
  summary_preview: z.string().nullable(),
  processing_status: z.strictObject({ metadata: Status, summary: Status }),
})

export const ArticleListSchema = z.strictObject({
  schema_version: z.literal("article_list.v1"),
  items: z.array(ArticleCardSchema),
  next_cursor: z.string().nullable(),
  has_more: z.boolean(),
  as_of: Timestamp,
  search_mode: z.enum(["none", "lexical"]),
})

const FacetCount = z.strictObject({
  id: z.string(),
  name: z.string(),
  count: z.int().min(0),
  domain: z.string().nullable().optional(),
  type: z.string().nullable().optional(),
})

export const ArticleFacetsSchema = z.strictObject({
  schema_version: z.literal("article_facets.v1"),
  semantics: z.literal("all_filters_applied"),
  as_of: Timestamp,
  total: z.int().min(0),
  topics: z.array(FacetCount),
  locations: z.array(FacetCount),
  sources: z.array(FacetCount),
  topics_unclassified: z.int().min(0),
  locations_unknown: z.int().min(0),
})

const LabelProvenance = z.strictObject({
  kind: z.enum(["topic", "location"]),
  id: z.string(),
  provenance: z.enum(["enrichment", "manual"]),
  model_version: z.string().nullable(),
  confidence: z.number().nullable(),
  match_method: z.string(),
  review_status: z.enum(["unreviewed", "approved", "rejected"]),
})

export const ArticleDetailSchema = z.strictObject({
  schema_version: z.literal("article_detail.v1"),
  article: ArticleCardSchema,
  content: z.string().nullable(),
  content_policy: z.enum(["full", "withheld"]),
  ingested_at: Timestamp.nullable(),
  updated_at_source: Timestamp.nullable(),
  language: z.string().nullable(),
  version: z.strictObject({
    article_version_id: VersionId,
    upstream_version_id: Id.nullable(),
    revision_count: z.int().min(1),
    is_current: z.literal(true),
  }),
  labels: z.array(LabelProvenance),
})

const SupportingSpan = z
  .strictObject({
    start_offset: z.int().min(0),
    end_offset: z.int().min(1),
    excerpt: z.string(),
    span_fingerprint: z.string(),
  })
  .refine((span) => span.end_offset > span.start_offset, "span must be ordered")

export const ArticleSummarySchema = z.strictObject({
  schema_version: z.literal("article_summary.v1"),
  article_id: Id,
  article_version_id: z.string(),
  status: Status,
  source: z.literal("ai"),
  text: z.string().nullable(),
  key_points: z.array(
    z.strictObject({ text: z.string(), supporting_spans: z.array(SupportingSpan) }),
  ),
  generator: z
    .strictObject({
      model_id: z.string(),
      model_version: z.string(),
      prompt_version: z.string(),
      schema_version: z.string(),
      summary_policy_id: z.string(),
    })
    .nullable(),
  failure_code: z.string().nullable(),
  publisher_summary: z.string().nullable(),
  created_at: Timestamp.nullable(),
})

export const TopicListSchema = z.strictObject({
  schema_version: z.literal("topic_list.v1"),
  taxonomy_version: z.string(),
  items: z.array(z.strictObject({ id: z.string(), name: z.string(), name_en: z.string() })),
})

export const LocationSchema = z.strictObject({
  id: z.string(),
  name: z.string(),
  type: z.enum(["country", "municipality", "province", "former_province"]),
  parent_id: z.string().nullable(),
  aliases: z.array(z.string()),
  valid_until: z.string().nullable(),
})

export const LocationListSchema = z.strictObject({
  schema_version: z.literal("location_list.v1"),
  gazetteer_version: z.string().nullable(),
  items: z.array(LocationSchema),
})

export const SourceListSchema = z.strictObject({
  schema_version: z.literal("source_list.v1"),
  items: z.array(
    z.strictObject({ id: Id, name: z.string(), domain: z.string(), article_count: z.int().min(0) }),
  ),
})

export type ArticleCard = z.infer<typeof ArticleCardSchema>
export type ArticleList = z.infer<typeof ArticleListSchema>
export type ArticleFacets = z.infer<typeof ArticleFacetsSchema>
export type ArticleDetail = z.infer<typeof ArticleDetailSchema>
export type ArticleSummary = z.infer<typeof ArticleSummarySchema>
export type KeyPoint = ArticleSummary["key_points"][number]
export type TopicList = z.infer<typeof TopicListSchema>
export type Location = z.infer<typeof LocationSchema>
export type LocationList = z.infer<typeof LocationListSchema>
export type SourceList = z.infer<typeof SourceListSchema>

/** Wire filters of `/articles` and `/article-facets` (dates already UTC with offset). */
export type CatalogQuery = {
  readonly q?: string | undefined
  readonly topic_ids?: readonly string[]
  readonly location_ids?: readonly string[]
  readonly source_ids?: readonly string[]
  readonly published_after?: string | undefined
  readonly published_before?: string | undefined
}

export type ArticleSort = "latest" | "oldest" | "relevance"

export const MAX_LOCATIONS = 100

const withQuery = (query: CatalogQuery) => ({
  q: query.q,
  topic_ids: query.topic_ids,
  location_ids: query.location_ids,
  source_ids: query.source_ids,
  published_after: query.published_after,
  published_before: query.published_before,
})

export type CatalogClient = {
  readonly listArticles: (
    query: CatalogQuery,
    page: { readonly sort: ArticleSort; readonly cursor: string | null; readonly limit: number },
    signal?: AbortSignal,
  ) => Promise<ApiResult<ArticleList>>
  readonly facets: (query: CatalogQuery, signal?: AbortSignal) => Promise<ApiResult<ArticleFacets>>
  readonly article: (
    articleId: string,
    signal?: AbortSignal,
    versionId?: string,
  ) => Promise<ApiResult<ArticleDetail>>
  readonly summary: (articleId: string, signal?: AbortSignal) => Promise<ApiResult<ArticleSummary>>
  readonly topics: (signal?: AbortSignal) => Promise<ApiResult<TopicList>>
  readonly locations: (signal?: AbortSignal) => Promise<ApiResult<LocationList>>
  readonly sources: (signal?: AbortSignal) => Promise<ApiResult<SourceList>>
}

const pathId = (value: string) => encodeURIComponent(value)

export const createCatalogClient = (http: HttpClient): CatalogClient => ({
  listArticles: (query, page, signal) =>
    http.request("/api/v1/articles", ArticleListSchema, {
      query: { ...withQuery(query), sort: page.sort, cursor: page.cursor, limit: page.limit },
      signal,
    }),
  facets: (query, signal) =>
    http.request("/api/v1/article-facets", ArticleFacetsSchema, {
      query: withQuery(query),
      signal,
    }),
  article: (articleId, signal, versionId) =>
    http.request(`/api/v1/articles/${pathId(articleId)}`, ArticleDetailSchema, {
      query: { article_version_id: versionId },
      signal,
    }),
  summary: (articleId, signal) =>
    http.request(`/api/v1/articles/${pathId(articleId)}/summary`, ArticleSummarySchema, { signal }),
  topics: (signal) => http.request("/api/v1/topics", TopicListSchema, { signal }),
  // The gazetteer is small (country + 34 units + former provinces): one page.
  locations: (signal) =>
    http.request("/api/v1/locations", LocationListSchema, {
      query: { limit: MAX_LOCATIONS },
      signal,
    }),
  sources: (signal) => http.request("/api/v1/sources", SourceListSchema, { signal }),
})
