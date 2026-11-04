import type { IncomingMessage, ServerResponse } from "node:http"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vite"

// Dev-only same-origin gateway, mirroring nginx.conf (the APIs set no CORS):
//   crud-api  /api/v1/articles[/...], /api/v1/article-facets, /api/v1/topics,
//             /api/v1/locations, /api/v1/sources
//   rag-api   /api/v1/sessions[/...], /api/v1/conversations[/...], /api/v1/runs[/...],
//             /briefing[/...]
//   blocked   /internal[/...] and any other /api/ path -> JSON 404 (never index.html)
// Only server-side URLs are read here; nothing is exposed to the bundle (no VITE_*).
const crudApiTarget = process.env["EVENTLENS_CRUD_API_URL"] ?? "http://localhost:8000"
const ragApiTarget = process.env["EVENTLENS_RAG_API_URL"] ?? "http://localhost:8001"

const CRUD_ROUTES = "^/api/v1/(articles|article-facets|topics|locations|sources)(/|\\?|$)"
const RAG_ROUTES = "^/api/v1/(sessions|conversations|runs)(/|\\?|$)"
const BRIEFING_ROUTES = "^/briefing(/|\\?|$)"

const notFound = (response: ServerResponse) => {
  response.statusCode = 404
  response.setHeader("content-type", "application/json")
  response.end(
    JSON.stringify({
      schema_version: "api_error.v1",
      error: { code: "not_found", reason: null, request_id: null },
    }),
  )
}

/** Runs before the proxy: /internal and unknown /api paths never reach a backend or the SPA. */
const blockUnroutedApi = (): Plugin => ({
  name: "eventlens-block-unrouted-api",
  configureServer(server) {
    server.middlewares.use((request: IncomingMessage, response: ServerResponse, next) => {
      const path = (request.url ?? "/").split("?")[0] ?? "/"
      if (/^\/internal(\/|$)/.test(path)) return notFound(response)
      if (
        path.startsWith("/api/") &&
        !new RegExp(CRUD_ROUTES).test(path) &&
        !new RegExp(RAG_ROUTES).test(path)
      ) {
        return notFound(response)
      }
      next()
    })
  },
})

export default defineConfig({
  plugins: [blockUnroutedApi(), react()],
  server: {
    proxy: {
      [CRUD_ROUTES]: { target: crudApiTarget },
      [RAG_ROUTES]: { target: ragApiTarget },
      [BRIEFING_ROUTES]: { target: ragApiTarget },
    },
  },
})
