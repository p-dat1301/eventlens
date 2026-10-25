import type { z } from "zod"

import {
  type BriefingErrorCode,
  BriefingErrorSchema,
  type BriefingRequest,
  BriefingRequestSchema,
  type BriefingResponse,
  BriefingResponseSchema,
  type BriefingSource,
  BriefingSourceSchema,
} from "./contract"

export type ClientFailure =
  | { readonly kind: "api"; readonly status: number; readonly code: BriefingErrorCode }
  | { readonly kind: "network" }
  | { readonly kind: "malformed"; readonly status: number }
  | { readonly kind: "cancelled" }

export type ClientResult<T> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly failure: ClientFailure }

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>

export type BriefingClient = {
  readonly submit: (
    request: BriefingRequest,
    signal?: AbortSignal,
  ) => Promise<ClientResult<BriefingResponse>>
  readonly inspectSource: (
    sourceRef: string,
    signal?: AbortSignal,
  ) => Promise<ClientResult<BriefingSource>>
}

type ClientOptions = {
  readonly baseUrl?: string
  readonly fetch?: FetchLike
}

const GATEWAY_STATUSES = new Set([502, 503, 504])

const failed = (failure: ClientFailure): ClientResult<never> => ({ ok: false, failure })

const isAbort = (error: unknown): boolean =>
  error instanceof DOMException && error.name === "AbortError"

const readJson = async (response: Response): Promise<unknown> => {
  try {
    return await response.json()
  } catch (error) {
    if (isAbort(error)) {
      throw error
    }
    return undefined
  }
}

const exchange = async <T>(
  send: () => Promise<Response>,
  schema: z.ZodType<T>,
): Promise<ClientResult<T>> => {
  let response: Response
  try {
    response = await send()
  } catch (error) {
    return failed(isAbort(error) ? { kind: "cancelled" } : { kind: "network" })
  }

  let body: unknown
  try {
    body = await readJson(response)
  } catch (error) {
    return failed(isAbort(error) ? { kind: "cancelled" } : { kind: "network" })
  }

  if (response.ok) {
    const parsed = schema.safeParse(body)
    return parsed.success
      ? { ok: true, data: parsed.data }
      : failed({ kind: "malformed", status: response.status })
  }

  // A gateway error without a JSON body means the service itself was unreachable.
  if (body === undefined && GATEWAY_STATUSES.has(response.status)) {
    return failed({ kind: "network" })
  }

  // Only the strict error envelope is trusted; any other body is never shown.
  const envelope = BriefingErrorSchema.safeParse(body)
  return envelope.success
    ? failed({ kind: "api", status: response.status, code: envelope.data.error.code })
    : failed({ kind: "malformed", status: response.status })
}

export const createBriefingClient = ({
  baseUrl = "",
  fetch: fetchImpl = (input, init) => fetch(input, init),
}: ClientOptions = {}): BriefingClient => ({
  submit: (request, signal) =>
    exchange(
      () =>
        fetchImpl(`${baseUrl}/briefing`, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/json" },
          body: JSON.stringify(BriefingRequestSchema.parse(request)),
          signal: signal ?? null,
        }),
      BriefingResponseSchema,
    ),
  inspectSource: (sourceRef, signal) =>
    exchange(
      () =>
        fetchImpl(`${baseUrl}/briefing/sources/${encodeURIComponent(sourceRef)}`, {
          method: "GET",
          headers: { accept: "application/json" },
          signal: signal ?? null,
        }),
      BriefingSourceSchema,
    ),
})
