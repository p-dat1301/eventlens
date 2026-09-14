import { describe, expect, test } from "bun:test"

import { parseFoundationState } from "./foundation"

describe("parseFoundationState", () => {
  test("accepts article.v1 when foundation boundary is valid", () => {
    // Given
    const input: unknown = {
      project: "EventLens",
      phase: "foundation",
      dataContract: "article.v1",
      apiHealth: "not_connected",
    }

    // When
    const state = parseFoundationState(input)

    // Then
    expect(state.dataContract).toBe("article.v1")
  })

  test("rejects unknown fields when foundation boundary is not exact", () => {
    // Given
    const input: unknown = {
      project: "EventLens",
      phase: "foundation",
      dataContract: "article.v1",
      apiHealth: "not_connected",
      dashboard: "not_requested",
    }

    // When
    const parse = () => parseFoundationState(input)

    // Then
    expect(parse).toThrow()
  })
})
