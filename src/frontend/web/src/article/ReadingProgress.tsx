import { type RefObject, useEffect, useRef } from "react"

/**
 * Thin bar under the sticky nav showing how much of the article *body* has
 * been read (not the whole page). Updated with transform only, once per frame,
 * from a passive scroll listener; removed on unmount. Purely decorative for
 * assistive tech (the text itself is the content).
 */
export const ReadingProgress = ({ target }: { readonly target: RefObject<HTMLElement | null> }) => {
  const bar = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let frame: number | null = null
    const update = () => {
      frame = null
      const node = target.current
      const fill = bar.current
      if (node === null || fill === null) return
      const rect = node.getBoundingClientRect()
      const viewport = window.innerHeight
      // 0 when the body top reaches the viewport top, 1 when its end is visible.
      const distance = rect.height - viewport
      const progress =
        distance <= 0 ? (rect.top <= 0 ? 1 : 0) : Math.min(1, Math.max(0, -rect.top / distance))
      fill.style.transform = `scaleX(${progress})`
    }
    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(update)
    }
    update()
    window.addEventListener("scroll", schedule, { passive: true })
    window.addEventListener("resize", schedule)
    return () => {
      window.removeEventListener("scroll", schedule)
      window.removeEventListener("resize", schedule)
      if (frame !== null) window.cancelAnimationFrame(frame)
    }
  }, [target])

  return (
    <div className="reading-progress" aria-hidden="true">
      <div ref={bar} className="reading-progress-fill" />
    </div>
  )
}
