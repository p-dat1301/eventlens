import { useEffect, useRef } from "react"

import type { BriefingCitation, BriefingSource } from "./contract"
import { describeFailure, formatTimestamp, type RequestFailure, segmentSource } from "./model"

export type SourceViewerState =
  | { readonly status: "closed" }
  | { readonly status: "loading"; readonly citation: BriefingCitation }
  | {
      readonly status: "open"
      readonly citation: BriefingCitation
      readonly source: BriefingSource
    }
  | {
      readonly status: "failed"
      readonly citation: BriefingCitation
      readonly failure: RequestFailure
    }

type SourceViewerProps = {
  readonly state: Exclude<SourceViewerState, { readonly status: "closed" }>
  readonly onClose: () => void
}

export const SourceViewer = ({ state, onClose }: SourceViewerProps) => {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const citationId = state.citation.citation_id

  // Move focus once per inspected citation so keyboard users land in the viewer.
  useEffect(() => {
    if (citationId.length > 0) {
      headingRef.current?.focus()
    }
  }, [citationId])

  return (
    <aside
      className="source-viewer"
      id="source-viewer"
      aria-labelledby="source-viewer-title"
      aria-busy={state.status === "loading"}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          onClose()
        }
      }}
    >
      <header className="section-header">
        <h2 id="source-viewer-title" ref={headingRef} tabIndex={-1}>
          Source
        </h2>
        <button className="button button-secondary" type="button" onClick={onClose}>
          Close source
        </button>
      </header>

      {state.status === "loading" ? (
        <p className="loading-note" role="status">
          <span className="loading-bar" aria-hidden="true" />
          Loading “{state.citation.source_title}”…
        </p>
      ) : null}

      {state.status === "failed" ? <SourceFailure failure={state.failure} /> : null}

      {state.status === "open" ? <SourceBody source={state.source} /> : null}
    </aside>
  )
}

const SourceFailure = ({ failure }: { readonly failure: RequestFailure }) => {
  const copy = describeFailure(failure)
  return (
    <div className="notice notice-error" role="alert">
      <p className="state-tag state-tag-error">Source unavailable</p>
      <h3>{copy.title}</h3>
      <p>{copy.message}</p>
      {failure.kind === "api" ? (
        <p className="notice-meta">
          Code {failure.code} · HTTP {failure.status}
        </p>
      ) : null}
    </div>
  )
}

const SourceBody = ({ source }: { readonly source: BriefingSource }) => {
  const segments = segmentSource(source)
  return (
    <article className="source-body" aria-label={source.title}>
      <h3>{source.title}</h3>
      <p className="citation-meta">
        {source.source_name} · {source.source_domain} ·{" "}
        {formatTimestamp(source.source_published_at)}
      </p>
      <a className="text-link" href={source.source_url} target="_blank" rel="noopener noreferrer">
        {source.source_url}
        <span className="visually-hidden"> (opens in new tab)</span>
      </a>
      <p className="source-text">
        {segments.before}
        <mark className="source-span">
          <span className="visually-hidden">Cited passage start. </span>
          {segments.cited}
          <span className="visually-hidden"> Cited passage end.</span>
        </mark>
        {segments.after}
      </p>
      <p className="notice-meta">
        Cited characters {source.span.start_offset}–{source.span.end_offset}
      </p>
    </article>
  )
}
