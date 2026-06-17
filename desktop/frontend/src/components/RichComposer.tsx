import {
  KeyboardEvent as ReactKeyboardEvent,
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
import { Icon } from "./Icon";

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
  placeholder?: string;
  rows?: number;
}

interface SlashItem {
  kind: TokenKind;
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
  const flushText = () => {
    if (buf) {
      out.push({ kind: "text", value: buf });
      buf = "";
    }
  };
  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      buf += (node.textContent ?? "").replace(/\u200B/g, "");
      continue;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      continue;
    }
    const el = node as HTMLElement;
    const kind = el.getAttribute("data-token-kind");
    if (kind && (kind === "attach" || kind === "skill" || kind === "mcp-tool" || kind === "mcp-prompt")) {
      flushText();
      const payload = el.getAttribute("data-token-payload") || "";
      out.push({ kind: kind as TokenKind, value: payload });
      continue;
    }
    // BR (Enter), or a stray inline span — flatten to text.
    if (el.tagName === "BR") {
      buf += "\n";
    } else {
      buf += (el.textContent ?? "").replace(/\u200B/g, "");
    }
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

function captureCaret(root: HTMLElement): CaretSnapshot | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  if (!root.contains(range.startContainer)) return null;
  const children = Array.from(root.childNodes);
  let segIndex = 0;
  for (let i = 0; i < children.length; i += 1) {
    const child = children[i];
    if (child === range.startContainer || child.contains(range.startContainer)) {
      // Pill node — caret sits next to it. We don't drill into pill internals.
      if (
        child.nodeType === Node.ELEMENT_NODE &&
        (child as HTMLElement).hasAttribute("data-token-kind")
      ) {
        return { segIndex: i, textOffset: 0 };
      }
      // Text-bearing node.
      const text = child.textContent ?? "";
      const offset = Math.min(range.startOffset, text.length);
      return { segIndex, textOffset: offset };
    }
    if (
      child.nodeType === Node.ELEMENT_NODE &&
      (child as HTMLElement).hasAttribute("data-token-kind")
    ) {
      segIndex += 1;
    } else {
      // Each contiguous text-bearing child counts as one text segment in our
      // canonical representation.
      segIndex += 1;
    }
  }
  return null;
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
    // Place caret just after the pill.
    r.setStartAfter(target);
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

function kindIconName(kind: TokenKind): string {
  switch (kind) {
    case "attach":
      return "info";
    case "skill":
      return "list-check";
    case "mcp-tool":
      return "shield";
    case "mcp-prompt":
      return "edit";
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

export function RichComposer({
  segments,
  onChange,
  onSubmit,
  placeholder,
  rows = 3,
}: RichComposerProps) {
  const { getCompletionCatalog, t } = useApp();
  const rootRef = useRef<HTMLDivElement | null>(null);
  // Track which segments are currently in the DOM to avoid redundant rebuilds
  // (and the cursor jumps they cause) while the user is typing.
  const lastRenderedRef = useRef<string>("");
  const [pool, setPool] = useState<SlashItem[]>([]);
  const [slash, setSlash] = useState<{
    open: boolean;
    query: string;
    selected: number;
  }>({ open: false, query: "", selected: 0 });

  // Load catalog once when the composer mounts and refresh it whenever the
  // user opens the slash menu so newly-added skills / reconnected MCP servers
  // become available without a manual refresh.
  useEffect(() => {
    void getCompletionCatalog().then((c) => setPool(buildSlashPool(c)));
  }, [getCompletionCatalog]);

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
    const caret = captureCaret(root);
    // Clear and rebuild.
    while (root.firstChild) {
      root.removeChild(root.firstChild);
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
        const closeBtn = document.createElement("span");
        closeBtn.className = "composer-pill-close";
        closeBtn.setAttribute("role", "button");
        closeBtn.setAttribute("aria-label", "Remove");
        closeBtn.textContent = "×";
        // Use a marker the input handler can detect; the actual deletion is
        // performed via a click listener registered in a separate effect so
        // we don't rebuild listeners on every render.
        closeBtn.setAttribute("data-token-remove", "1");
        pill.appendChild(closeBtn);
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
      lastRenderedRef.current = ""; // force rebuild
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
    // Find the last '/' that follows a boundary (start-of-text or whitespace).
    let slashAt = -1;
    for (let i = upto.length - 1; i >= 0; i -= 1) {
      const ch = upto[i];
      if (ch === "/") {
        const prev = i === 0 ? "" : upto[i - 1];
        if (i === 0 || /\s/.test(prev)) {
          slashAt = i;
        }
        break;
      }
      if (/\s/.test(ch)) {
        break;
      }
    }
    if (slashAt < 0) {
      return { open: false, query: "" };
    }
    return { open: true, query: upto.slice(slashAt + 1) };
  }, []);

  const filteredItems = useMemo(
    () => filterSlashItems(pool, slash.query),
    [pool, slash.query],
  );

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
  }, [computeSlashQuery, onChange]);

  const insertSelectedSlashItem = useCallback(
    (item: SlashItem) => {
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
        if (/\s/.test(before[i])) break;
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
    [onChange],
  );

  const handleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (slash.open && filteredItems.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSlash((p) => ({ ...p, selected: (p.selected + 1) % filteredItems.length }));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSlash((p) => ({
            ...p,
            selected: (p.selected - 1 + filteredItems.length) % filteredItems.length,
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
          insertSelectedSlashItem(filteredItems[slash.selected]);
          return;
        }
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        onSubmit();
        return;
      }
    },
    [filteredItems, insertSelectedSlashItem, onSubmit, slash.open, slash.selected],
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
        onBlur={() => {
          // Close the slash popup when the editor loses focus; the popup
          // catches clicks on its own buttons via mousedown.
          window.setTimeout(() => setSlash({ open: false, query: "", selected: 0 }), 100);
        }}
      />
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
