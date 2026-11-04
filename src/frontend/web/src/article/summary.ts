import { useCallback, useEffect, useState } from "react"

import type { ArticleSummary, CatalogClient } from "../api/catalog"
import type { ApiFailure } from "../api/http"
import { backoff, startPoller } from "../lib/poller"

// GET /articles/{id}/summary is read-only: it never asks for a summary to be
// generated. While the job is `pending` the page re-reads it with backoff
// (bounded); leaving the page or reaching ready/failed/unavailable stops it.

export const SUMMARY_POLL = {
  delays: backoff(2_000, 1.6, 30_000),
  maxAttempts: 12,
} as const

export type SummaryState =
  | { readonly status: "loading" }
  | {
      readonly status: "loaded"
      readonly summary: ArticleSummary
      readonly polling: boolean
      readonly exhausted: boolean
    }
  | { readonly status: "failed"; readonly failure: ApiFailure }

export const isFixtureGenerator = (summary: ArticleSummary): boolean =>
  summary.generator?.model_id.startsWith("fixture") === true

export const useArticleSummary = (
  catalog: CatalogClient,
  articleId: string,
  options: { readonly delays?: (attempt: number) => number; readonly maxAttempts?: number } = {},
) => {
  const [state, setState] = useState<SummaryState>({ status: "loading" })
  const [round, setRound] = useState(0)
  const delays = options.delays ?? SUMMARY_POLL.delays
  const maxAttempts = options.maxAttempts ?? SUMMARY_POLL.maxAttempts

  // biome-ignore lint/correctness/useExhaustiveDependencies: round starts a fresh bounded polling round on recheck
  useEffect(() => {
    let lastPending: ArticleSummary | null = null
    const stop = startPoller({
      delays,
      maxAttempts,
      tick: async (signal) => {
        const result = await catalog.summary(articleId, signal)
        if (signal.aborted) return "done"
        if (!result.ok) {
          if (result.failure.kind === "cancelled") return "done"
          setState({ status: "failed", failure: result.failure })
          return "done"
        }
        const pending = result.data.status === "pending"
        lastPending = pending ? result.data : null
        setState({ status: "loaded", summary: result.data, polling: pending, exhausted: false })
        return pending ? "continue" : "done"
      },
      onExhausted: () => {
        if (lastPending !== null) {
          setState({ status: "loaded", summary: lastPending, polling: false, exhausted: true })
        }
      },
    })
    return stop
    // round: "Kiểm tra lại" starts a fresh bounded polling round.
  }, [catalog, articleId, delays, maxAttempts, round])

  const recheck = useCallback(() => {
    setState((current) =>
      current.status === "loaded"
        ? { ...current, polling: true, exhausted: false }
        : { status: "loading" },
    )
    setRound((n) => n + 1)
  }, [])

  return { state, recheck }
}
