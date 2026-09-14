import { z } from "zod"

const FoundationStateSchema = z
  .strictObject({
    project: z.literal("EventLens"),
    phase: z.literal("foundation"),
    dataContract: z.literal("article.v1"),
    apiHealth: z.literal("not_connected"),
  })
  .readonly()

export type FoundationState = z.infer<typeof FoundationStateSchema>

export const parseFoundationState = (input: unknown): FoundationState =>
  FoundationStateSchema.parse(input)
