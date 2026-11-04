import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import type { ArticleDetail, ArticleSummary } from "../api/catalog"
import type { ApiFailure } from "../api/http"
import { useServices } from "../app/services"
import { FailureCallout, FixtureBadge, LoadingLine } from "../app/ui"
import { draftHref } from "../chat/draft"
import { codePoints, paragraphs, segmentParagraph, spanMatches } from "../lib/code-points"
import { formatDateTime } from "../lib/dates"
import { usePrefersReducedMotion } from "../lib/hooks"
import { describeFailure } from "../lib/messages"
import { Link, originOf, useDocumentTitle, useLocation, useScrollRestoration } from "../lib/router"
import { hostOf, safeExternalUrl } from "../lib/safe-url"
import { ReadingProgress } from "./ReadingProgress"
import { isFixtureGenerator, type SummaryState, useArticleSummary } from "./summary"

type DetailState =
  | { readonly status: "loading" }
  | { readonly status: "ready"; readonly detail: ArticleDetail }
  | { readonly status: "failed"; readonly failure: ApiFailure }

type ActiveSpan = {
  readonly start_offset: number
  readonly end_offset: number
  readonly id: number
}

const HIGHLIGHT_MS = 4_000

export const ArticlePage = ({ articleId }: { readonly articleId: string }) => {
  const { catalog } = useServices()
  const location = useLocation()
  const [detail, setDetail] = useState<DetailState>({ status: "loading" })
  const [attempt, setAttempt] = useState(0)
  const { state: summary, recheck } = useArticleSummary(catalog, articleId)
  const reduced = usePrefersReducedMotion()
  const [active, setActive] = useState<ActiveSpan | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const highlightTimer = useRef<number | null>(null)

  // biome-ignore lint/correctness/useExhaustiveDependencies: attempt re-runs the request when the reader retries
  useEffect(() => {
    const controller = new AbortController()
    setDetail({ status: "loading" })
    void catalog.article(articleId, controller.signal).then((result) => {
      if (controller.signal.aborted) return
      if (result.ok) setDetail({ status: "ready", detail: result.data })
      else if (result.failure.kind !== "cancelled")
        setDetail({ status: "failed", failure: result.failure })
    })
    return () => controller.abort()
    // attempt: retry button.
  }, [catalog, articleId, attempt])

  useEffect(
    () => () => {
      if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current)
    },
    [],
  )

  const title = detail.status === "ready" ? detail.detail.article.title : "Bài viết"
  useDocumentTitle(title)
  useScrollRestoration(location, detail.status !== "loading")

  const points = useMemo(
    () =>
      detail.status === "ready" && detail.detail.content !== null
        ? codePoints(detail.detail.content)
        : null,
    [detail],
  )

  const jumpTo = useCallback(
    (span: { readonly start_offset: number; readonly end_offset: number }) => {
      const id = Date.now()
      setActive({ ...span, id })
      if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current)
      highlightTimer.current = window.setTimeout(() => setActive(null), HIGHLIGHT_MS)
      // Wait for the <mark> to render, then bring it into view and focus it.
      window.requestAnimationFrame(() => {
        const mark = bodyRef.current?.querySelector<HTMLElement>("mark.span-highlight")
        if (mark === null || mark === undefined) return
        mark.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "center" })
        mark.focus({ preventScroll: true })
      })
    },
    [reduced],
  )

  if (detail.status === "loading") {
    return (
      <div className="shell-width article-page">
        <div className="article-skeleton" role="status" aria-label="Đang tải bài viết">
          <span className="skeleton skeleton-line short" />
          <span className="skeleton skeleton-title" />
          <span className="skeleton skeleton-line" />
          <span className="skeleton skeleton-line" />
        </div>
      </div>
    )
  }

  if (detail.status === "failed") {
    const gone = detail.failure.kind === "api" && [404, 410].includes(detail.failure.status)
    return (
      <div className="shell-width narrow-page">
        <p className="kicker">Bài viết</p>
        <h1 className="page-title">{describeFailure(detail.failure).title}</h1>
        <FailureCallout
          failure={detail.failure}
          onRetry={gone ? undefined : () => setAttempt((n) => n + 1)}
          retryLabel="Tải lại bài"
        />
        <p className="back-row">
          <Link className="link" to="/news">
            ← Về trang tin
          </Link>
        </p>
      </div>
    )
  }

  const { article, version } = detail.detail
  const origin = originOf(location.key)
  const cameFromFeed = origin?.startsWith("/news") === true
  const backTo = cameFromFeed && origin !== undefined ? origin : "/news"
  const original = safeExternalUrl(article.url)
  const summaryForVersion =
    summary.status === "loaded" && summary.summary.article_version_id === version.article_version_id
  const keyPoints =
    summary.status === "loaded" && summary.summary.status === "ready"
      ? summary.summary.key_points
      : []

  // Draft only: the conversation is created when its first question is sent,
  // pinned to the version being read.
  const askHref = draftHref({
    type: "article",
    articleId: article.article_id,
    versionId: version.article_version_id,
  })
  const keyPointItems = keyPoints.map((point) => ({
    text: point.text,
    span: point.supporting_spans.find(
      (candidate) => summaryForVersion && points !== null && spanMatches(points, candidate),
    ),
  }))

  return (
    <div className="article-page">
      {points === null ? null : <ReadingProgress target={bodyRef} />}
      <div className="shell-width article-column">
        <header className="article-header">
          <p className="back-row">
            <Link
              className="link"
              to={backTo}
              onClick={(event) => {
                // Opened from the feed in this tab: real Back restores its pages and position.
                if (cameFromFeed && event.button === 0 && !event.metaKey && !event.ctrlKey) {
                  event.preventDefault()
                  window.history.back()
                }
              }}
            >
              ← Tin tức
            </Link>
          </p>
          <h1 className="article-title">
            {article.title.trim() === "" ? "(Bài không có tiêu đề)" : article.title}
          </h1>
          <p className="meta-line article-meta">
            <span className="source-name">{article.source.name}</span>
            <time dateTime={article.published_at ?? undefined}>
              {article.published_at === null
                ? "Chưa rõ giờ đăng"
                : `Đăng ${formatDateTime(article.published_at)}`}
            </time>
            {original === null ? null : (
              <a className="link" href={original} target="_blank" rel="noopener noreferrer">
                Bài gốc trên {hostOf(original)}
                <span className="visually-hidden"> (mở tab mới)</span>
              </a>
            )}
          </p>
          <LabelRow detail={detail.detail} />
          <div className="article-actions">
            <Link className="btn btn-accent btn-small" to={askHref}>
              Hỏi về bài này
            </Link>
          </div>
        </header>

        <section className="quick-read" aria-labelledby="summary-title">
          <SummaryBlock
            state={summary}
            versionMatches={summaryForVersion}
            onRecheck={recheck}
            keyPoints={keyPointItems}
            hasText={points !== null}
            onJump={jumpTo}
          />
        </section>

        <section className="article-body" aria-labelledby="body-title" ref={bodyRef}>
          <h2 id="body-title" className="section-label">
            Nội dung bài
          </h2>
          {detail.detail.content_policy === "withheld" || points === null ? (
            <div className="callout callout-info">
              <h3>Nội dung không hiển thị tại đây</h3>
              <p>Theo chính sách nội dung, toàn văn bài này không được hiển thị trên EventLens.</p>
              {original === null ? null : (
                <p>
                  <a className="link" href={original} target="_blank" rel="noopener noreferrer">
                    Đọc bài gốc<span className="visually-hidden"> (mở tab mới)</span>
                  </a>
                </p>
              )}
            </div>
          ) : (
            <ArticleText points={points} active={active} />
          )}
        </section>

        <ArticleFacts detail={detail.detail} summary={summary} />
      </div>
    </div>
  )
}

// Technical facts (ingest time, version, classification, summary generator)
// stay one click away; source, publish time and the original link are in the
// header and never hidden.
const ArticleFacts = ({
  detail,
  summary,
}: {
  readonly detail: ArticleDetail
  readonly summary: SummaryState
}) => {
  const { article, version } = detail
  const generator =
    summary.status === "loaded" && summary.summary.status === "ready"
      ? summary.summary.generator
      : null
  const createdAt =
    summary.status === "loaded" && summary.summary.status === "ready"
      ? summary.summary.created_at
      : null
  return (
    <details className="article-facts">
      <summary>Thông tin bài</summary>
      <dl>
        <div>
          <dt>Nguồn</dt>
          <dd>
            {article.source.name} ({article.source.domain})
          </dd>
        </div>
        <div>
          <dt>Giờ đăng</dt>
          <dd>{formatDateTime(article.published_at)}</dd>
        </div>
        {detail.updated_at_source === null ? null : (
          <div>
            <dt>Nguồn cập nhật</dt>
            <dd>{formatDateTime(detail.updated_at_source)}</dd>
          </div>
        )}
        {detail.ingested_at === null ? null : (
          <div>
            <dt>Nhập vào EventLens</dt>
            <dd>{formatDateTime(detail.ingested_at)}</dd>
          </div>
        )}
        <div>
          <dt>Phiên bản</dt>
          <dd>
            {version.revision_count === 1
              ? "1 bản"
              : `${version.revision_count} bản, đang xem bản mới nhất`}
            <span className="version-id"> · {version.article_version_id.slice(7, 19)}</span>
          </dd>
        </div>
        <div>
          <dt>Chủ đề, địa phương</dt>
          <dd>
            {metadataLabel(
              article.processing_status.metadata,
              article.topics.length + article.locations.length,
            )}
          </dd>
        </div>
        {generator === null ? null : (
          <div>
            <dt>Bộ tạo tóm tắt</dt>
            <dd>
              {generator.model_id} ({generator.model_version})
              {createdAt === null ? null : `, tạo lúc ${formatDateTime(createdAt)}`}
            </dd>
          </div>
        )}
      </dl>
    </details>
  )
}

const metadataLabel = (status: string, count: number): string => {
  if (status === "ready")
    return count === 0 ? "Đã phân loại, không có nhãn" : `${count} nhãn đã kiểm duyệt`
  if (status === "pending") return "Đang chờ phân loại"
  if (status === "failed") return "Phân loại lỗi"
  return "Chưa phân loại (chưa có mô hình được tin cậy)"
}

const LabelRow = ({ detail }: { readonly detail: ArticleDetail }) => {
  const { topics, locations } = detail.article
  const labels = [...topics.map((t) => t.name), ...locations.map((l) => l.name)]
  return labels.length === 0 ? null : (
    <ul className="label-row" aria-label="Nhãn">
      {labels.map((label) => (
        <li key={label}>{label}</li>
      ))}
    </ul>
  )
}

const ArticleText = ({
  points,
  active,
}: {
  readonly points: readonly string[]
  readonly active: ActiveSpan | null
}) => {
  const blocks = useMemo(() => paragraphs(points), [points])
  return (
    <div className="article-text">
      {blocks.map((paragraph) => (
        <p key={paragraph.start} data-offset={paragraph.start}>
          {segmentParagraph(points, paragraph, active).map((segment, index) =>
            segment.marked ? (
              <mark
                // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional
                key={index}
                className="span-highlight"
                tabIndex={-1}
              >
                <span className="visually-hidden">Bắt đầu đoạn nguồn. </span>
                {segment.text}
                <span className="visually-hidden"> Kết thúc đoạn nguồn.</span>
              </mark>
            ) : (
              // biome-ignore lint/suspicious/noArrayIndexKey: segments are positional
              <span key={index}>{segment.text}</span>
            ),
          )}
        </p>
      ))}
    </div>
  )
}

type KeyPointItem = {
  readonly text: string
  readonly span: { readonly start_offset: number; readonly end_offset: number } | undefined
}

const normalize = (text: string) => text.normalize("NFC").replace(/\s+/g, " ").trim()

/**
 * True when the summary text says nothing beyond its key points (e.g. an
 * extractive summary made of the same sentences): it is then not offered as a
 * second, near-identical block.
 */
export const textRepeatsKeyPoints = (text: string, keyPoints: readonly { text: string }[]) => {
  if (keyPoints.length === 0) return false
  const strip = (value: string) => value.replace(/[\s.,;:–—-]/g, "")
  const full = strip(normalize(text))
  let rest = normalize(text)
  for (const point of keyPoints) rest = rest.replace(normalize(point.text), "")
  const left = strip(rest)
  // Only when the key points were actually found in it and little else remains.
  return left.length < full.length && left.length <= Math.max(12, full.length * 0.15)
}

/** "Nắm nhanh": key points with their sources first; the full summary on demand. */
const SummaryBlock = ({
  state,
  versionMatches,
  onRecheck,
  keyPoints,
  hasText,
  onJump,
}: {
  readonly state: SummaryState
  readonly versionMatches: boolean
  readonly onRecheck: () => void
  readonly keyPoints: readonly KeyPointItem[]
  readonly hasText: boolean
  readonly onJump: (span: { readonly start_offset: number; readonly end_offset: number }) => void
}) => {
  const heading = (
    <div className="summary-head">
      <h2 id="summary-title">Nắm nhanh</h2>
      {state.status === "loaded" && isFixtureGenerator(state.summary) ? <FixtureBadge /> : null}
    </div>
  )
  if (state.status === "loading") {
    return (
      <>
        {heading}
        <LoadingLine label="Đang kiểm tra tóm tắt…" />
      </>
    )
  }
  if (state.status === "failed") {
    return (
      <>
        {heading}
        <FailureCallout
          failure={state.failure}
          onRetry={onRecheck}
          retryLabel="Kiểm tra lại"
          heading="h3"
        />
      </>
    )
  }
  const summary = state.summary
  return (
    <>
      {heading}
      {summary.publisher_summary === null ? null : (
        <div className="publisher-summary">
          <h3>Sapo của nguồn</h3>
          <p>{summary.publisher_summary}</p>
        </div>
      )}
      {summary.status === "ready" ? (
        <ReadySummary
          summary={summary}
          versionMatches={versionMatches}
          keyPoints={keyPoints}
          hasText={hasText}
          onJump={onJump}
        />
      ) : (
        <SummaryStatus summary={summary} state={state} onRecheck={onRecheck} />
      )}
    </>
  )
}

const ReadySummary = ({
  summary,
  versionMatches,
  keyPoints,
  hasText,
  onJump,
}: {
  readonly summary: ArticleSummary
  readonly versionMatches: boolean
  readonly keyPoints: readonly KeyPointItem[]
  readonly hasText: boolean
  readonly onJump: (span: { readonly start_offset: number; readonly end_offset: number }) => void
}) => {
  const text = summary.text ?? ""
  const showText = text.trim() !== "" && !textRepeatsKeyPoints(text, keyPoints)
  return (
    <div className="summary-ready">
      {!versionMatches ? (
        <p className="field-note field-note-warning">
          Tóm tắt này được tạo cho một phiên bản khác của bài; các đoạn nguồn không được đánh dấu.
        </p>
      ) : null}
      {keyPoints.length > 0 ? (
        <ol className="keypoint-list">
          {keyPoints.map((point, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: ordered points of one immutable summary
            <li key={`${index}-${point.text}`}>
              <p>{point.text}</p>
              {point.span === undefined ? (
                <p className="field-note">
                  {hasText
                    ? "Không xác định được đoạn nguồn trong phiên bản đang hiển thị."
                    : "Nội dung bài không được hiển thị nên không mở được đoạn nguồn."}
                </p>
              ) : (
                <button
                  className="link-button"
                  type="button"
                  onClick={() => point.span && onJump(point.span)}
                >
                  Xem đoạn trong bài
                </button>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <p className="summary-text">{text}</p>
      )}
      {keyPoints.length > 0 && showText ? (
        <details className="summary-more">
          <summary>Đọc tóm tắt đầy đủ</summary>
          <p className="summary-text">{text}</p>
        </details>
      ) : null}
      <p className="summary-meta">
        Do máy tạo, có thể sai sót — đối chiếu với nội dung bài. Chi tiết bộ tạo trong “Thông tin
        bài”.
      </p>
    </div>
  )
}

const SummaryStatus = ({
  summary,
  state,
  onRecheck,
}: {
  readonly summary: ArticleSummary
  readonly state: Extract<SummaryState, { status: "loaded" }>
  readonly onRecheck: () => void
}) => {
  switch (summary.status) {
    case "ready":
      return null
    case "pending":
      return (
        <div className="summary-state" role="status">
          {state.exhausted ? (
            <>
              <p>
                <strong>Tóm tắt vẫn đang chờ xử lý.</strong> Có thể hệ thống AI tóm tắt chưa được
                bật hoặc đang bận.
              </p>
              <button className="btn btn-small" type="button" onClick={onRecheck}>
                Kiểm tra lại
              </button>
            </>
          ) : (
            <LoadingLine label="Tóm tắt đang được xếp hàng xử lý — trang tự kiểm tra lại." />
          )}
        </div>
      )
    case "failed":
      return (
        <div className="callout callout-warning">
          <h3>Không tạo được tóm tắt</h3>
          <p>
            {summary.failure_code === "content_too_long"
              ? "Bài quá dài để tóm tắt tự động (hệ thống không cắt bớt nội dung)."
              : "Quá trình tóm tắt bài này đã lỗi. Nội dung bài bên dưới vẫn đầy đủ."}
          </p>
          {summary.failure_code === null ? null : (
            <p className="callout-meta">Mã {summary.failure_code}</p>
          )}
        </div>
      )
    case "unavailable":
      return <p className="summary-state">Chưa có tóm tắt AI cho bài này.</p>
  }
}
