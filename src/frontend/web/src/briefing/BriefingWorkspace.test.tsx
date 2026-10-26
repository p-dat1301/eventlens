import { afterEach, describe, expect, test } from "bun:test"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"

import { App } from "../App"
import { deferred, scriptedFetch } from "../test/fake-fetch"
import {
  answeredResponse,
  CLAIM_CLOSE,
  CLAIM_REOPEN,
  errorBody,
  json,
  MODEL_DOUBT,
  modelAbstainedResponse,
  noEvidenceResponse,
  partialCoverageResponse,
  REF_CLOSE,
  sourceResponse,
} from "../test/fixtures"
import { createBriefingClient } from "./client"

let root: Root | null = null

afterEach(() => {
  act(() => root?.unmount())
  root = null
  document.body.innerHTML = ""
})

const mount = (...replies: Parameters<typeof scriptedFetch>) => {
  const scripted = scriptedFetch(...replies)
  const container = document.createElement("div")
  document.body.append(container)
  root = createRoot(container)
  const client = createBriefingClient({ fetch: scripted.fetch })
  act(() => root?.render(<App client={client} />))
  return { ...scripted, container }
}

const flush = async () => {
  for (let tick = 0; tick < 5; tick += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
  }
}

const query = (selector: string): HTMLElement => {
  const element = document.querySelector<HTMLElement>(selector)
  if (element === null) {
    throw new Error(`missing ${selector}`)
  }
  return element
}

const button = (label: string): HTMLButtonElement => {
  const match = [...document.querySelectorAll("button")].find(
    (candidate) => candidate.textContent?.trim() === label,
  )
  if (match === undefined) {
    throw new Error(`missing button ${label}`)
  }
  return match
}

const buttons = (label: string): HTMLButtonElement[] =>
  [...document.querySelectorAll("button")].filter(
    (candidate) => candidate.textContent?.trim() === label,
  )

const ask = async (text: string) => {
  const input = query("textarea")
  if (!(input instanceof HTMLTextAreaElement)) {
    throw new Error("query field is not a textarea")
  }
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set
    setter?.call(input, text)
    input.dispatchEvent(new Event("input", { bubbles: true }))
  })
  act(() => button("Request briefing").click())
  await flush()
}

const text = () => document.body.textContent ?? ""

describe("analyst briefing flow", () => {
  test("renders landmarks, a labelled field and the idle state", () => {
    mount()

    expect(document.querySelectorAll("header.site-header, main#main-content, footer").length).toBe(
      3,
    )
    const label = query("label")
    expect(query(`#${CSS.escape(label.getAttribute("for") ?? "")}`).tagName).toBe("TEXTAREA")
    expect(query("a.skip-link").getAttribute("href")).toBe("#main-content")
    expect(text()).toContain("No briefing requested yet")
  })

  test("blocks an empty question without calling the API", async () => {
    const { calls } = mount()

    await ask("   ")

    expect(calls).toHaveLength(0)
    expect(query("[role=alert]").textContent).toContain("Enter a question")
    expect(query("textarea").getAttribute("aria-invalid")).toBe("true")
    expect(query("textarea").getAttribute("aria-describedby")).toContain(query("[role=alert]").id)
  })

  test("shows a loading state while the briefing is in flight", async () => {
    const pending = deferred()
    mount(pending.reply)

    await ask("When did East Pier close and reopen?")

    expect(query("[role=status]").textContent).toContain("Retrieving evidence")
    expect(query(".result-panel").getAttribute("aria-busy")).toBe("true")

    pending.release(json(answeredResponse()))
    await flush()
    expect(document.querySelector("[role=status]")).toBeNull()
    expect(query(".result-panel").getAttribute("aria-busy")).toBe("false")
  })

  test("renders answer, claims, citations under the right claim and the audit", async () => {
    mount(json(answeredResponse()))

    await ask("When did East Pier close and reopen?")

    expect(query("#answer-title").textContent).toBe("Answer")
    expect(query(".briefing-result").dataset["outcome"]).toBe("answered")
    const closeClaim = query(`li.claim-item[data-claim-id="${CLAIM_CLOSE}"]`)
    const reopenClaim = query(`li.claim-item[data-claim-id="${CLAIM_REOPEN}"]`)
    expect(closeClaim.querySelector(".citation-title")?.textContent).toBe(
      "Duplicate harbor closure bulletin",
    )
    expect(closeClaim.querySelectorAll(".citation")).toHaveLength(1)
    expect(reopenClaim.querySelector(".citation-title")?.textContent).toBe(
      "Follow-up reopens East Pier gate",
    )
    expect(reopenClaim.querySelector(".citation-meta")?.textContent).toContain("fixture.example")
    expect(reopenClaim.querySelector("blockquote")?.textContent).toContain("reopened at 14:30")
    expect(reopenClaim.querySelector("a")?.getAttribute("href")).toBe(
      "https://fixture.example/rag/02",
    )
    expect(text()).toContain("source_published_at")
    expect(text()).toContain("Keyword only")
    expect(text()).toContain("Coverage complete")
    expect(text()).not.toContain("synthesis-prompt.v1")
  })

  test("opens a citation in the source viewer with the cited span highlighted", async () => {
    const { calls } = mount(json(answeredResponse()), json(sourceResponse()))
    await ask("When did East Pier close and reopen?")

    const inspect = buttons("Inspect source")[0]
    act(() => inspect?.click())
    await flush()

    expect(calls[1]?.url).toBe(`/briefing/sources/${REF_CLOSE}`)
    const viewer = query("#source-viewer")
    expect(viewer.tagName).toBe("ASIDE")
    expect(viewer.querySelector("mark")?.textContent).toContain("East Pier gate closes at 08:00")
    expect(viewer.textContent).toContain("Later paragraph without citation.")
    expect(document.activeElement?.id).toBe("source-viewer-title")
    expect(inspect?.getAttribute("aria-pressed")).toBe("true")

    act(() => button("Close source").click())
    expect(document.querySelector("#source-viewer")).toBeNull()
    expect(document.activeElement).toBe(inspect ?? null)
  })

  test("closes the source viewer with Escape", async () => {
    mount(json(answeredResponse()), json(sourceResponse()))
    await ask("East Pier")
    act(() => buttons("Inspect source")[0]?.click())
    await flush()

    act(() => {
      query("#source-viewer-title").dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      )
    })

    expect(document.querySelector("#source-viewer")).toBeNull()
  })

  test.each([
    ["source_stale", 410, "Source changed"],
    ["source_not_found", 404, "Source not found"],
  ])("shows %s from the source viewer as an error", async (code, status, title) => {
    mount(json(answeredResponse()), json(errorBody(code), status))
    await ask("East Pier")

    act(() => buttons("Inspect source")[1]?.click())
    await flush()

    const alert = query("#source-viewer [role=alert]")
    expect(alert.textContent).toContain(title)
    expect(alert.textContent).toContain(`Code ${code}`)
    expect(document.querySelector("#source-viewer mark")).toBeNull()
  })

  test("shows no-evidence as an abstention, not as an answer", async () => {
    mount(json(noEvidenceResponse()))

    await ask("Unrelated question")

    expect(document.querySelector("#answer-title")).toBeNull()
    expect(document.querySelector(".claim-item")).toBeNull()
    expect(query("#abstention-title").textContent).toBe("No supporting evidence found")
    expect(text()).toContain("No evidence")
    expect(text()).toContain("Reason: No matches")
    expect(text()).toContain("No retrieval matches")
  })

  test("shows a model abstention with its uncertainty", async () => {
    mount(json(modelAbstainedResponse()))

    await ask("Did the gate reopen the same day?")

    expect(document.querySelector("#answer-title")).toBeNull()
    expect(query("#abstention-title").textContent).toBe("The model declined to answer")
    expect(text()).toContain("Model abstained")
    expect(query(".uncertainty-list").textContent).toContain(MODEL_DOUBT)
    expect(query(".uncertainty-list").textContent).toContain("Source conflict")
  })

  test("labels partial coverage honestly alongside the answer", async () => {
    mount(json(partialCoverageResponse()))

    await ask("East Pier")

    expect(text()).toContain("Partial coverage")
    const warning = query("[aria-label='Coverage warning']")
    expect(warning.textContent).toContain("covered only part of the corpus")
    expect(warning.textContent).toContain("Semantic retrieval failed")
    expect(query("#answer-title").textContent).toBe("Answer")
  })

  test("does not render a malformed response as an answer", async () => {
    mount(json({ ...answeredResponse(), raw_model_output: "{}" }))

    await ask("East Pier")

    expect(document.querySelector(".briefing-result")).toBeNull()
    expect(query("[role=alert]").textContent).toContain("Unreadable response")
    expect(text()).not.toContain("Harbor Authority bulletin")
  })

  test("retries after a network failure and then shows the answer", async () => {
    const { calls } = mount(new TypeError("Failed to fetch"), json(answeredResponse()))

    await ask("East Pier")
    expect(query("[role=alert]").textContent).toContain("Briefing service unreachable")
    expect(query("[role=alert]").textContent).toContain("attempt 1")

    act(() => button("Retry briefing").click())
    await flush()

    expect(calls).toHaveLength(2)
    expect(calls[1]?.init.body).toBe(calls[0]?.init.body)
    expect(query("#answer-title").textContent).toBe("Answer")
  })

  test("shows the retry loading state for a 5xx failure", async () => {
    const pending = deferred()
    mount(json(errorBody("model_unavailable"), 503), pending.reply)

    await ask("East Pier")
    expect(query("[role=alert]").textContent).toContain("Model unavailable")
    expect(query("[role=alert]").textContent).toContain("Code model_unavailable · HTTP 503")

    act(() => button("Retry briefing").click())
    await flush()
    expect(query("[role=status]").textContent).toContain("attempt 2")

    pending.release(json(errorBody("internal_error"), 500))
    await flush()
    expect(query("[role=alert]").textContent).toContain("attempt 2")
  })

  test("offers no retry for a non-retryable request error", async () => {
    mount(json(errorBody("invalid_query"), 400))

    await ask("East Pier")

    expect(query("[role=alert]").textContent).toContain("Question rejected")
    expect(buttons("Retry briefing")).toHaveLength(0)
  })

  test("keeps every interactive control reachable by keyboard", async () => {
    mount(json(answeredResponse()))
    await ask("East Pier")

    const controls = [...document.querySelectorAll<HTMLElement>("a, button, textarea")]
    expect(controls.length).toBeGreaterThan(5)
    for (const control of controls) {
      expect(control.tabIndex).toBeGreaterThanOrEqual(0)
      if (control.tagName === "BUTTON") {
        expect(control.getAttribute("type")).toMatch(/^(button|submit)$/)
      }
    }
  })
})
