import { useEffect, useId, useMemo, useRef, useState } from "react"

import type { ArticleFacets, Location } from "../api/catalog"
import { FailureCallout, LoadingLine } from "../app/ui"
import { hcmDayOf } from "../lib/dates"
import { dateRangeIssue, EMPTY_FILTERS, MAX_IDS, type NewsFilters } from "../lib/filters"
import { type FacetsState, nameLookup, type Taxonomy, type TaxonomyState } from "./taxonomy"

export type DrawerSection = "time" | "sources"

type Props = {
  readonly open: boolean
  /** Group to bring into view and focus when the drawer opens (quick buttons). */
  readonly section?: DrawerSection | null
  readonly onClose: () => void
  readonly filters: NewsFilters
  readonly onChange: (next: NewsFilters) => void
  readonly taxonomy: TaxonomyState
  readonly onRetryTaxonomy: () => void
  readonly facets: FacetsState
}

const toggle = (values: readonly string[], id: string): readonly string[] =>
  values.includes(id) ? values.filter((v) => v !== id) : [...values, id].sort().slice(0, MAX_IDS)

const shiftDay = (day: string, days: number): string => {
  const [y, m, d] = day.split("-").map(Number)
  return new Date(Date.UTC(y ?? 1970, (m ?? 1) - 1, (d ?? 1) + days)).toISOString().slice(0, 10)
}

const fold = (text: string) =>
  text
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLowerCase()

/**
 * Filter panel in a native modal <dialog>: focus is trapped and restored by the
 * browser, Escape closes it. Every change applies immediately (URL + feed).
 * Groups with data come first (time, sources); topic and location start as a
 * one-line status and open on demand, so unlabelled taxonomies never push the
 * useful groups out of reach. Selected values are always listed with a way to
 * remove them, even when they currently match nothing.
 */
export const FilterDrawer = ({
  open,
  section = null,
  onClose,
  filters,
  onChange,
  taxonomy,
  onRetryTaxonomy,
  facets,
}: Props) => {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  useEffect(() => {
    const node = dialog.current
    if (node === null) return
    if (open && !node.open) {
      if (typeof node.showModal === "function") node.showModal()
      else node.setAttribute("open", "")
    } else if (!open && node.open) {
      if (typeof node.close === "function") node.close()
      else node.removeAttribute("open")
    }
  }, [open])

  // Quick buttons ("Thời gian", "Nguồn"): focus the first control of that group.
  useEffect(() => {
    if (!open || section === null) return
    const frame = window.requestAnimationFrame(() => {
      const group = dialog.current?.querySelector<HTMLElement>(`[data-section="${section}"]`)
      group?.scrollIntoView({ block: "start" })
      group?.querySelector<HTMLElement>("button, input")?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [open, section])

  const facetsReady = facets.status === "ready" ? facets.facets : null
  const total = facetsReady?.total

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: backdrop click only; Escape closes via onCancel
    <dialog
      ref={dialog}
      className="drawer"
      aria-labelledby={titleId}
      onClose={onClose}
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      onClick={(event) => {
        // Click on the backdrop (outside the panel) closes.
        if (event.target === dialog.current) onClose()
      }}
    >
      {open ? (
        <div className="drawer-panel">
          <header className="drawer-header">
            <h2 id={titleId}>Bộ lọc</h2>
            <button className="btn btn-quiet btn-small" type="button" onClick={onClose}>
              Đóng<span className="visually-hidden"> bộ lọc</span>
            </button>
          </header>
          <div className="drawer-body">
            <DateGroup filters={filters} onChange={onChange} />
            {taxonomy.status === "loading" ? <LoadingLine label="Đang tải danh mục…" /> : null}
            {taxonomy.status === "failed" ? (
              <FailureCallout failure={taxonomy.failure} onRetry={onRetryTaxonomy} heading="h3" />
            ) : null}
            {taxonomy.status === "ready" ? (
              <TaxonomyGroups
                taxonomy={taxonomy.taxonomy}
                filters={filters}
                onChange={onChange}
                facets={facetsReady}
                facetsFailed={facets.status === "failed"}
              />
            ) : null}
            {facets.status === "failed" ? (
              <p className="field-note">
                Không tải được số lượng theo bộ lọc; danh sách vẫn dùng được.
              </p>
            ) : null}
          </div>
          <footer className="drawer-footer">
            <button
              className="btn btn-small"
              type="button"
              onClick={() => onChange({ ...EMPTY_FILTERS, q: filters.q, sort: filters.sort })}
            >
              Bỏ chọn tất cả
            </button>
            <button className="btn btn-primary" type="button" onClick={onClose}>
              {total === undefined ? "Xem kết quả" : `Xem ${total.toLocaleString("vi-VN")} bài`}
            </button>
          </footer>
        </div>
      ) : null}
    </dialog>
  )
}

const DateGroup = ({
  filters,
  onChange,
}: {
  readonly filters: NewsFilters
  readonly onChange: (next: NewsFilters) => void
}) => {
  const fromId = useId()
  const toId = useId()
  const errorId = useId()
  const today = hcmDayOf(new Date())
  const inverted = dateRangeIssue(filters) === "inverted"
  const preset = (days: number) =>
    onChange({ ...filters, from: days === 0 ? today : shiftDay(today, -days + 1), to: today })
  return (
    <fieldset className="filter-group" data-section="time">
      <legend>Thời gian đăng</legend>
      <p className="field-note">Theo ngày giờ Việt Nam; gồm cả ngày kết thúc.</p>
      <div className="preset-row">
        <button className="chip-button" type="button" onClick={() => preset(0)}>
          Hôm nay
        </button>
        <button className="chip-button" type="button" onClick={() => preset(7)}>
          7 ngày qua
        </button>
        <button className="chip-button" type="button" onClick={() => preset(30)}>
          30 ngày qua
        </button>
      </div>
      <div className="date-row">
        <label htmlFor={fromId}>
          Từ ngày
          <input
            id={fromId}
            type="date"
            value={filters.from}
            max={filters.to === "" ? undefined : filters.to}
            aria-invalid={inverted}
            aria-describedby={inverted ? errorId : undefined}
            onChange={(event) => onChange({ ...filters, from: event.target.value })}
          />
        </label>
        <label htmlFor={toId}>
          Đến ngày
          <input
            id={toId}
            type="date"
            value={filters.to}
            min={filters.from === "" ? undefined : filters.from}
            aria-invalid={inverted}
            aria-describedby={inverted ? errorId : undefined}
            onChange={(event) => onChange({ ...filters, to: event.target.value })}
          />
        </label>
      </div>
      {inverted ? (
        <p className="field-error" id={errorId} role="alert">
          Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.
        </p>
      ) : null}
      {filters.from !== "" || filters.to !== "" ? (
        <button
          className="btn btn-quiet btn-small"
          type="button"
          onClick={() => onChange({ ...filters, from: "", to: "" })}
        >
          Bỏ lọc thời gian
        </button>
      ) : null}
    </fieldset>
  )
}

const countOf = (list: ArticleFacets["topics"] | undefined, id: string): number | null => {
  if (list === undefined) return null
  return list.find((item) => item.id === id)?.count ?? 0
}

/**
 * One-line status of a label group. Facets count the *current* filter set
 * only, so "no labelled article here" is never presented as "the corpus has no
 * labels"; while counts load or after a failure nothing is claimed.
 */
const groupStatus = (
  facets: ArticleFacets | null,
  failed: boolean,
  counts: ArticleFacets["topics"] | undefined,
  missing: number | undefined,
  noun: string,
): { readonly text: string; readonly hasData: boolean } => {
  if (facets === null || counts === undefined || missing === undefined) {
    return { text: failed ? "Chưa tải được số bài." : "Đang đếm số bài…", hasData: false }
  }
  const labelled = counts.some((item) => item.count > 0)
  if (labelled) return { text: `Có bài mang nhãn ${noun} trong kết quả hiện tại.`, hasData: true }
  if (facets.total === 0) return { text: "Kết quả hiện tại không có bài nào.", hasData: false }
  return {
    text: `Chưa bài nào trong kết quả hiện tại có nhãn ${noun} đã kiểm duyệt.`,
    hasData: false,
  }
}

const TaxonomyGroups = ({
  taxonomy,
  filters,
  onChange,
  facets,
  facetsFailed,
}: {
  readonly taxonomy: Taxonomy
  readonly filters: NewsFilters
  readonly onChange: (next: NewsFilters) => void
  readonly facets: ArticleFacets | null
  readonly facetsFailed: boolean
}) => {
  const [locationQuery, setLocationQuery] = useState("")
  const locationSearchId = useId()
  const names = nameLookup(taxonomy)
  const topicStatus = groupStatus(
    facets,
    facetsFailed,
    facets?.topics,
    facets?.topics_unclassified,
    "chủ đề",
  )
  const placeStatus = groupStatus(
    facets,
    facetsFailed,
    facets?.locations,
    facets?.locations_unknown,
    "địa phương",
  )

  // Search first: the gazetteer is long, so nothing is listed until the reader
  // types — except places that do have articles in the current result.
  const { current, former, withArticles } = useMemo(() => {
    const needle = fold(locationQuery.trim())
    const withCount = new Set(
      (facets?.locations ?? []).filter((item) => item.count > 0).map((item) => item.id),
    )
    const match = (location: Location) =>
      fold(location.name).includes(needle) ||
      location.aliases.some((alias) => fold(alias).includes(needle))
    const visible = needle === "" ? [] : taxonomy.locations.filter(match)
    return {
      current: visible.filter((l) => l.type !== "former_province"),
      former: visible.filter((l) => l.type === "former_province"),
      withArticles:
        needle === "" ? taxonomy.locations.filter((location) => withCount.has(location.id)) : [],
    }
  }, [taxonomy.locations, locationQuery, facets])

  const checkbox = (
    kind: "topics" | "locations" | "sources",
    id: string,
    label: string,
    count: number | null,
  ) => (
    <li key={id}>
      <label className="check">
        <input
          type="checkbox"
          checked={filters[kind].includes(id)}
          onChange={() => onChange({ ...filters, [kind]: toggle(filters[kind], id) })}
        />
        <span className="check-label">{label}</span>
        {count === null ? null : (
          <span className="check-count">
            {count.toLocaleString("vi-VN")}
            <span className="visually-hidden"> bài</span>
          </span>
        )}
      </label>
    </li>
  )

  const selectedChips = (kind: "topics" | "locations", label: (id: string) => string) =>
    filters[kind].length === 0 ? null : (
      <ul className="chips selected-chips" aria-label="Đang chọn">
        {filters[kind].map((id) => (
          <li key={id}>
            <button
              className="chip"
              type="button"
              aria-label={`Bỏ chọn ${label(id)}`}
              onClick={() =>
                onChange({ ...filters, [kind]: filters[kind].filter((value) => value !== id) })
              }
            >
              {label(id)}
              <span aria-hidden="true">×</span>
            </button>
          </li>
        ))}
      </ul>
    )

  return (
    <>
      <fieldset className="filter-group" data-section="sources">
        <legend>Nguồn</legend>
        {taxonomy.sources.length === 0 ? <p className="field-note">Chưa có nguồn nào.</p> : null}
        <ul className="check-list">
          {taxonomy.sources.map((source) =>
            checkbox("sources", source.id, source.name, countOf(facets?.sources, source.id)),
          )}
        </ul>
      </fieldset>

      <fieldset className="filter-group filter-group-compact">
        <legend>Chủ đề</legend>
        {selectedChips("topics", names.topic)}
        <details className="group-details" open={topicStatus.hasData || undefined}>
          <summary>
            <span className="group-status">{topicStatus.text}</span>
            <span className="group-toggle">Xem chủ đề</span>
          </summary>
          <p className="field-note">
            Chủ đề chỉ được gán từ nhãn đã kiểm duyệt; bài chưa phân loại không khớp bộ lọc chủ đề.
          </p>
          <ul className="check-list">
            {taxonomy.topics.map((topic) =>
              checkbox("topics", topic.id, topic.name, countOf(facets?.topics, topic.id)),
            )}
          </ul>
        </details>
      </fieldset>

      <fieldset className="filter-group filter-group-compact">
        <legend>Địa phương</legend>
        {selectedChips("locations", names.location)}
        <p className="group-status">{placeStatus.text}</p>
        <label className="field-label" htmlFor={locationSearchId}>
          Tìm tỉnh, thành
        </label>
        <input
          id={locationSearchId}
          className="text-input"
          type="search"
          value={locationQuery}
          placeholder="Ví dụ: Đà Nẵng, Binh Duong"
          onChange={(event) => setLocationQuery(event.target.value)}
        />
        <p className="field-note">Chọn một tỉnh/thành gồm cả các đơn vị cũ đã sáp nhập vào nó.</p>
        {withArticles.length > 0 ? (
          <ul className="check-list">
            {withArticles.map((location) =>
              checkbox(
                "locations",
                location.id,
                location.name,
                countOf(facets?.locations, location.id),
              ),
            )}
          </ul>
        ) : null}
        {current.length > 0 ? (
          <ul className="check-list">
            {current.map((location) =>
              checkbox(
                "locations",
                location.id,
                location.name,
                countOf(facets?.locations, location.id),
              ),
            )}
          </ul>
        ) : null}
        {former.length > 0 ? (
          <details className="former-units" open>
            <summary>Tỉnh, thành cũ (trước 01/07/2025)</summary>
            <ul className="check-list">
              {former.map((location) =>
                checkbox(
                  "locations",
                  location.id,
                  location.name,
                  countOf(facets?.locations, location.id),
                ),
              )}
            </ul>
          </details>
        ) : null}
        {locationQuery.trim() !== "" && current.length === 0 && former.length === 0 ? (
          <p className="field-note">Không có địa phương khớp “{locationQuery}”.</p>
        ) : null}
      </fieldset>
    </>
  )
}
