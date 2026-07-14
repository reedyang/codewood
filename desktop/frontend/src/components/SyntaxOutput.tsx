import hljs from "highlight.js/lib/common";
import "highlight.js/styles/atom-one-dark.css";
import { langFromPath } from "./DiffPreview";

// Extensions we treat as "source code" worth highlighting. Dirs / binaries /
// data without a real grammar are skipped.
const CODE_EXT =
  /(py|py3|js|jsx|mjs|cjs|ts|tsx|java|c|cc|cpp|h|hpp|cs|go|rs|rb|php|swift|kt|kts|scala|sh|bash|zsh|ps1|psm1|pl|pm|lua|r|sql|html|htm|xml|xhtml|json|jsonc|yaml|yml|toml|ini|cfg|conf|css|scss|less|md|markdown|vue|svelte|dockerfile|gradle|make|cmake|tex|dart|ex|exs|erl|clj|cljs|fs|fsx|fsi|hs|nim|zig|v|groovy|properties|gitignore|editorconfig|lock|proto|graphql|gql|rst|adoc|asm|s|sol|tf|tfvars|env)/i;

// A token that looks like a path to one of the code files above, anywhere in a
// tool-call prompt line (language-independent — works for localized "Read"/"Ran"
// labels too). Keeps CODE_EXT's grouping parentheses intact.
const FILE_TOKEN_RE = new RegExp(`[\\w./\\\\-]+\\.${CODE_EXT.source}`, "i");

const ANSI_RE = /\x1b\[[0-9;]*m/g;

function stripAnsi(s: string): string {
  return s.replace(ANSI_RE, "");
}

/** A payload the read tool produced: every non-empty line begins with a
 *  ``NN:`` prefix. Detected structurally so it works in any UI language. */
function looksLikeReadOutput(payload: string): boolean {
  const lines = payload.split("\n").filter((l) => l.trim().length > 0);
  if (lines.length === 0) {
    return false;
  }
  let numbered = 0;
  for (const l of lines) {
    if (/^\s*\d+:\s?/.test(l)) {
      numbered += 1;
    }
  }
  return numbered / lines.length >= 0.5;
}

/** Resolve a highlight.js language for a tool-call's file-content output.
 *
 *  - The file is found by scanning the prompt ``body`` for a code-file path
 *    (extension-based, UI-language independent).
 *  - ``lineNumbers`` is true when the ``payload`` carries the read tool's
 *    ``NN:`` line-number prefixes (so they render in a gutter instead of as
 *    highlighted text).
 *  Returns ``{ lang: "", lineNumbers: false }`` when nothing should highlight. */
export function resolveToolOutputLang(
  body: string,
  payload: string,
): { lang: string; lineNumbers: boolean } {
  const plain = stripAnsi(body);
  const fileMatch = plain.match(FILE_TOKEN_RE);
  const lang = fileMatch ? langFromPath(fileMatch[0]) : "";
  if (!lang) {
    return { lang: "", lineNumbers: false };
  }
  return { lang, lineNumbers: looksLikeReadOutput(payload) };
}

function highlight(code: string, lang: string): string | null {
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang, ignoreIllegals: true })
        .value;
    }
  } catch {
    return null;
  }
  return null;
}

/** Split highlighted HTML into per-line fragments, re-balancing any
 *  ``<span>`` tags that cross a newline boundary so each returned fragment is
 *  self-contained and safe to render independently (e.g. beside a gutter). */
function splitHighlightedByLines(html: string): string[] {
  const raw = html.split("\n");
  const out: string[] = [];
  let open = 0;
  for (const line of raw) {
    const opens = (line.match(/<span\b/g) || []).length;
    const closes = (line.match(/<\/span>/g) || []).length;
    const prefix = open > 0 ? "</span>".repeat(open) : "";
    const remaining = Math.max(0, open + opens - closes);
    const suffix = remaining > 0 ? "</span>".repeat(remaining) : "";
    out.push(prefix + line + suffix);
    open = remaining;
  }
  return out;
}

interface SynLine {
  no: string;
  code: string;
}

function stripReadLines(text: string): SynLine[] {
  return text.split("\n").map((line) => {
    const m = /^(\d+):[ \t]?(.*)$/.exec(line);
    if (m) {
      return { no: m[1], code: m[2] };
    }
    return { no: "", code: line };
  });
}

/** Syntax-highlighted rendering for a tool's file-content output.
 *
 *  - ``lang`` resolves from the tool-call prompt (see resolveToolOutputLang).
 *  - ``lineNumbers`` strips the read tool's ``NN:`` prefixes and renders them
 *    in a gray-backed gutter; the code is highlighted beside it.
 *  - returns ``null`` when nothing should be highlighted so the caller can
 *    fall back to plain ANSI rendering (e.g. colored shell output). */
export function SyntaxOutput({
  text,
  lang,
  lineNumbers,
}: {
  text: string;
  lang: string;
  lineNumbers?: boolean;
}): React.ReactNode | null {
  if (!lang || ANSI_RE.test(text)) {
    return null;
  }
  if (lineNumbers) {
    const parsed = stripReadLines(text);
    const codeText = parsed.map((p) => p.code).join("\n");
    const html = highlight(codeText, lang);
    if (html === null) {
      return null;
    }
    const htmlLines = splitHighlightedByLines(html);
    return (
      <div className="syn-output syn-output-linenums">
        {parsed.map((p, i) => (
          <div className="syn-line" key={i}>
            <span className="syn-gutter">{p.no}</span>
            <span
              className="syn-code"
              dangerouslySetInnerHTML={{ __html: htmlLines[i] ?? "" }}
            />
          </div>
        ))}
      </div>
    );
  }
  const html = highlight(text, lang);
  if (html === null) {
    return null;
  }
  return (
    <div className="syn-output">
      <pre className="hljs">
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}
