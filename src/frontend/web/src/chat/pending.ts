// The question being sent in one conversation, kept in sessionStorage so its
// Idempotency-Key survives a reload until the server answers definitively.
// `auto` marks a first question handed over by the /chat composer right after
// it created the conversation: the thread sends it once, with this key.

export type PendingRecord = {
  readonly key: string
  readonly question: string
  readonly auto?: boolean
}

const PENDING_PREFIX = "eventlens.pending-question."
// Same-tab fallback when sessionStorage is unavailable (private mode, quota):
// the hand-over to the thread still works, only reload recovery is lost.
const memory = new Map<string, PendingRecord>()

export const readPending = (conversationId: string): PendingRecord | null => {
  try {
    const raw = window.sessionStorage.getItem(PENDING_PREFIX + conversationId)
    if (raw === null) return memory.get(conversationId) ?? null
    const value: unknown = JSON.parse(raw)
    if (typeof value !== "object" || value === null) return null
    const key = Reflect.get(value, "key")
    const question = Reflect.get(value, "question")
    const auto = Reflect.get(value, "auto") === true
    return typeof key === "string" && typeof question === "string"
      ? { key, question, ...(auto ? { auto } : {}) }
      : null
  } catch {
    return memory.get(conversationId) ?? null
  }
}

/** False when storage is unavailable (in-page retry still works, reload does not). */
export const writePending = (conversationId: string, record: PendingRecord | null): boolean => {
  if (record === null) memory.delete(conversationId)
  else memory.set(conversationId, record)
  try {
    if (record === null) window.sessionStorage.removeItem(PENDING_PREFIX + conversationId)
    else window.sessionStorage.setItem(PENDING_PREFIX + conversationId, JSON.stringify(record))
    return true
  } catch {
    return false
  }
}

/** Tests only: forget same-tab fallbacks between cases. */
export const resetPendingMemory = () => memory.clear()
