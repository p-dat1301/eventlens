# EventLens Web Design System

Extends the original briefing system (paper, ink, terracotta rules) to the
Vietnamese news reader: feed `/news`, article `/articles/:id`, chat `/chat[/:id]`.
The analyst briefing moved to `/analyst` and keeps its components (§5.2).

## 0. Research Log

- 2026-10 (briefing): embedded refs Claude, Wired, ClickHouse; picked warm editorial clarity.
- 2026-11 (news + chat): `ui-ux-pro-max` workflow (local copy, `~/.codex/skills/ui-ux-pro-max`).
  - Product/stack: news reading + grounded Q&A; React 19 + TypeScript + Vite + Bun (from `package.json`).
  - `--design-system "news editorial reading app" --motion 3 --density 5`: kept the *Newsreader* heading
    font (news/editorial match) and "subtle hover, clear type hierarchy, fast loading"; **rejected** the
    proposed *Hero-Centric* pattern (a reading app must start with content, not a landing hero) and the
    "breaking red + link blue" palette (would imply breaking news the data cannot back; we keep the
    existing paper/ink/terracotta identity).
  - `--domain typography "vietnamese serif editorial"`: *Be Vietnam Pro* (designed for Vietnamese
    diacritics) for UI/body; Newsreader also ships a Vietnamese subset.
  - `--domain ux`: sticky navigation must not obscure content/focus (→ `scroll-padding-top`);
    reduced motion; animate 1–2 key elements per view; no scroll-jacking; no horizontal scroll.
  - `--stack react "effect cleanup race"`: every effect returns its cleanup; abort/ignore stale requests.

## 1. Atmosphere & Identity

Quiet editorial infrastructure. Warm paper, ink-black type, one terracotta accent.
Signature: **thick ink rules** (2px) that open sections, thin rules between stories,
and a terracotta underline for "current" (nav item, citation, cited span). Stories are
typography-first: the data has no images and none are invented.

## 2. Color

| Role | Token | Value | Contrast on canvas | Usage |
|---|---|---|---|---|
| Page | `--color-canvas` | `#f3f1e8` | — | Background |
| Surface | `--color-surface` | `#faf9f5` | — | Panels, inputs, summary, source pane |
| Ink | `--color-ink` | `#191916` | 15.6:1 | Text, rules, primary buttons |
| Muted | `--color-muted` | `#5f5d56` | 5.8:1 | Metadata (darkened from `#66645d`) |
| Faint | `--color-faint` | `#8b887f` | 3.1:1 | Decorative only, never text |
| Border | `--color-border` | `#dcd8cc` | — | Thin rules |
| Accent | `--color-accent` | `#a8432a` | 5.3:1 | Kicker, nav underline, CTA fill (was `#b94f32`: 4.39:1, below AA) |
| Accent strong | `--color-accent-strong` | `#8a3420` | 7.2:1 | Hover, labels |
| Focus | `--color-focus` | `#1769aa` | 5.1:1 | 3px focus ring |
| OK / Warning / Danger / Info | `--color-ok` `#2f6b3a`, `--color-warning` `#8a5a00`, `--color-danger` `#a12d2d`, `--color-info` `#285a85` | | ≥ 5.2:1 | Callout borders, badges (on their `-soft` backgrounds ≥ 4.7:1) |
| Fixture | `--color-fixture` `#5b4a8b` on hatched `--color-fixture-soft` | | 5.9:1 | **Only** for "Dữ liệu AI mô phỏng" |
| Highlight | `--color-highlight` | `#f6dc8c` | ink 13:1 | Key-point span and cited passage |

Semantics: accent = current/attention, never decoration. States always carry text
(badge label, callout heading), color only reinforces it.

## 3. Typography

| Level | Token | Size | Font / weight | Usage |
|---|---|---|---|---|
| Page / article title | `--type-title` / clamp up to 3.25rem | | Newsreader 600 | `h1` |
| Story title | `--type-item` | `clamp(1.1875rem, 1.05rem + 0.6vw, 1.5rem)` (≈19px phone → 24px desktop) | Newsreader 600, lh 1.25, clamped to 3 lines (link text stays complete) | Feed items |
| Reading | `--type-reading` | 1.1875rem | Newsreader 400, lh 1.72 | Article text, ≤ `--reading-max` |
| Body | `--type-body` | 1rem | Be Vietnam Pro 400 | UI copy |
| Small / Label | 0.875 / 0.75rem | | Be Vietnam Pro 600–700, labels uppercase + tracking | Metadata, kickers |

Fonts are self-hosted with `@fontsource/newsreader@5.3.0` and
`@fontsource/be-vietnam-pro@5.3.0` (latin, latin-ext and **vietnamese** subsets selected
by `unicode-range`); no third-party font request at runtime. Fallbacks: Georgia /
Avenir Next / Segoe UI. Vietnamese stacked diacritics (ẳ, ỡ, Ữ) were checked at every
size in the screenshots.

## 4. Grid, Spacing & Layout

- 4px base, tokens `--space-1 … --space-16`; page gutter `--gutter: clamp(1rem, …, 2rem)` (16px on phones).
- Shell max `--shell-max: 80rem`; reading column `--reading-max: 40rem` (~68 characters).
- Sticky stack: nav `--nav-height` 3.25rem (+ feed filter bar `--filterbar-height` on `/news`);
  `html { scroll-padding-top: var(--sticky-offset) }` so anchors/focus land below it; on chat
  `scroll-padding-bottom: 10rem` keeps focus above the sticky composer.
- Feed (2026-11 remediation): one even list, no lead/hero (no API field says the first story
  matters more). Day sections for latest/oldest, one ranking for relevance. ≥ 64rem a 17rem
  sticky sidebar (Hỏi đáp card, Nguồn báo with current-result counts); below, one compact
  "Hỏi về …" link above the list and no sidebar. Phones: page `h1` is visually hidden (nav names
  the page), masthead date hidden.
- Article: one reading column (`--reading-max`): title → source · time · original link →
  "Hỏi về bài này" → **Nắm nhanh** → **Nội dung bài** → "Thông tin bài" disclosure.
- Chat: 16rem conversation list + thread ≥ 64rem; source pane (20–26rem) beside the thread
  ≥ 72rem; narrower screens show list *or* thread and open sources in a full-screen dialog.

## 5. Components

### 5.1 News reader

- **Masthead + nav**: wordmark and date scroll away; the nav row is CSS-sticky. An
  IntersectionObserver on a 1px sentinel only adds a shadow (`data-stuck`) — no height change.
- **Search** (`<search>` + form, label for screen readers) and **filter bar** (sticky): API
  total ("N bài"; "Đang đếm…"/"Chưa đếm được" otherwise, never a guessed 0), quick buttons
  **Thời gian** and **Nguồn** (open the drawer focused on that group), "Bộ lọc (n)", sort
  (Mới nhất / Cũ nhất / Liên quan nhất only with a query). Active filters appear as removable
  chips below, with "Xóa bộ lọc". No chat button here and no "Bản tin" kicker or fetch time.
- **Filter drawer**: native modal `<dialog>` (focus trap, Escape, focus return by the
  browser); groups in order **Thời gian**, **Nguồn**, **Chủ đề**, **Địa phương**. Chủ đề is a
  one-line status ("Chưa bài nào trong kết quả hiện tại có nhãn chủ đề đã kiểm duyệt" /
  "Đang đếm…" / "Chưa tải được số bài") with the checkbox list in a `<details>` that opens by
  itself only when the current result has labelled articles. Địa phương is search-first (name
  or alias without diacritics); nothing is listed before typing except places that have
  articles in the current result. Selected topics/places are always shown as removable chips
  in their group, even when they match 0. Counts are API facets for the current filters only;
  they are never presented as "the corpus has no labels".
- **Story**: title link (Newsreader, ≤ 3 lines), then source · time (time only inside a day
  section; full date in relevance), labels only when present. **No `summary_preview`**: the card
  has no generator identity, so a simulated preview could not be marked before opening the
  article (no per-card summary fetch, no first-paragraph substitute). No image, view count,
  heat, "Mới đăng" or invented category.
- **Day sections**: `h2` "Thứ Tư, 29/10/2026" (Asia/Ho_Chi_Minh), consecutive runs of the server
  order only — nothing re-sorted; a run continuing on the next page joins its section;
  "Chưa rõ ngày đăng" for null.
- **Sidebar (≥ 64rem)**: "Hỏi đáp" card with **"Hỏi về N bài"** / **"Hỏi về N bài đang lọc"**
  (N = facets total of exactly these filters; "Hỏi về toàn bộ tin"/"các bài đang lọc" without a
  count) linking to a `/chat` draft; "Nguồn báo" toggles with facet counts labelled "Số bài trong
  kết quả đang xem" (never `/sources.article_count`, never hardcoded).
- **States**: skeleton (loading), "Kho tin chưa có bài nào" (empty corpus), "Không có bài nào
  khớp bộ lọc" (no results + clear), error callout with retry, "Tải thêm tin" (explicit,
  focus moves to the first new story), "Đã hiển thị hết N bài".
- **Article**: back link (real Back when opened from the feed), title, one meta line
  (source · "Đăng …" · "Bài gốc trên domain", http/https only), labels when present,
  "Hỏi về bài này" (link to a `/chat?article=…&version=…` draft). **Nắm nhanh** (surface block):
  fixture badge "Dữ liệu AI mô phỏng" in its heading; ready → key points with "Xem đoạn trong
  bài" (only when the span matches the displayed version, code points), the full summary text
  behind "Đọc tóm tắt đầy đủ" — omitted when it only repeats the key points; no key points → the
  text; pending (auto re-check, then "Kiểm tra lại"), failed (reason), unavailable. "Sapo của
  nguồn" when the API ever returns one. **Nội dung bài** follows. **Thông tin bài**
  (`<details>`): source/domain, publish, source update, ingest time, revisions + version id,
  classification status, summary generator model/version and creation time. Withheld content
  → notice + original link. 404/410 final states.
- **Callout**: left rule colored by state, heading + message + reference (code · HTTP ·
  request id) + optional retry. Never raw response bodies.

### 5.2 Analyst briefing (route `/analyst`, unchanged behaviour)

English copy, `lang="en"`. Behaviour unchanged; they use the shared tokens, so the
accent/muted contrast fixes apply to them too. Original spec, kept verbatim:

#### Query Panel

- **Structure**: display heading + labelled `textarea` + hint + submit button inside a `<form>`.
- **States**: idle; invalid (inline `role="alert"`, `aria-invalid`, described-by link, no request sent); loading (button reads "Briefing…").
- **Layout**: asymmetric 3fr/2fr grid; one column below `48rem`.

#### Briefing Result

- **Structure**: outcome tag row, coverage warning, answer (or abstention notice), ordered claims with nested citations, uncertainty list, retrieval audit `<dl>`.
- **Variants**: `answered`, `abstained_no_evidence`, `abstained_by_model`. Abstentions never render under the "Answer" heading.
- **States**: loading (`role="status"`, `aria-busy`), failed (`role="alert"` with code/HTTP/attempt and Retry when retryable), malformed response (error only; nothing rendered as an answer).
- **Accessibility**: every state tag carries text plus a glyph (✓ ! ×); accent color only reinforces it.

#### Citation

- **Structure**: source title, name · domain · published time, cited excerpt `<blockquote>`, "Inspect source" toggle (`aria-pressed`, `aria-controls`), external "Open original" link.

#### Source Viewer

- **Structure**: `<aside id="source-viewer">` beside the result on desktop (sticky), stacked below on narrow screens.
- **States**: loading, open (cited span in `<mark>` with screen-reader start/end markers), failed (`source_not_found`, `source_stale`, others).
- **Keyboard**: focus moves to the viewer heading on open; Escape or "Close source" closes and returns focus to the citation button.

Accent marks the current/attention state there too: active citation, warning/error tags,
cited span underline, claim index. Its only motion is the loading bar slide
(`background-position`), disabled under `prefers-reduced-motion`.

### 5.3 Chat

- **New conversation (`/chat`)**: `h1` "Bạn muốn tìm hiểu tin gì?", one scope chip
  (**Tất cả tin** by default; "Tin khớp bộ lọc: …" or "Một bài: <title>" when opened from the
  feed/article — the draft scope lives in the URL), "Phạm vi và trích dẫn" disclosure, then the
  composer (inline, not sticky). The conversation is created **only when the first question is
  sent** (never on render/focus/typing; a ref blocks double clicks); its id + a new
  Idempotency-Key are stored with the question before the run is submitted by the thread page.
  A failed create keeps the text and is never retried automatically.
- **Conversation list**: title (= first question), scope kind, updated time; delete asks
  "Xóa hẳn / Giữ lại". Session note: one line "Phiên ẩn danh · truy cập tối đa 7 ngày" with
  details on demand (not an account; access ends on expiry/cookie removal; no promise of
  physical deletion); warning when a session was replaced.
- **Thread header**: title + compact **scope box** from `conversation.scope` (never the feed's
  current filters or the draft URL): "Tất cả tin" / "Tin khớp bộ lọc" (names resolved from the API, "toàn bộ bài khớp",
  link to the same feed) / "Một bài" (title, "đã ghim phiên bản").
- **Turn**: question bubble (ink), then one of: "Đang chờ xử lý…" / "Đang tìm nguồn…" + Hủy;
  "Đang yêu cầu hủy…" (cancel requested, not yet confirmed); "Đã hủy theo yêu cầu."; answered
  (claims with numbered citation buttons, uncertainty notes, numbered source list, fixture
  badge, retrieval time); abstained (info callout); failed (danger callout, "Hỏi lại câu này",
  "Hỏi về phiên bản hiện tại" for `scope_stale`). No simulated streaming.
- **Composer**: sticky at the bottom (no nested scroll), Enter sends (not during IME
  composition), Shift+Enter new line, disabled while a run is active, rejected question
  returns to the box; "Chưa xác nhận được câu hỏi đã gửi" with "Gửi lại" (same key).
- **"Có câu trả lời mới ↓"**: shown instead of auto-scrolling when the reader is up in the history.
- **Source pane/dialog**: heading focused on open, Escape/Đóng closes and returns focus to the
  citation, the cited passage is scrolled into view inside the pane only; stale/denied/missing
  sources show the stored excerpt with "Bản lỗi thời — đoạn trích lúc trả lời".

## 6. Motion & Interaction

| Interaction | Implementation | Reduced motion |
|---|---|---|
| A. Sticky nav + filter bar | CSS `position: sticky`; observer toggles a shadow | shadow only, no transition |
| B. Reading progress | 3px bar under the nav, `transform: scaleX()` from the article body rect, rAF-throttled passive scroll listener, removed on unmount | still updates (no animation involved) |
| B'. Key point → span | `scrollIntoView({block:"center"})` + `<mark>` focused, fades over 4 s | instant jump, static highlight |
| C. Citation in context | pane beside the answer / modal dialog; focus management | same, no transitions |
| D. Reveal | `.reveal` 420 ms rise on the first six stories of a freshly loaded feed and on newly appended stories only; never on re-render/polling, never on a restored feed; content is visible without it | `animation: none` |
| E. Disclosures | native `<details>`; marker ▸ rotates 90° | no transition |

Rules: native scrolling only (no scroll-jacking, snapping or carousels); no infinite scroll;
transitions only on color/background-size/shadow/opacity/transform; every observer, timer,
rAF and poller is cleaned up on unmount.

## 7. Depth & Surface

Rules instead of cards: 2px ink rules open sections; 1px rules separate items. Shadows only
for floating UI (stuck nav, "new answer" pill). Radii: controls 0.5rem, pills 999px.

## 8. Responsive & Accessibility

- Checked at 320, 390, 768, 1280, 1440 and 640 (= 200% zoom of 1280): no horizontal page scroll
  (re-checked 2026-11-04 after the remediation, `e2e/ui-check.ts`).
- Touch targets ≥ 44px (`--touch-target`), citation buttons get an invisible 8px hit margin.
- Landmarks: masthead, nav, main (`#main-content`, skip link), footer; dialogs labelled;
  live regions for counts/run status; focus ring 3px blue, never removed.
- `interactive-widget=resizes-content` keeps the sticky composer above the on-screen keyboard.

### Navigation

Primary nav: **Tin tức / Hỏi đáp** only. The legacy analyst briefing (`/analyst`) is a
secondary footer entry "Phân tích nâng cao — công cụ cũ: một câu hỏi, một bản tóm lược có trích
dẫn, không lưu hội thoại"; the nav shows "Phân tích nâng cao" as context while on it.

### Accepted Debt

- The list card has no generator identity, so `summary_preview` is **not shown** in the feed
  until the API carries provenance (see README, API gaps).
- `POST /conversations` has no server idempotency: if its response is lost the conversation
  may exist without the client knowing; the UI refreshes the list and says so, but cannot
  guarantee exactly-once.
