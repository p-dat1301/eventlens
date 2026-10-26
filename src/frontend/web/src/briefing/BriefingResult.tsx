import type { BriefingCitation, BriefingClaim, BriefingResponse } from "./contract"
import {
  assessCoverage,
  citationsForClaim,
  describeRetrievalMode,
  formatTimestamp,
  humanize,
  uncertaintiesForClaim,
} from "./model"

type BriefingResultProps = {
  readonly response: BriefingResponse
  readonly activeCitationId: string | null
  readonly onInspect: (citation: BriefingCitation, trigger: HTMLButtonElement) => void
}

const OUTCOME_TAGS = {
  answered: { label: "Answered", tone: "state-tag-ok" },
  abstained_no_evidence: { label: "No evidence", tone: "state-tag-warning" },
  abstained_by_model: { label: "Model abstained", tone: "state-tag-warning" },
} as const

export const BriefingResult = ({ response, activeCitationId, onInspect }: BriefingResultProps) => {
  const coverage = assessCoverage(response)
  const tag = OUTCOME_TAGS[response.outcome]
  const general = uncertaintiesForClaim(response, null)
  const answered = response.outcome === "answered"

  return (
    <article className="briefing-result" data-outcome={response.outcome}>
      <div className="tag-row">
        <p className={`state-tag ${tag.tone}`}>{tag.label}</p>
        <p
          className={`state-tag ${coverage.level === "complete" ? "state-tag-neutral" : "state-tag-warning"}`}
        >
          {coverage.level === "complete" ? "Coverage complete" : "Partial coverage"}
        </p>
      </div>

      {coverage.level === "degraded" ? (
        <div className="notice notice-warning" role="note" aria-label="Coverage warning">
          <h3>Coverage is incomplete</h3>
          <ul className="plain-list">
            {coverage.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {answered ? (
        <section className="answer-block" aria-labelledby="answer-title">
          <h3 id="answer-title">Answer</h3>
          {paragraphs(response.answer).map((paragraph) => (
            <p className="answer-text" key={paragraph}>
              {paragraph}
            </p>
          ))}
        </section>
      ) : (
        <section className="notice notice-warning" aria-labelledby="abstention-title">
          <h3 id="abstention-title">
            {response.outcome === "abstained_no_evidence"
              ? "No supporting evidence found"
              : "The model declined to answer"}
          </h3>
          <p>{response.answer}</p>
          {response.abstention_reason === null ? null : (
            <p className="notice-meta">Reason: {humanize(response.abstention_reason)}</p>
          )}
        </section>
      )}

      {response.claims.length > 0 ? (
        <section aria-labelledby="claims-title">
          <h3 id="claims-title">Claims and citations</h3>
          <ol className="claim-list">
            {response.claims.map((claim, index) => (
              <ClaimItem
                key={claim.claim_id}
                claim={claim}
                index={index}
                response={response}
                activeCitationId={activeCitationId}
                onInspect={onInspect}
              />
            ))}
          </ol>
        </section>
      ) : null}

      <section aria-labelledby="uncertainty-title">
        <h3 id="uncertainty-title">Uncertainty</h3>
        {general.length === 0 ? (
          <p className="muted-note">No briefing-wide uncertainty reported.</p>
        ) : (
          <ul className="uncertainty-list">
            {general.map((item) => (
              <li key={item.uncertainty_id}>
                <span className="uncertainty-category">{humanize(item.category)}</span>
                {item.explanation}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="audit-block" aria-labelledby="audit-title">
        <h3 id="audit-title">Retrieval audit</h3>
        <dl className="audit-list">
          <div>
            <dt>Time basis</dt>
            <dd>
              Source publication time <code>{response.query_time_basis}</code>
            </dd>
          </div>
          <div>
            <dt>Retrieval mode</dt>
            <dd>{describeRetrievalMode(response.retrieval_mode)}</dd>
          </div>
          <div>
            <dt>Coverage</dt>
            <dd>{humanize(response.audit.coverage_status)}</dd>
          </div>
          <div>
            <dt>Semantic branch</dt>
            <dd>{humanize(response.audit.vector_branch_status)}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>
              {response.model_version} ({response.audit.mode})
            </dd>
          </div>
          <div>
            <dt>Retrieved</dt>
            <dd>{formatTimestamp(response.retrieved_at)}</dd>
          </div>
        </dl>
      </section>
    </article>
  )
}

const paragraphs = (text: string): readonly string[] =>
  text
    .split(/\n{2,}/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0)

type ClaimItemProps = {
  readonly claim: BriefingClaim
  readonly index: number
  readonly response: BriefingResponse
  readonly activeCitationId: string | null
  readonly onInspect: (citation: BriefingCitation, trigger: HTMLButtonElement) => void
}

const ClaimItem = ({ claim, index, response, activeCitationId, onInspect }: ClaimItemProps) => {
  const citations = citationsForClaim(response, claim.claim_id)
  const doubts = uncertaintiesForClaim(response, claim.claim_id)
  const temporal = claim.temporal_claim

  return (
    <li className="claim-item" data-claim-id={claim.claim_id}>
      <p className="claim-index">Claim {index + 1}</p>
      <p className="claim-text">{claim.text}</p>
      {temporal === null ? null : (
        <p className="claim-meta">
          {temporal.value_state === "known"
            ? `When: ${formatTimestamp(temporal.start_at)}${
                temporal.end_at === null ? "" : ` to ${formatTimestamp(temporal.end_at)}`
              } · precision ${temporal.precision}`
            : "When: not stated in the sources"}
        </p>
      )}
      {doubts.map((item) => (
        <p className="claim-uncertainty" key={item.uncertainty_id}>
          <span className="uncertainty-category">{humanize(item.category)}</span>
          {item.explanation}
        </p>
      ))}
      <ul className="citation-list" aria-label={`Citations for claim ${index + 1}`}>
        {citations.map((citation) => (
          <li className="citation" key={citation.citation_id} data-claim-id={citation.claim_id}>
            <p className="citation-title">{citation.source_title}</p>
            <p className="citation-meta">
              {citation.source_name} · {citation.source_domain} ·{" "}
              {formatTimestamp(citation.source_published_at)}
            </p>
            <blockquote className="cited-span">{citation.span.excerpt}</blockquote>
            <div className="citation-actions">
              <button
                className="button button-secondary"
                type="button"
                aria-controls="source-viewer"
                aria-pressed={activeCitationId === citation.citation_id}
                onClick={(event) => onInspect(citation, event.currentTarget)}
              >
                Inspect source
              </button>
              <a
                className="text-link"
                href={citation.source_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open original<span className="visually-hidden"> (opens in new tab)</span>
              </a>
            </div>
          </li>
        ))}
      </ul>
    </li>
  )
}
