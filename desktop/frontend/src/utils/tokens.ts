// Structured inline-token envelope shared by the composer, sent messages, and
// the edit flow. Each token is a single atomic unit wrapped with the
// private-use sentinel pair U+E100 / U+E101 so it can be safely embedded
// anywhere inside the user's prose and parsed back out without colliding with
// natural text. Token kinds we currently support:
//
//   ATTACH:<absolute path>
//   SKILL:<skill name>
//   MCP-TOOL:<server>::<tool>
//   MCP-PROMPT:<server>::<prompt>
//
// Multiple tokens may appear in any order, surrounded by free text. The
// envelope is deliberately minimal — we don't escape characters inside the
// payload because we strip the sentinel characters from any user-provided
// identifier before encoding, and the well-defined ``::`` separator inside
// MCP payloads is the only structural character we depend on.

const OPEN = "\uE100";
const CLOSE = "\uE101";

export type TokenKind = "attach" | "skill" | "mcp-tool" | "mcp-prompt";

export interface Token {
  kind: TokenKind;
  /** For ``attach`` the raw path; for ``skill`` the skill name; for MCP tokens
   *  ``server::name``. */
  payload: string;
}

const KIND_PREFIX: Record<TokenKind, string> = {
  attach: "ATTACH:",
  skill: "SKILL:",
  "mcp-tool": "MCP-TOOL:",
  "mcp-prompt": "MCP-PROMPT:",
};

const PREFIX_KIND: Array<[string, TokenKind]> = [
  ["ATTACH:", "attach"],
  ["SKILL:", "skill"],
  ["MCP-TOOL:", "mcp-tool"],
  ["MCP-PROMPT:", "mcp-prompt"],
];

function sanitizePayload(p: string): string {
  return String(p).replace(/[\uE100\uE101\r\n]/g, "").trim();
}

export function encodeToken(token: Token): string {
  const payload = sanitizePayload(token.payload);
  if (!payload) {
    return "";
  }
  return `${OPEN}${KIND_PREFIX[token.kind]}${payload}${CLOSE}`;
}

/** Strip any token characters/payloads that could collide with our sentinel
 *  bookkeeping when the user types raw prose into the composer. */
export function sanitizePlainText(text: string): string {
  return String(text).replace(/[\uE100\uE101]/g, "");
}

export interface Segment {
  kind: "text" | TokenKind;
  /** For ``text`` segments this is the literal text; for token segments this
   *  is the same payload that ``Token.payload`` carries. */
  value: string;
}

/** Tokenize an envelope-encoded string into ordered text / token segments. */
export function decodeSegments(text: string): Segment[] {
  const out: Segment[] = [];
  const src = String(text ?? "");
  let i = 0;
  let buf = "";
  while (i < src.length) {
    const ch = src[i];
    if (ch === OPEN) {
      const end = src.indexOf(CLOSE, i + 1);
      if (end < 0) {
        // Unterminated sentinel — treat as literal text.
        buf += ch;
        i += 1;
        continue;
      }
      const inner = src.slice(i + 1, end);
      const match = PREFIX_KIND.find(([p]) => inner.startsWith(p));
      if (match) {
        if (buf) {
          out.push({ kind: "text", value: buf });
          buf = "";
        }
        out.push({ kind: match[1], value: inner.slice(match[0].length) });
        i = end + 1;
        continue;
      }
      // Unknown token kind — also treat as literal.
      buf += src.slice(i, end + 1);
      i = end + 1;
      continue;
    }
    buf += ch;
    i += 1;
  }
  if (buf) {
    out.push({ kind: "text", value: buf });
  }
  return out;
}

/** Inverse of decodeSegments. */
export function encodeSegments(segments: readonly Segment[]): string {
  return segments
    .map((s) =>
      s.kind === "text"
        ? sanitizePlainText(s.value)
        : encodeToken({ kind: s.kind, payload: s.value }),
    )
    .join("");
}

/** Convenience: extract only attachment paths in document order. */
export function extractAttachments(text: string): string[] {
  return decodeSegments(text)
    .filter((s) => s.kind === "attach")
    .map((s) => s.value);
}

export const TOKEN_OPEN = OPEN;
export const TOKEN_CLOSE = CLOSE;
