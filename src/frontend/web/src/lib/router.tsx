import {
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useSyncExternalStore,
} from "react"

// Minimal History-API router: four routes do not justify a routing library.
// Each history entry carries a key; the scroll position of the entry being left
// is saved under that key so Back restores where the reader was (after the
// page says its content is ready), while new navigations start at the top.

export type RouterLocation = {
  readonly pathname: string
  readonly search: string
  readonly hash: string
  readonly key: string
  /** "pop" for Back/Forward and first load, "push"/"replace" otherwise. */
  readonly action: "pop" | "push" | "replace"
}

const SCROLL_KEY = "eventlens.scroll"
const listeners = new Set<() => void>()
let lastAction: RouterLocation["action"] = "pop"
let snapshot: RouterLocation | null = null

const newKey = () => Math.random().toString(36).slice(2, 10)

const currentKey = (): string => {
  const state: unknown = window.history.state
  if (typeof state === "object" && state !== null) {
    const key = Reflect.get(state, "key")
    if (typeof key === "string") return key
  }
  const key = newKey()
  window.history.replaceState({ key }, "")
  return key
}

const readLocation = (): RouterLocation => {
  const key = currentKey()
  const { pathname, search, hash } = window.location
  if (
    snapshot === null ||
    snapshot.key !== key ||
    snapshot.pathname !== pathname ||
    snapshot.search !== search ||
    snapshot.hash !== hash ||
    snapshot.action !== lastAction
  ) {
    snapshot = { pathname, search, hash, key, action: lastAction }
  }
  return snapshot
}

const emit = () => {
  for (const listener of listeners) listener()
}

const readPositions = (): Record<string, number> => {
  try {
    const parsed: unknown = JSON.parse(window.sessionStorage.getItem(SCROLL_KEY) ?? "{}")
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, number>) : {}
  } catch {
    return {}
  }
}

const savePosition = () => {
  try {
    const positions = readPositions()
    positions[currentKey()] = window.scrollY
    const keys = Object.keys(positions)
    // Bounded: only the most recent entries matter for Back.
    for (const key of keys.slice(0, Math.max(0, keys.length - 50))) delete positions[key]
    window.sessionStorage.setItem(SCROLL_KEY, JSON.stringify(positions))
  } catch {
    // Storage unavailable: Back still works, it just starts at the top.
  }
}

let installed = false
const install = () => {
  if (installed) return
  installed = true
  if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual"
  window.addEventListener("popstate", () => {
    lastAction = "pop"
    emit()
  })
  // Remember the position on reload/close too.
  window.addEventListener("pagehide", savePosition)
}

const subscribe = (listener: () => void) => {
  install()
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// key of an entry created by push -> in-app path it was opened from (this tab only).
const origins = new Map<string, string>()

/** In-app path this history entry was opened from, if it was a push in this tab. */
export const originOf = (key: string): string | undefined => origins.get(key)

export const navigate = (to: string, options: { readonly replace?: boolean } = {}) => {
  const target = new URL(to, window.location.href)
  if (target.origin !== window.location.origin) return
  const path = `${target.pathname}${target.search}${target.hash}`
  if (options.replace === true) {
    lastAction = "replace"
    window.history.replaceState({ key: currentKey() }, "", path)
  } else {
    savePosition()
    lastAction = "push"
    const key = newKey()
    origins.set(key, `${window.location.pathname}${window.location.search}`)
    window.history.pushState({ key }, "", path)
  }
  emit()
}

export const useLocation = (): RouterLocation =>
  useSyncExternalStore(subscribe, readLocation, readLocation)

/**
 * Restores the saved position of this entry on Back/Forward once `ready` is
 * true (content laid out); scrolls to the top on a new navigation.
 */
export const useScrollRestoration = (location: RouterLocation, ready: boolean) => {
  useLayoutEffect(() => {
    if (!ready) return
    if (location.action === "pop") {
      const saved = readPositions()[location.key]
      if (saved !== undefined) window.scrollTo({ top: saved, behavior: "instant" })
    } else if (location.action === "push" && location.hash === "") {
      window.scrollTo({ top: 0, behavior: "instant" })
    }
  }, [location.key, location.action, location.hash, ready])
}

/** Page title for screen readers and history entries. */
export const useDocumentTitle = (title: string) => {
  useEffect(() => {
    document.title = title === "" ? "EventLens" : `${title} · EventLens`
  }, [title])
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  readonly to: string
  readonly replace?: boolean
  readonly children: ReactNode
}

export const Link = ({ to, replace, onClick, children, ...rest }: LinkProps) => {
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event)
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      rest.target === "_blank"
    ) {
      return
    }
    event.preventDefault()
    navigate(to, replace === undefined ? {} : { replace })
  }
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  )
}

export type RouteMatch =
  | { readonly name: "news" }
  | { readonly name: "article"; readonly id: string }
  | { readonly name: "chat"; readonly id: string | null }
  | { readonly name: "analyst" }
  | { readonly name: "home" }
  | { readonly name: "not_found" }

const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

export const matchRoute = (pathname: string): RouteMatch => {
  const path = pathname.replace(/\/+$/, "") || "/"
  if (path === "/") return { name: "home" }
  if (path === "/news") return { name: "news" }
  if (path === "/chat") return { name: "chat", id: null }
  if (path === "/analyst") return { name: "analyst" }
  const article = new RegExp(`^/articles/(${UUID})$`).exec(path)
  if (article?.[1] !== undefined) return { name: "article", id: article[1].toLowerCase() }
  const chat = new RegExp(`^/chat/(${UUID})$`).exec(path)
  if (chat?.[1] !== undefined) return { name: "chat", id: chat[1].toLowerCase() }
  return { name: "not_found" }
}
