// Inline image-reference protocol for pasted clipboard bitmaps.
//
// A pasted image is carried inside a chat message as a single inline token
// wrapping its absolute on-disk path between a pair of private-use Unicode
// sentinels:
//
//   \uE008<abs-path>\uE009
//
// To the model the sentinel content is just an absolute file path, so the
// reference reads like a normal "look at this file" mention it can resolve with
// `read_image`; no special backend parsing is required (the sentinels live in
// the private-use plane U+E008/U+E009 and never appear in real paths or LLM
// tokens). The GUI parses these tokens out of the message text and renders a
// thumbnail in their place, leaving the surrounding prose untouched.
//
// These code points are distinct from the head-block file-attachment envelope
// (U+E100/U+E101 in ./attachments.ts) and the command/diff sentinels
// (U+E000..U+E007), so the two systems never collide.

export const IMG_OPEN = "\uE008";
export const IMG_CLOSE = "\uE009";

function sanitizePath(p: string): string {
  return String(p).replace(/[\uE008\uE009\r\n]/g, "").trim();
}

/** Wrap a single absolute image path as an inline reference token. */
export function encodeImageRef(path: string): string {
  const clean = sanitizePath(path);
  return clean ? `${IMG_OPEN}${clean}${IMG_CLOSE}` : "";
}

/** Append image references to ``body`` (each on its own trailing line). */
export function appendImageRefs(
  body: string,
  paths: readonly string[],
): string {
  const tokens = paths
    .map((p) => encodeImageRef(p))
    .filter((t) => t.length > 0);
  if (tokens.length === 0) {
    return body;
  }
  const head = tokens.join("\n");
  const trimmed = body ?? "";
  return trimmed ? `${trimmed}\n${head}` : head;
}

export type ImageRefSegment =
  | { kind: "text"; text: string }
  | { kind: "image"; path: string };

// One image token: OPEN + path (no sentinels/newlines) + CLOSE.
const IMG_TOKEN = new RegExp(
  `${IMG_OPEN}([^${IMG_OPEN}${IMG_CLOSE}\\r\\n]+)${IMG_CLOSE}`,
  "g",
);

/** Split ``text`` into ordered text / image segments. */
export function parseImageRefs(text: string): ImageRefSegment[] {
  const src = String(text ?? "");
  const out: ImageRefSegment[] = [];
  let last = 0;
  IMG_TOKEN.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = IMG_TOKEN.exec(src)) !== null) {
    if (m.index > last) {
      out.push({ kind: "text", text: src.slice(last, m.index) });
    }
    out.push({ kind: "image", path: m[1] });
    last = m.index + m[0].length;
  }
  if (last < src.length) {
    out.push({ kind: "text", text: src.slice(last) });
  }
  return out;
}

/** True when ``text`` contains at least one inline image reference. */
export function hasImageRef(text: string): boolean {
  const re = new RegExp(
    `${IMG_OPEN}[^${IMG_OPEN}${IMG_CLOSE}\\r\\n]+${IMG_CLOSE}`,
  );
  return re.test(String(text ?? ""));
}

/** Strip image-reference tokens, leaving only surrounding prose (trimmed). */
export function stripImageRefs(text: string): string {
  return String(text ?? "")
    .replace(IMG_TOKEN, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
