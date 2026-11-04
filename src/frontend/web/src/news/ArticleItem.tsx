import type { ArticleCard } from "../api/catalog"
import { formatDateTime, formatTime } from "../lib/dates"
import { Link } from "../lib/router"

// Typography-first story: no image (the API has none, and stock photos would
// misrepresent real news). Only fields the API returns are shown: publisher,
// publish time, title, and labels when there are any. `summary_preview` is
// deliberately not rendered: the card carries no generator identity, so a
// simulated (fixture) preview could not be told apart from a real model's
// before the article is opened. It returns once the API provides provenance.

type Props = {
  readonly article: ArticleCard
  /** Inside a day section the date is in the heading: show only the time. */
  readonly timeOnly: boolean
  readonly headingLevel: 2 | 3
}

export const ArticleItem = ({ article, timeOnly, headingLevel }: Props) => {
  const Heading = headingLevel === 2 ? "h2" : "h3"
  const labels = [...article.topics.map((t) => t.name), ...article.locations.map((l) => l.name)]
  const title = article.title.trim() === "" ? "(Bài không có tiêu đề)" : article.title
  const time = timeOnly ? formatTime(article.published_at) : null
  return (
    <article className="story" data-article-id={article.article_id}>
      <Heading className="story-title">
        {/* Title is clamped visually on small screens; the link text stays complete. */}
        <Link className="story-link" to={`/articles/${article.article_id}`}>
          {title}
        </Link>
      </Heading>
      <p className="meta-line story-meta">
        <span className="source-name">{article.source.name}</span>
        <time dateTime={article.published_at ?? undefined}>
          {article.published_at === null
            ? "Chưa rõ giờ đăng"
            : (time ?? formatDateTime(article.published_at))}
        </time>
      </p>
      {labels.length > 0 ? (
        <ul className="label-row" aria-label="Nhãn">
          {labels.map((label) => (
            <li key={label}>{label}</li>
          ))}
        </ul>
      ) : null}
    </article>
  )
}
