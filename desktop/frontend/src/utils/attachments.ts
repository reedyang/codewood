// Structured attachment envelope for chat messages.
//
// To let the GUI re-render attachments as chips both in sent messages and in
// the edit flow, we encode each file path with a pair of private-use Unicode
// sentinels at the head of the message:
//
//   \uE100ATTACH:<path>\uE101\n
//   \uE100ATTACH:<path>\uE101\n
//   <user body>
//
// Private-use code points U+E100/U+E101 are reserved for private use and will
// not appear in normal text, paths, or LLM tokens. We parse only contiguous
// attachment lines from the start of the message; anything else falls through
// to the body untouched. The encoded form is also human/LLM-readable: each
// line reads as "ATTACH:<path>" surrounded by invisible markers.
//
// Encoding rules:
//   - Paths must not contain U+E100, U+E101, CR, or LF; such characters are
//     stripped on encode (file pickers don't produce them in practice).
//   - The body is appended after a blank line when both attachments and body
//     are present; when there is no body, the encoded form is just the tokens.
//   - When there are no attachments the original text is returned unchanged.

const OPEN = "\uE100";
const CLOSE = "\uE101";
const PREFIX = "ATTACH:";

function sanitizePath(p: string): string {
  return String(p).replace(/[\uE100\uE101\r\n]/g, "").trim();
}

export function encodeAttachments(paths: readonly string[], body: string): string {
  const clean = paths.map(sanitizePath).filter((p) => p.length > 0);
  if (clean.length === 0) {
    return body;
  }
  const head = clean.map((p) => `${OPEN}${PREFIX}${p}${CLOSE}`).join("\n");
  const trimmedBody = body ?? "";
  return trimmedBody ? `${head}\n\n${trimmedBody}` : head;
}

export interface ParsedAttachments {
  paths: string[];
  body: string;
}

// Token line regex: one OPEN + "ATTACH:" + path (no sentinels/newlines) + CLOSE,
// optionally followed by trailing whitespace until the next newline.
const TOKEN_LINE = new RegExp(
  `^${OPEN}${PREFIX}([^${OPEN}${CLOSE}\\r\\n]+)${CLOSE}[ \\t]*(?:\\r?\\n|$)`,
);

export function decodeAttachments(text: string): ParsedAttachments {
  const out: string[] = [];
  let rest = String(text ?? "");
  while (true) {
    const m = TOKEN_LINE.exec(rest);
    if (!m) {
      break;
    }
    out.push(m[1]);
    rest = rest.slice(m[0].length);
  }
  if (out.length === 0) {
    return { paths: [], body: rest };
  }
  // Skip exactly one blank separator line between the head block and the body
  // (the encoder adds one; tolerate either CR+LF or LF, and accept no
  // separator when the body is empty).
  const sep = /^\r?\n/.exec(rest);
  if (sep) {
    rest = rest.slice(sep[0].length);
  }
  return { paths: out, body: rest };
}

// Lightweight check used to drive UI behavior (e.g. whether to render chips)
// without allocating the full parsed result.
export function hasAttachmentEnvelope(text: string): boolean {
  return TOKEN_LINE.test(String(text ?? ""));
}
