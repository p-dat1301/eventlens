// Backend offsets count Unicode code points of NFC text (Python str indices).
// JavaScript strings index UTF-16 units, which differ for characters outside
// the BMP, so every slice goes through Array.from.

export const codePoints = (text: string): readonly string[] => Array.from(text)

export type Span = {
  readonly start_offset: number
  readonly end_offset: number
  readonly excerpt: string
}

/** True when `span` addresses exactly its excerpt inside `points`. */
export const spanMatches = (points: readonly string[], span: Span): boolean =>
  span.start_offset >= 0 &&
  span.end_offset <= points.length &&
  span.end_offset > span.start_offset &&
  points.slice(span.start_offset, span.end_offset).join("") === span.excerpt

export type Paragraph = { readonly start: number; readonly end: number; readonly text: string }

/** Splits plain text into paragraphs on blank lines/newlines, keeping code-point offsets. */
export const paragraphs = (points: readonly string[]): readonly Paragraph[] => {
  const result: Paragraph[] = []
  let start = 0
  const flush = (end: number) => {
    let s = start
    let e = end
    while (s < e && /\s/.test(points[s] ?? "")) s += 1
    while (e > s && /\s/.test(points[e - 1] ?? "")) e -= 1
    if (e > s) result.push({ start: s, end: e, text: points.slice(s, e).join("") })
  }
  for (let i = 0; i < points.length; i += 1) {
    if (points[i] === "\n") {
      flush(i)
      start = i + 1
    }
  }
  flush(points.length)
  return result
}

export type Segment = { readonly text: string; readonly marked: boolean }

/** Splits one paragraph into plain/marked segments for a highlighted span. */
export const segmentParagraph = (
  points: readonly string[],
  paragraph: Paragraph,
  span: { readonly start_offset: number; readonly end_offset: number } | null,
): readonly Segment[] => {
  if (span === null || span.end_offset <= paragraph.start || span.start_offset >= paragraph.end) {
    return [{ text: paragraph.text, marked: false }]
  }
  const s = Math.max(span.start_offset, paragraph.start)
  const e = Math.min(span.end_offset, paragraph.end)
  return [
    { text: points.slice(paragraph.start, s).join(""), marked: false },
    { text: points.slice(s, e).join(""), marked: true },
    { text: points.slice(e, paragraph.end).join(""), marked: false },
  ].filter((segment) => segment.text.length > 0)
}
