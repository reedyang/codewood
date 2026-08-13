import { useMemo, useState } from "react";
import hljs from "highlight.js/lib/common";
import "highlight.js/styles/atom-one-dark.css";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

/**
 * Syntax-highlighted fenced code block for the GUI Markdown renderer.
 *
 * Encapsulates all highlight.js usage so the rest of the Markdown renderer
 * stays library-agnostic (mirrors the standalone Python `SyntaxHighlighter`).
 * Highlighting only runs once the fenced block is fully closed
 * (`highlight={true}`); while the model is still streaming an open fence the
 * code is shown as plain escaped text. This is important: re-highlighting a
 * growing buffer on every streamed chunk is wasteful, and (combined with a
 * past parser bug) used to freeze the renderer.
 *
 * Security: highlight.js escapes the source it tokenizes, so the returned HTML
 * contains only its own `<span class="hljs-...">` markup around escaped text —
 * no untrusted HTML reaches `dangerouslySetInnerHTML`. The plain-text fallback
 * is escaped explicitly.
 */
export function CodeBlock({
  code,
  lang,
  highlight = true,
}: {
  code: string;
  lang?: string;
  highlight?: boolean;
}) {
  const { html, resolvedLang } = useMemo(() => {
    const normalized = normalizeLang(lang);
    // While streaming, render plain escaped text — don't highlight a partial,
    // growing buffer on every chunk.
    if (!highlight) {
      return { html: escapeHtml(code), resolvedLang: normalized || "" };
    }
    try {
      if (normalized && hljs.getLanguage(normalized)) {
        const res = hljs.highlight(code, {
          language: normalized,
          ignoreIllegals: true,
        });
        return { html: res.value, resolvedLang: res.language ?? normalized };
      }
      // Unknown/blank language on a closed block: auto-detect. This is bounded
      // (runs once per closed block, never per streamed chunk).
      const res = hljs.highlightAuto(code);
      return { html: res.value, resolvedLang: res.language ?? "" };
    } catch {
      // Defensive: never let a highlighter error break rendering.
      return { html: escapeHtml(code), resolvedLang: "" };
    }
  }, [code, lang, highlight]);

  // Offer an in-browser preview for closed HTML blocks only (an open, still-
  // streaming fence isn't a complete document yet).
  const isHtml =
    highlight && (resolvedLang === "xml" || normalizeLang(lang) === "html");

  return (
    <pre className="md-pre">
      {resolvedLang && !isHtml ? (
        <span className="md-pre-lang" aria-hidden>
          {resolvedLang}
        </span>
      ) : null}
      {isHtml ? <PreviewButton code={code} /> : null}
      <CopyButton code={code} />
      <code className="hljs" dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  );
}

/** Floating "Preview" button shown on HTML code blocks; opens the rendered
 *  snippet in the embedded browser tab. */
function PreviewButton({ code }: { code: string }) {
  const { previewHtmlInBrowser, t } = useApp();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="md-pre-preview"
      title={t("browser.preview")}
      aria-label={t("browser.preview")}
      disabled={busy}
      onClick={() => {
        setBusy(true);
        void previewHtmlInBrowser(code).finally(() => setBusy(false));
      }}
    >
      <Icon name="eye" size={13} />
      <span>{t("browser.preview")}</span>
    </button>
  );
}

/** Floating "Copy" button shown on hover in the top-right corner of every
 *  fenced code block; copies the raw code text to the clipboard. */
function CopyButton({ code }: { code: string }) {
  const { t } = useApp();
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="md-pre-copy"
      title={t("msg.copy")}
      aria-label={t("msg.copy")}
      onClick={() => {
        void navigator.clipboard?.writeText(code);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
    >
      <Icon name={copied ? "check" : "copy"} size={13} />
    </button>
  );
}

// Map common fence tags / aliases to highlight.js language ids. highlight.js
// understands most aliases already; we only normalize case and a few tags the
// model emits that aren't built-in aliases.
function normalizeLang(lang?: string): string {
  const l = (lang || "").trim().toLowerCase();
  if (!l) return "";
  const aliases: Record<string, string> = {
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    console: "bash",
    "c++": "cpp",
    "c#": "csharp",
    cs: "csharp",
    py: "python",
    py3: "python",
    python3: "python",
    js: "javascript",
    jsx: "javascript",
    node: "javascript",
    ts: "typescript",
    tsx: "typescript",
    rs: "rust",
    rb: "ruby",
    yml: "yaml",
    ps: "powershell",
    ps1: "powershell",
    pwsh: "powershell",
    golang: "go",
    docker: "dockerfile",
  };
  return aliases[l] || l;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
