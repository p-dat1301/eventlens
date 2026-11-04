// Calendar days are chosen in Asia/Ho_Chi_Minh (UTC+07:00 all year, no DST)
// and sent as UTC instants with an explicit offset; ranges are [after, before).

const HCM_OFFSET_MS = 7 * 60 * 60 * 1000
const DAY_MS = 24 * 60 * 60 * 1000
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/

/** Parses `YYYY-MM-DD`; rejects impossible dates such as 2026-02-30. */
export const parseDay = (value: string): { y: number; m: number; d: number } | null => {
  const match = ISO_DAY.exec(value)
  if (match === null) return null
  const [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const probe = new Date(Date.UTC(y, m - 1, d))
  return probe.getUTCFullYear() === y && probe.getUTCMonth() === m - 1 && probe.getUTCDate() === d
    ? { y, m, d }
    : null
}

const startOfHcmDayUtcMs = (day: string): number | null => {
  const parsed = parseDay(day)
  return parsed === null ? null : Date.UTC(parsed.y, parsed.m - 1, parsed.d) - HCM_OFFSET_MS
}

const isoZ = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z")

/** `from` day (inclusive) → `published_after`, e.g. 2026-10-29 → 2026-10-28T17:00:00Z. */
export const dayStartUtc = (day: string): string | null => {
  const ms = startOfHcmDayUtcMs(day)
  return ms === null ? null : isoZ(ms)
}

/** `to` day (inclusive) → exclusive `published_before` = start of the next day. */
export const dayEndExclusiveUtc = (day: string): string | null => {
  const ms = startOfHcmDayUtcMs(day)
  return ms === null ? null : isoZ(ms + DAY_MS)
}

/** Calendar day in Asia/Ho_Chi_Minh of an instant (`YYYY-MM-DD`). */
export const hcmDayOf = (instant: Date): string => {
  const shifted = new Date(instant.getTime() + HCM_OFFSET_MS)
  return shifted.toISOString().slice(0, 10)
}

/** Inverse of dayStartUtc / dayEndExclusiveUtc for displaying stored scopes. */
export const hcmDayFromUtc = (iso: string, exclusiveEnd: boolean): string | null => {
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return null
  return hcmDayOf(new Date(exclusiveEnd ? ms - 1 : ms))
}

const DATE_TIME = new Intl.DateTimeFormat("vi-VN", {
  timeZone: "Asia/Ho_Chi_Minh",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
})

const DATE_ONLY = new Intl.DateTimeFormat("vi-VN", {
  timeZone: "Asia/Ho_Chi_Minh",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
})

/** "14:05, 29/10/2026" in Vietnam time; null → "Chưa rõ thời gian đăng". */
export const formatDateTime = (iso: string | null): string => {
  if (iso === null) return "Chưa rõ thời gian đăng"
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return "Chưa rõ thời gian đăng"
  const parts = DATE_TIME.formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? ""
  return `${part("hour")}:${part("minute")}, ${part("day")}/${part("month")}/${part("year")}`
}

export const formatDay = (day: string): string => {
  const parsed = parseDay(day)
  if (parsed === null) return day
  return DATE_ONLY.format(new Date(Date.UTC(parsed.y, parsed.m - 1, parsed.d, 5)))
}

const WEEKDAY_DATE = new Intl.DateTimeFormat("vi-VN", {
  timeZone: "Asia/Ho_Chi_Minh",
  weekday: "long",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
})

/** Day heading of the feed: "Thứ Tư, 29/10/2026" (calendar day in Vietnam time). */
export const formatDayHeading = (day: string): string => {
  const parsed = parseDay(day)
  if (parsed === null) return day
  const label = WEEKDAY_DATE.format(new Date(Date.UTC(parsed.y, parsed.m - 1, parsed.d, 5)))
  return label.charAt(0).toUpperCase() + label.slice(1)
}

/** "14:05" in Vietnam time; null when the instant is missing or invalid. */
export const formatTime = (iso: string | null): string | null => {
  if (iso === null) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const parts = DATE_TIME.formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? ""
  return `${part("hour")}:${part("minute")}`
}
