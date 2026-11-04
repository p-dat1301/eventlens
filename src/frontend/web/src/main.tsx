import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import { App } from "./App"
// Self-hosted fonts (Fontsource): each CSS declares latin, latin-ext and
// vietnamese subsets with unicode-range, so only the needed files load.
import "@fontsource/newsreader/400.css"
import "@fontsource/newsreader/500.css"
import "@fontsource/newsreader/600.css"
import "@fontsource/newsreader/400-italic.css"
import "@fontsource/be-vietnam-pro/400.css"
import "@fontsource/be-vietnam-pro/600.css"
import "@fontsource/be-vietnam-pro/700.css"
import "./styles.css"

const rootElement = document.getElementById("root")

if (rootElement === null) {
  throw new Error("Root element missing")
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
