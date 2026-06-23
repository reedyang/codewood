import { createElement, type ReactNode } from "react";
import { stripLeakedToolMarkup } from "../utils/tokens";

// Compact, dependency-free Markdown renderer. It mirrors the structure the
// terminal highlights (headings, emphasis, inline/fenced code, lists, quotes,
// links) so the model reply is colored in the GUI without a heavy dependency.
// All text flows through React children, so it is escaped by default.

const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)|(~~[^~]+~~)|(\[[^\]]+\]\([^)]+\))/g;

// Curated LaTeX-command -> Unicode map for inline math the model commonly
// emits in narrative (e.g. `$\rightarrow$`). The GUI has no TeX engine, so
// these would render literally. Mirrors `_LATEX_MATH_SYMBOLS` in
// cli/core/assistant_output_highlighter.py — keep the two in sync.
const LATEX_MATH_SYMBOLS: Record<string, string> = {
  leftrightarrow: "\u2194",
  Leftrightarrow: "\u21d4",
  rightarrow: "\u2192",
  Rightarrow: "\u21d2",
  leftarrow: "\u2190",
  Leftarrow: "\u21d0",
  longrightarrow: "\u27f6",
  longleftarrow: "\u27f5",
  uparrow: "\u2191",
  downarrow: "\u2193",
  mapsto: "\u21a6",
  to: "\u2192",
  gets: "\u2190",
  times: "\u00d7",
  div: "\u00f7",
  cdot: "\u00b7",
  pm: "\u00b1",
  mp: "\u2213",
  leq: "\u2264",
  le: "\u2264",
  geq: "\u2265",
  ge: "\u2265",
  neq: "\u2260",
  ne: "\u2260",
  approx: "\u2248",
  equiv: "\u2261",
  infty: "\u221e",
  ldots: "\u2026",
  cdots: "\u22ef",
};

// Inline math spans: `$...$` (not `$$`) and `\(...\)`.
const INLINE_MATH_RE = /(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)|\\\(([^\n]+?)\\\)/g;

function renderInlineMathSpan(body: string): string {
  let s = body
    .replace(/\\,/g, "\u202f")
    .replace(/\\;/g, " ")
    .replace(/\\!/g, "");
  s = s.replace(/\\([A-Za-z]+)/g, (whole, name: string) =>
    Object.prototype.hasOwnProperty.call(LATEX_MATH_SYMBOLS, name)
      ? LATEX_MATH_SYMBOLS[name]
      : whole,
  );
  return s.replace(/[{}]/g, "").trim();
}

/**
 * Convert common inline LaTeX math (`$...$` / `\(...\)`) to Unicode. Only
 * spans that contain a LaTeX command and fully convert (no leftover backslash
 * command) are unwrapped, so plain `$` usage (prices, shell vars) and Greek/
 * unsupported macros are left verbatim. Mirrors `convert_inline_latex_math`
 * in the Python display layer so TUI and GUI render arrows the same way.
 */
export function convertInlineLatexMath(text: string): string {
  if (!text || (!text.includes("$") && !text.includes("\\("))) {
    return text;
  }
  return text.replace(INLINE_MATH_RE, (whole, dollarBody, parenBody) => {
    const body: string | undefined = dollarBody ?? parenBody;
    if (body == null || !body.includes("\\")) {
      return whole;
    }
    const converted = renderInlineMathSpan(body);
    return converted.includes("\\") ? whole : converted;
  });
}

function safeHref(url: string): string | null {
  const u = url.trim();
  return /^(https?:|mailto:)/i.test(u) ? u : null;
}

type TableAlign = "left" | "center" | "right" | null;

// Split a GitHub-style table row into trimmed cells, honoring escaped pipes
// (``\|``) inside a cell and tolerating the optional leading/trailing pipe.
function splitTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) {
    s = s.slice(1);
  }
  if (s.endsWith("|") && !s.endsWith("\\|")) {
    s = s.slice(0, -1);
  }
  const cells: string[] = [];
  let buf = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") {
      buf += "|";
      i++;
      continue;
    }
    if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
      continue;
    }
    buf += ch;
  }
  cells.push(buf.trim());
  return cells;
}

// A delimiter row separates a table header from its body, e.g.
// ``| --- | :--: | ---: |``. Every cell must be dashes with optional
// alignment colons; at least one dash is required so a plain ``---`` rule or
// prose is not mistaken for a table.
function isTableDelimiterRow(line: string): boolean {
  const s = line.trim();
  if (!s.includes("-") || !/^[\s|:-]+$/.test(s)) {
    return false;
  }
  const cells = splitTableRow(s);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c));
}

function tableAligns(delimLine: string): TableAlign[] {
  return splitTableRow(delimLine).map((c) => {
    const left = c.startsWith(":");
    const right = c.endsWith(":");
    if (left && right) {
      return "center";
    }
    if (right) {
      return "right";
    }
    if (left) {
      return "left";
    }
    return null;
  });
}

function alignClass(align: TableAlign): string | undefined {
  return align ? `md-table-${align}` : undefined;
}

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = new RegExp(INLINE_RE);
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      // Convert LaTeX math only on plain-text runs between inline tokens, so
      // inline code (a matched token) is never touched.
      nodes.push(convertInlineLatexMath(text.slice(last, m.index)));
    }
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (tok.startsWith("`")) {
      nodes.push(
        <code key={k} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      nodes.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("~~")) {
      nodes.push(<del key={k}>{tok.slice(2, -2)}</del>);
    } else if (tok.startsWith("*") || tok.startsWith("_")) {
      nodes.push(<em key={k}>{tok.slice(1, -1)}</em>);
    } else if (tok.startsWith("[")) {
      const mm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      const href = mm ? safeHref(mm[2]) : null;
      if (mm && href) {
        nodes.push(
          <a key={k} href={href} target="_blank" rel="noreferrer noopener">
            {mm[1]}
          </a>,
        );
      } else {
        nodes.push(tok);
      }
    }
    last = re.lastIndex;
  }
  if (last < text.length) {
    nodes.push(convertInlineLatexMath(text.slice(last)));
  }
  return nodes;
}

// Plan-mode wraps its finished plan in `<proposed_plan>...</proposed_plan>`
// (see cli/prompts/collaboration-mode/plan.md). Mirror the Python helper
// cli/core/proposed_plan.py: render the block as a dedicated card instead of
// leaking the literal tags into the bubble.
const PROPOSED_PLAN_RE = /<proposed_plan>\s*([\s\S]*?)\s*<\/proposed_plan>/gi;

/**
 * Split assistant text into ordinary segments and proposed-plan cards. Each
 * proposed-plan body is rendered through MarkdownText inside a styled card; the
 * surrounding prose renders normally. Returns null when there is no plan block
 * so the caller can fall back to the plain render path.
 */
function renderWithProposedPlan(text: string, baseKey: string): ReactNode[] | null {
  if (!text || !text.includes("<proposed_plan>")) {
    return null;
  }
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  // Fresh regex instance per call so the global `lastIndex` is never shared
  // across calls (that would make matching non-deterministic / re-entrant).
  const re = new RegExp(PROPOSED_PLAN_RE.source, "gi");
  while ((m = re.exec(text)) !== null) {
    const before = text.slice(last, m.index).trim();
    if (before) {
      // Render surrounding prose with the plain markdown body — NOT MarkdownText
      // — so we never re-enter the proposed-plan splitter. Re-feeding text that
      // still contains an (incomplete) `<proposed_plan>` opener into the
      // splitter recurses forever and crashes the renderer (out of memory).
      nodes.push(<MarkdownBody key={`${baseKey}-pre-${i}`} text={before} />);
    }
    const body = (m[1] || "").trim();
    if (body) {
      nodes.push(
        <div key={`${baseKey}-plan-${i}`} className="proposed-plan-card">
          <div className="proposed-plan-card-title">Proposed Plan</div>
          <MarkdownBody text={body} />
        </div>,
      );
    }
    last = m.index + m[0].length;
    i++;
    if (m.index === re.lastIndex) {
      re.lastIndex++; // defensive: avoid a stuck loop on a zero-width match
    }
  }
  // No COMPLETE block matched (e.g. the closing tag has not streamed yet):
  // fall back to plain rendering of the whole text instead of recursing.
  if (last === 0) {
    return null;
  }
  const after = text.slice(last).trim();
  if (after) {
    nodes.push(<MarkdownBody key={`${baseKey}-post`} text={after} />);
  }
  return nodes;
}

export function MarkdownText({ text }: { text: string }): ReactNode {
  const planNodes = renderWithProposedPlan(text, "pp");
  if (planNodes) {
    return <>{planNodes}</>;
  }
  return <MarkdownBody text={text} />;
}

function MarkdownBody({ text }: { text: string }): ReactNode {
  // Defense-in-depth: drop any leaked pseudo tool-call / envelope markup that
  // survived the backend streaming cutter (e.g. stale chat records). Complete
  // <proposed_plan> blocks are handled by renderWithProposedPlan before we get
  // here; this only removes dangling openers and angle-bracket tool fragments.
  const lines = stripLeakedToolMarkup(text).replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  // A table starts where the current line is a row and the next line is a
  // delimiter row. Used both to emit the table and to stop a paragraph from
  // swallowing a table that follows it without a blank separator line.
  const isTableStart = (idx: number): boolean =>
    idx + 1 < lines.length &&
    lines[idx].includes("|") &&
    isTableDelimiterRow(lines[idx + 1]);

  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line.trim())) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        buf.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      blocks.push(
        <pre key={key++} className="md-pre">
          <code>{buf.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      const level = Math.min(heading[1].length, 6);
      blocks.push(
        createElement(
          `h${level}`,
          { key: key++, className: "md-h" },
          renderInline(heading[2], `h${key}`),
        ),
      );
      i++;
      continue;
    }

    if (/^\s*([-*_])(\s*\2){2,}\s*$/.test(line)) {
      blocks.push(<hr key={key++} className="md-hr" />);
      i++;
      continue;
    }

    if (isTableStart(i)) {
      const headerCells = splitTableRow(line);
      const aligns = tableAligns(lines[i + 1]);
      i += 2;
      const bodyRows: string[][] = [];
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        lines[i].includes("|")
      ) {
        bodyRows.push(splitTableRow(lines[i]));
        i++;
      }
      const tableKey = key++;
      blocks.push(
        <table key={tableKey} className="md-table">
          <thead>
            <tr>
              {headerCells.map((cell, ci) => (
                <th key={ci} className={alignClass(aligns[ci] ?? null)}>
                  {renderInline(cell, `th${tableKey}-${ci}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {bodyRows.map((row, ri) => (
              <tr key={ri}>
                {headerCells.map((_, ci) => (
                  <td key={ci} className={alignClass(aligns[ci] ?? null)}>
                    {renderInline(row[ci] ?? "", `td${tableKey}-${ri}-${ci}`)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items: ReactNode[] = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        const content = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "");
        items.push(<li key={items.length}>{renderInline(content, `li${key}-${items.length}`)}</li>);
        i++;
      }
      blocks.push(
        ordered ? (
          <ol key={key++} className="md-list">
            {items}
          </ol>
        ) : (
          <ul key={key++} className="md-list">
            {items}
          </ul>
        ),
      );
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={key++} className="md-quote">
          {renderInline(buf.join(" "), `q${key}`)}
        </blockquote>,
      );
      continue;
    }

    if (line.trim() === "") {
      i++;
      continue;
    }

    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i].trim()) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*([-*+]|\d+\.)\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !isTableStart(i)
    ) {
      para.push(lines[i]);
      i++;
    }
    const inlineNodes: ReactNode[] = [];
    para.forEach((p, idx) => {
      if (idx > 0) {
        inlineNodes.push(<br key={`br-${key}-${idx}`} />);
      }
      inlineNodes.push(...renderInline(p, `p${key}-${idx}`));
    });
    blocks.push(
      <p key={key++} className="md-p">
        {inlineNodes}
      </p>,
    );
  }

  return <div className="md">{blocks}</div>;
}
