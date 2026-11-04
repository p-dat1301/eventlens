import type { ReactNode } from "react"

import type { ApiFailure } from "../api/http"
import { describeFailure, failureReference } from "../lib/messages"

type FailureCalloutProps = {
  readonly failure: ApiFailure
  readonly onRetry?: (() => void) | undefined
  readonly retryLabel?: string
  readonly heading?: "h2" | "h3"
  readonly children?: ReactNode
}

/** Error state: Vietnamese copy from the code, technical reference, optional retry. */
export const FailureCallout = ({
  failure,
  onRetry,
  retryLabel = "Thử lại",
  heading = "h2",
  children,
}: FailureCalloutProps) => {
  const copy = describeFailure(failure)
  const reference = failureReference(failure)
  const Heading = heading
  return (
    <div className="callout callout-danger" role="alert">
      <Heading>{copy.title}</Heading>
      <p>{copy.message}</p>
      {children}
      {reference === null ? null : <p className="callout-meta">{reference}</p>}
      {copy.retryable && onRetry !== undefined ? (
        <div className="callout-actions">
          <button className="btn btn-small" type="button" onClick={onRetry}>
            {retryLabel}
          </button>
        </div>
      ) : null}
    </div>
  )
}

export const LoadingLine = ({ label }: { readonly label: string }) => (
  <p className="loading-line" role="status">
    <span className="loading-dot" aria-hidden="true" />
    {label}
  </p>
)

/** Simulated AI output must always say so, next to the content it labels. */
export const FixtureBadge = () => (
  <span
    className="badge badge-fixture"
    title="Nội dung do bộ sinh mô phỏng tạo, không phải mô hình AI thật"
  >
    Dữ liệu AI mô phỏng
  </span>
)
