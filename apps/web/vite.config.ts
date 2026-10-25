import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Dev-only proxy so the browser calls the API same-origin (the API sets no CORS).
const apiTarget = process.env["EVENTLENS_API_URL"] ?? "http://127.0.0.1:8000"

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/briefing": apiTarget,
    },
  },
})
