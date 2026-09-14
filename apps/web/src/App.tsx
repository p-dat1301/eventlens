import { parseFoundationState } from "./foundation"

const foundationState = parseFoundationState({
  project: "EventLens",
  phase: "foundation",
  dataContract: "article.v1",
  apiHealth: "not_connected",
})

const milestones = [
  { label: "Project shell", detail: "React and Vite", status: "Defined" },
  { label: "Data boundary", detail: foundationState.dataContract, status: "Current" },
  { label: "API connection", detail: "No request occurs", status: "Not connected" },
] as const

export const App = () => (
  <div className="page-shell">
    <a className="skip-link" href="#main-content">
      Skip to content
    </a>

    <header className="site-header">
      <p className="wordmark">{foundationState.project}</p>
      <p className="phase-label">Foundation / 01</p>
    </header>

    <main id="main-content">
      <section className="hero" aria-labelledby="page-title">
        <div className="hero-copy">
          <p className="eyebrow">Editorial event intelligence</p>
          <h1 id="page-title">See the event before the noise.</h1>
        </div>
        <p className="hero-intro">
          EventLens is establishing its inward data boundary first. Product behavior begins only
          after that contract is trustworthy.
        </p>
      </section>

      <section className="foundation-grid" aria-label="Foundation readiness">
        <article className="status-panel">
          <header className="section-header">
            <p className="section-index">01</p>
            <h2>Foundation status</h2>
          </header>
          <ol className="status-ledger">
            {milestones.map((milestone) => (
              <li className="status-item" key={milestone.label}>
                <span className="timeline-marker" aria-hidden="true" />
                <div>
                  <p className="status-label">{milestone.status}</p>
                  <h3>{milestone.label}</h3>
                  <p className="status-detail">{milestone.detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </article>

        <article className="contract-plate">
          <div>
            <p className="section-index">02 / Contract</p>
            <h2>One boundary, exact shape.</h2>
          </div>
          <div className="contract-name">
            <code>{foundationState.dataContract}</code>
            <span>Validated</span>
          </div>
          <p>
            Unknown fields stop at entry. Internal code receives readonly, parsed data instead of
            unchecked input.
          </p>
        </article>
      </section>
    </main>

    <footer className="site-footer">
      <p>Foundation scaffold</p>
      <p>API intentionally disconnected</p>
    </footer>
  </div>
)
