// Must run before anything that may touch Web Storage: on WebKitGTK (Linux
// pywebview) ``localStorage`` is undefined for file:// pages, which otherwise
// crashes the app on first access. Installs an in-memory fallback if needed.
import "./utils/ensureStorage";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "katex/dist/katex.min.css";
import "./styles.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root container not found");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
