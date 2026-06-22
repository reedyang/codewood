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

/** Known Plan-mode prefixes the backend USED TO prepend to recorded user
 *  messages when ``_plan_mode_sticky`` was on. The backend no longer writes the
 *  directive into history at all (it is appended only to the model-facing send),
 *  so new chats never carry it. This stripper is retained purely for backward
 *  compatibility with chat records written by older versions, where the prefix
 *  was prepended to the stored user message; it keeps those legacy bubbles from
 *  showing the planning directive as if the user had typed it.
 *
 *  The list mirrors ``builtin.plan_mode_prefix`` in every locale we ship
 *  under ``src/config/locales``; the lookup is exact-prefix only so a
 *  user who genuinely types one of these sentences first will (deliberately)
 *  see it stripped. The cost of that edge case is very low compared to the
 *  benefit of never surfacing the directive as a user message. */
const PLAN_MODE_PREFIXES: readonly string[] = [
  "Plan mode: please outline the proposed approach as a step-by-step plan first. Do not run write or execute tools yet; ask for confirmation before making changes.",
  "Plan 模式：请先以分步骤计划的形式给出建议方案。在我确认前不要调用写入或执行类工具。",
];

export function stripPlanModePrefix(text: string): string {
  let s = String(text ?? "");
  for (const prefix of PLAN_MODE_PREFIXES) {
    if (s.startsWith(prefix)) {
      // Drop the prefix and any whitespace separator the backend inserted
      // between the directive and the original user input.
      s = s.slice(prefix.length).replace(/^\s+/, "");
      break;
    }
  }
  return s;
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
  // Drop GUI-internal decorations the user never typed: the Plan-mode
  // directive the backend prepends while plan mode is sticky, and any
  // CONTROL envelope from the "Execute now" nudge. Without this the Edit
  // flow would re-populate the composer with that machine-authored text.
  rest = stripHiddenControl(stripPlanModePrefix(rest));
  // Re-tokenize the inline reference pills (``[skill: ...]``,
  // ``[mcp tool: srv/name]``, ``[mcp prompt: srv/name]``) back into
  // segments so editing a sent message surfaces them as image/text-mixed
  // pills the user can keep or remove — instead of raw bracket text.
  for (const seg of retokenizeReferencePills(rest)) {
    out.push(seg);
  }
  return out;
}

/** Split a plain body into text/skill/mcp segments by recognising the inline
 *  reference pill markers emitted by ``composeMessageText``. Exported so the
 *  sent-message bubble can render the same image/text-mixed pills the
 *  composer shows, instead of leaking raw ``[skill: ...]`` bracket text. */
export function retokenizeReferencePills(body: string): Segment[] {
  const out: Segment[] = [];
  const src = String(body ?? "");
  if (!src) {
    return out;
  }
  // Order matters: try the MCP forms (which contain a "/") before the
  // generic skill form. Each alternative is captured so we can classify.
  // We accept BOTH the GUI bracket forms (``[skill: name]``,
  // ``[mcp tool|prompt: srv/name]``) and the TUI slash forms
  // (``/skills/<name>``, ``/mcp/<srv>/<name>``) so a message authored in
  // either client renders as the same image/text-mixed pill on reload.
  const PILL = new RegExp(
    "\\[skill:\\s*([^\\]\\r\\n]+?)\\s*\\]" +
      "|\\[mcp\\s+tool:\\s*([^\\]\\r\\n/]+?)\\s*/\\s*([^\\]\\r\\n]+?)\\s*\\]" +
      "|\\[mcp\\s+prompt:\\s*([^\\]\\r\\n/]+?)\\s*/\\s*([^\\]\\r\\n]+?)\\s*\\]" +
      "|(?:^|(?<=\\s))/mcp/([^\\s/]+)/([^\\s]+)" +
      "|(?:^|(?<=\\s))/skills/([^\\s/]+)",
    "gi",
  );
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = PILL.exec(src)) !== null) {
    if (m.index > last) {
      out.push({ kind: "text", value: src.slice(last, m.index) });
    }
    if (m[1] != null) {
      out.push({ kind: "skill", value: m[1] });
    } else if (m[2] != null && m[3] != null) {
      out.push({ kind: "mcp-tool", value: `${m[2]}::${m[3]}` });
    } else if (m[4] != null && m[5] != null) {
      out.push({ kind: "mcp-prompt", value: `${m[4]}::${m[5]}` });
    } else if (m[6] != null && m[7] != null) {
      // Slash MCP form: tool vs prompt is indistinguishable from the token
      // alone; treat as a tool reference for display purposes.
      out.push({ kind: "mcp-tool", value: `${m[6]}::${m[7]}` });
    } else if (m[8] != null) {
      out.push({ kind: "skill", value: m[8] });
    }
    last = m.index + m[0].length;
  }
  if (last < src.length) {
    out.push({ kind: "text", value: src.slice(last) });
  }
  return out;
}
