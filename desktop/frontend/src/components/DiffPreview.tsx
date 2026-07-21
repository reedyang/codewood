import { useEffect, useMemo, useRef, useState } from "react";
import hljs from "highlight.js/lib/common";
import "highlight.js/styles/atom-one-dark.css";
import type { DiffRow } from "../api/types";

/**
 * Responsive, syntax-highlighted change preview for an apply_patch diff.
 *
 * Renders the structured ``DiffRow`` list the backend sends with a ``confirm``
 * event. The layout adapts to the available width (measured live via a
 * ``ResizeObserver`` so resizing the window re-lays-out the diff):
 *   - wide  → two columns (old │ new), side-by-side
 *   - narrow → inline unified view (removed lines then added lines)
 *
 * Highlighting: each cell's text is highlighted with highlight.js using the
 * language inferred from the patched file's extension (``lang``). highlight.js
 * escapes the tokenized source, so only its own ``<span>`` markup reaches
 * ``dangerouslySetInnerHTML`` — no untrusted HTML is injected. Plain-text
 * fallback is escaped explicitly.
 */
const SIDE_BY_SIDE_MIN_WIDTH = 760;

export function DiffPreview({ rows, lang }: { rows: DiffRow[]; lang?: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [wide, setWide] = useState(true);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") {
      return;
    }
    const apply = (width: number) => setWide(width >= SIDE_BY_SIDE_MIN_WIDTH);
    apply(el.clientWidth);
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        apply(entry.contentRect.width);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const resolvedLang = useMemo(() => normalizeLang(lang), [lang]);

  if (!rows || rows.length === 0) {
    return null;
  }

  return (
    <div className="diff-preview" ref={containerRef}>
      {wide ? (
        <div className="diff-side-by-side">
          {rows.map((row, idx) => (
            <SideBySideRow key={`d-${idx}`} row={row} lang={resolvedLang} />
          ))}
        </div>
      ) : (
        <div className="diff-inline">
          {rows.map((row, idx) => (
            <InlineRows key={`d-${idx}`} row={row} lang={resolvedLang} />
          ))}
        </div>
      )}
    </div>
  );
}

function SideBySideRow({ row, lang }: { row: DiffRow; lang: string }) {
  if (row.type === "omitted") {
    return (
      <div className="diff-row diff-row-omitted">
        <span className="diff-omitted-text">{row.oldText}</span>
      </div>
    );
  }
  const leftCls = row.type === "del" || row.type === "change" ? "diff-del" : "";
  const rightCls = row.type === "add" || row.type === "change" ? "diff-add" : "";
  return (
    <div className="diff-row">
      <div className={`diff-cell diff-cell-old ${leftCls}`}>
        <span className="diff-lineno">{row.oldNo ?? ""}</span>
        <Code text={row.oldText} lang={lang} />
      </div>
      <div className={`diff-cell diff-cell-new ${rightCls}`}>
        <span className="diff-lineno">{row.newNo ?? ""}</span>
        <Code text={row.newText} lang={lang} />
      </div>
    </div>
  );
}

function InlineRows({ row, lang }: { row: DiffRow; lang: string }) {
  if (row.type === "omitted") {
    return (
      <div className="diff-iline diff-row-omitted">
        <span className="diff-omitted-text">{row.oldText}</span>
      </div>
    );
  }
  if (row.type === "context") {
    return (
      <div className="diff-iline">
        <span className="diff-lineno">{row.newNo ?? row.oldNo ?? ""}</span>
        <span className="diff-sign"> </span>
        <Code text={row.newText || row.oldText} lang={lang} />
      </div>
    );
  }
  // del / add / change → show removed line(s) then added line(s).
  return (
    <>
      {row.type === "del" || row.type === "change" ? (
        <div className="diff-iline diff-del">
          <span className="diff-lineno">{row.oldNo ?? ""}</span>
          <span className="diff-sign">-</span>
          <Code text={row.oldText} lang={lang} />
        </div>
      ) : null}
      {row.type === "add" || row.type === "change" ? (
        <div className="diff-iline diff-add">
          <span className="diff-lineno">{row.newNo ?? ""}</span>
          <span className="diff-sign">+</span>
          <Code text={row.newText} lang={lang} />
        </div>
      ) : null}
    </>
  );
}

export function Code({ text, lang }: { text: string; lang: string }) {
  const html = useMemo(() => {
    if (!text) {
      return "";
    }
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(text, { language: lang, ignoreIllegals: true })
          .value;
      }
    } catch {
      // fall through to escaped plain text
    }
    return escapeHtml(text);
  }, [text, lang]);
  return (
    <code
      className="hljs diff-code"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

function normalizeLang(lang?: string): string {
  const l = (lang || "").trim().toLowerCase();
  if (!l) return "";
  const aliases: Record<string, string> = {
    "c++": "cpp",
    "c#": "csharp",
    cs: "csharp",
    py: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "typescript",
    rs: "rust",
    rb: "ruby",
    yml: "yaml",
    ps1: "powershell",
    golang: "go",
    htm: "xml",
    html: "xml",
    vue: "xml",
    md: "markdown",
  };
  return aliases[l] || l;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** Infer a highlight.js language id from a file path's extension. */
export function langFromPath(path?: string): string {
  const p = (path || "").trim();
  const m = /\.([A-Za-z0-9]+)\s*$/.exec(p);
  if (!m) return "";
  return normalizeLang(m[1]);
}
