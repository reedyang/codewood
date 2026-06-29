import { createElement, type ReactNode } from "react";
import katex from "katex";
import { stripLeakedToolMarkup } from "../utils/tokens";
import { CodeBlock } from "./CodeBlock";

// Compact, dependency-free Markdown renderer. It mirrors the structure the
// terminal highlights (headings, emphasis, inline/fenced code, lists, quotes,
// links) so the model reply is colored in the GUI without a heavy dependency.
// All text flows through React children, so it is escaped by default.

const INLINE_RE =
  /(`[^`]+`)|(\*\*\*[^*]+\*\*\*)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*]+\*)|(_[^_]+_)|(~~[^~]+~~)|(<u>[^<]*<\/u>)|(\[[^\]]+\]\([^)]+\))|(\[\^[^\]]+\])/g;

// Curated LaTeX-command -> Unicode map for inline math the model commonly
// emits in narrative (e.g. `$\rightarrow$`). The GUI has no TeX engine, so
// these would render literally. Mirrors `_LATEX_MATH_SYMBOLS` in
// cli/core/text_output_renderer.py — keep the two in sync.
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

// Render a LaTeX math span to KaTeX HTML. KaTeX escapes the math source while
// building its own markup, so the returned string is safe to inject. On a parse
// error we fall back to the raw source (with throwOnError:false KaTeX renders a
// styled error node instead of crashing the whole reply).
function renderKatex(body: string, display: boolean): string {
  try {
    return katex.renderToString(body, {
      displayMode: display,
      throwOnError: false,
      output: "html",
    });
  } catch {
    return "";
  }
}

let mathKeySeq = 0;
let _fnMap: Map<string, string> | null = null;

function katexNode(body: string, display: boolean): ReactNode {
  const html = renderKatex(body, display);
  const key = `katex-${mathKeySeq++}`;
  if (!html) {
    // KaTeX could not render: show the original source verbatim so the user at
    // least sees the formula text rather than nothing.
    return display ? (
      <div key={key} className="md-math-block">
        {display ? `$$${body}$$` : `$${body}$`}
      </div>
    ) : (
      <span key={key}>{`$${body}$`}</span>
    );
  }
  const Tag = display ? "div" : "span";
  return createElement(Tag, {
    key,
    className: display ? "md-math-block" : "md-math-inline",
    dangerouslySetInnerHTML: { __html: html },
  });
}

// Inline math: `$...$` (single dollars, not `$$`) and `\(...\)`. A span must
// not start/end with whitespace and must not span blank lines so plain `$`
// usage (prices, shell vars) is left alone.
const INLINE_MATH_KATEX_RE =
  /(?<!\\)\$(?!\$)(?!\s)((?:\\.|[^$\\\n])+?)(?<!\s)\$(?!\$)|\\\(([\s\S]+?)\\\)/g;

// A `$...$` body that is only digits/separators/spaces (e.g. `100 到 `, `5.00`)
// is currency, never math — guards against `$100 到 $200` pairing the wrong
// dollars. Mirrors `_CURRENCY_BODY_RE` in cli/core/text_output_renderer.py.
const CURRENCY_BODY_RE = /^[\d.,\s]+$/;

// Treat a `$...$` body as math only when it carries a math signal (a LaTeX
// command, a `^`/`_` script, a relation, or a variable next to an operator).
// Mirrors `_looks_like_inline_math` in the Python display layer so the GUI and
// TUI agree on which spans are math vs. plain `$` usage. `\(...\)` is always
// math (explicit delimiters) so it bypasses this check.
function looksLikeInlineMath(body: string): boolean {
  if (!body) {
    return false;
  }
  if (CURRENCY_BODY_RE.test(body)) {
    return false;
  }
  if (body.includes("\\") || body.includes("^") || body.includes("_")) {
    return true;
  }
  if (/[=<>]|\\(?:le|ge|leq|geq|neq|ne|approx|equiv)\b/.test(body)) {
    return true;
  }
  if (/[A-Za-z]\s*[-+*/=]\s*[A-Za-z0-9]/.test(body)) {
    return true;
  }
  // A bare 1-2 letter variable, e.g. `$x$` / `$y$`. A paired `$<letters>$` is
  // far likelier a math variable than stray `$` text.
  return /^[A-Za-z\u0370-\u03ff]{1,2}$/.test(body.trim());
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

// Render a plain-text run (already free of inline markdown tokens like code or
// emphasis) into nodes, turning inline `$...$` / `\(...\)` math into KaTeX and
// leaving the rest as text. Plain `$` not forming a math span is preserved.
function renderTextRunWithMath(run: string, keyBase: string): ReactNode[] {
  if (!run) {
    return [];
  }
  if (!run.includes("$") && !run.includes("\\(")) {
    return [run];
  }
  const out: ReactNode[] = [];
  const re = new RegExp(INLINE_MATH_KATEX_RE.source, "g");
  let last = 0;
  let mm: RegExpExecArray | null;
  let n = 0;
  while ((mm = re.exec(run)) !== null) {
    const dollarBody = mm[1];
    const parenBody = mm[2];
    const body = (dollarBody ?? parenBody ?? "").trim();
    // `\(...\)` is explicit math; `$...$` must pass the math heuristic so plain
    // `$` usage (prices, shell vars) is not eaten as a formula.
    const isMath =
      body !== "" && (parenBody != null || looksLikeInlineMath(body));
    if (!isMath) {
      // Not math: keep the original text (including the `$` delimiters) verbatim
      // and continue scanning right after this `$` so a later real span matches.
      out.push(run.slice(last, mm.index + 1));
      last = mm.index + 1;
      re.lastIndex = mm.index + 1;
      continue;
    }
    if (mm.index > last) {
      out.push(run.slice(last, mm.index));
    }
    out.push(katexNode(body, false));
    last = re.lastIndex;
    if (mm.index === re.lastIndex) {
      re.lastIndex++;
    }
    n++;
  }
  if (last < run.length) {
    out.push(run.slice(last));
  }
  // Keep a stable-ish key namespace for the consumed run.
  void keyBase;
  void n;
  return out;
}

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = new RegExp(INLINE_RE);
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      // Convert inline math (KaTeX) only on plain-text runs between inline
      // tokens, so inline code (a matched token) is never touched.
      nodes.push(...renderTextRunWithMath(text.slice(last, m.index), `${keyBase}-tx${i}`));
    }
    const tok = m[0];
    const k = `${keyBase}-${i++}`;
    if (tok.startsWith("`")) {
      nodes.push(
        <code key={k} className="md-code">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("***")) {
      nodes.push(<strong key={k}><em>{tok.slice(3, -3)}</em></strong>);
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      nodes.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("<u>")) {
      nodes.push(<u key={k}>{tok.slice(3, -4)}</u>);
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
      } else if (tok.startsWith("[^")) {
        const fn = /^\[(\^[^\]]+)\]$/.exec(tok);
        if (fn && _fnMap?.has(fn[1])) {
          nodes.push(
            <sup key={k}>
              <a href={`#fn-${fn[1]}`} className="md-footnote-ref">{fn[1].slice(1)}</a>
            </sup>,
          );
        } else {
          nodes.push(tok);
        }
      } else {
        nodes.push(tok);
      }
    }
    last = re.lastIndex;
  }
  if (last < text.length) {
    nodes.push(...renderTextRunWithMath(text.slice(last), `${keyBase}-txend`));
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
  const rawLines = stripLeakedToolMarkup(text).replace(/\r\n/g, "\n").split("\n");

  // ---- footnotes ----
  // Collect footnote definitions (`[^label]: content ...`) before rendering.
  const footnotes = new Map<string, string>();
  for (let li = 0; li < rawLines.length; li++) {
    const m = /^\[(\^[^\]]+)\]:\s*(.*)$/.exec(rawLines[li]);
    if (m) {
      const label = m[1];
      const parts: string[] = [m[2]];
      li++;
      while (li < rawLines.length && /^\s{2,}/.test(rawLines[li])) {
        parts.push(rawLines[li].trimStart());
        li++;
      }
      li--;
      footnotes.set(label, parts.join(" "));
    }
  }
  const lines = rawLines.filter((l) => !/^\[\^[^\]]+\]:\s*/.test(l));
  _fnMap = footnotes.size > 0 ? footnotes : null;

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

    // Block math: a `$$` fence on its own line opens a display-math block that
    // runs until the closing `$$`. Also support a single-line `$$ ... $$`.
    const trimmed = line.trim();
    if (trimmed.startsWith("$$")) {
      const singleLine = /^\$\$(.+?)\$\$$/.exec(trimmed);
      if (singleLine) {
        blocks.push(
          <div key={key++} className="md-math-wrap">
            {katexNode(singleLine[1].trim(), true)}
          </div>,
        );
        i++;
        continue;
      }
      // Opening fence (possibly with content after `$$` on the same line).
      const buf: string[] = [];
      const afterOpen = trimmed.slice(2);
      if (afterOpen.trim() !== "") {
        buf.push(afterOpen);
      }
      i++;
      let closed = false;
      while (i < lines.length) {
        const cur = lines[i];
        const closeIdx = cur.indexOf("$$");
        if (closeIdx >= 0) {
          const before = cur.slice(0, closeIdx);
          if (before.trim() !== "") {
            buf.push(before);
          }
          i++;
          closed = true;
          break;
        }
        buf.push(cur);
        i++;
      }
      const body = buf.join("\n").trim();
      if (closed) {
        blocks.push(
          <div key={key++} className="md-math-wrap">
            {katexNode(body, true)}
          </div>,
        );
      } else if (body) {
        // No closing fence (e.g. still streaming): render as plain text so the
        // partial source is visible instead of swallowed.
        blocks.push(
          <p key={key++} className="md-p">
            {`$$${body}`}
          </p>,
        );
      }
      continue;
    }

    // Fence open: any line that begins with 3+ backticks/tildes. We
    // deliberately match the same broad shape the paragraph loop below uses to
    // stop (``/^```/``); using a stricter regex here once caused an infinite
    // loop for info-string fences like ```js title="x" that this branch
    // skipped but the paragraph loop refused to consume.
    const fenceOpen = /^\s*(`{3,}|~{3,})(.*)$/.exec(line);
    if (fenceOpen) {
      const fenceChar = fenceOpen[1][0];
      // First whitespace-delimited token after the fence is the language tag;
      // ignore any trailing info-string attributes.
      const lang = (fenceOpen[2] || "").trim().split(/\s+/)[0] || "";
      const closeRe =
        fenceChar === "`" ? /^\s*`{3,}\s*$/ : /^\s*~{3,}\s*$/;
      const buf: string[] = [];
      i++;
      let closed = false;
      while (i < lines.length) {
        if (closeRe.test(lines[i])) {
          closed = true;
          i++; // skip closing fence
          break;
        }
        buf.push(lines[i]);
        i++;
      }
      // Only run syntax highlighting once the block is fully closed. While the
      // model is still streaming an open fence, highlighting the growing buffer
      // on every chunk is expensive (and pointless), so render it as plain
      // preformatted text until the closing fence arrives.
      blocks.push(
        <CodeBlock
          key={key++}
          code={buf.join("\n")}
          lang={lang}
          highlight={closed}
        />,
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

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
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
      !lines[i].trim().startsWith("$$") &&
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

  // ---- footnote definitions at bottom ----
  if (footnotes.size > 0) {
    const items: ReactNode[] = [];
    let fnKey = 0;
    footnotes.forEach((body, label) => {
      items.push(
        <li key={fnKey++} className="md-footnote-item" id={`fn-${label}`}>
          <sup className="md-footnote-label">{label.slice(1)}</sup>
          {renderInline(body, `fn-${label}`)}
        </li>,
      );
    });
    blocks.push(
      <hr key={key++} className="md-hr" />,
      <ol key={key++} className="md-footnotes">{items}</ol>,
    );
  }

  return <div className="md">{blocks}</div>;
}
