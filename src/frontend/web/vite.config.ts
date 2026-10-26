import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Dev-only proxy so the browser calls rag-api same-origin (the API sets no CORS).
const ragApiTarget = process.env["EVENTLENS_RAG_API_URL"] ?? "http://localhost:8001"

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/briefing": ragApiTarget,
    },
  },
})
