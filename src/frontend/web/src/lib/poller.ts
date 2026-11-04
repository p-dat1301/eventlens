// Polling loop used for run status and pending summaries:
// - one request in flight at a time (no overlap), each with its own AbortSignal;
// - delays grow with `delays(attempt)` (backoff) up to `maxAttempts`/`maxDurationMs`;
// - paused while the tab is hidden, resumed with an immediate tick when visible;
// - `stop()` aborts the in-flight request and clears every timer (unmount).

export type PollOutcome = "continue" | "done"

export type Clock = {
  readonly now: () => number
  readonly setTimeout: (callback: () => void, ms: number) => unknown
  readonly clearTimeout: (handle: unknown) => void
}

export type Visibility = {
  readonly hidden: () => boolean
  readonly subscribe: (listener: () => void) => () => void
}

export type PollerOptions = {
  readonly tick: (signal: AbortSignal) => Promise<PollOutcome>
  readonly delays: (attempt: number) => number
  readonly maxAttempts?: number
  readonly maxDurationMs?: number
  /** Called once when attempts/duration run out without a "done". */
  readonly onExhausted?: () => void
  /** Run the first tick immediately (default) or after delays(0). */
  readonly immediate?: boolean
  readonly clock?: Clock
  readonly visibility?: Visibility
}

const realClock: Clock = {
  now: () => Date.now(),
  setTimeout: (callback, ms) => window.setTimeout(callback, ms),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
}

export const documentVisibility: Visibility = {
  hidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
  subscribe: (listener) => {
    document.addEventListener("visibilitychange", listener)
    return () => document.removeEventListener("visibilitychange", listener)
  },
}

/** Backoff: base * factor^attempt, capped. */
export const backoff =
  (baseMs: number, factor: number, capMs: number) =>
  (attempt: number): number =>
    Math.min(capMs, Math.round(baseMs * factor ** attempt))

export const startPoller = (options: PollerOptions): (() => void) => {
  const clock = options.clock ?? realClock
  const visibility = options.visibility ?? documentVisibility
  const startedAt = clock.now()
  let attempt = 0
  let timer: unknown = null
  let controller: AbortController | null = null
  let stopped = false
  let inFlight = false
  let pendingWhileHidden = false

  const exhausted = () =>
    (options.maxAttempts !== undefined && attempt >= options.maxAttempts) ||
    (options.maxDurationMs !== undefined && clock.now() - startedAt >= options.maxDurationMs)

  const finish = () => {
    stopped = true
    unsubscribe()
  }

  const schedule = (ms: number) => {
    if (stopped) return
    timer = clock.setTimeout(() => {
      timer = null
      void run()
    }, ms)
  }

  const run = async () => {
    if (stopped || inFlight) return
    if (visibility.hidden()) {
      pendingWhileHidden = true
      return
    }
    if (exhausted()) {
      finish()
      options.onExhausted?.()
      return
    }
    inFlight = true
    controller = new AbortController()
    let outcome: PollOutcome = "continue"
    try {
      outcome = await options.tick(controller.signal)
    } catch {
      outcome = "continue"
    } finally {
      inFlight = false
      controller = null
    }
    if (stopped) return
    if (outcome === "done") {
      finish()
      return
    }
    attempt += 1
    schedule(options.delays(attempt))
  }

  const onVisibility = () => {
    if (stopped || visibility.hidden()) return
    // Back in view: sync now instead of waiting for the remaining delay.
    if (pendingWhileHidden || timer !== null) {
      pendingWhileHidden = false
      if (timer !== null) {
        clock.clearTimeout(timer)
        timer = null
      }
      void run()
    }
  }
  const unsubscribe = visibility.subscribe(onVisibility)

  if (options.immediate === false) schedule(options.delays(0))
  else void run()

  return () => {
    if (timer !== null) clock.clearTimeout(timer)
    timer = null
    controller?.abort()
    finish()
  }
}
