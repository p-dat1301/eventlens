import { type FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react"

import type { ArticleSort } from "../api/catalog"
import { useServices } from "../app/services"
import { FailureCallout, LoadingLine } from "../app/ui"
import { draftHref, feedDraft } from "../chat/draft"
import { formatDay, formatDayHeading } from "../lib/dates"
import {
  dateRangeIssue,
  EMPTY_FILTERS,
  effectiveSort,
  hasActiveFilters,
  MAX_IDS,
  MAX_QUERY,
  type NewsFilters,
  parseFilters,
  serializeFilters,
} from "../lib/filters"
import { Link, navigate, useDocumentTitle, useLocation, useScrollRestoration } from "../lib/router"
import { ArticleItem } from "./ArticleItem"
import { type DrawerSection, FilterDrawer } from "./FilterDrawer"
import { type FeedState, useFeed } from "./feed"
import { groupFeed, UNKNOWN_DAY } from "./grouping"
import {
  type FacetsState,
  nameLookup,
  type TaxonomyState,
  useFacets,
  useTaxonomy,
} from "./taxonomy"

const SORT_LABELS: Readonly<Record<ArticleSort, string>> = {
  latest: "Mới nhất",
  oldest: "Cũ nhất",
  relevance: "Liên quan nhất",
}

export const NewsPage = () => {
  const { catalog } = useServices()
  const location = useLocation()
  const filters = useMemo(() => parseFilters(location.search), [location.search])
  const invalid = dateRangeIssue(filters) !== null
  const { state: feed, loadMore, retry } = useFeed(catalog, filters, invalid)
  const facetKey = serializeFilters({ ...filters, sort: null })
  const { state: facets, retry: retryFacets } = useFacets(catalog, filters, facetKey, invalid)
  const { state: taxonomy, retry: retryTaxonomy } = useTaxonomy(catalog)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [drawerSection, setDrawerSection] = useState<DrawerSection | null>(null)
  const drawerSession = useRef(false)
  const names = nameLookup(taxonomy.status === "ready" ? taxonomy.taxonomy : null)

  useDocumentTitle(filters.q.trim() === "" ? "Tin tức" : `Tìm “${filters.q.trim()}”`)
  useScrollRestoration(location, feed.status !== "loading")

  // One history entry per drawer session: the first change pushes, the
  // following ones replace, so Back leaves the drawer's edits in one step.
  const apply = useCallback(
    (next: NewsFilters) => {
      const target = `/news${serializeFilters(next)}`
      if (drawerOpen && drawerSession.current) navigate(target, { replace: true })
      else {
        navigate(target)
        if (drawerOpen) drawerSession.current = true
      }
    },
    [drawerOpen],
  )

  const openDrawer = (section: DrawerSection | null) => {
    drawerSession.current = false
    setDrawerSection(section)
    setDrawerOpen(true)
  }

  const chips = activeChips(filters, names)
  const total = facets.status === "ready" ? facets.facets.total : null
  const filtered = hasActiveFilters(filters)
  const sort = effectiveSort(filters)

  return (
    <div className="news-page">
      <div className="shell-width news-head">
        <h1 className="page-title news-title">Tin tức</h1>
        <SearchForm key={filters.q} filters={filters} onSearch={apply} />
      </div>

      <section className="filter-bar" aria-label="Lọc và sắp xếp">
        <div className="filter-bar-inner shell-width">
          <p className="result-count" aria-live="polite">
            {total === null
              ? facets.status === "failed"
                ? "Chưa đếm được"
                : "Đang đếm…"
              : `${total.toLocaleString("vi-VN")} bài`}
          </p>
          <div className="filter-quick">
            <button
              className="btn btn-small"
              type="button"
              onClick={() => openDrawer("time")}
              aria-haspopup="dialog"
            >
              Thời gian{filters.from !== "" || filters.to !== "" ? " •" : ""}
            </button>
            <button
              className="btn btn-small"
              type="button"
              onClick={() => openDrawer("sources")}
              aria-haspopup="dialog"
            >
              Nguồn{filters.sources.length > 0 ? ` (${filters.sources.length})` : ""}
            </button>
            <button
              className="btn btn-small"
              type="button"
              onClick={() => openDrawer(null)}
              aria-haspopup="dialog"
            >
              Bộ lọc{chips.length > 0 ? ` (${chips.length})` : ""}
            </button>
          </div>
          <SortSelect filters={filters} onChange={apply} />
        </div>
      </section>

      <div className="shell-width news-layout">
        <div className="news-body">
          {chips.length > 0 ? (
            <div className="chip-row">
              <span className="chip-row-label">Đang lọc:</span>
              <ul className="chips">
                {chips.map((chip) => (
                  <li key={chip.key}>
                    <button
                      className="chip"
                      type="button"
                      onClick={() => apply(chip.remove())}
                      aria-label={`Bỏ lọc ${chip.label}`}
                    >
                      {chip.label}
                      <span aria-hidden="true">×</span>
                    </button>
                  </li>
                ))}
              </ul>
              <button
                className="btn btn-quiet btn-small"
                type="button"
                onClick={() =>
                  apply({
                    ...EMPTY_FILTERS,
                    sort: filters.sort === "relevance" ? null : filters.sort,
                  })
                }
              >
                Xóa bộ lọc
              </button>
            </div>
          ) : null}

          {/* Below the desktop breakpoint the sidebar is hidden: one compact entry point. */}
          {invalid ? null : (
            <p className="ask-inline">
              <AskLink filters={filters} total={total} />
            </p>
          )}

          {invalid ? (
            <div className="callout callout-warning" role="alert">
              <h2>Khoảng thời gian không hợp lệ</h2>
              <p>Ngày bắt đầu đang sau ngày kết thúc. Mở bộ lọc để chỉnh lại.</p>
            </div>
          ) : (
            <Feed
              feed={feed}
              sort={sort}
              filtered={filtered}
              onRetry={() => {
                retry()
                retryFacets()
              }}
              onLoadMore={loadMore}
              onClear={() => apply(EMPTY_FILTERS)}
              facetsNote={
                facets.status === "ready" &&
                feed.status === "ready" &&
                feed.items.length === 0 &&
                (filters.topics.length > 0 || filters.locations.length > 0)
              }
            />
          )}
        </div>

        <aside className="news-aside" aria-label="Công cụ trang tin">
          {invalid ? null : <AskCard filters={filters} total={total} />}
          <SourcesCard
            filters={filters}
            onChange={apply}
            taxonomy={taxonomy}
            facets={facets}
            onRetry={() => {
              retryTaxonomy()
              retryFacets()
            }}
          />
        </aside>
      </div>

      <FilterDrawer
        open={drawerOpen}
        section={drawerSection}
        onClose={() => setDrawerOpen(false)}
        filters={filters}
        onChange={apply}
        taxonomy={taxonomy}
        onRetryTaxonomy={retryTaxonomy}
        facets={facets}
      />
    </div>
  )
}

// Contextual chat entry: the whole filter set (never the loaded page). The
// number is shown only when the API counted exactly this filter set.
const askLabel = (filtered: boolean, total: number | null): string => {
  if (total === null) return filtered ? "Hỏi về các bài đang lọc" : "Hỏi về toàn bộ tin"
  const n = total.toLocaleString("vi-VN")
  return filtered ? `Hỏi về ${n} bài đang lọc` : `Hỏi về ${n} bài`
}

const AskLink = ({
  filters,
  total,
}: {
  readonly filters: NewsFilters
  readonly total: number | null
}) => (
  <Link className="btn btn-accent btn-small ask-link" to={draftHref(feedDraft(filters))}>
    {askLabel(hasActiveFilters(filters), total)}
    <span aria-hidden="true"> →</span>
  </Link>
)

const AskCard = ({
  filters,
  total,
}: {
  readonly filters: NewsFilters
  readonly total: number | null
}) => (
  <section className="aside-card" aria-labelledby="ask-card-title">
    <h2 id="ask-card-title" className="aside-title">
      Hỏi đáp
    </h2>
    <p className="aside-text">
      {hasActiveFilters(filters)
        ? "Hỏi về toàn bộ bài khớp bộ lọc này, không chỉ các bài đang hiện trên trang."
        : "Đặt câu hỏi về các tin trong kho; câu trả lời dẫn đoạn trích nguồn."}
    </p>
    <AskLink filters={filters} total={total} />
  </section>
)

// Sources of the current result set. Counts are facet counts for exactly the
// active filters; while they load or after a failure no number is shown
// (never a guessed 0).
const SourcesCard = ({
  filters,
  onChange,
  taxonomy,
  facets,
  onRetry,
}: {
  readonly filters: NewsFilters
  readonly onChange: (next: NewsFilters) => void
  readonly taxonomy: TaxonomyState
  readonly facets: FacetsState
  readonly onRetry: () => void
}) => {
  if (taxonomy.status !== "ready") {
    return taxonomy.status === "failed" ? (
      <section className="aside-card" aria-labelledby="sources-card-title">
        <h2 id="sources-card-title" className="aside-title">
          Nguồn báo
        </h2>
        <p className="aside-text">Chưa tải được danh sách nguồn.</p>
        <button className="btn btn-small" type="button" onClick={onRetry}>
          Thử lại
        </button>
      </section>
    ) : null
  }
  const sources = taxonomy.taxonomy.sources
  if (sources.length === 0) return null
  const counts =
    facets.status === "ready"
      ? new Map(facets.facets.sources.map((item) => [item.id, item.count]))
      : null
  const toggle = (id: string) =>
    onChange({
      ...filters,
      sources: filters.sources.includes(id)
        ? filters.sources.filter((value) => value !== id)
        : [...filters.sources, id].sort().slice(0, MAX_IDS),
    })
  return (
    <section className="aside-card" aria-labelledby="sources-card-title">
      <h2 id="sources-card-title" className="aside-title">
        Nguồn báo
      </h2>
      <p className="aside-note">
        {facets.status === "ready"
          ? "Số bài trong kết quả đang xem"
          : facets.status === "failed"
            ? "Chưa đếm được số bài theo bộ lọc"
            : "Đang đếm số bài…"}
      </p>
      <ul className="source-filter-list">
        {sources.map((source) => {
          const selected = filters.sources.includes(source.id)
          const count = counts === null ? null : (counts.get(source.id) ?? 0)
          return (
            <li key={source.id}>
              <button
                className="source-filter"
                type="button"
                aria-pressed={selected}
                onClick={() => toggle(source.id)}
              >
                <span className="source-filter-name">{source.name}</span>
                {count === null ? null : (
                  <span className="source-filter-count">
                    {count.toLocaleString("vi-VN")}
                    <span className="visually-hidden"> bài</span>
                  </span>
                )}
              </button>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

const SearchForm = ({
  filters,
  onSearch,
}: {
  readonly filters: NewsFilters
  readonly onSearch: (next: NewsFilters) => void
}) => {
  const [text, setText] = useState(filters.q)
  const inputId = useId()
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const q = text.trim().slice(0, MAX_QUERY)
    onSearch({
      ...filters,
      q,
      sort: q === "" && filters.sort === "relevance" ? null : filters.sort,
    })
  }
  return (
    <search className="search-form">
      <form className="search-form-inner" onSubmit={submit}>
        <label className="visually-hidden" htmlFor={inputId}>
          Tìm trong tin
        </label>
        <input
          id={inputId}
          className="search-input"
          type="search"
          name="q"
          value={text}
          maxLength={MAX_QUERY}
          placeholder="Tìm theo từ khóa…"
          onChange={(event) => setText(event.target.value)}
        />
        <button className="btn btn-primary" type="submit">
          Tìm
        </button>
      </form>
    </search>
  )
}

const SortSelect = ({
  filters,
  onChange,
}: {
  readonly filters: NewsFilters
  readonly onChange: (next: NewsFilters) => void
}) => {
  const id = useId()
  const hasQuery = filters.q.trim() !== ""
  const value = filters.sort ?? (hasQuery ? "relevance" : "latest")
  return (
    <div className="sort-control">
      <label htmlFor={id}>Sắp xếp</label>
      <select
        id={id}
        value={value}
        onChange={(event) => {
          const next = event.target.value as ArticleSort
          const isDefault = next === (hasQuery ? "relevance" : "latest")
          onChange({ ...filters, sort: isDefault ? null : next })
        }}
      >
        <option value="latest">{SORT_LABELS.latest}</option>
        <option value="oldest">{SORT_LABELS.oldest}</option>
        {hasQuery ? <option value="relevance">{SORT_LABELS.relevance}</option> : null}
      </select>
    </div>
  )
}

type Chip = { readonly key: string; readonly label: string; readonly remove: () => NewsFilters }

const activeChips = (filters: NewsFilters, names: ReturnType<typeof nameLookup>): Chip[] => {
  const chips: Chip[] = []
  if (filters.q.trim() !== "") {
    chips.push({
      key: "q",
      label: `“${filters.q.trim()}”`,
      remove: () => ({ ...filters, q: "", sort: null }),
    })
  }
  for (const id of filters.topics) {
    chips.push({
      key: `t-${id}`,
      label: names.topic(id),
      remove: () => ({ ...filters, topics: filters.topics.filter((v) => v !== id) }),
    })
  }
  for (const id of filters.locations) {
    chips.push({
      key: `l-${id}`,
      label: names.location(id),
      remove: () => ({ ...filters, locations: filters.locations.filter((v) => v !== id) }),
    })
  }
  for (const id of filters.sources) {
    chips.push({
      key: `s-${id}`,
      label: names.source(id),
      remove: () => ({ ...filters, sources: filters.sources.filter((v) => v !== id) }),
    })
  }
  if (filters.from !== "" || filters.to !== "") {
    const label =
      filters.from !== "" && filters.to !== ""
        ? filters.from === filters.to
          ? `Ngày ${formatDay(filters.from)}`
          : `${formatDay(filters.from)} – ${formatDay(filters.to)}`
        : filters.from !== ""
          ? `Từ ${formatDay(filters.from)}`
          : `Đến ${formatDay(filters.to)}`
    chips.push({ key: "date", label, remove: () => ({ ...filters, from: "", to: "" }) })
  }
  return chips
}

type FeedProps = {
  readonly feed: FeedState
  readonly sort: ArticleSort
  readonly filtered: boolean
  readonly onRetry: () => void
  readonly onLoadMore: () => void
  readonly onClear: () => void
  readonly facetsNote: boolean
}

const Feed = ({ feed, sort, filtered, onRetry, onLoadMore, onClear, facetsNote }: FeedProps) => {
  const firstNew = useRef<HTMLLIElement>(null)
  const lastFocused = useRef<number | null>(null)
  const groups = useMemo(() => groupFeed(feed.items, sort), [feed.items, sort])

  // After "Tải thêm", move focus to the first new story (keyboard/screen reader
  // users continue where the list grew) without moving the scroll position.
  useEffect(() => {
    if (feed.appendedAt === null || feed.appendedAt === lastFocused.current || feed.restored) return
    lastFocused.current = feed.appendedAt
    firstNew.current
      ?.querySelector<HTMLAnchorElement>(".story-link")
      ?.focus({ preventScroll: true })
  }, [feed.appendedAt, feed.restored])

  if (feed.status === "loading") return <FeedSkeleton />
  if (feed.status === "failed" && feed.failure !== null) {
    return (
      <FailureCallout failure={feed.failure} onRetry={onRetry} retryLabel="Tải lại danh sách" />
    )
  }
  if (feed.items.length === 0) {
    return filtered ? (
      <div className="empty-state">
        <h2>Không có bài nào khớp bộ lọc</h2>
        <p>Thử bỏ bớt điều kiện hoặc đổi từ khóa.</p>
        {facetsNote ? (
          <p className="field-note">
            Lọc theo chủ đề/địa phương chỉ ra kết quả với bài đã được phân loại bằng nhãn đã kiểm
            duyệt.
          </p>
        ) : null}
        <button className="btn btn-small" type="button" onClick={onClear}>
          Xóa toàn bộ bộ lọc
        </button>
      </div>
    ) : (
      <div className="empty-state">
        <h2>Kho tin chưa có bài nào</h2>
        <p>Chưa có bài viết nào được nhập vào hệ thống. Khi có dữ liệu, tin mới sẽ hiện ở đây.</p>
      </div>
    )
  }

  const chronological = sort !== "relevance"
  // Reveal: the first page animates once when it mounts, appended stories when
  // they arrive; a restored feed (Back) and re-renders never animate.
  const revealClass = (index: number) => {
    if (feed.restored) return undefined
    if (feed.appendedAt === null) return index < 6 ? "reveal" : undefined
    return index >= feed.appendedAt ? "reveal" : undefined
  }

  return (
    <div className="feed">
      {chronological ? null : <h2 className="visually-hidden">Kết quả theo mức liên quan</h2>}
      {groups.map((group) => {
        const first = group.entries[0]?.index ?? 0
        const headingId = `day-${first}`
        return (
          <section
            key={`${group.day}-${first}`}
            className="feed-group"
            aria-labelledby={chronological ? headingId : undefined}
          >
            {chronological ? (
              <h2 className="day-heading" id={headingId}>
                {group.day === UNKNOWN_DAY ? (
                  "Chưa rõ ngày đăng"
                ) : (
                  <time dateTime={group.day}>{formatDayHeading(group.day)}</time>
                )}
              </h2>
            ) : null}
            <ol className="story-list" start={first + 1}>
              {group.entries.map(({ article, index }) => (
                <li
                  key={article.article_id}
                  ref={index === feed.appendedAt ? firstNew : undefined}
                  className={revealClass(index)}
                >
                  <ArticleItem article={article} timeOnly={chronological} headingLevel={3} />
                </li>
              ))}
            </ol>
          </section>
        )
      })}
      <div className="feed-more">
        {feed.more === "failed" && feed.moreFailure !== null ? (
          <FailureCallout
            failure={feed.moreFailure}
            onRetry={onLoadMore}
            retryLabel="Thử tải thêm lần nữa"
            heading="h3"
          />
        ) : null}
        {feed.hasMore ? (
          <button
            className="btn"
            type="button"
            onClick={onLoadMore}
            disabled={feed.more === "loading"}
            aria-busy={feed.more === "loading"}
          >
            {feed.more === "loading" ? "Đang tải…" : "Tải thêm tin"}
          </button>
        ) : (
          <p className="feed-end">
            Đã hiển thị hết {feed.items.length.toLocaleString("vi-VN")} bài.
          </p>
        )}
        {feed.more === "loading" ? <LoadingLine label="Đang tải thêm tin…" /> : null}
      </div>
    </div>
  )
}

const FeedSkeleton = () => (
  <div className="feed-skeleton" role="status" aria-label="Đang tải danh sách tin">
    <span className="skeleton skeleton-line short" />
    {[0, 1, 2, 3, 4, 5].map((i) => (
      <span key={i} className="skeleton skeleton-item" />
    ))}
  </div>
)
