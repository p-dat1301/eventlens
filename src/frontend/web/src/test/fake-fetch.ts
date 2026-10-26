import type { FetchLike } from "../briefing/client"

type Reply = Response | Error | (() => Promise<Response>)

export type RecordedCall = { readonly url: string; readonly init: RequestInit }

// Scripted fetch: each call consumes the next reply; calls are recorded.
export const scriptedFetch = (...replies: Reply[]) => {
  const queue = [...replies]
  const calls: RecordedCall[] = []
  const fetch: FetchLike = async (url, init) => {
    calls.push({ url, init })
    const reply = queue.shift()
    if (reply === undefined) {
      throw new Error(`unexpected fetch ${url}`)
    }
    if (reply instanceof Error) {
      throw reply
    }
    return reply instanceof Response ? reply : reply()
  }
  return { fetch, calls }
}

// A reply that stays pending until released, for observing loading states.
export const deferred = () => {
  let release: (response: Response) => void = () => undefined
  const pending = new Promise<Response>((resolve) => {
    release = resolve
  })
  return { reply: () => pending, release: (response: Response) => release(response) }
}
