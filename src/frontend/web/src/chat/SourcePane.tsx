import { useEffect, useRef } from "react"

import type { BriefingSource } from "../api/chat"
import { FailureCallout, LoadingLine } from "../app/ui"
import { codePoints } from "../lib/code-points"
import { formatDateTime } from "../lib/dates"
import { hostOf, safeExternalUrl } from "../lib/safe-url"
import type { ViewerState } from "./citations"

type Props = {
  readonly state: Exclude<ViewerState, { readonly status: "closed" }>
  readonly onClose: () => void
  /** Desktop: pane beside the answer; narrow screens: modal dialog. */
  readonly modal: boolean
}

/**
 * Source of one citation. Focus moves to the heading on open; Escape and the
 * close button close it and the caller returns focus to the citation. The cited
 * passage is scrolled into view inside the pane only (the thread never moves).
 */
export const SourcePane = ({ state, onClose, modal }: Props) => {
  const dialog = useRef<HTMLDialogElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const citationId = state.citation.citation_id

  // A modal dialog makes the page inert: close it first so focus can return
  // to the citation that opened it.
  const requestClose = () => {
    const node = dialog.current
    if (node?.open && typeof node.close === "function") node.close()
    onClose()
  }

  useEffect(() => {
    const node = dialog.current
    if (!modal || node === null || node.open) return
    if (typeof node.showModal === "function") node.showModal()
    else node.setAttribute("open", "")
    return () => {
      if (node.open && typeof node.close === "function") node.close()
    }
  }, [modal])

  useEffect(() => {
    if (citationId.length > 0) heading.current?.focus({ preventScroll: true })
  }, [citationId])

  useEffect(() => {
    if (state.status !== "open") return
    const container = scroller.current
    const mark = container?.querySelector<HTMLElement>("mark")
    if (container === null || container === undefined || mark === null || mark === undefined) return
    // Scroll the pane, not the window: keeps the reader's place in the thread.
    container.scrollTop = Math.max(0, mark.offsetTop - container.clientHeight / 3)
  }, [state])

  const body = (
    <div className="source-pane-inner">
      <header className="source-pane-header">
        <h2 ref={heading} tabIndex={-1} id="source-pane-title">
          Nguồn trích dẫn
        </h2>
        <button className="btn btn-small" type="button" onClick={requestClose}>
          Đóng<span className="visually-hidden"> nguồn</span>
        </button>
      </header>
      <div className="source-pane-scroll" ref={scroller}>
        {state.status === "loading" ? (
          <LoadingLine label={`Đang mở “${state.citation.source_title}”…`} />
        ) : null}
        {state.status === "failed" ? (
          <>
            <FailureCallout failure={state.failure} heading="h3" />
            <StoredExcerpt state={state} />
          </>
        ) : null}
        {state.status === "open" ? <SourceText source={state.source} /> : null}
      </div>
    </div>
  )

  if (modal) {
    return (
      <dialog
        ref={dialog}
        className="source-dialog"
        aria-labelledby="source-pane-title"
        onCancel={(event) => {
          event.preventDefault()
          requestClose()
        }}
      >
        {body}
      </dialog>
    )
  }
  return (
    <aside
      className="source-pane"
      aria-labelledby="source-pane-title"
      aria-busy={state.status === "loading"}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation()
          onClose()
        }
      }}
    >
      {body}
    </aside>
  )
}

/** What the answer cited, as stored with it (shown when the live source is gone). */
const StoredExcerpt = ({
  state,
}: {
  readonly state: Extract<ViewerState, { status: "failed" }>
}) => {
  const { citation } = state
  const stale =
    state.failure.kind === "api" &&
    (state.failure.code === "source_stale" || state.failure.status === 410)
  return (
    <figure className="stored-excerpt">
      <figcaption>
        <span className={stale ? "badge badge-warning" : "badge badge-neutral"}>
          {stale ? "Bản lỗi thời — đoạn trích lúc trả lời" : "Đoạn trích đã lưu cùng câu trả lời"}
        </span>
        <span className="stored-title">{citation.source_title}</span>
        <span className="meta-line">
          <span className="source-name">{citation.source_name}</span>
          <span>{formatDateTime(citation.source_published_at)}</span>
        </span>
      </figcaption>
      <blockquote>{citation.span.excerpt}</blockquote>
    </figure>
  )
}

const SourceText = ({ source }: { readonly source: BriefingSource }) => {
  const points = codePoints(source.normalized_content)
  const before = points.slice(0, source.span.start_offset).join("")
  const cited = points.slice(source.span.start_offset, source.span.end_offset).join("")
  const after = points.slice(source.span.end_offset).join("")
  const link = safeExternalUrl(source.source_url)
  return (
    <article className="source-article" aria-label={source.title}>
      <h3>{source.title}</h3>
      <p className="meta-line">
        <span className="source-name">{source.source_name}</span>
        <span>{formatDateTime(source.source_published_at)}</span>
      </p>
      {link === null ? null : (
        <p>
          <a className="link" href={link} target="_blank" rel="noopener noreferrer">
            Mở bài gốc trên {hostOf(link)}
            <span className="visually-hidden"> (mở tab mới)</span>
          </a>
        </p>
      )}
      <p className="source-pane-text">
        {before}
        <mark className="cited-mark">
          <span className="visually-hidden">Bắt đầu đoạn được trích. </span>
          {cited}
          <span className="visually-hidden"> Kết thúc đoạn được trích.</span>
        </mark>
        {after}
      </p>
    </article>
  )
}
