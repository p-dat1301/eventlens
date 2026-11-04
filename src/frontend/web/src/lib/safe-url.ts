/** Only absolute http(s) links from the backend are rendered as links. */
export const safeExternalUrl = (value: string): string | null => {
  try {
    const url = new URL(value)
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null
  } catch {
    return null
  }
}

export const hostOf = (value: string): string | null => {
  const safe = safeExternalUrl(value)
  return safe === null ? null : new URL(safe).hostname
}
