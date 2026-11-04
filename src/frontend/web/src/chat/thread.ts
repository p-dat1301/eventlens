import type { Conversation, Message, Run, RunAccepted } from "../api/chat"
import { isTerminal } from "../api/chat"
import type { ApiFailure } from "../api/http"

// Pure state of one conversation thread. A thread is a list of runs (one per
// question) ordered by sequence; each run's live state comes from
// GET /runs/{id}. Nothing here is guessed: a run without a server response
// shows only its question and the status the server reported.

export type RunEntry = {
  readonly runId: string
  readonly sequence: number
  readonly question: string
  readonly run: Run | null
  readonly statusUrl: string
  readonly loadFailure: ApiFailure | null
}

export type Submission = {
  readonly key: string
  readonly question: string
  readonly phase: "sending" | "uncertain" | "rejected"
  readonly failure: ApiFailure | null
  readonly attempts: number
}

export type ThreadState = {
  readonly status: "loading" | "ready" | "failed"
  readonly conversation: Conversation | null
  readonly runs: readonly RunEntry[]
  readonly failure: ApiFailure | null
  readonly submission: Submission | null
  /** Notice for actions on a run (cancel failure, conflict, …). */
  readonly notice: ApiFailure | null
}

export type ThreadAction =
  | { readonly type: "loading" }
  | {
      readonly type: "loaded"
      readonly conversation: Conversation
      readonly messages: readonly Message[]
    }
  | { readonly type: "load_failed"; readonly failure: ApiFailure }
  | { readonly type: "run"; readonly run: Run }
  | { readonly type: "run_failed"; readonly runId: string; readonly failure: ApiFailure }
  | { readonly type: "run_retry"; readonly runId: string }
  | { readonly type: "submit"; readonly key: string; readonly question: string }
  | { readonly type: "accepted"; readonly accepted: RunAccepted; readonly question: string }
  | { readonly type: "submit_uncertain"; readonly failure: ApiFailure }
  | { readonly type: "submit_rejected"; readonly failure: ApiFailure }
  | { readonly type: "submit_cleared" }
  | { readonly type: "notice"; readonly failure: ApiFailure | null }

export const initialThread: ThreadState = {
  status: "loading",
  conversation: null,
  runs: [],
  failure: null,
  submission: null,
  notice: null,
}

export const runPath = (runId: string) => `/api/v1/runs/${runId}`

/** One entry per user message (= one run), in sequence order. */
export const entriesFromMessages = (messages: readonly Message[]): RunEntry[] => {
  const byRun = new Map<string, RunEntry>()
  for (const message of [...messages].sort((a, b) => a.sequence - b.sequence)) {
    if (message.role !== "user" || byRun.has(message.run_id)) continue
    byRun.set(message.run_id, {
      runId: message.run_id,
      sequence: message.sequence,
      question: message.content,
      run: null,
      statusUrl: runPath(message.run_id),
      loadFailure: null,
    })
  }
  return [...byRun.values()]
}

const upsert = (runs: readonly RunEntry[], entry: RunEntry): RunEntry[] => {
  const index = runs.findIndex((item) => item.runId === entry.runId)
  const next = index === -1 ? [...runs, entry] : runs.map((item, i) => (i === index ? entry : item))
  return next.sort((a, b) => a.sequence - b.sequence)
}

export const threadReducer = (state: ThreadState, action: ThreadAction): ThreadState => {
  switch (action.type) {
    case "loading":
      return { ...state, status: "loading", failure: null }
    case "loaded": {
      const fresh = entriesFromMessages(action.messages)
      // Keep run details already fetched for entries that still exist.
      const known = new Map(state.runs.map((entry) => [entry.runId, entry]))
      const merged = fresh.map((entry) => {
        const previous = known.get(entry.runId)
        return previous?.run ? { ...entry, run: previous.run } : entry
      })
      // A just-accepted run may not be in the history page yet: keep it.
      for (const entry of state.runs) {
        if (!merged.some((item) => item.runId === entry.runId)) merged.push(entry)
      }
      return {
        ...state,
        status: "ready",
        conversation: action.conversation,
        runs: merged.sort((a, b) => a.sequence - b.sequence),
        failure: null,
      }
    }
    case "load_failed":
      return { ...state, status: "failed", failure: action.failure }
    case "run": {
      const existing = state.runs.find((entry) => entry.runId === action.run.run_id)
      // Never move a run backwards from a terminal state (late poll response).
      if (existing?.run && isTerminal(existing.run.status) && !isTerminal(action.run.status)) {
        return state
      }
      return {
        ...state,
        runs: upsert(state.runs, {
          runId: action.run.run_id,
          sequence: action.run.sequence,
          question: action.run.question,
          run: action.run,
          statusUrl: existing?.statusUrl ?? runPath(action.run.run_id),
          loadFailure: null,
        }),
      }
    }
    case "run_failed":
      return {
        ...state,
        runs: state.runs.map((entry) =>
          entry.runId === action.runId ? { ...entry, loadFailure: action.failure } : entry,
        ),
      }
    case "run_retry":
      return {
        ...state,
        runs: state.runs.map((entry) =>
          entry.runId === action.runId ? { ...entry, loadFailure: null } : entry,
        ),
      }
    case "submit":
      return {
        ...state,
        notice: null,
        submission: {
          key: action.key,
          question: action.question,
          phase: "sending",
          failure: null,
          attempts: (state.submission?.key === action.key ? state.submission.attempts : 0) + 1,
        },
      }
    case "accepted": {
      const existing = state.runs.find((entry) => entry.runId === action.accepted.run_id)
      const lastSequence = state.runs.at(-1)?.sequence ?? 0
      return {
        ...state,
        submission: null,
        runs: existing
          ? state.runs
          : upsert(state.runs, {
              runId: action.accepted.run_id,
              // Placeholder order until the run itself reports its sequence.
              sequence: lastSequence + 1,
              question: action.question,
              run: null,
              statusUrl: action.accepted.status_url,
              loadFailure: null,
            }),
      }
    }
    case "submit_uncertain":
      return state.submission === null
        ? state
        : {
            ...state,
            submission: { ...state.submission, phase: "uncertain", failure: action.failure },
          }
    case "submit_rejected":
      return state.submission === null
        ? state
        : {
            ...state,
            submission: { ...state.submission, phase: "rejected", failure: action.failure },
          }
    case "submit_cleared":
      return { ...state, submission: null }
    case "notice":
      return { ...state, notice: action.failure }
  }
}

/** The run the server still works on (at most one per conversation). */
export const activeRun = (state: ThreadState): RunEntry | null =>
  state.runs.find((entry) => entry.run !== null && !isTerminal(entry.run.status)) ?? null

/** Entries whose run state has not been read yet (history or just accepted). */
export const unhydrated = (state: ThreadState): readonly RunEntry[] =>
  state.runs.filter((entry) => entry.run === null && entry.loadFailure === null)

/** A submit outcome where the server may or may not have stored the run. */
export const isUncertain = (failure: ApiFailure): boolean =>
  failure.kind === "timeout" ||
  failure.kind === "network" ||
  failure.kind === "unavailable" ||
  (failure.kind === "api" && failure.status >= 500 && failure.code !== "model_unavailable")
