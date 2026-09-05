import {
  KeyboardEvent as ReactKeyboardEvent,
  type ClipboardEvent as ReactClipboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import { useApp } from "../state/AppContext";
import type { CompletionCatalog } from "../api/types";
import type { Segment, TokenKind } from "../utils/tokens";
import {
  composeMessageText,
  decodeSegments,
  encodeSegments,
} from "../utils/tokens";
import { Icon } from "./Icon";
import { useCopyContextMenu } from "./CopyContextMenu";
import { hostApi } from "../utils/hostApi";

/** Rich-mixed composer.
 *
 *  Renders a contentEditable surface where atomic pills (file attachments,
 *  skills, MCP tools, MCP prompts) flow inline with the user's prose. The
 *  parent owns the canonical model as ``Segment[]``; the DOM is a derived
 *  view we rebuild whenever the model changes, and an ``input`` listener
 *  reconstructs the model whenever the user types.
 *
 *  Typing ``/`` at the start of input or after whitespace opens a popup
 *  filtered against the loaded catalog of skills + MCP tools + MCP prompts.
 *  Selecting an item replaces the in-progress ``/<query>`` text with an
 *  atomic pill segment at the cursor position. */

export interface RichComposerProps {
  segments: Segment[];
  onChange: (segments: Segment[]) => void;
  onSubmit: () => void;
  /** Called on Ctrl/Cmd+Enter: send immediately, bypassing the pending queue
   * (the parent decides what "Steer" means for the current chat state). */
  onSubmitSteer?: () => void;
  placeholder?: string;
  rows?: number;
  /** Called with pasted clipboard bitmaps (as data URLs). When provided and
   *  the paste carries image items, those items are consumed here instead of
   *  being inserted as text. */
  onPasteImages?: (dataUrls: string[]) => void;
  /** Called when the user selects the "/compact" slash suggestion. */
  onCompact?: () => void;
  /** Called with drag-dropped files from the OS file manager. */
  onDropFiles?: (files: FileList) => void;
}

interface SlashItem {
  kind: TokenKind | "compact";
  /** What we insert as the pill payload. */
  payload: string;
  /** Visible label in both the popup and the inserted pill. */
  label: string;
  /** Long-form description shown in the popup row's subtitle. */
  description: string;
  /** Search key (lowercased) for the filter. */
  search: string;
}

const ZWSP = "\u200B";

/** Block-level tags the browser may use to wrap a paragraph break inside a
 *  ``contentEditable`` surface. A user-entered line break can land either as a
 *  ``<br>`` or as its own block element (``<div>``, ``<p>``, ``<li>`` …)
 *  depending on the platform/IME. ``readSegmentsFromDom`` treats every line
 *  break uniformly as ``\\n`` so the model stays identical across those
 *  renderings, which is why block elements are handled alongside ``<br>``. */
const BLOCK_TAGS = new Set([
  "DIV", "P", "LI", "UL", "OL", "TD", "TH", "PRE", "H1", "H2", "H3", "H4",
  "H5", "H6", "BLOCKQUOTE", "SECTION", "ARTICLE", "ASIDE", "HEADER", "FOOTER",
  "FIGURE", "FIGCAPTION", "DD", "DT", "CAPTION",
]);

/** True when ``tagName`` denotes a block-level element whose edges should be
 *  treated as line breaks when flattening the editor to the segment model. */
function isBlockLevel(tagName: string): boolean {
  return BLOCK_TAGS.has(tagName.toUpperCase());
}

/** The canonical text a single DOM child contributes to the flattened segment
 *  model, mirroring ``readSegmentsFromDom`` exactly. Text nodes and inline
 *  spans contribute their (ZWSP-stripped) text; a line break — whether a
 *  ``<br>`` or a block-level element — contributes a leading ``\n`` only when
 *  it is *followed by* content (``precededByContent``), i.e. the ``\n`` is the
 *  separator BETWEEN two lines rather than a stray leading break at the very
 *  start of the editor. Pills contribute nothing (they are emitted as their own
 *  segment by the caller). Sharing this one rule across the whole-DOM reader,
 *  the selection-range reader and the caret→model mapper keeps their offsets in
 *  lockstep so editing operations (delete / cut / copy) never drift by a stray
 *  newline. */
function flattenNodeText(child: Node, precededByContent: boolean): string {
  if (child.nodeType === Node.TEXT_NODE) {
    return (child.textContent ?? "").replace(/\u200B/g, "");
  }
  if (child.nodeType !== Node.ELEMENT_NODE) {
    return "";
  }
  const el = child as HTMLElement;
  const kind = el.getAttribute("data-token-kind");
  if (
    kind &&
    (kind === "attach" || kind === "skill" || kind === "mcp-tool" || kind === "mcp-prompt")
  ) {
    return "";
  }
  if (el.tagName === "BR" || isBlockLevel(el.tagName)) {
    const text = (el.textContent ?? "").replace(/\u200B/g, "");
    if (!precededByContent) {
      return text.trim() !== "" ? text : "";
    }
    return `\n${text.trim() !== "" ? text : ""}`;
  }
  return (el.textContent ?? "").replace(/\u200B/g, "");
}

/** Build the flat suggestion pool from the backend catalog. Skills come
 *  first (most likely intent for a power user), then MCP tools, then
 *  prompts; within each group the entries keep their backend order so the
 *  user's mental model from ``/mcp status`` carries over. */
function buildSlashPool(catalog: CompletionCatalog): SlashItem[] {
  const pool: SlashItem[] = [];
  for (const s of catalog.skills) {
    pool.push({
      kind: "skill",
      payload: s.name,
      label: s.name,
      description: s.description,
      search: `${s.name}\n${s.description}`.toLowerCase(),
    });
  }
  for (const t of catalog.mcpTools) {
    pool.push({
      kind: "mcp-tool",
      payload: `${t.server}::${t.name}`,
      label: `${t.server}/${t.name}`,
      description: t.description,
      search: `${t.server} ${t.name}\n${t.description}`.toLowerCase(),
    });
  }
  for (const p of catalog.mcpPrompts) {
    pool.push({
      kind: "mcp-prompt",
      payload: `${p.server}::${p.name}`,
      label: `${p.server}/${p.name}`,
      description: p.description,
      search: `${p.server} ${p.name}\n${p.description}`.toLowerCase(),
    });
  }
  return pool;
}

/** Build the full suggestion pool including the built-in "/compact" item.
 *  The compact item is always prepended so it appears first in the list. */
function buildFullSlashPool(catalog: CompletionCatalog): SlashItem[] {
  const pool = buildSlashPool(catalog);
  pool.unshift({
    kind: "compact",
    payload: "",
    label: "Compact",
    description: "Compact conversation context",
    search: "compact",
  });
  return pool;
}

function filterSlashItems(pool: SlashItem[], query: string): SlashItem[] {
  const q = query.trim().toLowerCase();
  if (!q) {
    return pool.slice(0, 30);
  }
  // Score by: prefix match on label > substring on label > substring on
  // description. We don't bother with full fuzzy matching — a flat list of
  // skill / tool names rarely benefits from it.
  const scored = pool
    .map((item) => {
      const label = item.label.toLowerCase();
      if (label.startsWith(q)) return { item, score: 0 };
      if (label.includes(q)) return { item, score: 1 };
      if (item.search.includes(q)) return { item, score: 2 };
      return null;
    })
    .filter((x): x is { item: SlashItem; score: number } => x != null);
  scored.sort((a, b) => a.score - b.score);
  return scored.slice(0, 30).map((x) => x.item);
}

/** Reconstruct ``Segment[]`` from the live DOM. Walks children of the
 *  contentEditable root in order: text-only children produce text segments;
 *  pill spans produce token segments using their ``data-kind`` /
 *  ``data-payload`` attributes. */
function readSegmentsFromDom(root: HTMLElement): Segment[] {
  const out: Segment[] = [];
  let buf = "";
  // ``sawContent`` tracks whether any content (text or pill) has been emitted
  // before the current node. A block / <br> is the separator between two lines,
  // so it only prefixes a "\n" when it is preceded by content — otherwise the
  // very first line would gain a spurious leading newline. This must mirror the
  // identical state machine in ``readSegmentsFromRange`` and
  // ``canonicalFromSnapshot`` so editing offsets never drift.
  let sawContent = false;
  const flushText = () => {
    if (buf) {
      out.push({ kind: "text", value: buf });
      buf = "";
    }
  };
  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType !== Node.ELEMENT_NODE) {
      const t = flattenNodeText(node, sawContent);
      if (t) sawContent = true;
      buf += t;
      continue;
    }
    const el = node as HTMLElement;
    const kind = el.getAttribute("data-token-kind");
    if (kind && (kind === "attach" || kind === "skill" || kind === "mcp-tool" || kind === "mcp-prompt")) {
      flushText();
      const payload = el.getAttribute("data-token-payload") || "";
      out.push({ kind: kind as TokenKind, value: payload });
      sawContent = true;
      continue;
    }
    const t = flattenNodeText(node, sawContent);
    if (t) sawContent = true;
    buf += t;
  }
  flushText();
  return out;
}

/** Read the segments covered by the current selection ``range`` within the
 *  editor ``root``. Pills are included whole when the range intersects them;
 *  text is clipped to the selected portion. Used by the copy/cut handlers so
 *  copying a mixed selection preserves the pinned references (files / skills /
 *  MCP items) rather than dropping them or leaking placeholder text. */
function readSegmentsFromRange(root: HTMLElement, range: Range): Segment[] {
  const out: Segment[] = [];
  let buf = "";
  // Same separator state machine as ``readSegmentsFromDom`` (see its
  // ``sawContent`` comment) so a block / <br> only contributes a leading "\n"
  // when it is preceded by content within the selection.
  let sawContent = false;
  const flushText = () => {
    if (buf) {
      out.push({ kind: "text", value: buf });
      buf = "";
    }
  };
  for (const node of Array.from(root.childNodes)) {
    if (!range.intersectsNode(node)) {
      continue;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const full = (node.textContent ?? "").replace(/\u200B/g, "");
      // Clip the text node to the selected sub-range when the selection
      // starts or ends inside it; otherwise take the whole node.
      let startOff = 0;
      let endOff = full.length;
      const raw = node.textContent ?? "";
      if (node === range.startContainer) {
        startOff = Math.min(range.startOffset, raw.length);
      }
      if (node === range.endContainer) {
        endOff = Math.min(range.endOffset, raw.length);
      }
      // Map raw offsets onto the ZWSP-stripped string conservatively: since
      // ZWSP only appears as standalone anchors next to pills, the common
      // case is a plain text node where raw === full.
      const slice =
        node === range.startContainer || node === range.endContainer
          ? raw.slice(startOff, endOff).replace(/\u200B/g, "")
          : full;
      buf += slice;
      if (slice) sawContent = true;
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      continue;
    }
    const el = node as HTMLElement;
    const kind = el.getAttribute("data-token-kind");
    if (
      kind &&
      (kind === "attach" || kind === "skill" || kind === "mcp-tool" || kind === "mcp-prompt")
    ) {
      flushText();
      out.push({ kind: kind as TokenKind, value: el.getAttribute("data-token-payload") || "" });
      sawContent = true;
      continue;
    }
    const t = flattenNodeText(node, sawContent);
    if (t) sawContent = true;
    buf += t;
  }
  flushText();
  return out;
}

/** Persist the current selection as a (segmentIndex, textOffset) tuple so
 *  we can restore the caret after a re-render rebuilds the DOM. Only text
 *  segments support an internal offset; for a caret that sits before/after
 *  a pill we encode it as the offset 0 of an adjacent text segment. */
interface CaretSnapshot {
  segIndex: number;
  textOffset: number;
}

/** Map a DOM point (container + offset) to a ``CaretSnapshot`` (child index +
 *  in-node offset). Shared by the live-caret capture and selection-range
 *  endpoint mapping. */
function snapshotFromPoint(
  root: HTMLElement,
  container: Node,
  offset: number,
): CaretSnapshot | null {
  if (!root.contains(container)) return null;
  const children = Array.from(root.childNodes);
  // Point placed directly on the root element (a child boundary), e.g. right
  // before the first pill when there's no leading text node. ``offset`` is the
  // index of the child the point sits before.
  if (container === root) {
    return { segIndex: Math.min(offset, children.length), textOffset: 0 };
  }
  for (let i = 0; i < children.length; i += 1) {
    const child = children[i];
    if (child === container || child.contains(container)) {
      if (
        child.nodeType === Node.ELEMENT_NODE &&
        (child as HTMLElement).hasAttribute("data-token-kind")
      ) {
        return { segIndex: i, textOffset: 0 };
      }
      const text = child.textContent ?? "";
      return { segIndex: i, textOffset: Math.min(offset, text.length) };
    }
  }
  return null;
}

function captureCaret(root: HTMLElement): CaretSnapshot | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  return snapshotFromPoint(root, range.startContainer, range.startOffset);
}

function restoreCaret(root: HTMLElement, snap: CaretSnapshot | null) {
  if (!snap) return;
  const children = Array.from(root.childNodes);
  const target = children[Math.min(snap.segIndex, children.length - 1)];
  if (!target) {
    // Empty editor — place caret at root start.
    const sel = window.getSelection();
    if (sel) {
      const r = document.createRange();
      r.setStart(root, 0);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
    }
    return;
  }
  const sel = window.getSelection();
  if (!sel) return;
  const r = document.createRange();
  if (
    target.nodeType === Node.ELEMENT_NODE &&
    (target as HTMLElement).hasAttribute("data-token-kind")
  ) {
    // A snapshot whose child index lands on a pill means "the caret position
    // just BEFORE this pill" (canonical ``{segIdx:i, offset:0}``). Positions
    // AFTER a pill are encoded by targeting the following node instead.
    r.setStartBefore(target);
  } else {
    const text = target.textContent ?? "";
    const offset = Math.min(snap.textOffset, text.length);
    if (target.nodeType === Node.TEXT_NODE) {
      r.setStart(target, offset);
    } else {
      // For span-wrapped text find the first text node inside.
      const firstText = (target as HTMLElement).firstChild;
      if (firstText && firstText.nodeType === Node.TEXT_NODE) {
        r.setStart(firstText, Math.min(offset, (firstText.textContent ?? "").length));
      } else {
        r.setStart(target, 0);
      }
    }
  }
  r.collapse(true);
  sel.removeAllRanges();
  sel.addRange(r);
}

/** A caret position expressed in the canonical ``Segment[]`` model: the index
 *  of the segment and, for a text segment, the character offset inside it.
 *  ``offset`` is 0 for a position at the very start of a segment. */
interface CanonicalCaret {
  segIdx: number;
  offset: number;
}

/** Translate the live DOM caret into canonical-segment coordinates. The DOM
 *  has separate child nodes for pills and the ZWSP anchors next to them, and
 *  may split text across multiple nodes, whereas the canonical model collapses
 *  contiguous text into one segment and strips ZWSP. We walk the children up to
 *  the caret to count how much canonical content precedes it. */
/** Convert an arbitrary selection endpoint (the ``CaretSnapshot`` shape, a
 *  child index + offset) into canonical coordinates against the live DOM. */
function canonicalFromSnapshot(
  root: HTMLElement,
  snap: CaretSnapshot,
): CanonicalCaret {
  const kids = Array.from(root.childNodes);
  let segIdx = 0;
  let pendingTextLen = 0;
  // Mirrors the ``sawContent`` separator state machine in ``readSegmentsFromDom``
  // (see its comment) so a block / <br> contributes a leading "\n" only when it
  // is preceded by content. Keeping the two in lockstep is what stops cut /
  // delete between multi-line blocks from drifting by a stray newline.
  let sawContent = false;
  for (let i = 0; i < kids.length && i < snap.segIndex; i += 1) {
    const child = kids[i];
    const isPill =
      child.nodeType === Node.ELEMENT_NODE &&
      (child as HTMLElement).hasAttribute("data-token-kind");
    if (isPill) {
      if (pendingTextLen > 0) {
        segIdx += 1;
        pendingTextLen = 0;
      }
      segIdx += 1;
      sawContent = true;
    } else {
      const t = flattenNodeText(child, sawContent);
      pendingTextLen += t.length;
      if (t) sawContent = true;
    }
  }
  const at = kids[snap.segIndex];
  const atIsPill =
    at != null &&
    at.nodeType === Node.ELEMENT_NODE &&
    (at as HTMLElement).hasAttribute("data-token-kind");
  if (atIsPill) {
    if (pendingTextLen > 0) {
      segIdx += 1;
    }
    return { segIdx, offset: 0 };
  }
  // A block-level or <br> child that is preceded by content contributes a
  // synthetic leading "\n" that is not part of its own textContent, so the
  // caret offset measured inside ``snap.textOffset`` lands one character later
  // in the flattened model. A leading block (no content before it) has no such
  // separator.
  const atLeading =
    at != null &&
    at.nodeType === Node.ELEMENT_NODE &&
    ((at as HTMLElement).tagName === "BR" || isBlockLevel((at as HTMLElement).tagName)) &&
    sawContent
      ? 1
      : 0;
  return { segIdx, offset: pendingTextLen + atLeading + snap.textOffset };
}

function canonicalCaretFromDom(root: HTMLElement): CanonicalCaret | null {
  const snap = captureCaret(root);
  if (!snap) return null;
  return canonicalFromSnapshot(root, snap);
}

/** Convert a canonical caret position into a ``CaretSnapshot`` that
 *  ``restoreCaret`` can consume against the DOM that ``segments`` will build.
 *  In that DOM each text segment is one child node and each pill is two (pill +
 *  ZWSP). A position at ``segIdx`` with ``offset`` points to the start of that
 *  segment's node plus the offset; positions past the end clamp to the end of
 *  the last segment. */
function snapshotForCanonical(
  segments: Segment[],
  caret: CanonicalCaret,
): CaretSnapshot {
  // The rebuild prepends a zero-width anchor node when the first segment is a
  // pill (so the caret can sit before it); mirror that +1 child offset here.
  const pad = segments.length > 0 && segments[0].kind !== "text" ? 1 : 0;
  const clampedSeg = Math.max(0, Math.min(caret.segIdx, segments.length));
  let childIdx = pad;
  for (let i = 0; i < clampedSeg; i += 1) {
    childIdx += segments[i].kind === "text" ? 1 : 2;
  }
  if (clampedSeg < segments.length) {
    const seg = segments[clampedSeg];
    if (seg.kind === "text") {
      return { segIndex: childIdx, textOffset: caret.offset };
    }
    return { segIndex: childIdx, textOffset: 0 };
  }
  // Past the last segment: sit at the end of the final segment.
  const lastIdx = segments.length - 1;
  if (lastIdx < 0) {
    return { segIndex: 0, textOffset: 0 };
  }
  let lastChildIdx = pad;
  for (let i = 0; i < lastIdx; i += 1) {
    lastChildIdx += segments[i].kind === "text" ? 1 : 2;
  }
  const last = segments[lastIdx];
  if (last.kind === "text") {
    return { segIndex: lastChildIdx, textOffset: last.value.length };
  }
  // Trailing pill: caret goes AFTER it. The pill occupies ``lastChildIdx`` and
  // is followed by its ZWSP anchor (and the rebuild's guaranteed trailing text
  // node), so target the node after the pill.
  return { segIndex: lastChildIdx + 1, textOffset: 0 };
}

/** Flatten a segment model into a linear list of "atoms": one entry per text
 *  character and one per pill. This lets us diff two models at character
 *  granularity to locate exactly where an edit happened. Each atom records the
 *  canonical (segIdx, offset) of the position JUST BEFORE it. */
interface Atom {
  key: string; // identity for equality ("c:<char>" or "p:<kind>:<value>")
}

function flattenAtoms(segments: Segment[]): Atom[] {
  const atoms: Atom[] = [];
  for (const seg of segments) {
    if (seg.kind === "text") {
      for (const ch of seg.value) {
        atoms.push({ key: `c:${ch}` });
      }
    } else {
      atoms.push({ key: `p:${seg.kind}:${seg.value}` });
    }
  }
  return atoms;
}

/** Map a linear atom index (0..total) back to a canonical caret position. */
function atomIndexToCanonical(
  segments: Segment[],
  atomIndex: number,
): CanonicalCaret {
  let remaining = atomIndex;
  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i];
    const len = seg.kind === "text" ? Array.from(seg.value).length : 1;
    if (remaining < len || (remaining === len && i === segments.length - 1)) {
      if (seg.kind === "text") {
        // Convert the char-offset (code points) into a UTF-16 offset.
        const chars = Array.from(seg.value);
        const slice = chars.slice(0, remaining).join("");
        return { segIdx: i, offset: slice.length };
      }
      // Pill: remaining is 0 (before) or 1 (after).
      return remaining === 0
        ? { segIdx: i, offset: 0 }
        : { segIdx: i + 1, offset: 0 };
    }
    remaining -= len;
  }
  return { segIdx: segments.length, offset: 0 };
}

/** Determine where the caret should land after replacing model ``from`` with
 *  model ``to`` (e.g. an undo or redo). We diff the two at character/pill
 *  granularity: the region between the common prefix and common suffix is the
 *  change. Per the insert/delete rule the caret goes to the END of that region
 *  in ``to`` — which is "after the inserted block" for a net insertion and "at
 *  the gap" (the gap collapses to a point) for a net deletion. */
function caretAfterModelSwap(from: Segment[], to: Segment[]): CanonicalCaret {
  const a = flattenAtoms(from);
  const b = flattenAtoms(to);
  let pre = 0;
  while (pre < a.length && pre < b.length && a[pre].key === b[pre].key) {
    pre += 1;
  }
  let suf = 0;
  while (
    suf < a.length - pre &&
    suf < b.length - pre &&
    a[a.length - 1 - suf].key === b[b.length - 1 - suf].key
  ) {
    suf += 1;
  }
  // End of the changed region within ``to``.
  const endAtom = b.length - suf;
  return atomIndexToCanonical(to, Math.max(0, endAtom));
}

/** True iff the text node's previous DOM sibling is a pill element. We use
 *  this to detect the "caret sits right after a pill" boundary that should
 *  allow ``/`` to reopen the slash popup. */
function isPreviousSiblingPill(node: Node): boolean {
  const prev = node.previousSibling;
  if (!prev) return false;
  if (prev.nodeType !== Node.ELEMENT_NODE) return false;
  return (prev as HTMLElement).hasAttribute("data-token-kind");
}

function kindIconName(kind: TokenKind | "compact"): string {
  switch (kind) {
    case "attach":
      return "paperclip";
    case "skill":
      return "sparkles";
    case "mcp-tool":
      return "wrench";
    case "mcp-prompt":
      return "message-square";
    case "compact":
      return "archive";
  }
}

function kindLabel(kind: TokenKind, payload: string): { primary: string; secondary?: string } {
  if (kind === "attach") {
    const parts = payload.split(/[\\/]/);
    return { primary: parts[parts.length - 1] || payload };
  }
  if (kind === "skill") {
    return { primary: payload };
  }
  // mcp-tool / mcp-prompt: "server::name" → "server / name"
  const [server, name] = payload.split("::");
  return name ? { primary: name, secondary: server } : { primary: payload };
}

/** Toggle an ``is-selected`` class on every pill (in the composer *and* in
 *  sent-message bodies) that the current selection range intersects.
 *
 *  The native ``::selection`` pseudo only tints inline text runs, not the
 *  rounded box of an inline-flex pill, so a mixed selection looked broken
 *  (text blue, pills not). Marking the whole pill lets CSS paint a solid
 *  accent box that visually joins the blue text on either side. Scoped to the
 *  document so it works uniformly across the composer and the message echo
 *  area; installed once (guarded by a ref) even if multiple composers mount. */
let _pillHighlightRefCount = 0;
let _pillHighlightHandler: (() => void) | null = null;

function usePillSelectionHighlight(): void {
  useEffect(() => {
    if (_pillHighlightRefCount === 0) {
      _pillHighlightHandler = () => {
        const sel = window.getSelection();
        const hasRange = sel != null && sel.rangeCount > 0 && !sel.isCollapsed;
        const range = hasRange ? (sel as Selection).getRangeAt(0) : null;
        const pills = document.querySelectorAll<HTMLElement>(
          ".composer-pill, .msg-ref-pill",
        );
        for (const pill of Array.from(pills)) {
          let selected = false;
          if (range != null) {
            try {
              selected = range.intersectsNode(pill);
            } catch {
              selected = false;
            }
          }
          pill.classList.toggle("is-selected", selected);
        }
      };
      document.addEventListener("selectionchange", _pillHighlightHandler);
    }
    _pillHighlightRefCount += 1;
    return () => {
      _pillHighlightRefCount -= 1;
      if (_pillHighlightRefCount === 0 && _pillHighlightHandler) {
        document.removeEventListener("selectionchange", _pillHighlightHandler);
        _pillHighlightHandler = null;
      }
    };
  }, []);
}

// Sentinel stored in ``lastRenderedRef`` to force the next rebuild. It must be
// a value that a real segment signature can never equal — in particular it must
// differ from "" (the signature of an EMPTY editor), otherwise deleting all
// content would collide with the sentinel and skip the DOM rebuild, leaving the
// stale content on screen while the model is already empty.
const FORCE_REBUILD = "\x00force-rebuild\x00";

export function RichComposer({
  segments,
  onChange,
  onSubmit,
  onSubmitSteer,
  placeholder,
  rows = 3,
  onPasteImages,
  onCompact,
  onDropFiles,
}: RichComposerProps) {
  const { getCompletionCatalog, searchWorkspaceFiles, t } = useApp();
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Track which segments are currently in the DOM to avoid redundant rebuilds
  // (and the cursor jumps they cause) while the user is typing.
  const lastRenderedRef = useRef<string>("");
  // When an edit knows exactly where the caret should land after the next
  // rebuild (e.g. right after pasted content), it stashes the target here.
  // ``useLayoutEffect`` consumes it instead of re-deriving the caret from the
  // pre-rebuild DOM, which would otherwise leave the caret before the inserted
  // text. Cleared after a single use.
  const pendingCaretRef = useRef<CaretSnapshot | null>(null);

  // Undo/redo history. The composer rewrites its own DOM and drives state
  // through React, which defeats the browser's native contentEditable undo
  // stack (so Ctrl+Z does nothing after a paste or pill edit). We keep our
  // own snapshot stack of the canonical Segment[] model and intercept the
  // undo/redo shortcuts. ``undoStack`` holds past states (most recent last);
  // ``redoStack`` holds states undone-from. ``suppressHistoryRef`` prevents an
  // undo/redo-driven onChange from itself being recorded as a new edit.
  const undoStackRef = useRef<Segment[][]>([]);
  const redoStackRef = useRef<Segment[][]>([]);
  const suppressHistoryRef = useRef<boolean>(false);
  const lastHistorySigRef = useRef<string>("__init__");
  const segmentsSigRef = useRef<string>("__init__");
  const lastPrevSegmentsRef = useRef<Segment[]>(segments);
  const MAX_HISTORY = 200;

  const segmentsSignature = useCallback(
    (segs: Segment[]) =>
      segs
        .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
        .join("\x1e"),
    [],
  );

  // Record the *current* model as a history checkpoint before the next edit
  // mutates it. Coalesces no-op pushes via the last-recorded signature.
  const pushHistory = useCallback(
    (current: Segment[]) => {
      const sig = segmentsSignature(current);
      if (sig === lastHistorySigRef.current) {
        return;
      }
      undoStackRef.current.push(current.map((s) => ({ ...s })));
      if (undoStackRef.current.length > MAX_HISTORY) {
        undoStackRef.current.shift();
      }
      lastHistorySigRef.current = sig;
      // Any fresh edit invalidates the redo chain.
      redoStackRef.current = [];
    },
    [segmentsSignature],
  );
  const [pool, setPool] = useState<SlashItem[]>([]);
  const [slash, setSlash] = useState<{
    open: boolean;
    query: string;
    selected: number;
  }>({ open: false, query: "", selected: 0 });
  // '@' quick file reference: a popup of workspace files filtered by the
  // partial filename typed after '@'. Selecting one inserts an attachment
  // pill — the same effect as the "Attach files" action.
  const [at, setAt] = useState<{
    open: boolean;
    query: string;
    selected: number;
  }>({ open: false, query: "", selected: 0 });
  const [atFiles, setAtFiles] = useState<string[]>([]);
  // Monotonic token so a slow file-search response can't overwrite the
  // results of a newer query.
  const atQuerySeq = useRef<number>(0);
  // Refs that mirror popup state, so handleKeyDown always reads the latest
  // values even when its useCallback closure is a render behind (avoiding the
  // "stale closure" problem where ArrowDown fires before the re-render
  // triggered by handleInput commits).
  const slashRef = useRef(slash);
  slashRef.current = slash;
  const atRef = useRef(at);
  atRef.current = at;
  const atFilesRef = useRef(atFiles);
  atFilesRef.current = atFiles;

  // Load catalog when the composer mounts and re-fetch whenever the user
  // opens the slash menu so newly-added skills / reconnected MCP servers
  // become available without a manual refresh. Silently ignore fetch failures
  // (e.g. backend restarting) — the pool stays at its last-known-good state.
  useEffect(() => {
    void getCompletionCatalog().then((c) => setPool(buildFullSlashPool(c))).catch(() => {});
  }, [getCompletionCatalog]);
  useEffect(() => {
    if (slash.open) {
      void getCompletionCatalog().then((c) => setPool(buildFullSlashPool(c))).catch(() => {});
    }
  }, [slash.open, getCompletionCatalog]);

  // Track the incoming model and feed the undo history. When ``segments``
  // changes for any reason other than an undo/redo we apply ourselves, record
  // the PREVIOUS model as an undo checkpoint. Initializing the ref lazily on
  // first run seeds the baseline without recording a phantom edit.
  useEffect(() => {
    const prevSig = segmentsSigRef.current;
    const nextSig = segmentsSignature(segments);
    if (prevSig === "__init__") {
      // First observation: seed baseline, nothing to record.
      segmentsSigRef.current = nextSig;
      lastPrevSegmentsRef.current = segments;
      return;
    }
    if (prevSig === nextSig) {
      return;
    }
    if (!suppressHistoryRef.current) {
      // A genuine user edit: checkpoint the previous model so Ctrl+Z restores
      // it. ``lastPrevSegmentsRef`` holds the model as it was before this
      // change landed.
      pushHistory(lastPrevSegmentsRef.current);
    }
    suppressHistoryRef.current = false;
    segmentsSigRef.current = nextSig;
    lastPrevSegmentsRef.current = segments;
  }, [segments, segmentsSignature, pushHistory]);

  // Render the canonical segments into the DOM whenever they change. We do
  // this with direct DOM mutation rather than React children because mixing
  // React-managed contentEditable nodes with the browser's selection model is
  // a long-standing source of caret bugs; the DOM is so simple here (one
  // level of children) that owning it manually is the simpler path.
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const signature = segments
      .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
      .join("\x1e");
    if (signature === lastRenderedRef.current) {
      return;
    }
    lastRenderedRef.current = signature;
    // Prefer an explicitly requested caret target (set by edits that know the
    // post-rebuild position, e.g. paste); otherwise preserve the live caret.
    const caret = pendingCaretRef.current ?? captureCaret(root);
    pendingCaretRef.current = null;
    // Clear and rebuild.
    while (root.firstChild) {
      root.removeChild(root.firstChild);
    }
    // When the very first segment is a pill there is no text node before it for
    // the caret to land in, so Home / caret-before-pill would vanish and a lone
    // leading pill could wrap awkwardly. Prepend a zero-width anchor text node
    // in that case. ``snapshotForCanonical`` adds the matching +1 child offset,
    // and ``readSegmentsFromDom`` ignores the ZWSP so the model is unaffected.
    if (segments.length > 0 && segments[0].kind !== "text") {
      root.appendChild(document.createTextNode(ZWSP));
    }
    for (let segIdx = 0; segIdx < segments.length; segIdx += 1) {
      const seg = segments[segIdx];
      if (seg.kind === "text") {
        const textNode = document.createTextNode(seg.value);
        root.appendChild(textNode);
      } else {
        const pill = document.createElement("span");
        pill.className = `composer-pill composer-pill-${seg.kind}`;
        pill.setAttribute("contenteditable", "false");
        pill.setAttribute("data-token-kind", seg.kind);
        pill.setAttribute("data-token-payload", seg.value);
        pill.setAttribute("data-token-index", String(segIdx));
        const info = kindLabel(seg.kind, seg.value);
        const labelText = info.secondary
          ? `${info.secondary} / ${info.primary}`
          : info.primary;
        const labelNode = document.createElement("span");
        labelNode.className = "composer-pill-label";
        labelNode.textContent = labelText;
        pill.appendChild(labelNode);
        // No inline "×" remove button in the composer: it was visually noisy,
        // appeared only after rebuilds (paste/undo) so pills looked
        // inconsistent, and broke the selection highlight. Pills are removed
        // with Backspace like any atomic token instead.
        pill.title = seg.value;
        root.appendChild(pill);
        // Anchor so the caret can land between two consecutive pills.
        const zwsp = document.createTextNode(ZWSP);
        root.appendChild(zwsp);
      }
    }
    // Ensure at least one trailing text node so the caret has somewhere to
    // land when the composer is empty.
    if (!root.lastChild || root.lastChild.nodeType !== Node.TEXT_NODE) {
      root.appendChild(document.createTextNode(""));
    }
    restoreCaret(root, caret);
  }, [segments]);

  // Wire up clicks on per-pill close buttons. We attach a single delegated
  // listener at the root rather than per-pill handlers so re-renders don't
  // leak listeners; the listener inspects the click target's
  // ``data-token-remove`` marker to decide what to remove.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      const close = t.closest?.("[data-token-remove]") as HTMLElement | null;
      if (!close) return;
      const pill = close.closest("[data-token-kind]") as HTMLElement | null;
      if (!pill) return;
      e.preventDefault();
      e.stopPropagation();
      const kind = pill.getAttribute("data-token-kind") || "";
      const payload = pill.getAttribute("data-token-payload") || "";
      // Find the occurrence number of this pill among same-kind+payload
      // pills so we delete THIS one (not the first match in segments).
      let occurrence = 0;
      const allPills = root.querySelectorAll("[data-token-kind]");
      for (const node of Array.from(allPills)) {
        if (node === pill) break;
        const el = node as HTMLElement;
        if (
          el.getAttribute("data-token-kind") === kind &&
          el.getAttribute("data-token-payload") === payload
        ) {
          occurrence += 1;
        }
      }
      const next = readSegmentsFromDom(root);
      let seen = 0;
      const dropped: Segment[] = [];
      let removed = false;
      for (const s of next) {
        if (!removed && s.kind === kind && s.value === payload) {
          if (seen === occurrence) {
            removed = true;
            continue;
          }
          seen += 1;
        }
        dropped.push(s);
      }
      if (!removed) return;
      lastRenderedRef.current = FORCE_REBUILD; // force rebuild
      onChange(dropped);
    };
    root.addEventListener("click", onClick);
    return () => root.removeEventListener("click", onClick);
  }, [onChange]);

  const computeSlashQuery = useCallback((): { open: boolean; query: string } => {
    const root = rootRef.current;
    if (!root) return { open: false, query: "" };
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return { open: false, query: "" };
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (!root.contains(node)) return { open: false, query: "" };
    if (node.nodeType !== Node.TEXT_NODE) {
      return { open: false, query: "" };
    }
    const text = node.textContent ?? "";
    const upto = text.slice(0, range.startOffset);
    // Find the last '/' that follows a "word boundary". A boundary is one of:
    //   * the start of the text node;
    //   * standard whitespace (space, newline, tab);
    //   * the zero-width space (U+200B) we insert next to pills, so typing
    //     ``/`` immediately after a pill still opens the menu;
    //   * (when the slash is at index 0) a pill DOM sibling immediately to
    //     the left of this text node, since the caret can otherwise sit at
    //     the very front of a text node whose ZWSP was eaten by previous
    //     edits.
    let slashAt = -1;
    for (let i = upto.length - 1; i >= 0; i -= 1) {
      const ch = upto[i];
      if (ch === "/") {
        const prev = i === 0 ? "" : upto[i - 1];
        const startBoundary =
          i === 0 &&
          isPreviousSiblingPill(node) &&
          // If the text node already contains non-whitespace BEFORE the
          // slash, the slash is mid-word; that case is handled above with
          // ``i === 0`` always being false there.
          true;
        if (
          i === 0 ||
          /\s/.test(prev) ||
          prev === "\u200B" ||
          startBoundary
        ) {
          slashAt = i;
        }
        break;
      }
      if (/\s/.test(ch) || ch === "\u200B") {
        break;
      }
    }
    if (slashAt < 0) {
      return { open: false, query: "" };
    }
    return { open: true, query: upto.slice(slashAt + 1) };
  }, []);

  // Mirror of ``computeSlashQuery`` for the ``@`` file-reference trigger.
  // The boundary rules match: ``@`` opens the popup at the start of the
  // text node, after whitespace, or right after a pill's ZWSP.
  const computeAtQuery = useCallback((): { open: boolean; query: string } => {
    const root = rootRef.current;
    if (!root) return { open: false, query: "" };
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return { open: false, query: "" };
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (!root.contains(node)) return { open: false, query: "" };
    if (node.nodeType !== Node.TEXT_NODE) {
      return { open: false, query: "" };
    }
    const text = node.textContent ?? "";
    const upto = text.slice(0, range.startOffset);
    let atPos = -1;
    for (let i = upto.length - 1; i >= 0; i -= 1) {
      const ch = upto[i];
      if (ch === "@") {
        const prev = i === 0 ? "" : upto[i - 1];
        if (
          i === 0 ||
          /\s/.test(prev) ||
          prev === "\u200B" ||
          (i === 0 && isPreviousSiblingPill(node))
        ) {
          atPos = i;
        }
        break;
      }
      // A space inside the partial filename ends the trigger; filenames
      // with spaces aren't supported by the quick reference.
      if (/\s/.test(ch) || ch === "\u200B" || ch === "@") {
        break;
      }
    }
    if (atPos < 0) {
      return { open: false, query: "" };
    }
    return { open: true, query: upto.slice(atPos + 1) };
  }, []);

  // Debounced async fetch of file candidates whenever the '@' query changes.
  useEffect(() => {
    if (!at.open) {
      return;
    }
    const seq = (atQuerySeq.current += 1);
    const handle = window.setTimeout(() => {
      void searchWorkspaceFiles(at.query, 10).then((files) => {
        // Drop stale responses.
        if (seq !== atQuerySeq.current) return;
        setAtFiles(files);
      });
    }, 80);
    return () => window.clearTimeout(handle);
  }, [at.open, at.query, searchWorkspaceFiles]);

  const filteredItems = useMemo(
    () => filterSlashItems(pool, slash.query),
    [pool, slash.query],
  );
  const filteredItemsRef = useRef(filteredItems);
  filteredItemsRef.current = filteredItems;

  const handleInput = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const next = readSegmentsFromDom(root);
    // Skip the round-trip if nothing actually changed (e.g. for selection-
    // only changes). React will short-circuit identical state but the parent
    // still re-renders on every onChange, so it's cheaper to bail here.
    const sig = next
      .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
      .join("\x1e");
    if (sig !== lastRenderedRef.current) {
      lastRenderedRef.current = sig;
      onChange(next);
    }
    const sq = computeSlashQuery();
    setSlash((prev) => {
      if (!sq.open) {
        if (prev.open) return { open: false, query: "", selected: 0 };
        return prev;
      }
      if (prev.query === sq.query && prev.open) return prev;
      return { open: true, query: sq.query, selected: 0 };
    });
    // Sync refs immediately so handleKeyDown reads the latest popup state
    // before React's re-render commits (avoids stale-closure window between
    // handleInput and the next keydown event).
    {
      const cur = slashRef.current;
      const next = sq.open
        ? { open: true, query: sq.query, selected: 0 }
        : { open: false, query: "", selected: 0 };
      if (next.open !== cur.open || next.query !== cur.query) {
        slashRef.current = next;
      }
    }
    // The slash popup takes precedence; only evaluate '@' when '/' isn't
    // active so the two popups never overlap.
    const aq = sq.open ? { open: false, query: "" } : computeAtQuery();
    setAt((prev) => {
      if (!aq.open) {
        if (prev.open) return { open: false, query: "", selected: 0 };
        return prev;
      }
      if (prev.query === aq.query && prev.open) return prev;
      return { open: true, query: aq.query, selected: 0 };
    });
    {
      const cur = atRef.current;
      const next = aq.open
        ? { open: true, query: aq.query, selected: 0 }
        : { open: false, query: "", selected: 0 };
      if (next.open !== cur.open || next.query !== cur.query) {
        atRef.current = next;
      }
    }
  }, [computeAtQuery, computeSlashQuery, onChange]);

  const insertSelectedSlashItem = useCallback(
    (item: SlashItem) => {
      if (item.kind === "compact") {
        // "/compact" is a built-in action, not a pill. Clear the slash text
        // when possible, but ALWAYS invoke the callback even if the current
        // DOM selection is no longer in the expected shape (for example after
        // a chat switch / refocus changed the caret node).
        const root = rootRef.current;
        if (root) {
          const sel = window.getSelection();
          if (sel && sel.rangeCount > 0) {
            const range = sel.getRangeAt(0);
            const node = range.startContainer;
            if (node.nodeType === Node.TEXT_NODE && root.contains(node)) {
              const text = node.textContent ?? "";
              const before = text.slice(0, range.startOffset);
              let slashAt = -1;
              for (let i = before.length - 1; i >= 0; i -= 1) {
                if (before[i] === "/") {
                  slashAt = i;
                  break;
                }
                if (/\s/.test(before[i]) || before[i] === "\u200B") break;
              }
              if (slashAt >= 0) {
                const head = before.slice(0, slashAt);
                const after = text.slice(range.startOffset);
                node.textContent = head + after;
                const next = readSegmentsFromDom(root);
                lastRenderedRef.current = next
                  .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
                  .join("\x1e");
                onChange(next);
              }
            }
          }
        }
        setSlash({ open: false, query: "", selected: 0 });
        onCompact?.();
        return;
      }
      const root = rootRef.current;
      if (!root) return;
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      if (node.nodeType !== Node.TEXT_NODE || !root.contains(node)) return;
      const text = node.textContent ?? "";
      const before = text.slice(0, range.startOffset);
      const after = text.slice(range.startOffset);
      // Find the active '/' to remove the in-progress query so the pill
      // replaces it cleanly.
      let slashAt = -1;
      for (let i = before.length - 1; i >= 0; i -= 1) {
        if (before[i] === "/") {
          slashAt = i;
          break;
        }
        if (/\s/.test(before[i]) || before[i] === "\u200B") break;
      }
      if (slashAt < 0) return;
      const head = before.slice(0, slashAt);
      // Splice in: head text node, pill, after text node.
      node.textContent = head;
      const pill = document.createElement("span");
      pill.className = `composer-pill composer-pill-${item.kind}`;
      pill.setAttribute("contenteditable", "false");
      pill.setAttribute("data-token-kind", item.kind);
      pill.setAttribute("data-token-payload", item.payload);
      const info = kindLabel(item.kind, item.payload);
      pill.textContent = info.secondary
        ? `${info.secondary} / ${info.primary}`
        : info.primary;
      pill.title = item.payload;
      // Insert after the truncated text node.
      const parent = node.parentNode;
      if (!parent) return;
      const tail = document.createTextNode(ZWSP + after);
      parent.insertBefore(tail, node.nextSibling);
      parent.insertBefore(pill, tail);
      // Place caret right after the pill (between pill and ZWSP).
      const r = document.createRange();
      r.setStart(tail, 1);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
      setSlash({ open: false, query: "", selected: 0 });
      // Re-extract and push to parent so external state stays in sync.
      const next = readSegmentsFromDom(root);
      lastRenderedRef.current = next
        .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
        .join("\x1e");
      onChange(next);
    },
    [onChange, onCompact],
  );

  const insertSelectedAtItem = useCallback(
    (filePath: string) => {
      const root = rootRef.current;
      if (!root) return;
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      if (node.nodeType !== Node.TEXT_NODE || !root.contains(node)) return;
      const text = node.textContent ?? "";
      const before = text.slice(0, range.startOffset);
      const after = text.slice(range.startOffset);
      // Find the active '@' so the inserted pill replaces the "@query".
      let atPos = -1;
      for (let i = before.length - 1; i >= 0; i -= 1) {
        if (before[i] === "@") {
          atPos = i;
          break;
        }
        if (/\s/.test(before[i]) || before[i] === "\u200B") break;
      }
      if (atPos < 0) return;
      const head = before.slice(0, atPos);
      node.textContent = head;
      const pill = document.createElement("span");
      pill.className = "composer-pill composer-pill-attach";
      pill.setAttribute("contenteditable", "false");
      pill.setAttribute("data-token-kind", "attach");
      pill.setAttribute("data-token-payload", filePath);
      const info = kindLabel("attach", filePath);
      pill.textContent = info.primary;
      pill.title = filePath;
      const parent = node.parentNode;
      if (!parent) return;
      const tail = document.createTextNode(ZWSP + after);
      parent.insertBefore(tail, node.nextSibling);
      parent.insertBefore(pill, tail);
      const r = document.createRange();
      r.setStart(tail, 1);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
      setAt({ open: false, query: "", selected: 0 });
      const next = readSegmentsFromDom(root);
      lastRenderedRef.current = next
        .map((s) => (s.kind === "text" ? `T:${s.value}` : `${s.kind}:${s.value}`))
        .join("\x1e");
      onChange(next);
    },
    [onChange],
  );

  // Select the entire editor content (all text + pills). Mirrors the native
  // Ctrl/Cmd+A but scoped to the editor so the selection never escapes into
  // the surrounding page, which keeps the copy/cut serializers below working
  // on a well-defined range.
  const selectAllContent = useCallback(() => {
    const root = rootRef.current;
    if (!root) return;
    const sel = window.getSelection();
    if (!sel) return;
    const r = document.createRange();
    r.selectNodeContents(root);
    sel.removeAllRanges();
    sel.addRange(r);
  }, []);

  // Restore the previous model from the undo stack. The current model is moved
  // onto the redo stack first. ``suppressHistoryRef`` keeps the resulting
  // onChange from being recorded as a brand-new edit.
  const undo = useCallback(() => {
    if (undoStackRef.current.length === 0) {
      return;
    }
    const prev = undoStackRef.current.pop() as Segment[];
    const current = lastPrevSegmentsRef.current;
    redoStackRef.current.push(current.map((s) => ({ ...s })));
    suppressHistoryRef.current = true;
    lastHistorySigRef.current = "__init__";
    lastRenderedRef.current = FORCE_REBUILD; // force the DOM to rebuild from the model
    // Place the caret at the end of whatever the undo changed (insert/delete
    // rule), computed by diffing the outgoing and incoming models.
    pendingCaretRef.current = snapshotForCanonical(
      prev,
      caretAfterModelSwap(current, prev),
    );
    onChange(prev);
  }, [onChange, segmentsSignature]);

  const redo = useCallback(() => {
    if (redoStackRef.current.length === 0) {
      return;
    }
    const next = redoStackRef.current.pop() as Segment[];
    const current = lastPrevSegmentsRef.current;
    undoStackRef.current.push(current.map((s) => ({ ...s })));
    suppressHistoryRef.current = true;
    lastHistorySigRef.current = "__init__";
    lastRenderedRef.current = FORCE_REBUILD;
    pendingCaretRef.current = snapshotForCanonical(
      next,
      caretAfterModelSwap(current, next),
    );
    onChange(next);
  }, [onChange, segmentsSignature]);

  // Intercept the browser's native undo/redo via ``beforeinput``. This is more
  // reliable than a keydown shortcut: WebKitGTK and Chromium emit
  // ``historyUndo`` / ``historyRedo`` input types for Ctrl+Z / Ctrl+Shift+Z /
  // Ctrl+Y (and menu/gesture undo) regardless of keyboard layout, and catching
  // it here also stops the native (broken) contentEditable undo from running.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onBeforeInput = (e: Event) => {
      const inputType = (e as InputEvent).inputType;
      if (inputType === "historyUndo") {
        e.preventDefault();
        undo();
      } else if (inputType === "historyRedo") {
        e.preventDefault();
        redo();
      }
    };
    root.addEventListener("beforeinput", onBeforeInput);
    return () => root.removeEventListener("beforeinput", onBeforeInput);
  }, [undo, redo]);

  // (Pill selection highlighting is handled globally — see usePillSelectionHighlight.)
  usePillSelectionHighlight();

  // Serialize the current selection (or the whole editor when nothing is
  // selected) into the clipboard. We write BOTH a plain-text human-readable
  // form (so pasting into other apps shows sensible text) and our private
  // envelope form on a custom MIME type, so pasting back into the composer
  // restores the pills losslessly.
  const writeSelectionToClipboard = useCallback(
    (e: ReactClipboardEvent<HTMLDivElement>): boolean => {
      const root = rootRef.current;
      if (!root) return false;
      const sel = window.getSelection();
      let segs: Segment[];
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
        segs = readSegmentsFromDom(root);
      } else {
        const range = sel.getRangeAt(0);
        if (!root.contains(range.commonAncestorContainer)) {
          return false;
        }
        segs = readSegmentsFromRange(root, range);
      }
      if (segs.length === 0) {
        return false;
      }
      // Human-readable text for external apps; envelope form for ourselves.
      const plain = composeMessageText(segs);
      const envelope = encodeSegments(segs);
      try {
        e.clipboardData.setData("text/plain", plain);
        e.clipboardData.setData("application/x-codewood-segments", envelope);
      } catch {
        return false;
      }
      return true;
    },
    [],
  );

  // Delete the active (non-collapsed) selection through the canonical model.
  // Returns false if it can't resolve the selection (caller then falls back to
  // native behavior). Caret lands at the deletion gap (delete-text rule).
  const deleteSelectionViaModel = useCallback((): boolean => {
    const root = rootRef.current;
    if (!root) return false;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return false;
    const range = sel.getRangeAt(0);
    if (
      !root.contains(range.startContainer) ||
      !root.contains(range.endContainer)
    ) {
      return false;
    }
    const startSnap = snapshotFromPoint(
      root,
      range.startContainer,
      range.startOffset,
    );
    const endSnap = snapshotFromPoint(root, range.endContainer, range.endOffset);
    if (!startSnap || !endSnap) return false;
    const existing = readSegmentsFromDom(root);
    let startC = canonicalFromSnapshot(root, startSnap);
    let endC = canonicalFromSnapshot(root, endSnap);
    // Normalize so start <= end in (segIdx, offset) order.
    const before = (p: CanonicalCaret, q: CanonicalCaret) =>
      p.segIdx < q.segIdx || (p.segIdx === q.segIdx && p.offset <= q.offset);
    if (!before(startC, endC)) {
      const tmp = startC;
      startC = endC;
      endC = tmp;
    }
    if (startC.segIdx === endC.segIdx && startC.offset === endC.offset) {
      return false;
    }
    // Build the kept head (everything before ``startC``) and tail (everything
    // from ``endC`` onward), splitting text segments at the cut points.
    const head: Segment[] = [];
    for (let i = 0; i < startC.segIdx && i < existing.length; i += 1) {
      head.push(existing[i]);
    }
    let headEndsInSplitText = false;
    const startSeg = existing[startC.segIdx];
    if (startSeg && startSeg.kind === "text" && startC.offset > 0) {
      head.push({ kind: "text", value: startSeg.value.slice(0, startC.offset) });
      headEndsInSplitText = true;
    }
    const tail: Segment[] = [];
    const endSeg = existing[endC.segIdx];
    if (endSeg) {
      if (endSeg.kind === "text") {
        const rest = endSeg.value.slice(endC.offset);
        if (rest) tail.push({ kind: "text", value: rest });
      } else if (endC.offset === 0) {
        tail.push(endSeg);
      }
    }
    for (let i = endC.segIdx + 1; i < existing.length; i += 1) {
      tail.push(existing[i]);
    }

    // Caret target (the gap) in canonical coordinates. It sits at the end of
    // the head: if the head ends in a split text segment, at that slice's end;
    // otherwise at the start of the tail. We keep head/tail segments un-merged
    // (the rebuild renders each as its own node; ``readSegmentsFromDom``
    // coalesces adjacent text on the next read), so the index is stable.
    const merged: Segment[] = [...head, ...tail];
    const lastHead = head[head.length - 1];
    const caretTarget: CanonicalCaret =
      lastHead && lastHead.kind === "text" && headEndsInSplitText
        ? { segIdx: head.length - 1, offset: lastHead.value.length }
        : { segIdx: head.length, offset: 0 };
    pendingCaretRef.current = snapshotForCanonical(merged, caretTarget);
    lastRenderedRef.current = FORCE_REBUILD;
    onChange(merged);
    return true;
  }, [onChange]);

  // Replace the current selection with ``segs`` (pills + text), then push the
  // new model to the parent and refocus the caret after the inserted content.
  const replaceSelectionWithSegments = useCallback(
    (segs: Segment[]) => {
      const root = rootRef.current;
      if (!root) return;
      const sel = window.getSelection();
      // Delete the active selection so paste replaces it (matching native
      // editor behavior) before splicing in the new model.
      if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
        const range = sel.getRangeAt(0);
        if (root.contains(range.commonAncestorContainer)) {
          range.deleteContents();
        }
      }
      // Capture where the caret sits (in canonical-segment coordinates) BEFORE
      // we rebuild, so we can splice the pasted segments there and leave the
      // caret right after them — appending blindly at the end made the caret
      // jump to the wrong place.
      const existing = readSegmentsFromDom(root);
      const insertAt =
        canonicalCaretFromDom(root) ?? { segIdx: existing.length, offset: 0 };

      // Build the merged model by splitting the canonical segments at the caret
      // and inserting ``segs`` between the two halves.
      const head: Segment[] = [];
      const tail: Segment[] = [];
      for (let i = 0; i < existing.length; i += 1) {
        const s = existing[i];
        if (i < insertAt.segIdx) {
          head.push(s);
        } else if (i > insertAt.segIdx) {
          tail.push(s);
        } else if (
          s.kind === "text" &&
          insertAt.offset > 0 &&
          insertAt.offset < s.value.length
        ) {
          // Caret is mid-text: split this segment around it.
          head.push({ kind: "text", value: s.value.slice(0, insertAt.offset) });
          tail.push({ kind: "text", value: s.value.slice(insertAt.offset) });
        } else if (s.kind === "text" && insertAt.offset >= s.value.length) {
          // Caret at the end of this text segment: it belongs to the head.
          head.push(s);
        } else {
          // Caret at the start of this segment (offset 0, or a pill): it goes
          // to the tail so the paste lands before it.
          tail.push(s);
        }
      }
      const merged: Segment[] = [...head, ...segs, ...tail];

      // Caret lands right AFTER the inserted content (insert-text rule). Anchor
      // it to the END of the LAST inserted segment rather than the START of the
      // following segment: when that following segment is a pill, "start of the
      // pill" would resolve (via setStartAfter) to AFTER the pill, wrongly
      // jumping the caret past it. Anchoring to the end of the last pasted
      // segment keeps the caret tucked between the paste and the pill.
      let caretTarget: CanonicalCaret;
      if (segs.length > 0) {
        const lastInsertedIdx = head.length + segs.length - 1;
        const lastSeg = merged[lastInsertedIdx];
        caretTarget =
          lastSeg.kind === "text"
            ? { segIdx: lastInsertedIdx, offset: lastSeg.value.length }
            : { segIdx: lastInsertedIdx + 1, offset: 0 };
      } else {
        caretTarget = { segIdx: head.length, offset: 0 };
      }
      pendingCaretRef.current = snapshotForCanonical(merged, caretTarget);

      lastRenderedRef.current = FORCE_REBUILD; // force a full rebuild
      onChange(merged);
    },
    [onChange],
  );

  const handlePaste = useCallback(
    (e: ReactClipboardEvent<HTMLDivElement>) => {
      const root = rootRef.current;
      if (!root) return;
      // Clipboard bitmaps (e.g. a screenshot or a copied image) arrive as
      // ``items`` of kind "file" with an ``image/*`` type. When the host opted
      // in, consume them as attachments rather than inserting anything as text.
      if (onPasteImages) {
        const items = Array.from(e.clipboardData.items || []);
        const imageFiles = items
          .filter(
            (it) => it.kind === "file" && it.type.startsWith("image/"),
          )
          .map((it) => it.getAsFile())
          .filter((f): f is File => f != null);
        if (imageFiles.length > 0) {
          e.preventDefault();
          Promise.all(
            imageFiles.map(
              (file) =>
                new Promise<string>((resolve) => {
                  const reader = new FileReader();
                  reader.onload = () =>
                    resolve(String(reader.result || ""));
                  reader.onerror = () => resolve("");
                  reader.readAsDataURL(file);
                }),
            ),
          ).then((urls) => {
            const valid = urls.filter((u) => u.startsWith("data:image/"));
            if (valid.length > 0) {
              onPasteImages(valid);
            }
          });
          return;
        }
      }
      const envelope = e.clipboardData.getData(
        "application/x-codewood-segments",
      );
      e.preventDefault();
      if (envelope) {
        // Round-trip our own pill-bearing payload back into segments.
        replaceSelectionWithSegments(decodeSegments(envelope));
        return;
      }
      // Plain external paste: also recognise pills the user may have copied
      // from a sent message bubble (which emits the readable bracket / slash
      // forms) so pasting them restores pills; otherwise insert as text.
      const plain = e.clipboardData.getData("text/plain");
      if (!plain) {
        return;
      }
      replaceSelectionWithSegments(decodeSegments(plain));
    },
    [replaceSelectionWithSegments, onPasteImages],
  );

  // Paste triggered from the right-click menu. The menu button steals focus
  // from the editor, so re-focus it first (Chromium restores the caret), then
  // insert the clipboard text through the segment model. Reading the clipboard
  // via the browser API makes Chromium pop a permission prompt on the
  // ``file://`` origin, so the pywebview host reads it natively; the async
  // Clipboard API is only a dev-mode fallback when the host bridge is absent.
  const pasteFromContextMenu = useCallback(() => {
    const root = rootRef.current;
    if (!root) {
      return;
    }
    root.focus();
    const apply = (text: string) => {
      if (text) {
        replaceSelectionWithSegments(decodeSegments(text));
      }
    };
    const api = hostApi();
    if (api?.get_clipboard_text) {
      void Promise.resolve(api.get_clipboard_text()).then(apply).catch(() => {});
      return;
    }
    void navigator.clipboard?.readText().then(apply).catch(() => {});
  }, [replaceSelectionWithSegments]);

  // Cut triggered from the right-click menu: copy the selection to the system
  // clipboard (``writeText`` needs no read permission, so no Chromium prompt),
  // then delete it through the model — the same semantics as Ctrl+X. Only the
  // plain-text form is written here (matching the right-click Copy item); the
  // pill-preserving envelope is reserved for the native Ctrl+X path.
  const cutFromContextMenu = useCallback(() => {
    const root = rootRef.current;
    if (!root) {
      return;
    }
    root.focus();
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) {
      return;
    }
    const text = sel.toString();
    if (!text.trim()) {
      return;
    }
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard
        .writeText(text)
        .then(() => {
          deleteSelectionViaModel();
        })
        .catch(() => {});
    }
  }, [deleteSelectionViaModel]);

  const copyCtx = useCopyContextMenu({
    onPaste: pasteFromContextMenu,
    onCut: cutFromContextMenu,
  });

  // Drag-and-drop from the OS file manager.
  const dragCounterRef = useRef(0);
  const [dragOver, setDragOver] = useState(false);
  const handleDragEnter = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    console.log("[drag-drop] RichComposer dragEnter, counter=" + dragCounterRef.current + " types=" + JSON.stringify(Array.from(e.dataTransfer.types)));
    setDragOver(true);
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current <= 0) {
      dragCounterRef.current = 0;
      setDragOver(false);
    }
  }, []);
  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);
  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    console.log("[drag-drop] RichComposer handleDrop, items=" + e.dataTransfer.items.length + " files=" + e.dataTransfer.files.length);
    dragCounterRef.current = 0;
    setDragOver(false);
    const files = e.dataTransfer.files;
    if (files && files.length > 0 && onDropFiles) {
      onDropFiles(files);
    }
  }, [onDropFiles]);

  const handleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      // While an IME composition is in flight (e.g. Chinese Pinyin holding
      // pending Latin text, or half-width English awaiting confirmation), the
      // IME owns the keyboard: the Enter that commits/confirms the composition
      // must NOT be treated as "send". Let the IME consume every key here —
      // including that committing Enter (detected via ``isComposing`` or the
      // legacy 229 keyCode) — instead of routing it to our shortcuts, popup
      // selection or submit handlers.
      if (e.nativeEvent.isComposing || (e.nativeEvent as KeyboardEvent).keyCode === 229) {
        return;
      }
      // Undo / redo. We own the history because manual DOM rewrites defeat the
      // browser's native contentEditable undo (Ctrl+Z would otherwise do
      // nothing, most visibly after pasting pill-bearing content).
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) {
          redo();
        } else {
          undo();
        }
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) {
        e.preventDefault();
        redo();
        return;
      }
      // Ctrl/Cmd+A selects the whole editor content (text + pills) so the
      // user can copy or replace everything in one shot.
      if ((e.ctrlKey || e.metaKey) && (e.key === "a" || e.key === "A")) {
        e.preventDefault();
        selectAllContent();
        return;
      }
      // Deleting a non-collapsed selection: do it through our canonical model
      // instead of letting contentEditable mutate the DOM. Native deletion can
      // leave a stray <br>/empty node behind (e.g. removing text in front of a
      // lone pill pushed the pill onto a phantom second line), and it bypasses
      // our undo history. We rebuild the model with the selected span removed
      // and drop the caret at the gap (delete-text rule).
      if (
        (e.key === "Backspace" || e.key === "Delete") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        const sel = window.getSelection();
        if (
          sel &&
          sel.rangeCount > 0 &&
          !sel.isCollapsed &&
          deleteSelectionViaModel()
        ) {
          e.preventDefault();
          return;
        }
      }
      // Home / End: when the document starts (or ends) with a pill there is no
      // text node for the native caret to land in, so the caret can vanish.
      // Only intercept when the respective edge is a pill; otherwise let the
      // browser handle Home/End natively (line-based navigation).
      if (
        (e.key === "Home" || e.key === "End") &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        const root = rootRef.current;
        const sel = window.getSelection();
        if (root && sel && sel.rangeCount > 0) {
          const kids = Array.from(root.childNodes);
          if (kids.length > 0) {
            const isPillOrAnchor = (node: Node): boolean => {
              if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).hasAttribute("data-token-kind")) {
                return true;
              }
              if (node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").startsWith(ZWSP)) {
                return true;
              }
              return false;
            };
            const atEdge = e.key === "Home"
              ? isPillOrAnchor(kids[0])
              : isPillOrAnchor(kids[kids.length - 1]);
            if (atEdge) {
              const r = document.createRange();
              if (e.key === "Home") {
                const first = kids[0];
                if (
                  first.nodeType === Node.TEXT_NODE &&
                  (first.textContent ?? "").startsWith(ZWSP)
                ) {
                  r.setStart(first, Math.min(1, (first.textContent ?? "").length));
                } else {
                  r.setStartBefore(first);
                }
              } else {
                const last = kids[kids.length - 1];
                r.setStartAfter(last);
              }
              r.collapse(true);
              if (e.shiftKey) {
                const cur = sel.getRangeAt(0);
                if (e.key === "Home") {
                  cur.setStart(r.startContainer, r.startOffset);
                } else {
                  cur.setEnd(r.startContainer, r.startOffset);
                }
                sel.removeAllRanges();
                sel.addRange(cur);
              } else {
                sel.removeAllRanges();
                sel.addRange(r);
              }
              e.preventDefault();
              return;
            }
          }
        }
      }
      // Read popup state from refs so the keyboard handler always sees the
      // latest values even when the useCallback closure is a render behind
      // (the "stale closure" problem with synchronous ArrowDown right after
      // handleInput opens the popup).
      const curAt = atRef.current;
      const curAtFiles = atFilesRef.current;
      const curSlash = slashRef.current;
      const curFiltered = filteredItemsRef.current;

      if (curAt.open && curAtFiles.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setAt((p) => ({ ...p, selected: (p.selected + 1) % curAtFiles.length }));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setAt((p) => ({
            ...p,
            selected: (p.selected - 1 + curAtFiles.length) % curAtFiles.length,
          }));
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setAt({ open: false, query: "", selected: 0 });
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          insertSelectedAtItem(curAtFiles[curAt.selected]);
          return;
        }
      }
      if (curSlash.open && curFiltered.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSlash((p) => ({ ...p, selected: (p.selected + 1) % curFiltered.length }));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSlash((p) => ({
            ...p,
            selected: (p.selected - 1 + curFiltered.length) % curFiltered.length,
          }));
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setSlash({ open: false, query: "", selected: 0 });
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          insertSelectedSlashItem(curFiltered[curSlash.selected]);
          return;
        }
      }
      // Ctrl/Cmd+Enter: "Steer" — jump the queue and send right away. The
      // plain Enter handler below keeps its queueing behavior for busy chats;
      // this modifier path is what lets the user interrupt mid-task.
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        onSubmitSteer?.();
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        onSubmit();
        return;
      }
    },
    [
      deleteSelectionViaModel,
      insertSelectedAtItem,
      insertSelectedSlashItem,
      onSubmit,
      onSubmitSteer,
      redo,
      selectAllContent,
      undo,
    ],
  );

  // Visual placeholder: shown when no segments are present (the editor has no
  // textual content). Browsers can't show a real placeholder on
  // contentEditable, so we use a CSS ``::before`` driven by ``data-empty``.
  const isEmpty = segments.length === 0 ||
    (segments.length === 1 && segments[0].kind === "text" && !segments[0].value.trim());

  return (
    <div className="rich-composer">
      <div
        ref={rootRef}
        className="rich-composer-editor"
        contentEditable
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        data-empty={isEmpty || undefined}
        data-placeholder={placeholder || ""}
        style={{ minHeight: `${rows * 22}px` }}
        onInput={handleInput}
        onKeyDown={handleKeyDown}
        onContextMenu={copyCtx.onContextMenu}
        onCopy={(e) => {
          if (writeSelectionToClipboard(e)) {
            e.preventDefault();
          }
        }}
        onCut={(e) => {
          if (writeSelectionToClipboard(e)) {
            e.preventDefault();
            // Delete through the canonical model (same path as Backspace on a
            // selection) rather than the browser's native ``deleteContents``.
            // Native cut could leave a stray <br> behind AND it raced our
            // ``onChange`` so the pre-cut state was sometimes never recorded on
            // the undo stack — which is why Ctrl+Z often failed to restore the
            // cut text. The model path makes a single, history-tracked change
            // and drops the caret at the gap (delete-text rule).
            deleteSelectionViaModel();
          }
        }}
        onPaste={handlePaste}
        onDragOver={handleDragOver}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        data-drag-over={dragOver || undefined}
        onBlur={() => {
          // Close the slash / '@' popups when the editor loses focus; the
          // popups catch clicks on their own buttons via mousedown.
          window.setTimeout(() => {
            setSlash({ open: false, query: "", selected: 0 });
            setAt({ open: false, query: "", selected: 0 });
          }, 100);
        }}
      />
      {copyCtx.menuNode}
      {slash.open && filteredItems.length > 0 && (
        <div className="rich-composer-popup">
          <div className="rich-composer-popup-hint">{t("composer.slashHint")}</div>
          {filteredItems.map((item, idx) => (
            <button
              key={`${item.kind}:${item.payload}`}
              type="button"
              className={`rich-composer-popup-item ${idx === slash.selected ? "is-active" : ""}`}
              onMouseDown={(e) => {
                // Use mousedown rather than click so the editor doesn't blur
                // first and reset the selection we need to insert at.
                e.preventDefault();
                insertSelectedSlashItem(item);
              }}
            >
              <Icon name={kindIconName(item.kind) as never} size={13} />
              <span className="rich-composer-popup-label">{item.label}</span>
              {item.description && (
                <span className="rich-composer-popup-desc">{item.description}</span>
              )}
            </button>
          ))}
        </div>
      )}
      {at.open && atFiles.length > 0 && (
        <div className="rich-composer-popup">
          <div className="rich-composer-popup-hint">
            {t("composer.atFileHint")}
          </div>
          {atFiles.map((file, idx) => {
            const leaf = file.split(/[\\/]/).pop() || file;
            const dir = file.slice(0, file.length - leaf.length);
            return (
              <button
                key={file}
                type="button"
                className={`rich-composer-popup-item ${idx === at.selected ? "is-active" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  insertSelectedAtItem(file);
                }}
              >
                <Icon name={"paperclip" as never} size={13} />
                <span className="rich-composer-popup-label">{leaf}</span>
                {dir && <span className="rich-composer-popup-desc">{dir}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Render a sent (or history) user message as inline mixed pills + text.
 *  Used by ``UserEntry`` to keep the bubble visually consistent with what
 *  the user sees while composing. */
export function MessageBody({ segments }: { segments: Segment[] }) {
  return (
    <div className="message-body">
      {segments.map((seg, idx) => {
        if (seg.kind === "text") {
          // Preserve newlines so messages stay multi-line.
          return (
            <span key={idx} className="message-text">
              {seg.value}
            </span>
          );
        }
        const info = kindLabel(seg.kind, seg.value);
        return (
          <span
            key={idx}
            className={`composer-pill composer-pill-${seg.kind} composer-pill-readonly`}
            title={seg.value}
          >
            <Icon name={kindIconName(seg.kind) as never} size={11} className="muted-icon" />
            {info.secondary ? (
              <>
                <span className="composer-pill-secondary">{info.secondary}</span>
                <span className="composer-pill-sep">/</span>
                <span className="composer-pill-primary">{info.primary}</span>
              </>
            ) : (
              <span className="composer-pill-primary">{info.primary}</span>
            )}
          </span>
        );
      })}
    </div>
  );
}

// React/TS pacifier: the file exports a ChangeEvent type reference for
// completeness so future editors can attach pasteable handlers (we don't use
// it directly).
export type _MaybeChange = ChangeEvent<HTMLDivElement>;
