import { useCallback, useEffect, useRef, useState } from "react"

import type { BriefingSource, ChatClient, Citation } from "../api/chat"
import { isCitationRefPath, isViewerPath } from "../api/chat"
import type { ApiFailure } from "../api/http"

// Opening a stored citation always asks the server for a fresh source-ref
// (POST source_ref_url), then reads the viewer URL it returns. A ref is never
// cached or treated as a permanent source id. 403/404/410 are explained; a
// stale source keeps showing the excerpt stored with the answer, marked as
// outdated, and is never swapped for the article's newer text.

export type ViewerState =
  | { readonly status: "closed" }
  | { readonly status: "loading"; readonly citation: Citation; readonly runId: string }
  | {
      readonly status: "open"
      readonly citation: Citation
      readonly runId: string
      readonly source: BriefingSource
    }
  | {
      readonly status: "failed"
      readonly citation: Citation
      readonly runId: string
      readonly failure: ApiFailure
    }

export const openCitation = async (
  chat: ChatClient,
  runId: string,
  citation: Citation,
  signal: AbortSignal,
): Promise<{ ok: true; source: BriefingSource } | { ok: false; failure: ApiFailure }> => {
  if (!isCitationRefPath(citation.source_ref_url, runId, citation.citation_id)) {
    return { ok: false, failure: { kind: "malformed", status: 0 } }
  }
  const ref = await chat.sourceRef(citation.source_ref_url, signal)
  if (!ref.ok) return ref
  if (!isViewerPath(ref.data.viewer_url))
    return { ok: false, failure: { kind: "malformed", status: 201 } }
  const source = await chat.viewSource(ref.data.viewer_url, signal)
  if (!source.ok) return source
  // The viewer must show the cited passage; anything else is not displayed.
  if (source.data.span.excerpt !== citation.span.excerpt) {
    return { ok: false, failure: { kind: "malformed", status: 200 } }
  }
  return { ok: true, source: source.data }
}

export const useCitationViewer = (chat: ChatClient) => {
  const [state, setState] = useState<ViewerState>({ status: "closed" })
  const abort = useRef<AbortController | null>(null)
  const trigger = useRef<HTMLElement | null>(null)

  useEffect(() => () => abort.current?.abort(), [])

  const open = useCallback(
    async (runId: string, citation: Citation, from: HTMLElement) => {
      trigger.current = from
      abort.current?.abort()
      const controller = new AbortController()
      abort.current = controller
      setState({ status: "loading", citation, runId })
      const result = await openCitation(chat, runId, citation, controller.signal)
      if (controller.signal.aborted) return
      if (result.ok) setState({ status: "open", citation, runId, source: result.source })
      else if (result.failure.kind === "cancelled") setState({ status: "closed" })
      else setState({ status: "failed", citation, runId, failure: result.failure })
    },
    [chat],
  )

  const close = useCallback(() => {
    abort.current?.abort()
    // Back to the citation that opened the viewer (before the pane unmounts, so
    // focus never falls to <body>), without moving the thread.
    trigger.current?.focus({ preventScroll: true })
    setState({ status: "closed" })
  }, [])

  return { state, open, close }
}
