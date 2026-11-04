import { useEffect, useMemo, useRef, useState } from "react"
import { createServices, type Services, ServicesProvider } from "./app/services"
import { ArticlePage } from "./article/ArticlePage"
import { BriefingWorkspace } from "./briefing/BriefingWorkspace"
import { type BriefingClient, createBriefingClient } from "./briefing/client"
import { ChatPage } from "./chat/ChatPage"
import { hcmDayOf } from "./lib/dates"
import { Link, matchRoute, navigate, useDocumentTitle, useLocation } from "./lib/router"
import { NewsPage } from "./news/NewsPage"

const baseUrl = import.meta.env["VITE_API_BASE_URL"] ?? ""
const defaultClient = createBriefingClient({ baseUrl })
const defaultServices = createServices({ baseUrl })

type AppProps = {
  readonly client?: BriefingClient
  readonly services?: Services
}

const TODAY = new Intl.DateTimeFormat("vi-VN", {
  timeZone: "Asia/Ho_Chi_Minh",
  weekday: "long",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
})

export const App = ({ client = defaultClient, services = defaultServices }: AppProps) => {
  const location = useLocation()
  const route = matchRoute(location.pathname)

  useEffect(() => {
    if (route.name === "home") navigate(`/news${location.search}`, { replace: true })
  }, [route.name, location.search])

  return (
    <ServicesProvider services={services}>
      <div className="page-shell">
        <a className="skip-link" href="#main-content">
          Bỏ qua, đến nội dung chính
        </a>
        <Masthead />
        <SiteNav section={route.name} />
        <main id="main-content" className="site-main" tabIndex={-1}>
          {route.name === "news" || route.name === "home" ? <NewsPage /> : null}
          {route.name === "article" ? <ArticlePage key={route.id} articleId={route.id} /> : null}
          {route.name === "chat" ? <ChatPage conversationId={route.id} /> : null}
          {route.name === "analyst" ? (
            <div className="shell-width" lang="en">
              <BriefingWorkspace client={client} />
            </div>
          ) : null}
          {route.name === "not_found" ? <NotFound /> : null}
        </main>
        <footer className="site-footer">
          <div className="site-footer-inner shell-width">
            <p>EventLens — đọc tin và hỏi đáp có trích dẫn nguồn.</p>
            <p>Thời gian hiển thị theo giờ Việt Nam (UTC+7), dựa trên giờ đăng của nguồn.</p>
            <p className="footer-tools">
              <Link className="link" to="/analyst">
                Phân tích nâng cao
              </Link>{" "}
              — công cụ cũ: một câu hỏi, một bản tóm lược có trích dẫn, không lưu hội thoại (giao
              diện tiếng Anh).
            </p>
          </div>
        </footer>
      </div>
    </ServicesProvider>
  )
}

const Masthead = () => {
  const today = useMemo(() => {
    const label = TODAY.format(new Date())
    return label.charAt(0).toUpperCase() + label.slice(1)
  }, [])
  return (
    <header className="masthead site-header">
      <div className="masthead-inner shell-width">
        <Link className="wordmark" to="/news">
          Event<span>Lens</span>
        </Link>
        <p className="masthead-date">
          <time dateTime={hcmDayOf(new Date())}>{today}</time>
        </p>
      </div>
    </header>
  )
}

// Two primary destinations. The legacy analyst briefing stays reachable from
// the footer (secondary entry with a description), not as a third peer.
const NAV = [
  { to: "/news", label: "Tin tức", sections: ["news", "home", "article"] },
  { to: "/chat", label: "Hỏi đáp", sections: ["chat"] },
] as const

const SiteNav = ({ section }: { readonly section: string }) => {
  const sentinel = useRef<HTMLDivElement>(null)
  const [stuck, setStuck] = useState(false)

  // The nav row is CSS-sticky; an observer on a 1px sentinel above it only
  // toggles the condensed look (shadow under the bar). No scroll handler.
  useEffect(() => {
    const node = sentinel.current
    if (node === null || typeof IntersectionObserver === "undefined") return
    const observer = new IntersectionObserver(([entry]) =>
      setStuck(entry?.isIntersecting === false),
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return (
    <>
      <div ref={sentinel} className="nav-sentinel" aria-hidden="true" />
      <nav className="site-nav" aria-label="Điều hướng chính" data-stuck={stuck}>
        <div className="site-nav-inner shell-width">
          <ul className="nav-links">
            {NAV.map((item) => (
              <li key={item.to}>
                <Link
                  className="nav-link"
                  to={item.to}
                  aria-current={
                    (item.sections as readonly string[]).includes(section) ? "page" : undefined
                  }
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
          {section === "analyst" ? <span className="nav-context">Phân tích nâng cao</span> : null}
        </div>
      </nav>
    </>
  )
}

const NotFound = () => {
  useDocumentTitle("Không tìm thấy trang")
  return (
    <div className="shell-width narrow-page">
      <p className="kicker">404</p>
      <h1 className="page-title">Không tìm thấy trang</h1>
      <p>Đường dẫn không tồn tại.</p>
      <p>
        <Link className="link" to="/news">
          Về trang tin
        </Link>
      </p>
    </div>
  )
}
