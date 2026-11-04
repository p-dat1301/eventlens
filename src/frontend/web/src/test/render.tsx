import { act, StrictMode } from "react"
import { createRoot, type Root } from "react-dom/client"

import { App } from "../App"
import { createServices } from "../app/services"
import { resetPendingMemory } from "../chat/pending"
import { clearFeedCache } from "../news/feed"
import { resetTaxonomyCache } from "../news/taxonomy"
import { routedFetch } from "./news-fixtures"

let root: Root | null = null

export const mountApp = (
  path: string,
  routes: Parameters<typeof routedFetch>[0],
  options: { readonly strict?: boolean } = {},
) => {
  const routed = routedFetch(routes)
  clearFeedCache()
  resetTaxonomyCache()
  window.history.replaceState({ key: "test" }, "", path)
  const container = document.createElement("div")
  document.body.append(container)
  root = createRoot(container)
  const services = createServices({ fetch: routed.fetch })
  const app = <App services={services} />
  act(() => root?.render(options.strict ? <StrictMode>{app}</StrictMode> : app))
  return routed
}

export const unmountApp = () => {
  act(() => root?.unmount())
  root = null
  document.body.innerHTML = ""
  window.localStorage.clear()
  window.sessionStorage.clear()
  resetPendingMemory()
}

export const settle = async (rounds = 8) => {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
  }
}

export const text = () => document.body.textContent ?? ""

export const byText = <T extends HTMLElement = HTMLElement>(selector: string, label: string): T => {
  const found = [...document.querySelectorAll<T>(selector)].find((node) =>
    (node.textContent ?? "").replace(/\s+/g, " ").trim().includes(label),
  )
  if (found === undefined) throw new Error(`missing ${selector} "${label}"`)
  return found
}

export const click = async (node: HTMLElement) => {
  await act(async () => {
    node.click()
  })
  await settle()
}

export const go = async (path: string) => {
  const { navigate } = await import("../lib/router")
  await act(async () => navigate(path))
  await settle()
}

/** Sets a controlled textarea/input value the way React listens for it. */
export const typeInto = async (selector: string, value: string) => {
  const box = document.querySelector<HTMLTextAreaElement | HTMLInputElement>(selector)
  if (box === null) throw new Error(`missing ${selector}`)
  const proto = box instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto.prototype, "value")?.set?.call(box, value)
    box.dispatchEvent(new Event("input", { bubbles: true }))
  })
}
