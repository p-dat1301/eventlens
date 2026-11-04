import { useEffect, useState } from "react"

import { type Scope, ScopeSchema } from "../api/chat"
import { useServices } from "../app/services"
import { formatDay, hcmDayFromUtc } from "../lib/dates"
import { EMPTY_FILTERS, type NewsFilters, serializeFilters } from "../lib/filters"
import { Link } from "../lib/router"
import { nameLookup, useTaxonomy } from "../news/taxonomy"
import type { DraftScope } from "./draft"

// The scope shown is always the one stored on the conversation
// (conversation.scope), never the feed's current filters.

export const parseScope = (raw: Readonly<Record<string, unknown>>): Scope | null => {
  const parsed = ScopeSchema.safeParse(raw)
  return parsed.success ? parsed.data : null
}

export const scopeKind = (scope: Scope | null): string => {
  if (scope === null) return "Phạm vi không xác định"
  if (scope.type === "all") return "Tất cả tin"
  if (scope.type === "filtered") return "Tin khớp bộ lọc"
  return (scope.articles?.length ?? 0) > 1 ? "Các bài đã chọn" : "Một bài"
}

/** Filters of a stored `filtered` scope, as a feed URL (to show the same set). */
export const scopeToFilters = (scope: Scope): NewsFilters => {
  const f = scope.filters ?? {}
  const from = f.published_after ? hcmDayFromUtc(f.published_after, false) : null
  const to = f.published_before ? hcmDayFromUtc(f.published_before, true) : null
  return {
    ...EMPTY_FILTERS,
    q: f.q ?? "",
    topics: f.topic_ids ?? [],
    locations: f.location_ids ?? [],
    sources: f.source_ids ?? [],
    from: from ?? "",
    to: to ?? "",
  }
}

/** Human description of a filter set, names resolved from the API taxonomy. */
export const describeFilters = (
  filters: NewsFilters,
  names: ReturnType<typeof nameLookup>,
): string[] =>
  [
    filters.q ? `từ khóa “${filters.q}”` : null,
    ...filters.topics.map((id) => `chủ đề ${names.topic(id)}`),
    ...filters.locations.map((id) => names.location(id)),
    ...filters.sources.map((id) => names.source(id)),
    filters.from ? `từ ${formatDay(filters.from)}` : null,
    filters.to ? `đến ${formatDay(filters.to)}` : null,
  ].filter((part): part is string => part !== null)

/** Short explanation of scope and citations, opened on demand (native, keyboard-operable). */
export const ScopeHelp = () => (
  <details className="scope-help">
    <summary>Phạm vi và trích dẫn</summary>
    <div className="scope-help-body">
      <p>
        Mỗi hội thoại giữ một phạm vi cố định: tất cả tin, toàn bộ tin khớp một bộ lọc, hoặc một
        bài. Câu trả lời chỉ tìm nguồn trong phạm vi đó; muốn hỏi phạm vi khác, hãy mở hội thoại
        mới.
      </p>
      <p>
        Mỗi ý trong câu trả lời dẫn số nguồn; bấm số để xem đúng đoạn trích trong bài. Câu trả lời
        do máy tạo, có thể sai — hãy đối chiếu với nguồn.
      </p>
    </div>
  </details>
)

/** Scope of a conversation not created yet (from the /chat URL). */
export const DraftScopeChip = ({ scope }: { readonly scope: DraftScope }) => {
  const { catalog } = useServices()
  const { state: taxonomy } = useTaxonomy(catalog)
  const names = nameLookup(taxonomy.status === "ready" ? taxonomy.taxonomy : null)
  if (scope.type === "all") {
    return (
      <p className="scope-chip-row">
        <span className="scope-chip">
          <span className="scope-chip-label">Phạm vi</span> Tất cả tin
        </span>
      </p>
    )
  }
  if (scope.type === "filtered") {
    return (
      <p className="scope-chip-row">
        <span className="scope-chip">
          <span className="scope-chip-label">Phạm vi</span> Tin khớp bộ lọc:{" "}
          {describeFilters(scope.filters, names).join(" · ")}
        </span>
        <span className="scope-chip-actions">
          <Link className="link" to={`/news${serializeFilters(scope.filters)}`}>
            Xem các tin
          </Link>
          <Link className="link" to="/chat">
            Hỏi trên tất cả tin
          </Link>
        </span>
      </p>
    )
  }
  return (
    <p className="scope-chip-row">
      <span className="scope-chip">
        <span className="scope-chip-label">Phạm vi</span> Một bài:{" "}
        <PinnedArticle articleId={scope.articleId} pinned={false} plain />
      </span>
      <span className="scope-chip-actions">
        <Link className="link" to="/chat">
          Hỏi trên tất cả tin
        </Link>
      </span>
    </p>
  )
}

export const ScopeSummary = ({ raw }: { readonly raw: Readonly<Record<string, unknown>> }) => {
  const { catalog } = useServices()
  const scope = parseScope(raw)
  const { state: taxonomy } = useTaxonomy(catalog)
  const names = nameLookup(taxonomy.status === "ready" ? taxonomy.taxonomy : null)

  if (scope === null) {
    return (
      <div className="scope-box">
        <span className="scope-kind">Phạm vi không xác định</span>
      </div>
    )
  }

  if (scope.type === "all") {
    return (
      <div className="scope-box">
        <span className="scope-kind">Phạm vi: Tất cả tin</span>
      </div>
    )
  }

  if (scope.type === "filtered") {
    const filters = scopeToFilters(scope)
    const parts = describeFilters(filters, names)
    return (
      <div className="scope-box">
        <span className="scope-kind">Phạm vi: Tin khớp bộ lọc</span>
        <span className="scope-detail">
          {parts.length > 0 ? parts.join(" · ") : "Bộ lọc trống"} — toàn bộ bài khớp tại thời điểm
          hỏi, không chỉ một trang.
        </span>
        <Link className="link scope-link" to={`/news${serializeFilters(filters)}`}>
          Xem các tin trong phạm vi
        </Link>
      </div>
    )
  }

  return (
    <div className="scope-box">
      <span className="scope-kind">Phạm vi: {scopeKind(scope)}</span>
      <ul className="scope-articles">
        {(scope.articles ?? []).map((pin) => (
          <li key={pin.article_id}>
            <PinnedArticle articleId={pin.article_id} pinned={pin.article_version_id != null} />
          </li>
        ))}
      </ul>
    </div>
  )
}

const PinnedArticle = ({
  articleId,
  pinned,
  plain = false,
}: {
  readonly articleId: string
  readonly pinned: boolean
  readonly plain?: boolean
}) => {
  const { catalog } = useServices()
  const [title, setTitle] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    void catalog.article(articleId, controller.signal).then((result) => {
      if (controller.signal.aborted) return
      if (result.ok) setTitle(result.data.article.title)
      else if (result.failure.kind === "api" && [404, 410].includes(result.failure.status))
        setMissing(true)
    })
    return () => controller.abort()
  }, [catalog, articleId])
  return (
    <>
      {missing ? (
        <span>Bài không còn trong kho tin</span>
      ) : plain ? (
        <span className="scope-chip-title">{title ?? "Đang tải tiêu đề…"}</span>
      ) : (
        <Link className="link" to={`/articles/${articleId}`}>
          {title ?? "Đang tải tiêu đề…"}
        </Link>
      )}
      {pinned ? <span className="scope-detail"> — đã ghim phiên bản lúc tạo hội thoại</span> : null}
    </>
  )
}
