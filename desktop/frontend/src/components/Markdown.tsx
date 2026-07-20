import { createElement, type ReactNode } from "react";
import katex from "katex";
import { stripLeakedToolMarkup } from "../utils/tokens";
import { CodeBlock } from "./CodeBlock";

// Compact, dependency-free Markdown renderer. It mirrors the structure the
// terminal highlights (headings, emphasis, inline/fenced code, lists, quotes,
// links) so the model reply is colored in the GUI without a heavy dependency.
// All text flows through React children, so it is escaped by default.

// Underscore / asterisk emphasis (`_italic_`, `*italic*`, `__bold__`,
// `**bold**`, `***bolditalic***`) is only treated as emphasis when the char
// immediately OUTSIDE each delimiter is NOT a letter, digit, or underscore
// (i.e. not `[\w]`). This mirrors the TUI rule in
// cli/core/text_output_renderer.py so identifiers like `project_context_search`
// / `a*b*c` / `run_subagent` are NOT mistaken for emphasis (which would
// otherwise swallow the whole span — including any `$...$` math — as a single
// <em>/<strong>). Plain `_x_` / `*x*` surrounded by spaces still italicize.
const INLINE_RE =
  /(`[^`]+`)|(?<!\w)\*\*\*[^*]+\*\*\*(?!\w)|(?<!\w)\*\*[^*]+\*\*(?!\w)|(?<!\w)__[^\s_]+__(?!\w)|(?<!\w)\*[^*]+\*(?!\w)|(?<!\w)_[^\s_]+_(?!\w)|(~~[^~]+~~)|(<u>[^<]*<\/u>)|(\[[^\]]+\]\([^)]+\))|(\[\^[^\]]+\])/g;

// ---------------------------------------------------------------------------
// Bare-LaTeX auto-detection: the model sometimes emits LaTeX without `$`
// delimiters (e.g. `\xrightarrow{call}`, `\begin{cases}...\end{cases}`).
// KaTeX can render these, but the text never reaches KaTeX without delimiters.
// This pre-processor detects high-confidence bare-LaTeX constructs and wraps
// them in `$...$` / `$$...$$` so the existing KaTeX pipeline handles them.
// ---------------------------------------------------------------------------

// Matches `\begin{xxx}...\end{xxx}` (single-line or multi-line). The inner
// content may contain `\\`, `&`, `\text{...}`, etc. We match greedily up to
// the matching `\end{xxx}` on the same line for the common single-line case,
// and across lines for multi-line environments.
const BARE_ENV_RE = /\\begin\{(\w+)\}([\s\S]*?)\\end\{\1\}/g;

// Matches a LaTeX command name followed by `{` — the start of a braced
// argument. The brace body is matched separately via balanced-brace scanning
// in `collectBareCmdMatches`. We use this as a fast pre-filter.
const BARE_CMD_START_RE = /\\([a-zA-Z]+)\{/g;

// High-confidence LaTeX commands that are almost always math when bare.
// Commands like \left, \right, \Big, \newpage, \label, \item, etc. are
// excluded to avoid false positives on regular prose.
const MATH_CMD_NAMES = new Set([
  // Greek letters
  "alpha","beta","gamma","delta","epsilon","zeta","eta","theta","iota",
  "kappa","lambda","mu","nu","xi","pi","rho","sigma","tau","upsilon",
  "phi","chi","psi","omega",
  "Gamma","Delta","Theta","Lambda","Xi","Pi","Sigma","Phi","Psi","Omega",
  // Fractions, roots, binomials
  "frac","dfrac","tfrac","sqrt","binom","dbinom","tbinom",
  // Display/text style
  "displaystyle","textstyle","scriptstyle","scriptscriptstyle",
  // Text/font
  "text","textrm","textbf","textit","textsf","texttt",
  "mathrm","mathbf","mathit","mathbb","mathcal","mathfrak","mathsf","mathtt",
  "operatorname",
  // Accents/decorations
  "hat","bar","vec","dot","ddot","tilde","breve","acute","grave","check",
  "widehat","widetilde","overline","underline","overbrace","underbrace",
  // Arrows
  "rightarrow","leftarrow","leftrightarrow","longrightarrow","longleftarrow",
  "xrightarrow","xleftarrow","mapsto","hookrightarrow","hookleftarrow",
  "nearrow","searrow","swarrow","nwarrow",
  "Rightarrow","Leftarrow","Leftrightarrow","Longrightarrow","Longleftarrow",
  "Leftrightarrow","leftrightsquigarrow",
  // Relations
  "leq","geq","le","ge","ll","gg","neq","ne","approx","simeq","cong",
  "equiv","propto","sim","nsim","ncong","napprox",
  "perp","parallel","asymp","prec","succ","preceq","succeq",
  "subset","supset","subseteq","supseteq","in","ni","notin",
  "vdash","dashv","models","doteq","approxeq","triangleq",
  // Operators
  "sum","prod","coprod","int","iint","iiint","oint",
  "bigcup","bigcap","bigoplus","bigotimes",
  "lim","inf","sup","max","min","dim","ker","deg","det","gcd","hom",
  "log","ln","exp","sin","cos","tan","sec","csc","cot",
  "arcsin","arccos","arctan","sinh","cosh","tanh","coth","Pr",
  "varlimsup","varliminf","limsup","liminf",
  "varprojlim","varinjlim","projlim","injlim",
  // Set/logic
  "cup","cap","vee","wedge","oplus","otimes","circ","bullet","star",
  "dagger","ddagger",
  // Matrix delimiters
  "bmatrix","pmatrix","vmatrix","Bmatrix","Vmatrix","cases","aligned",
  "gathered","array","matrix",
]);

/**
 * Detect bare LaTeX in `text` and wrap it in `$...$` / `$$...$$` delimiters
 * so KaTeX can render it.  Only high-confidence patterns are wrapped to avoid
 * false positives on regular prose that happens to contain backslashes.
 *
 * Strategy: collect ALL math regions (existing `$...$` + new display
 * environments) FIRST, then only wrap bare commands that are outside ALL
 * regions. This prevents double-wrapping (e.g. `\frac` inside a
 * `\begin{cases}` that was just wrapped in `$$...$$`).
 */
function wrapBareLatex(text: string): string {
  if (!text || !text.includes("\\")) {
    return text;
  }

  // Phase 1: collect all math regions on the ORIGINAL text.
  // Regions are [start, end) half-open intervals.
  const regions: Array<[number, number]> = [];

  // 1a. Existing inline/display math: $...$ and $$...$$
  const mathRe = /\$\$([\s\S]+?)\$\$|(?<!\\)\$(?!\$)((?:\\.|[^$\\\n])+?)(?<!\\)\$(?!\$)/g;
  let rm: RegExpExecArray | null;
  while ((rm = mathRe.exec(text)) !== null) {
    regions.push([rm.index, rm.index + rm[0].length]);
  }

  // 1b. Bare display-math environments: \begin{cases}...\end{cases}
  //     (only those NOT already inside a math region).
  const bareEnvs: Array<{ start: number; end: number; env: string; body: string }> = [];
  const envRe = new RegExp(BARE_ENV_RE.source, "g");
  let em: RegExpExecArray | null;
  while ((em = envRe.exec(text)) !== null) {
    const start = em.index;
    const end = start + em[0].length;
    if (!regions.some(([rs, re]) => start >= rs && end <= re)) {
      bareEnvs.push({ start, end, env: em[1], body: em[2] });
      regions.push([start, end]);
    }
  }

  // Phase 2: collect bare commands, excluding those inside any region.
  const matches: Array<{ start: number; end: number; full: string }> = [];

  // 2a. Commands with braced arguments: \frac{1}{2}, \xrightarrow{call}, etc.
  const cmdRe = new RegExp(BARE_CMD_START_RE.source, "g");
  let cm: RegExpExecArray | null;
  while ((cm = cmdRe.exec(text)) !== null) {
    const cmdName = cm[1];
    if (!MATH_CMD_NAMES.has(cmdName)) continue;
    const cmdStart = cm.index;
    if (regions.some(([rs, re]) => cmdStart >= rs && cmdStart < re)) continue;
    const braceStart = cm.index + cm[0].length - 1;
    if (braceStart >= text.length || text[braceStart] !== "{") continue;
    let depth = 1;
    let j = braceStart + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth++;
      else if (text[j] === "}") depth--;
      j++;
    }
    if (depth !== 0) continue;
    matches.push({ start: cmdStart, end: j, full: text.slice(cmdStart, j) });
  }

  // 2b. Standalone commands without braces: \alpha, \infty, \leq, etc.
  const standaloneRe = /\\([a-zA-Z]+)/g;
  let sm: RegExpExecArray | null;
  while ((sm = standaloneRe.exec(text)) !== null) {
    const cmdName = sm[1];
    if (!MATH_CMD_NAMES.has(cmdName)) continue;
    const cmdStart = sm.index;
    if (regions.some(([rs, re]) => cmdStart >= rs && cmdStart < re)) continue;
    if (text[cmdStart + sm[0].length] === "{") continue; // handled by 2a
    matches.push({ start: cmdStart, end: cmdStart + sm[0].length, full: sm[0] });
  }

  if (bareEnvs.length === 0 && matches.length === 0) {
    return text;
  }

  // Build the full list of edits: envs → $$...$$, commands → $...$
  const edits: Array<{ start: number; end: number; replacement: string }> = [];
  for (const e of bareEnvs) {
    edits.push({ start: e.start, end: e.end, replacement: `$$\\begin{${e.env}}${e.body}\\end{${e.env}}$$` });
  }
  for (const m of matches) {
    edits.push({ start: m.start, end: m.end, replacement: `$${m.full}$` });
  }

  // Sort by start desc, deduplicate (longest match wins for overlapping ranges).
  edits.sort((a, b) => b.start - a.start);
  const deduped: typeof edits = [];
  let lastEnd = Infinity;
  for (const e of edits) {
    if (e.end <= lastEnd) {
      deduped.push(e);
      lastEnd = e.start;
    }
  }

  // Apply in reverse order so offsets stay valid.
  let s = text;
  for (const e of deduped) {
    s = s.slice(0, e.start) + e.replacement + s.slice(e.end);
  }
  return s;
}

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
  subset: "\u2282",
  supset: "\u2283",
  cup: "\u222a",
  cap: "\u2229",
  forall: "\u2200",
  exists: "\u2203",
  neg: "\u00ac",
  land: "\u2227",
  lor: "\u2228",
 oplus: "\u2295",
  otimes: "\u2297",
  nabla: "\u2207",
  partial: "\u2202",
  alpha: "\u03b1",
  beta: "\u03b2",
  gamma: "\u03b3",
  delta: "\u03b4",
  epsilon: "\u03b5",
  theta: "\u03b8",
  lambda: "\u03bb",
  mu: "\u03bc",
  pi: "\u03c0",
  sigma: "\u03c3",
  phi: "\u03c6",
  psi: "\u03c8",
  omega: "\u03c9",
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
      nodes.push(<strong key={k}><em>{renderInline(tok.slice(3, -3), `${k}-bi`)}</em></strong>);
    } else if (tok.startsWith("**") || tok.startsWith("__")) {
      nodes.push(<strong key={k}>{renderInline(tok.slice(2, -2), `${k}-b`)}</strong>);
    } else if (tok.startsWith("<u>")) {
      nodes.push(<u key={k}>{renderInline(tok.slice(3, -4), `${k}-u`)}</u>);
    } else if (tok.startsWith("~~")) {
      nodes.push(<del key={k}>{renderInline(tok.slice(2, -2), `${k}-d`)}</del>);
    } else if (tok.startsWith("*") || tok.startsWith("_")) {
      nodes.push(<em key={k}>{renderInline(tok.slice(1, -1), `${k}-i`)}</em>);
    } else if (tok.startsWith("[")) {
      const mm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      const href = mm ? safeHref(mm[2]) : null;
      if (mm && href) {
        nodes.push(
          <a key={k} href={href} target="_blank" rel="noreferrer noopener">
            {renderInline(mm[1], `${k}-a`)}
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
  // Auto-detect bare LaTeX (no `$` delimiters) and wrap it so KaTeX can render
  // it. Must run before line splitting so multi-line environments like
  // `\begin{cases}...\end{cases}` are handled as a single unit.
  const rawLines = stripLeakedToolMarkup(wrapBareLatex(text)).replace(/\r\n/g, "\n").split("\n");

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
