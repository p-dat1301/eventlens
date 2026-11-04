import type { Citation, Run } from "../api/chat"
import { FailureCallout, FixtureBadge } from "../app/ui"
import { formatDateTime } from "../lib/dates"
import { runFailureMessage } from "../lib/messages"
import type { RunEntry } from "./thread"

type Props = {
  readonly entry: RunEntry
  readonly activeCitationId: string | null
  readonly onCite: (runId: string, citation: Citation, trigger: HTMLElement) => void
  readonly onCancel: (runId: string) => void
  readonly onAskAgain: (question: string) => void
  readonly onReload: (runId: string) => void
  readonly onNewConversation: (() => void) | null
  readonly canAskAgain: boolean
}

const ABSTENTION_COPY: Readonly<Record<string, string>> = {
  abstained_no_evidence: "Không tìm thấy nguồn phù hợp trong phạm vi hội thoại.",
  abstained_by_model: "Bộ trả lời từ chối vì nguồn tìm được không đủ căn cứ.",
}

export const RunView = (props: Props) => {
  const { entry } = props
  return (
    <li className="turn" data-run-id={entry.runId}>
      <div className="turn-question">
        <p className="turn-label">Bạn hỏi</p>
        <p>{entry.question}</p>
      </div>
      <div className="turn-answer" aria-live="polite">
        <TurnBody {...props} />
      </div>
    </li>
  )
}

const TurnBody = ({
  entry,
  activeCitationId,
  onCite,
  onCancel,
  onAskAgain,
  onReload,
  onNewConversation,
  canAskAgain,
}: Props) => {
  const run = entry.run
  if (run === null) {
    if (entry.loadFailure !== null) {
      return (
        <FailureCallout
          failure={entry.loadFailure}
          onRetry={() => onReload(entry.runId)}
          retryLabel="Tải lại lượt hỏi"
          heading="h3"
        />
      )
    }
    return <p className="turn-status">Đang đọc trạng thái…</p>
  }

  switch (run.status) {
    case "queued":
    case "running":
      return (
        <div className="turn-progress" role="status">
          <span className="loading-dot" aria-hidden="true" />
          <p>
            {run.cancel_requested
              ? "Đang yêu cầu hủy — chờ máy chủ xác nhận…"
              : run.status === "queued"
                ? "Đang chờ xử lý…"
                : "Đang tìm nguồn và soạn câu trả lời…"}
          </p>
          {run.cancel_requested ? null : (
            <button className="btn btn-small" type="button" onClick={() => onCancel(run.run_id)}>
              Hủy
            </button>
          )}
        </div>
      )
    case "cancelled":
      return <p className="turn-status turn-cancelled">Đã hủy theo yêu cầu.</p>
    case "failed":
      return (
        <div className="callout callout-danger">
          <h3>Chưa trả lời được</h3>
          <p>{runFailureMessage(run.error?.code ?? "internal_error")}</p>
          {run.error === null ? null : <p className="callout-meta">Mã {run.error.code}</p>}
          <div className="callout-actions">
            {run.error?.code === "scope_stale" && onNewConversation !== null ? (
              <button className="btn btn-small" type="button" onClick={onNewConversation}>
                Hỏi về phiên bản hiện tại
              </button>
            ) : null}
            {canAskAgain ? (
              <button
                className="btn btn-small"
                type="button"
                onClick={() => onAskAgain(run.question)}
              >
                Hỏi lại câu này
              </button>
            ) : null}
          </div>
        </div>
      )
    case "completed":
      return run.outcome === "answered" && run.answer !== null ? (
        <Answer run={run} activeCitationId={activeCitationId} onCite={onCite} />
      ) : (
        <Abstained run={run} />
      )
  }
}

const AnswerMeta = ({ run }: { readonly run: Run }) =>
  run.answer === null ? null : (
    <p className="answer-meta">
      {run.answer.mode === "fixture" ? <FixtureBadge /> : null}
      <span>Nguồn truy xuất lúc {formatDateTime(run.answer.retrieved_at)}</span>
    </p>
  )

const Abstained = ({ run }: { readonly run: Run }) => (
  <div className="callout callout-info">
    <h3>Không đủ nguồn để trả lời</h3>
    <p>
      {ABSTENTION_COPY[run.abstention_reason ?? ""] ??
        "Hệ thống không trả lời vì không có nguồn đủ căn cứ trong phạm vi hội thoại."}
    </p>
    {run.answer !== null && run.answer.uncertainties.length > 0 ? (
      <ul className="uncertainty-notes">
        {run.answer.uncertainties.map((item) => (
          <li key={item.uncertainty_id}>{item.explanation}</li>
        ))}
      </ul>
    ) : null}
    <AnswerMeta run={run} />
  </div>
)

const Answer = ({
  run,
  activeCitationId,
  onCite,
}: {
  readonly run: Run
  readonly activeCitationId: string | null
  readonly onCite: Props["onCite"]
}) => {
  const answer = run.answer
  if (answer === null) return null
  const numbering = new Map(
    run.citations.map((citation, index) => [citation.citation_id, index + 1]),
  )
  return (
    <div className="answer">
      <p className="turn-label">EventLens trả lời</p>
      <AnswerMeta run={run} />
      {answer.claims.length > 0 ? (
        <ol className="claims">
          {answer.claims.map((claim) => {
            const cites = run.citations.filter((citation) => citation.claim_id === claim.claim_id)
            const uncertain = answer.uncertainties.filter(
              (item) => item.claim_id === claim.claim_id,
            )
            return (
              <li key={claim.claim_id} className="claim">
                <p>
                  {claim.text}{" "}
                  {cites.map((citation) => (
                    <button
                      key={citation.citation_id}
                      className="cite"
                      type="button"
                      aria-pressed={activeCitationId === citation.citation_id}
                      aria-label={`Nguồn ${numbering.get(citation.citation_id)}: ${citation.source_title}`}
                      onClick={(event) => onCite(run.run_id, citation, event.currentTarget)}
                    >
                      {numbering.get(citation.citation_id)}
                    </button>
                  ))}
                </p>
                {uncertain.map((item) => (
                  <p key={item.uncertainty_id} className="claim-note">
                    Lưu ý: {item.explanation}
                  </p>
                ))}
              </li>
            )
          })}
        </ol>
      ) : (
        <p className="answer-text">{answer.answer}</p>
      )}
      {answer.uncertainties.some((item) => item.claim_id === null) ? (
        <ul className="uncertainty-notes">
          {answer.uncertainties
            .filter((item) => item.claim_id === null)
            .map((item) => (
              <li key={item.uncertainty_id}>{item.explanation}</li>
            ))}
        </ul>
      ) : null}
      {run.citations.length > 0 ? (
        <ol className="source-list" aria-label="Nguồn được trích">
          {run.citations.map((citation) => (
            <li key={citation.citation_id}>
              <button
                className="source-item"
                type="button"
                aria-pressed={activeCitationId === citation.citation_id}
                onClick={(event) => onCite(run.run_id, citation, event.currentTarget)}
              >
                <span className="source-num">{numbering.get(citation.citation_id)}</span>
                <span className="source-item-text">
                  <span className="source-item-title">{citation.source_title}</span>
                  <span className="source-item-meta">
                    {citation.source_name} · {formatDateTime(citation.source_published_at)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  )
}
