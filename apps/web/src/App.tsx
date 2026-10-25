import { BriefingWorkspace } from "./briefing/BriefingWorkspace"
import { type BriefingClient, createBriefingClient } from "./briefing/client"

const defaultClient = createBriefingClient({ baseUrl: import.meta.env["VITE_API_BASE_URL"] ?? "" })

type AppProps = {
  readonly client?: BriefingClient
}

export const App = ({ client = defaultClient }: AppProps) => (
  <div className="page-shell">
    <a className="skip-link" href="#main-content">
      Skip to content
    </a>

    <header className="site-header">
      <p className="wordmark">EventLens</p>
      <p className="phase-label">Analyst briefing</p>
    </header>

    <main id="main-content">
      <BriefingWorkspace client={client} />
    </main>

    <footer className="site-footer">
      <p>Every claim links to a cited passage</p>
      <p>Time basis: source publication</p>
    </footer>
  </div>
)
