import { useCallback, useEffect, useRef, useState } from "react"

import type { ApiFailure } from "../api/http"
import { useServices } from "../app/services"
import { newUuid } from "../lib/hooks"
import { navigate } from "../lib/router"
import { type DraftScope, toScopeRequest } from "./draft"
import { writePending } from "./pending"

/**
 * First question of a new conversation, sent from the /chat composer:
 * 1. `POST /conversations` with the draft scope — only now, never on render,
 *    focus or typing, and at most once per send intent (a ref blocks a double
 *    click before React re-renders);
 * 2. the new conversation id and a fresh Idempotency-Key are stored with the
 *    question *before* the run is submitted;
 * 3. the thread page submits it (same key on every retry, reload included).
 *
 * A failed create is never retried automatically and the question stays in
 * the box. The create endpoint has no server-side idempotency: when its
 * response is lost (timeout/network) the conversation may exist anyway; the
 * list is refreshed so it shows up, but exactly-once cannot be guaranteed.
 */
export const useFirstQuestion = (onSettled: () => void) => {
  const { chat } = useServices()
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState<ApiFailure | null>(null)
  const inFlight = useRef(false)
  const abort = useRef<AbortController | null>(null)

  useEffect(() => () => abort.current?.abort(), [])

  /** Resolves true once the question has been handed to its new conversation. */
  const send = useCallback(
    async (scope: DraftScope, question: string): Promise<boolean> => {
      if (inFlight.current) return false
      inFlight.current = true
      const controller = new AbortController()
      abort.current = controller
      setPending(true)
      setFailure(null)
      const result = await chat.createConversation(
        toScopeRequest(scope),
        conversationTitle(question),
        controller.signal,
      )
      inFlight.current = false
      if (controller.signal.aborted) return false
      setPending(false)
      if (!result.ok) {
        if (result.failure.kind !== "cancelled") setFailure(result.failure)
        // A lost response may hide a created conversation: show the real list.
        onSettled()
        return false
      }
      const id = result.data.conversation_id
      writePending(id, { key: newUuid(), question, auto: true })
      onSettled()
      navigate(`/chat/${id}`)
      return true
    },
    [chat, onSettled],
  )

  return { send, pending, failure, dismiss: () => setFailure(null) }
}

/** Conversation title: trimmed, bounded, never empty. */
export const conversationTitle = (text: string): string => {
  const clean = text.replace(/\s+/g, " ").trim()
  const points = Array.from(clean)
  return points.length > 120 ? `${points.slice(0, 119).join("")}…` : clean
}
