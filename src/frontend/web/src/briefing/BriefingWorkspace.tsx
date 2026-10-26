import { type FormEvent, useCallback, useEffect, useId, useReducer, useRef, useState } from "react"
import { BriefingResult } from "./BriefingResult"
import type { BriefingClient } from "./client"
import type { BriefingCitation, BriefingRequest } from "./contract"
import {
  briefingReducer,
  buildBriefingRequest,
  describeFailure,
  initialBriefingState,
  QUERY_ISSUE_MESSAGES,
  type RequestFailure,
} from "./model"
import { SourceViewer, type SourceViewerState } from "./SourceViewer"

type BriefingWorkspaceProps = {
  readonly client: BriefingClient
}

export const BriefingWorkspace = ({ client }: BriefingWorkspaceProps) => {
  const [state, dispatch] = useReducer(briefingReducer, initialBriefingState)
  const [query, setQuery] = useState("")
  const [viewer, setViewer] = useState<SourceViewerState>({ status: "closed" })
  const briefingAbort = useRef<AbortController | null>(null)
  const sourceAbort = useRef<AbortController | null>(null)
  const requestCounter = useRef(0)
  const returnFocus = useRef<HTMLButtonElement | null>(null)
  const queryId = useId()
  const queryHintId = useId()
  const queryErrorId = useId()

  useEffect(
    () => () => {
      briefingAbort.current?.abort()
      sourceAbort.current?.abort()
    },
    [],
  )

  const closeViewer = useCallback(() => {
    sourceAbort.current?.abort()
    setViewer({ status: "closed" })
  }, [])

  const send = useCallback(
    async (request: BriefingRequest, requestId: number) => {
      briefingAbort.current?.abort()
      const controller = new AbortController()
      briefingAbort.current = controller
      const result = await client.submit(request, controller.signal)
      dispatch(
        result.ok
          ? { type: "resolved", requestId, response: result.data }
          : { type: "failed", requestId, failure: result.failure },
      )
    },
    [client],
  )

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const validation = buildBriefingRequest(query)
    if (!validation.ok) {
      dispatch({ type: "rejected", issue: validation.issue })
      return
    }
    closeViewer()
    requestCounter.current += 1
    dispatch({ type: "submitted", request: validation.request, requestId: requestCounter.current })
    void send(validation.request, requestCounter.current)
  }

  const onRetry = () => {
    if (state.status !== "failed") {
      return
    }
    requestCounter.current += 1
    dispatch({ type: "retried", requestId: requestCounter.current })
    void send(state.request, requestCounter.current)
  }

  const inspect = async (citation: BriefingCitation, trigger: HTMLButtonElement) => {
    returnFocus.current = trigger
    sourceAbort.current?.abort()
    const controller = new AbortController()
    sourceAbort.current = controller
    setViewer({ status: "loading", citation })
    const result = await client.inspectSource(citation.source_ref, controller.signal)
    if (controller.signal.aborted) {
      return
    }
    setViewer(
      result.ok
        ? { status: "open", citation, source: result.data }
        : result.failure.kind === "cancelled"
          ? { status: "closed" }
          : { status: "failed", citation, failure: result.failure },
    )
  }

  const onCloseViewer = () => {
    closeViewer()
    returnFocus.current?.focus()
  }

  const invalid = state.status === "invalid"
  const loading = state.status === "loading"

  return (
    <div className="briefing-workspace">
      <section className="query-panel" aria-labelledby="query-title">
        <div className="query-copy">
          <p className="eyebrow">Analyst briefing</p>
          <h1 id="query-title">Ask the evidence.</h1>
          <p className="hero-intro">
            Answers cite the passages they rest on. When the sources are thin, the briefing says so.
          </p>
        </div>

        <form className="query-form" onSubmit={onSubmit} noValidate>
          <label className="field-label" htmlFor={queryId}>
            Question
          </label>
          <textarea
            id={queryId}
            name="query"
            className="query-input"
            rows={3}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-invalid={invalid}
            aria-describedby={invalid ? `${queryHintId} ${queryErrorId}` : queryHintId}
            placeholder="When did East Pier close and reopen?"
          />
          <p className="field-hint" id={queryHintId}>
            Times are matched against when sources were published.
          </p>
          {invalid ? (
            <p className="field-error" id={queryErrorId} role="alert">
              <span className="state-tag state-tag-warning">Check question</span>
              {QUERY_ISSUE_MESSAGES[state.issue]}
            </p>
          ) : null}
          <button className="button button-primary" type="submit">
            {loading ? "Briefing…" : "Request briefing"}
          </button>
        </form>
      </section>

      <div className={viewer.status === "closed" ? "result-grid" : "result-grid has-viewer"}>
        <section className="result-panel" aria-labelledby="result-title" aria-busy={loading}>
          <header className="section-header">
            <h2 id="result-title">Briefing</h2>
            <p className="section-index">{statusLabel(state.status)}</p>
          </header>
          <div className="result-body" aria-live="polite">
            {state.status === "idle" || state.status === "invalid" ? (
              <p className="empty-note">
                No briefing requested yet. Submit a question to retrieve cited evidence.
              </p>
            ) : null}
            {state.status === "loading" ? (
              <p className="loading-note" role="status">
                <span className="loading-bar" aria-hidden="true" />
                {state.attempt > 1
                  ? `Retrying — attempt ${state.attempt}. Retrieving evidence…`
                  : "Retrieving evidence and checking citations…"}
              </p>
            ) : null}
            {state.status === "failed" ? (
              <FailureNotice failure={state.failure} attempt={state.attempt} onRetry={onRetry} />
            ) : null}
            {state.status === "answered" ? (
              <BriefingResult
                response={state.response}
                activeCitationId={viewer.status === "closed" ? null : viewer.citation.citation_id}
                onInspect={inspect}
              />
            ) : null}
          </div>
        </section>

        {viewer.status === "closed" ? null : (
          <SourceViewer state={viewer} onClose={onCloseViewer} />
        )}
      </div>
    </div>
  )
}

const statusLabel = (status: string): string => {
  switch (status) {
    case "loading":
      return "Loading"
    case "answered":
      return "Received"
    case "failed":
      return "Failed"
    default:
      return "Waiting"
  }
}

type FailureNoticeProps = {
  readonly failure: RequestFailure
  readonly attempt: number
  readonly onRetry: () => void
}

const FailureNotice = ({ failure, attempt, onRetry }: FailureNoticeProps) => {
  const copy = describeFailure(failure)
  return (
    <div className="notice notice-error" role="alert">
      <p className="state-tag state-tag-error">Error</p>
      <h3>{copy.title}</h3>
      <p>{copy.message}</p>
      <p className="notice-meta">
        {failure.kind === "api" ? `Code ${failure.code} · HTTP ${failure.status}` : null}
        {failure.kind === "malformed" ? `HTTP ${failure.status} · response rejected` : null}
        {failure.kind === "network" ? "No response received" : null}
        {` · attempt ${attempt}`}
      </p>
      {copy.retryable ? (
        <button className="button button-secondary" type="button" onClick={onRetry}>
          Retry briefing
        </button>
      ) : null}
    </div>
  )
}
