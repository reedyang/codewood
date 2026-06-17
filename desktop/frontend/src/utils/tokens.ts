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

/** Compose the over-the-wire message string for a list of composer segments.
 *
 *  Attachment tokens are emitted as the legacy ATTACH header (one wrapped
 *  line per file path, followed by a blank line) so the existing backend
 *  consumer in ``serve_app`` continues to recognize and surface them. All
 *  other token kinds become readable inline markers (``[skill: foo]``,
 *  ``[mcp tool: srv/name]``, ``[mcp prompt: srv/name]``) so the LLM sees a
 *  sensible representation of what the user pinned. */
/** Wrap an arbitrary instruction in our private-use sentinels so the GUI
 *  knows to strip it from the user's chat bubble before display. The agent
 *  still sees the inner text as part of the message body. */
export function encodeHiddenInstruction(text: string): string {
  return `${OPEN}CONTROL:${sanitizePayload(text)}${CLOSE}`;
}

/** Strip any ``CONTROL:`` envelope from a message string. Used by the GUI
 *  to hide the "please execute the plan" nudge from the visible chat
 *  bubble while still leaving the user's own prose intact. */
export function stripHiddenControl(text: string): string {
  return String(text ?? "").replace(
    new RegExp(`${OPEN}CONTROL:[^${OPEN}${CLOSE}]*${CLOSE}\\s*`, "g"),
    "",
  );
}

export function composeMessageText(segments: readonly Segment[]): string {
  const attachPaths: string[] = [];
  const bodyParts: string[] = [];
  for (const seg of segments) {
    if (seg.kind === "attach") {
      const v = sanitizePayload(seg.value);
      if (v) attachPaths.push(v);
    } else if (seg.kind === "text") {
      bodyParts.push(sanitizePlainText(seg.value));
    } else if (seg.kind === "skill") {
      bodyParts.push(`[skill: ${sanitizePayload(seg.value)}]`);
    } else if (seg.kind === "mcp-tool") {
      const [srv, name] = seg.value.split("::");
      bodyParts.push(`[mcp tool: ${sanitizePayload(srv)}/${sanitizePayload(name ?? "")}]`);
    } else if (seg.kind === "mcp-prompt") {
      const [srv, name] = seg.value.split("::");
      bodyParts.push(`[mcp prompt: ${sanitizePayload(srv)}/${sanitizePayload(name ?? "")}]`);
    }
  }
  const body = bodyParts.join("");
  if (attachPaths.length === 0) {
    return body;
  }
  const head = attachPaths.map((p) => `${OPEN}ATTACH:${p}${CLOSE}`).join("\n");
  return body ? `${head}\n\n${body}` : head;
}

/** Inverse of ``composeMessageText`` for use in the edit flow: re-parse a
 *  message string back into composer segments. Attachments are picked off
 *  the header (matching the legacy parser in ``attachments.ts``) and the
 *  remaining body is exposed as a single text segment. Inline pills for
 *  skills / MCP tools / prompts are intentionally NOT re-tokenized — they
 *  were emitted as readable plain text, so re-editing surfaces them as
 *  text that the user can keep, rephrase, or delete naturally. */
export function parseMessageToSegments(text: string): Segment[] {
  const src = String(text ?? "");
  const out: Segment[] = [];
  const TOKEN_LINE = new RegExp(
    `^${OPEN}ATTACH:([^${OPEN}${CLOSE}\\r\\n]+)${CLOSE}[ \\t]*(?:\\r?\\n|$)`,
  );
  let rest = src;
  while (true) {
    const m = TOKEN_LINE.exec(rest);
    if (!m) break;
    out.push({ kind: "attach", value: m[1] });
    rest = rest.slice(m[0].length);
  }
  if (out.length > 0) {
    const sep = /^\r?\n/.exec(rest);
    if (sep) rest = rest.slice(sep[0].length);
  }
  if (rest) {
    out.push({ kind: "text", value: rest });
  }
  return out;
}
