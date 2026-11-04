import { useCallback, useEffect, useReducer, useRef } from "react"

import type { ChatClient, Conversation, Message } from "../api/chat"
import { isTerminal } from "../api/chat"
import type { ApiResult } from "../api/http"
import { newUuid } from "../lib/hooks"
import { backoff, startPoller } from "../lib/poller"
import { readPending, writePending } from "./pending"
import {
  activeRun,
  initialThread,
  isUncertain,
  type Submission,
  threadReducer,
  unhydrated,
} from "./thread"

// Lifecycle of one conversation (the component is keyed by conversation id,
// so nothing from conversation A can be applied to B):
// - history: all message pages, then each run's state (bounded concurrency);
// - the single active run is polled with backoff, paused while the tab is
//   hidden, until completed/failed/cancelled;
// - a question keeps one Idempotency-Key across retries (also across reload,
//   via sessionStorage) until the server answers definitively; a new question
//   gets a new key;
// - a first question handed over by the /chat composer (pending record with
//   `auto`) is sent once on mount with the key chosen there.

export const RUN_POLL = {
  delays: backoff(1_000, 1.5, 5_000),
  maxDurationMs: 11 * 60 * 1000,
} as const

const HYDRATE_CONCURRENCY = 4
const AUTO_RETRY_DELAY_MS = 1_500

const loadAllMessages = async (
  chat: ChatClient,
  conversationId: string,
  signal: AbortSignal,
): Promise<ApiResult<{ conversation: Conversation; messages: Message[] }>> => {
  const messages: Message[] = []
  let after = 0
  for (let page = 0; page < 50; page += 1) {
    const result = await chat.messages(conversationId, after, signal)
    if (!result.ok) return result
    messages.push(...result.data.items)
    if (result.data.next_after_sequence === null) {
      return { ok: true, status: 200, data: { conversation: result.data.conversation, messages } }
    }
    after = result.data.next_after_sequence
  }
  return { ok: false, failure: { kind: "malformed", status: 200 } }
}

export const useThread = (
  chat: ChatClient,
  conversationId: string,
  options: { readonly pollDelays?: (attempt: number) => number } = {},
) => {
  const [state, dispatch] = useReducer(threadReducer, initialThread, (initial) => {
    const pending = readPending(conversationId)
    if (pending === null) return initial
    const submission: Submission = pending.auto
      ? {
          key: pending.key,
          question: pending.question,
          phase: "sending",
          failure: null,
          attempts: 0,
        }
      : {
          key: pending.key,
          question: pending.question,
          phase: "uncertain",
          failure: null,
          attempts: 1,
        }
    return { ...initial, submission }
  })
  const [reloadToken, reload] = useReducer((n: number) => n + 1, 0)
  const hydrating = useRef(new Set<string>())
  const submitAbort = useRef<AbortController | null>(null)
  const retryTimer = useRef<number | null>(null)
  const mounted = useRef(true)
  const stateRef = useRef(state)
  stateRef.current = state

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      submitAbort.current?.abort()
      if (retryTimer.current !== null) window.clearTimeout(retryTimer.current)
    }
  }, [])

  // History.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reloadToken forces a history refresh
  useEffect(() => {
    const controller = new AbortController()
    dispatch({ type: "loading" })
    void loadAllMessages(chat, conversationId, controller.signal).then((result) => {
      if (controller.signal.aborted) return
      if (result.ok) {
        dispatch({
          type: "loaded",
          conversation: result.data.conversation,
          messages: result.data.messages,
        })
      } else if (result.failure.kind !== "cancelled") {
        dispatch({ type: "load_failed", failure: result.failure })
      }
    })
    return () => controller.abort()
    // reloadToken: explicit refresh (retry, conflict resolution).
  }, [chat, conversationId, reloadToken])

  // Run details for history entries and just-accepted runs.
  const pendingHydration = unhydrated(state)
  const hydrationKey = pendingHydration.map((entry) => entry.runId).join(",")
  useEffect(() => {
    if (hydrationKey === "") return
    const controller = new AbortController()
    const queue = unhydrated(stateRef.current).filter(
      (entry) => !hydrating.current.has(entry.runId),
    )
    const worker = async () => {
      for (let entry = queue.shift(); entry !== undefined; entry = queue.shift()) {
        hydrating.current.add(entry.runId)
        const result = await chat.run(entry.statusUrl, controller.signal)
        hydrating.current.delete(entry.runId)
        if (controller.signal.aborted) return
        if (result.ok) dispatch({ type: "run", run: result.data })
        else if (result.failure.kind !== "cancelled") {
          dispatch({ type: "run_failed", runId: entry.runId, failure: result.failure })
        }
      }
    }
    for (let i = 0; i < HYDRATE_CONCURRENCY; i += 1) void worker()
    return () => {
      controller.abort()
      hydrating.current.clear()
    }
  }, [chat, hydrationKey])

  // Poll the active run.
  const active = activeRun(state)
  const activeId = active?.runId ?? null
  const activePath = active?.statusUrl ?? null
  const pollDelays = options.pollDelays ?? RUN_POLL.delays
  useEffect(() => {
    if (activeId === null || activePath === null) return
    return startPoller({
      delays: pollDelays,
      maxDurationMs: RUN_POLL.maxDurationMs,
      immediate: false,
      tick: async (signal) => {
        const result = await chat.run(activePath, signal)
        if (signal.aborted) return "done"
        if (!result.ok) {
          if (result.failure.kind === "cancelled") return "done"
          // Transient errors keep polling (backoff); a 404 means the run is gone.
          if (result.failure.kind === "api" && result.failure.status === 404) {
            dispatch({ type: "run_failed", runId: activeId, failure: result.failure })
            return "done"
          }
          return "continue"
        }
        dispatch({ type: "run", run: result.data })
        return isTerminal(result.data.status) ? "done" : "continue"
      },
    })
  }, [chat, activeId, activePath, pollDelays])

  const send = useCallback(
    async (key: string, question: string) => {
      if (retryTimer.current !== null) {
        window.clearTimeout(retryTimer.current)
        retryTimer.current = null
      }
      submitAbort.current?.abort()
      const controller = new AbortController()
      submitAbort.current = controller
      writePending(conversationId, { key, question })
      dispatch({ type: "submit", key, question })
      const result = await chat.submit(conversationId, question, key, controller.signal)
      if (controller.signal.aborted || !mounted.current) return
      if (result.ok) {
        writePending(conversationId, null)
        dispatch({ type: "accepted", accepted: result.data, question })
        return
      }
      const failure = result.failure
      if (failure.kind === "cancelled") return
      if (isUncertain(failure)) {
        // The server may have stored the run: keep the same key so a retry
        // returns that run instead of creating a second one.
        dispatch({ type: "submit_uncertain", failure })
        const attempts = stateRef.current.submission?.attempts ?? 1
        if (attempts < 2) {
          retryTimer.current = window.setTimeout(() => {
            retryTimer.current = null
            void send(key, question)
          }, AUTO_RETRY_DELAY_MS)
        }
        return
      }
      // A definitive answer: the run was not created with this key.
      writePending(conversationId, null)
      dispatch({ type: "submit_rejected", failure })
      if (failure.kind === "api" && failure.code === "run_in_progress") {
        // Show the run the server is actually working on.
        reload()
      }
    },
    [chat, conversationId],
  )

  const submit = useCallback((question: string) => send(newUuid(), question), [send])

  // First question from the /chat composer. Deferred by a timer that the
  // cleanup clears, so React StrictMode's mount → unmount → mount sends it once;
  // the record loses `auto` as soon as it is sent, so a reload only offers
  // "Gửi lại" with the same key instead of sending again by itself.
  // biome-ignore lint/correctness/useExhaustiveDependencies: once per mounted conversation
  useEffect(() => {
    const pending = readPending(conversationId)
    if (pending === null || pending.auto !== true) return
    const timer = window.setTimeout(() => void send(pending.key, pending.question), 0)
    return () => window.clearTimeout(timer)
  }, [conversationId])

  const retrySubmission = useCallback(() => {
    const submission = stateRef.current.submission
    if (submission !== null) void send(submission.key, submission.question)
  }, [send])

  const discardSubmission = useCallback(() => {
    if (retryTimer.current !== null) window.clearTimeout(retryTimer.current)
    submitAbort.current?.abort()
    writePending(conversationId, null)
    dispatch({ type: "submit_cleared" })
  }, [conversationId])

  const cancel = useCallback(
    async (runId: string) => {
      const result = await chat.cancel(runId)
      if (!mounted.current) return
      if (result.ok) {
        // The real state: "cancelled" only when the server says so; a running
        // run may report cancel_requested and finish later.
        dispatch({ type: "run", run: result.data })
        dispatch({ type: "notice", failure: null })
      } else if (result.failure.kind !== "cancelled") {
        dispatch({ type: "notice", failure: result.failure })
      }
    },
    [chat],
  )

  // Re-read a run whose state could not be loaded (not a new question).
  const retryRun = useCallback((runId: string) => dispatch({ type: "run_retry", runId }), [])

  return {
    state,
    submit,
    retrySubmission,
    discardSubmission,
    cancel,
    reload,
    retryRun,
    dismissNotice: () => dispatch({ type: "notice", failure: null }),
  }
}
