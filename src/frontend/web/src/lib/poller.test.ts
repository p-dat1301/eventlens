import { describe, expect, test } from "bun:test"

import { backoff, type Clock, type PollOutcome, startPoller, type Visibility } from "./poller"

// Deterministic clock: timers run only when the test advances time.
const fakeClock = () => {
  let now = 0
  let id = 0
  const timers = new Map<number, { at: number; callback: () => void }>()
  const clock: Clock = {
    now: () => now,
    setTimeout: (callback, ms) => {
      id += 1
      timers.set(id, { at: now + ms, callback })
      return id
    },
    clearTimeout: (handle) => {
      timers.delete(handle as number)
    },
  }
  const advance = async (ms: number) => {
    now += ms
    for (const [key, timer] of [...timers]) {
      if (timer.at <= now) {
        timers.delete(key)
        timer.callback()
      }
    }
    await Promise.resolve()
    await Promise.resolve()
  }
  return { clock, advance, pending: () => [...timers.values()].map((t) => t.at - now) }
}

const fakeVisibility = () => {
  let hidden = false
  const listeners = new Set<() => void>()
  const visibility: Visibility = {
    hidden: () => hidden,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
  const set = (value: boolean) => {
    hidden = value
    for (const listener of listeners) listener()
  }
  return { visibility, set, listeners }
}

const flush = async () => {
  for (let i = 0; i < 4; i += 1) await Promise.resolve()
}

describe("poller", () => {
  test("backs off between ticks and stops on done", async () => {
    const { clock, advance, pending } = fakeClock()
    const { visibility } = fakeVisibility()
    const outcomes: PollOutcome[] = ["continue", "continue", "done"]
    let ticks = 0
    startPoller({
      clock,
      visibility,
      delays: backoff(1000, 2, 5000),
      tick: async () => {
        ticks += 1
        return outcomes.shift() ?? "done"
      },
    })
    await flush()
    expect(ticks).toBe(1)
    expect(pending()).toEqual([2000])
    await advance(2000)
    await flush()
    expect(ticks).toBe(2)
    expect(pending()).toEqual([4000])
    await advance(4000)
    await flush()
    expect(ticks).toBe(3)
    expect(pending()).toEqual([])
  })

  test("never overlaps: a slow tick holds the next one", async () => {
    const { clock, advance, pending } = fakeClock()
    const { visibility, set } = fakeVisibility()
    let release: (value: PollOutcome) => void = () => undefined
    let ticks = 0
    startPoller({
      clock,
      visibility,
      delays: () => 100,
      tick: () => {
        ticks += 1
        return new Promise<PollOutcome>((resolve) => {
          release = resolve
        })
      },
    })
    await flush()
    // Visibility churn while a tick is in flight must not start another one.
    set(true)
    set(false)
    await advance(1000)
    expect(ticks).toBe(1)
    expect(pending()).toEqual([])
    release("continue")
    await flush()
    expect(pending()).toEqual([100])
  })

  test("pauses while hidden and syncs immediately when visible again", async () => {
    const { clock, advance } = fakeClock()
    const { visibility, set } = fakeVisibility()
    let ticks = 0
    startPoller({
      clock,
      visibility,
      delays: () => 1000,
      tick: async () => {
        ticks += 1
        return "continue"
      },
    })
    await flush()
    expect(ticks).toBe(1)
    set(true)
    await advance(5000)
    await flush()
    expect(ticks).toBe(1)
    set(false)
    await flush()
    expect(ticks).toBe(2)
  })

  test("stop aborts the in-flight request, clears timers and unsubscribes", async () => {
    const { clock, pending } = fakeClock()
    const { visibility, listeners } = fakeVisibility()
    let signal: AbortSignal | null = null
    const stop = startPoller({
      clock,
      visibility,
      delays: () => 1000,
      tick: (given) => {
        signal = given
        return new Promise<PollOutcome>(() => undefined)
      },
    })
    await flush()
    stop()
    expect(signal).not.toBeNull()
    expect((signal as AbortSignal | null)?.aborted).toBe(true)
    expect(pending()).toEqual([])
    expect(listeners.size).toBe(0)
  })

  test("reports exhaustion once after the attempt budget", async () => {
    const { clock, advance } = fakeClock()
    const { visibility } = fakeVisibility()
    let exhausted = 0
    let ticks = 0
    startPoller({
      clock,
      visibility,
      delays: () => 10,
      maxAttempts: 3,
      tick: async () => {
        ticks += 1
        return "continue"
      },
      onExhausted: () => {
        exhausted += 1
      },
    })
    for (let i = 0; i < 6; i += 1) {
      await flush()
      await advance(10)
    }
    expect(ticks).toBe(3)
    expect(exhausted).toBe(1)
  })
})
