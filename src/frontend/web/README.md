# EventLens web — tin tức, tóm tắt và hỏi đáp

React 19 + TypeScript + Vite + Bun. Routes: `/news` (feed), `/articles/:id`, `/chat`,
`/chat/:conversationId`, `/analyst` (legacy analyst briefing; `/` redirects to `/news`).
Design system: [`DESIGN.md`](DESIGN.md). Backend contract: `docs/news-backend.md`,
`docs/openapi/*.openapi.json`.

## Code map

| Path | Role |
| --- | --- |
| `src/api/http.ts` | single HTTP boundary: path allowlist, timeouts, `api_error.v1`/`briefing_error.v1` → typed failure, strict schema parse |
| `src/api/catalog.ts`, `src/api/chat.ts` | zod mirrors of `catalog.v1` and the chat DTOs; clients; anonymous session manager |
| `src/lib/` | router (History API + scroll restoration), filters ⇄ URL, Asia/Ho_Chi_Minh dates, code-point spans, poller, Vietnamese error copy |
| `src/news/`, `src/article/`, `src/chat/` | pages, hooks (`useFeed`, `useArticleSummary`, `useThread`, `useCitationViewer`) |
| `src/briefing/` | legacy `/briefing` workspace (unchanged contract) |
| `e2e/layout.e2e.ts` | browser suite for `/analyst` (stubbed API, runs in CI) |
| `e2e/news.e2e.ts` | browser suite against a **running stack** (real APIs) |
| `e2e/visual-qa.ts` | screenshot sweep for manual review (writes to a gitignored folder) |
| `e2e/ui-check.ts` | hands-on check: clicks the main flows at 390/768/1280/200 % zoom, keyboard, reduced motion, facets down; screenshots to a gitignored folder |

## Run

### Development (Vite, hot reload)

```sh
cd src/frontend/web && bun install
EVENTLENS_CRUD_API_URL=http://127.0.0.1:18000 \
EVENTLENS_RAG_API_URL=http://127.0.0.1:18001 \
  bun run dev -- --port 5180          # http://127.0.0.1:5180/news
```

Defaults are `http://localhost:8000` / `:8001` (`make crud-api`, `make rag-api`). The
targets are read by the Vite server only; no `VITE_*` variable carries a URL or token.

### Persistent demo (production image, recommended)

`scripts/news/demo.sh` drives Compose project **`eventlens-news-demo`**
(`docker/news-demo.compose.yaml`): PostgreSQL 17 + pgvector with volume
`eventlens-news-demo_news-demo-pg`, database **`eventlens_news_eval`**, rag-api, crud-api,
`rag-worker serve` and the production web image (Nginx). Ports bind to 127.0.0.1 only:
web **18080**, crud-api 18000, rag-api 18001, PostgreSQL 55442 (none of the default stack's).

```sh
scripts/news/demo.sh up          # first run: generates .private/news-demo.env, builds, migrates, starts
scripts/news/demo.sh import      # handoff-import $NEWS_DEMO_HANDOFF_DIR/{articles.jsonl,manifest.json}
                                 # (read-only, _eval guard, idempotent) + `eventlens-rag rebuild`
                                 # (retrieval chunks; fake embeddings, lexical branch real)
scripts/news/demo.sh mode none     # AI unavailable (default)
scripts/news/demo.sh mode fixture  # explicit simulated AI; output labelled "Dữ liệu AI mô phỏng"
scripts/news/demo.sh stop | start | restart | status | logs [service] | rebuild
```

Open **http://127.0.0.1:18080/news**. Data, sessions, conversations and cursors survive
`stop/start/restart` (volume + fixed `EVENTLENS_RAG_CURSOR_SECRET`). The script never runs
`down -v`; to remove everything deliberately: `docker compose --env-file .private/news-demo.env
-f docker/news-demo.compose.yaml down -v` (destroys the demo database).

`.private/news-demo.env` holds the DB password, the internal service token, the cursor secret,
the AI mode and the handoff folder; it is gitignored and excluded from Docker build contexts.

### Production web image alone

`docker/web.Dockerfile` builds the bundle and serves it with `nginx.conf` (+
`proxy-common.inc`). It expects services named `crud-api` and `rag-api` on the Docker network
(`compose.yaml`, `docker/news-demo.compose.yaml`).

## Same-origin route table (Vite dev and Nginx)

| Path | Upstream |
| --- | --- |
| `/api/v1/articles`, `/api/v1/articles/*`, `/api/v1/article-facets`, `/api/v1/topics`, `/api/v1/locations`, `/api/v1/sources` | crud-api :8000 |
| `/api/v1/sessions[/*]`, `/api/v1/conversations[/*]`, `/api/v1/runs[/*]`, `/briefing`, `/briefing/*` | rag-api :8001 |
| `/internal`, `/internal/*` | blocked: JSON `404 not_found` |
| any other `/api/*` | JSON `404 not_found` (never `index.html`) |
| upstream down | Nginx answers JSON `503 upstream_unavailable` |
| everything else | static files, SPA fallback to `index.html` (deep links reload) |

Path and query string are forwarded unchanged; `Cookie`, `Set-Cookie` and `X-Request-ID` pass
through. Nginx resolves upstream names per request (Docker DNS `127.0.0.11`), so a recreated
API container is reached without restarting the web container.

## Tests

```sh
bun test                    # unit + component (happy-dom), API mocked per test
bun run check && bun run typecheck && bun run build
bun run test:e2e            # /analyst browser suite (stubbed API) — CI
EVENTLENS_BROWSER=chromium EVENTLENS_E2E_AI_MODE=fixture bun run test:e2e:live   # real stack at :18080
EVENTLENS_E2E_BASE_URL=http://127.0.0.1:5180 …                                    # or against Vite
bun e2e/visual-qa.ts http://127.0.0.1:18080 ../../../.private/news-frontend-qa <label>
```

`test:e2e:live` asserts the mode it is told (`none` or `fixture`); it does not switch it.
Only two of its cases mock the network (marked `MOCK`): a refused connection (error + retry)
and a labelled fixture catalog for topic/location/date combinations (the real corpus has no
trusted labels yet).

## Behaviour notes

- Chat entry points (feed sidebar/inline link, "Hỏi về bài này", "Hỏi về phiên bản hiện tại")
  open a **draft** at `/chat?…` (`scope=filtered&<feed filter keys>` or
  `article=<id>&version=<sha256:…>`); the conversation is created only when the first question
  is sent, with the question as its title. The id and a new Idempotency-Key are stored
  (sessionStorage, same-tab memory fallback) before the run is submitted; retries reuse both.
- The feed never shows `summary_preview` (no provenance on the card, see API gaps).

- Session: `POST /api/v1/sessions` only after a `401`, shared by concurrent calls (single
  flight); the token stays in the HttpOnly cookie. A non-secret expiry marker in
  `localStorage` lets the UI say "Phiên cũ đã hết hạn" when a session had to be replaced.
- Runs: one `Idempotency-Key` per question, reused for retries (also after reload through
  `sessionStorage`) when the server's answer is unknown (timeout/network/5xx); new question →
  new key. `409 run_in_progress` reloads history and follows the real active run. Cancel shows
  "Đã hủy" only when the run status is `cancelled`.
- Polling: runs 1 s × 1.5 up to 5 s (≤ 11 min); pending summaries 2 s × 1.6 up to 30 s
  (12 reads, then "Kiểm tra lại"); one request at a time, paused while the tab is hidden,
  immediate re-sync when visible, stopped on unmount.
- Dates: calendar days in Asia/Ho_Chi_Minh → `[after, before)` UTC instants with offset.
- Spans: offsets are Unicode code points (`Array.from`), checked against the displayed
  version and excerpt before highlighting.

## Limits (state on 2026-11-03)

- **No real model is connected.** Mode `none`: runs are refused with `503 model_unavailable`
  and summaries stay `pending` (the UI says so). Mode `fixture`: deterministic fake answers
  and extractive summaries, always labelled "Dữ liệu AI mô phỏng". Summaries produced while
  in fixture mode stay stored (and labelled) after switching back to `none`.
- **No trusted metadata.** The 200 imported articles have `metadata` `pending`/`unavailable`;
  topic/location filters are wired to the API but return 0 results, with a notice.
- Retrieval chunks are built by `eventlens-rag rebuild` with fake embeddings; the vector
  branch carries no meaning, the lexical branch is real.
- Lexical search uses PostgreSQL `simple` (no Vietnamese word segmentation or accent folding).
- Sessions are anonymous browser sessions (7 days), not accounts.

## API gaps found while building the UI (not changed)

1. `ArticleCardV1` has no summary generator identity, so the feed cannot mark a preview as
   simulated; the article page can (`article_summary.v1.generator`). The feed therefore hides
   previews entirely (no per-card summary request).
2. No "current session" endpoint: the UI learns that a session is missing only from a `401`
   (handled, single flight).
3. No "active run" field on the conversation: after `409 run_in_progress` or a reload the UI
   derives it from the message history and `GET /runs/{id}`.
4. The OpenAPI of rag-api lists only `422` for errors (FastAPI default) while the routes answer
   `api_error.v1` with 400/401/403/404/409/410/503; the UI follows `docs/news-backend.md` §4.
5. `POST /api/v1/conversations` has no idempotency: a create whose response is lost may leave
   an empty conversation; the UI does not retry it by itself and refreshes the list, but
   exactly-once is not guaranteed.
6. Facets cannot tell "no labelled article in this result" from "the corpus has no labels";
   the drawer only states the former.
7. `/briefing/sources/{ref}` errors keep the legacy `briefing_error.v1` envelope while
   `source-ref` uses `api_error.v1`; the client accepts both.
