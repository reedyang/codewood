import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { useApp } from "../state/AppContext";
import { ConsolePanel } from "./ConsolePanel";
import type { HistoryRound, HistoryTurn, Turn, TurnRound } from "../api/types";
import { Icon, type IconName } from "./Icon";
import { MarkdownText } from "./Markdown";
import { StepsView, countToolCalls, getLastToolPromptBody } from "./Steps";
import { ChatTitleBar } from "./ChatTitleBar";
import { AskMoreInfoPanel } from "./AskMoreInfoPanel";
import { ConfirmDialog } from "./ConfirmDialog";
import { chatKey } from "./chatMenu";
import { decodeAttachments } from "../utils/attachments";
import {
  appendImageRefs,
  parseImageRefs,
} from "../utils/imageRefs";
import {
  AttachmentStrip,
  SentImageThumb,
  type PastedImage,
} from "./ImageAttachments";
import {
  composeMessageText,
  decodeSegments,
  encodeHiddenInstruction,
  encodeSegments,
  hasProposedPlan,
  parseMessageToSegments,
  retokenizeReferencePills,
  stripHiddenControl,
  stripPlanModePrefix,
} from "../utils/tokens";
import type { Segment, TokenKind } from "../utils/tokens";
import { RichComposer } from "./RichComposer";
import { shouldShowRoundTimer } from "./chatRoundTimer";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

function refPillIconName(kind: TokenKind): IconName {
  switch (kind) {
    case "skill":
      return "sparkles";
    case "mcp-tool":
      return "wrench";
    case "mcp-prompt":
      return "message-square";
    case "attach":
      return "paperclip";
    default:
      return "info";
  }
}

function refPillLabel(kind: TokenKind, payload: string): string {
  if (kind === "skill") {
    return payload;
  }
  if (kind === "attach") {
    // Show just the file name; the full path stays in the ``title`` tooltip.
    return baseName(payload);
  }
  // mcp-tool / mcp-prompt carry "server::name".
  const [server, name] = payload.split("::");
  return name ? `${server} / ${name}` : payload;
}

/** Render a sent user-message body with inline reference pills so the chat
 *  bubble mirrors the composer's image/text-mixed look for ``[skill: ...]``,
 *  ``[mcp tool: ...]`` and ``[mcp prompt: ...]`` markers instead of leaking
 *  the raw bracket text. Plain text is preserved verbatim. */
/** Serialize the current selection inside a sent-message body into segments,
 *  turning each ``msg-ref-pill`` element back into its token. Walking the
 *  cloned range fragment keeps text and pills in document order so a partial
 *  selection round-trips. */
function readSegmentsFromMessageSelection(
  range: Range,
): Segment[] {
  const frag = range.cloneContents();
  const out: Segment[] = [];
  let textBuf = "";
  const flush = () => {
    if (textBuf) {
      out.push({ kind: "text", value: textBuf });
      textBuf = "";
    }
  };
  const visit = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      textBuf += node.textContent ?? "";
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return;
    }
    const el = node as HTMLElement;
    const kind = el.getAttribute("data-token-kind");
    const payload = el.getAttribute("data-token-payload");
    if (kind && payload != null) {
      flush();
      out.push({ kind: kind as TokenKind, value: payload });
      return;
    }
    for (const child of Array.from(node.childNodes)) {
      visit(child);
    }
  };
  for (const child of Array.from(frag.childNodes)) {
    visit(child);
  }
  flush();
  return out;
}

/** Copy handler for sent-message bodies. When the selection contains reference
 *  pills we write BOTH a human-readable form (with the inline ATTACH envelope,
 *  so the LLM/other apps see something sensible) and our private segment
 *  envelope, so pasting back into the composer restores the pills instead of
 *  dropping them to plain label text. */
function handleMessageBodyCopy(e: ReactClipboardEvent<HTMLDivElement>): void {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
    return;
  }
  const range = sel.getRangeAt(0);
  const segs = readSegmentsFromMessageSelection(range);
  if (!segs.some((s) => s.kind !== "text")) {
    // Plain-text-only selection: let the browser handle it natively.
    return;
  }
  try {
    e.clipboardData.setData("text/plain", composeMessageText(segs));
    e.clipboardData.setData("application/x-codewood-segments", encodeSegments(segs));
    e.preventDefault();
  } catch {
    // Fall back to the native copy if the clipboard rejects our payload.
  }
}

/** Render a sent message body that may contain inline image references,
 *  splitting on the image sentinels and rendering each as a thumbnail while the
 *  surrounding prose flows through the normal ``MessageBody`` pill renderer. */
function MessageBodyWithImages({ text }: { text: string }) {
  const parts = parseImageRefs(text);
  const hasImage = parts.some((p) => p.kind === "image");
  if (!hasImage) {
    return <MessageBody text={text} />;
  }
  const images = parts.filter(
    (p): p is { kind: "image"; path: string } => p.kind === "image",
  );
  const prose = parts
    .filter((p): p is { kind: "text"; text: string } => p.kind === "text")
    .map((p) => p.text)
    .join("")
    .trim();
  return (
    <>
      <div className="message-images">
        {images.map((img, i) => (
          <SentImageThumb key={`${img.path}-${i}`} path={img.path} />
        ))}
      </div>
      {prose && <MessageBody text={prose} />}
    </>
  );
}

function MessageBody({ text }: { text: string }) {
  // Decode inline tokens (ATTACH sentinels) first, then re-tokenize the
  // readable reference-pill markers inside each text run, so file/skill/mcp
  // pills all render inline at their authored position and participate in text
  // selection like the surrounding prose.
  const segments: Segment[] = [];
  for (const seg of decodeSegments(text)) {
    if (seg.kind !== "text") {
      segments.push(seg);
      continue;
    }
    for (const inner of retokenizeReferencePills(seg.value)) {
      segments.push(inner);
    }
  }
  const hasPill = segments.some((s) => s.kind !== "text");
  if (!hasPill) {
    return <div className="entry-text">{text}</div>;
  }
  return (
    <div className="entry-text" onCopy={handleMessageBodyCopy}>
      {segments.map((seg, i) => {
        if (seg.kind === "text") {
          return <span key={i}>{seg.value}</span>;
        }
        const kind = seg.kind as TokenKind;
        return (
          <span
            key={i}
            className={`msg-ref-pill msg-ref-pill-${kind}`}
            data-token-kind={kind}
            data-token-payload={seg.value}
            title={kind === "attach" ? seg.value : refPillLabel(kind, seg.value)}
          >
            <Icon name={refPillIconName(kind)} size={12} />
            <span className="msg-ref-pill-label">
              {refPillLabel(kind, seg.value)}
            </span>
          </span>
        );
      })}
    </div>
  );
}

interface ModelGroup {
  provider: string;
  items: { selector: string; name: string }[];
}

/** Group "provider/name" model selectors under their provider, preserving order. */
export function groupModelsByProvider(selectors: string[]): ModelGroup[] {
  const groups: ModelGroup[] = [];
  const byProvider = new Map<string, ModelGroup>();
  for (const sel of selectors) {
    const idx = sel.indexOf(":");
    const provider = idx > 0 ? sel.slice(0, idx) : "Other";
    const name = idx > 0 ? sel.slice(idx + 1) : sel;
    let group = byProvider.get(provider);
    if (!group) {
      group = { provider, items: [] };
      byProvider.set(provider, group);
      groups.push(group);
    }
    group.items.push({ selector: sel, name });
  }
  return groups;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function fileExt(path: string): string {
  const name = baseName(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "";
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

/** Render a message time as e.g. "Jun 1, 6:11 PM" (locale-aware). */
function formatMessageTime(ms?: number): string {
  if (ms == null || Number.isNaN(ms)) {
    return "";
  }
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function parseHistoryTime(value?: string): number | undefined {
  if (!value) {
    return undefined;
  }
  const ts = Date.parse(value.replace(" ", "T"));
  return Number.isNaN(ts) ? undefined : ts;
}

interface MessageHandlers {
  onCopy: (text: string) => void;
  onFork: (index: number) => void;
  onEdit: (index: number, text: string) => void;
}

/** A user message with a hover-only action row (timestamp + copy/fork/edit).
 *  `index` is the from-end genuine-user index (e.g. -1 = last) used to address
 *  this turn for the backend `/chat fork|edit` commands. */
function UserEntry({
  text,
  timeMs,
  index,
  handlers,
}: {
  text: string;
  timeMs?: number;
  index: number;
  handlers: MessageHandlers;
}) {
  const { t } = useApp();
  const [copied, setCopied] = useState(false);
  const time = formatMessageTime(timeMs);
  const canAct = index < 0;
  // Legacy chat records (older versions) prepended a Plan-mode directive to
  // recorded user messages while ``_plan_mode_sticky`` was on. New records no
  // longer carry it (the directive is appended only to the model-facing send),
  // but we still strip a leading directive here so legacy bubbles don't show it
  // as if the user typed it. Strip it BEFORE decoding attachments: in those old
  // records the directive sat ahead of the ``\uE100ATTACH:..\uE101`` envelope,
  // which would otherwise push the attachment tokens off the start of the
  // string and make them leak into the body as garbled text.
  const { paths: attachedPaths, body } = decodeAttachments(stripPlanModePrefix(text));
  // ``stripHiddenControl`` then unwraps the CONTROL envelope used by the GUI's
  // own "Execute now" nudge.
  const visibleBody = stripHiddenControl(stripPlanModePrefix(body));
  // A message that consists ONLY of a CONTROL envelope (e.g. the GUI's
  // "Execute now" nudge) becomes invisible in the chat transcript — we
  // don't render an empty bubble for it, since the user never typed it.
  if (!visibleBody && attachedPaths.length === 0) {
    return null;
  }
  return (
    <div className="user-message">
      <div className="entry-input">
        <span className="entry-label">{t("chat.you")}</span>
        {attachedPaths.length > 0 && (
          <div className="attachments attachments-readonly">
            {attachedPaths.map((path) => (
              <span className="attachment-chip" key={path} title={path}>
                <Icon name="info" size={13} className="muted-icon" />
                <span className="attachment-name">{baseName(path)}</span>
                <span className="attachment-ext">{fileExt(path)}</span>
              </span>
            ))}
          </div>
        )}
        {visibleBody && <MessageBodyWithImages text={visibleBody} />}
      </div>
      <div className="entry-actions">
        <span className="entry-time">{time}</span>
        <div className="entry-action-buttons">
          <button
            className="entry-action-btn"
            title={t("msg.copy")}
            aria-label={t("msg.copy")}
            onClick={() => {
              // Copy the user-visible body, not the sentinel-wrapped envelope.
              handlers.onCopy(visibleBody || text);
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1200);
            }}
          >
            <Icon name={copied ? "check" : "copy"} size={14} />
          </button>
          <button
            className="entry-action-btn"
            title={t("msg.fork")}
            aria-label={t("msg.fork")}
            disabled={!canAct}
            onClick={() => handlers.onFork(index)}
          >
            <Icon name="fork" size={14} />
          </button>
          <button
            className="entry-action-btn"
            title={t("msg.edit")}
            aria-label={t("msg.edit")}
            disabled={!canAct}
            onClick={() => handlers.onEdit(index, text)}
          >
            <Icon name="edit" size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

const POLICIES = ["unlimited", "moderate", "confirmation"] as const;

type ChatMode = "agent" | "plan";

/** Plus-button menu in the composer: attach files, switch between Agent and
 *  Plan modes. The Plan mode is GUI-only — selecting it injects a planning
 *  instruction at send time and lights up a "Plan" badge so the user can see
 *  at a glance which mode the next message will go out under. */
function ComposerPlusMenu({
  onAttach,
  mode,
  onChangeMode,
}: {
  onAttach: () => void;
  mode: ChatMode;
  onChangeMode: (m: ChatMode) => void;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(open, () => setOpen(false));
  return (
    <div className="dropdown composer-plus-menu" ref={ref}>
      <button
        className={`icon-btn round ${mode === "plan" ? "is-plan" : ""}`}
        aria-label={t("composer.plusMenu")}
        title={t("composer.plusMenu")}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="plus" size={16} />
      </button>
      {open && (
        <div className="dropdown-menu">
          <button
            className="dropdown-item"
            onClick={() => {
              setOpen(false);
              onAttach();
            }}
          >
            <span className="dropdown-check" />
            <span>{t("attach.add")}</span>
          </button>
          <div className="dropdown-divider" />
          <button
            className={`dropdown-item ${mode === "agent" ? "active" : ""}`}
            onClick={() => {
              setOpen(false);
              onChangeMode("agent");
            }}
          >
            <span className="dropdown-check">
              {mode === "agent" && <Icon name="check" size={13} />}
            </span>
            <span>{t("composer.modeAgent")}</span>
          </button>
          <button
            className={`dropdown-item ${mode === "plan" ? "active" : ""}`}
            onClick={() => {
              setOpen(false);
              onChangeMode("plan");
            }}
          >
            <span className="dropdown-check">
              {mode === "plan" && <Icon name="check" size={13} />}
            </span>
            <span>{t("composer.modePlan")}</span>
          </button>
        </div>
      )}
    </div>
  );
}

function useOutsideClose(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointer = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    window.addEventListener("mousedown", onPointer);
    return () => window.removeEventListener("mousedown", onPointer);
  }, [open, onClose]);
  return ref;
}

export function ChatView() {
  const {
    state,
    activeWorkspaceId,
    activeChatId,
    activeChats,
    turns,
    historyTurns,
    historyStart,
    historyLoading,
    loadOlderHistory,
    busy,
    now,
    sendInput,
    pasteImage,
    chatImageUrl,
    interrupt,
    setExecutionPolicy,
    setModel,
    setReasoning,
    pickFiles,
    forkChat,
    editChat,
    setPlanMode,
    draftMode,
    draftWorkspaceId,
    setDraftWorkspace,
    askMoreInfo,
    answerAskMoreInfo,
    consoleOpen,
    t,
  } = useApp();
  // Drafts (in-progress composer segments) are kept per chat so switching
  // between chats never bleeds an unsent message into a sibling. A synthetic
  // key is used while we're still in "draft mode" (no chat exists yet) so
  // that first composition survives until the user sends or discards.
  const DRAFT_KEY = "__draft__";
  const draftKey = draftMode ? DRAFT_KEY : chatKey(activeWorkspaceId, activeChatId);
  const [segmentsByChat, setSegmentsByChat] = useState<Record<string, Segment[]>>({});
  const segments = segmentsByChat[draftKey] ?? [];
  // Pending pasted-image attachments for the active draft, keyed by chat so
  // switching chats never bleeds one chat's attachments into another. Each
  // entry holds the saved on-disk path (sent to the model as an inline
  // reference) plus the data URL used to render the thumbnail before send.
  const [imageAttachmentsByChat, setImageAttachmentsByChat] = useState<
    Record<string, PastedImage[]>
  >({});
  const imageAttachments = imageAttachmentsByChat[draftKey] ?? [];
  // Per-chat compose mode (Agent or Plan). The backend records a sticky
  // Plan-mode flag on each chat record root (``planMode`` in the chat
  // summary), so the mode survives an app restart: we seed each chat's
  // entry from that persisted flag the first time we encounter it, then
  // track in-session toggles locally. User toggles are written back to the
  // backend (see ``setPlanMode``), keeping the persisted flag in step.
  const [chatModeMap, setChatModeMap] = useState<Record<string, ChatMode>>({});
  // Per-chat record of the plan text the user explicitly dismissed via
  // "No, and tell <App> what to do differently". The plan chooser is otherwise
  // driven purely by "does the latest assistant message contain a
  // <proposed_plan> block" — which stays true after the user clicks No (the
  // message is unchanged), so without this the buttons would never disappear
  // and clicking No would look like a no-op. We hide the chooser while the
  // latest plan equals the dismissed one; a newly proposed (different) plan
  // re-surfaces it.
  const [dismissedPlanMap, setDismissedPlanMap] = useState<
    Record<string, string>
  >({});
  // Chat ids already seeded from the persisted ``planMode`` flag, so a later
  // state refresh never overrides an in-session toggle the user just made.
  const seededPlanModeRef = useRef<Set<string>>(new Set());
  const chatMode: ChatMode = chatModeMap[draftKey] ?? "agent";
  const setChatMode = (m: ChatMode) =>
    setChatModeMap((prev) => ({ ...prev, [draftKey]: m }));
  const setSegments = (
    value: Segment[] | ((prev: Segment[]) => Segment[]),
  ) => {
    setSegmentsByChat((prev) => {
      const current = prev[draftKey] ?? [];
      const next =
        typeof value === "function"
          ? (value as (p: Segment[]) => Segment[])(current)
          : value;
      return { ...prev, [draftKey]: next };
    });
  };
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const prevHeightRef = useRef<number | null>(null);
  // Whether the transcript should auto-stick to the bottom on live updates.
  // Starts true (a fresh/switched chat opens pinned to the latest message)
  // and flips off the moment the user scrolls away from the bottom, so an
  // in-flight streaming turn can't yank them back down while they read
  // earlier messages. Re-pins as soon as they scroll back to the bottom.
  const stickToBottomRef = useRef<boolean>(true);

  // Seed each chat's compose mode from the backend's persisted Plan-mode flag
  // the first time we see it (e.g. after an app restart). Only seeds chats not
  // yet tracked locally, so an in-session toggle is never overridden by a later
  // state refresh.
  useEffect(() => {
    const chats = activeChats;
    if (chats.length === 0) {
      return;
    }
    const seeds: Record<string, ChatMode> = {};
    for (const c of chats) {
      const id = chatKey(activeWorkspaceId, c?.id || "");
      if (!id || seededPlanModeRef.current.has(id)) {
        continue;
      }
      seededPlanModeRef.current.add(id);
      if (c.planMode) {
        seeds[id] = "plan";
      }
    }
    if (Object.keys(seeds).length > 0) {
      setChatModeMap((prev) => ({ ...seeds, ...prev }));
    }
  }, [activeChats, activeWorkspaceId]);

  // Sync the agent's sticky plan-mode flag with the active chat's mode so
  // that switching back into a chat that was last left in Plan mode keeps
  // the runtime injection in step with the visible "Plan" badge. The per-chat
  // mode is persisted on the backend (see the seeding effect above), so a
  // fresh launch resumes each chat in the mode it was last left in.
  useEffect(() => {
    void setPlanMode(chatMode === "plan");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey, chatMode]);

  // Derived view of just the attachment paths for callers that still want a
  // flat list (the empty-state workspace selector, the send-button enabling
  // check). Order matches their order in the composer.
  const attachmentPaths = segments
    .filter((s) => s.kind === "attach")
    .map((s) => s.value);
  const draftText = segments
    .filter((s) => s.kind === "text")
    .map((s) => s.value)
    .join("");

  const messageHandlers: MessageHandlers = {
    onCopy: (text) => {
      void navigator.clipboard?.writeText(text);
    },
    onEdit: (index, text) => {
      // Pull inline pasted-image references out first so they return to the
      // composer's thumbnail strip (the way they were shown before sending),
      // rather than re-appearing as raw sentinel text. Directly-attached image
      // FILES travel in the ATTACH envelope and are intentionally left in the
      // segment list so they keep rendering as file pills.
      const parts = parseImageRefs(text);
      const imgPaths = parts
        .filter((p): p is { kind: "image"; path: string } => p.kind === "image")
        .map((p) => p.path);
      const rest = parts
        .filter((p): p is { kind: "text"; text: string } => p.kind === "text")
        .map((p) => p.text)
        .join("");
      // Restore the original segment list (sans image refs) so the composer
      // re-renders the same attachment chips and prose that the user sent.
      setSegments(parseMessageToSegments(rest));
      const key = draftKey;
      setImageAttachmentsByChat((prev) => ({
        ...prev,
        [key]: imgPaths.map((path) => ({
          path,
          name: path.split(/[\\/]/).pop() || "image",
          dataUrl: chatImageUrl(path),
        })),
      }));
      void editChat(index);
    },
    onFork: (index) => {
      void forkChat(index);
    },
  };

  // Live updates stick to the bottom ONLY while the user is already pinned
  // there. If they scrolled up to read earlier messages, streaming tokens
  // and the per-second timer tick must leave their viewport alone.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [turns, now]);

  // When history turns change: a prepend (older page) preserves the viewport;
  // a replacement (initial load / switch) jumps to the bottom.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) {
      return;
    }
    if (prevHeightRef.current != null) {
      // Older page prepended: preserve the viewport (the user is reading
      // up, so keep them away from the bottom — don't re-pin).
      el.scrollTop = el.scrollHeight - prevHeightRef.current;
      prevHeightRef.current = null;
    } else {
      // Initial load / chat switch: jump to the latest message and re-pin.
      el.scrollTop = el.scrollHeight;
      stickToBottomRef.current = true;
    }
  }, [historyTurns]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) {
      return;
    }
    // Re-evaluate whether the user is pinned to the bottom. A small
    // threshold tolerates sub-pixel rounding and the in-flight growth of
    // the streaming turn. Once unpinned, live updates stop following.
    const distanceFromBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distanceFromBottom <= 80;

    if (historyLoading || historyStart <= 0) {
      return;
    }
    if (el.scrollTop < 80) {
      prevHeightRef.current = el.scrollHeight;
      void loadOlderHistory();
    }
  };

  // Upload each pasted bitmap and add it to the active draft's attachment
  // strip. Failures are dropped silently (the backend rejects bad mime / size).
  const onPasteImages = (dataUrls: string[]) => {
    const key = draftKey;
    void Promise.all(
      dataUrls.map(async (dataUrl) => {
        const saved = await pasteImage(dataUrl);
        if (!saved) {
          return null;
        }
        return { path: saved.path, name: saved.name, dataUrl } as PastedImage;
      }),
    ).then((results) => {
      const additions = results.filter((r): r is PastedImage => r != null);
      if (additions.length === 0) {
        return;
      }
      setImageAttachmentsByChat((prev) => {
        const existing = prev[key] ?? [];
        const seen = new Set(existing.map((a) => a.path));
        const merged = [...existing];
        for (const a of additions) {
          if (!seen.has(a.path)) {
            seen.add(a.path);
            merged.push(a);
          }
        }
        return { ...prev, [key]: merged };
      });
    });
  };

  const removeImageAttachment = (path: string) => {
    const key = draftKey;
    setImageAttachmentsByChat((prev) => {
      const existing = prev[key] ?? [];
      const next = existing.filter((a) => a.path !== path);
      return { ...prev, [key]: next };
    });
  };

  const canSend =
    draftText.trim().length > 0 ||
    attachmentPaths.length > 0 ||
    imageAttachments.length > 0;

  const submit = async () => {
    if (!canSend) {
      return;
    }
    // While an request_user_input prompt is pending, the turn is paused waiting on
    // the user's answer — it is NOT accepting a fresh prompt (that would just
    // queue behind the blocked turn and never run). So a composer send here
    // is the user's custom freeform answer: route the typed text straight to
    // ``answerAskMoreInfo`` so the blocked turn resumes with it. Attachments /
    // skill tokens have no meaning as a clarification, so we use the plain
    // text body only.
    if (askMoreInfo) {
      const answer = draftText.trim();
      if (!answer) {
        return;
      }
      setSegments([]);
      await answerAskMoreInfo(answer);
      return;
    }
    // Build the over-the-wire string from the composer segments. Attachments
    // go into the legacy ATTACH header (so the agent + chat history layer can
    // still pick them off the front of the message); everything else flows
    // into the body, with skill / MCP tokens flattened to readable inline
    // markers the LLM can reason about naturally. The planning instruction
    // is NOT prefixed to the visible message: instead we toggle the agent's
    // sticky plan-mode flag so the runtime loop injects the instruction on
    // the agent side. That keeps the user's bubble showing only what they
    // actually typed.
    await setPlanMode(chatMode === "plan");
    const baseMessage = composeMessageText(segments);
    // Append each pasted image as an inline path reference the model can
    // resolve with ``read``; the GUI re-renders these as thumbnails.
    const message = appendImageRefs(
      baseMessage,
      imageAttachments.map((a) => a.path),
    );
    const key = draftKey;
    setSegments([]);
    setImageAttachmentsByChat((prev) => ({ ...prev, [key]: [] }));
    await sendInput(message);
  };

  // Continue a plan-mode turn by clicking "Execute now". We disable plan
  // mode for the follow-up so the agent moves from planning to execution,
  // and we send a short prompt asking it to carry out the plan. The exact
  // wording is localized and intentionally short — the model already has
  // the plan in context.
  const continueFromPlan = async () => {
    await setPlanMode(false);
    setChatMode("agent");
    // Wrap the proceed-instruction in a CONTROL envelope so the GUI hides
    // it from the chat bubble; the agent still receives the inner text as
    // part of the message body and can respond as if the user said it.
    const prompt = t("composer.executePlanPrompt");
    await sendInput(encodeHiddenInstruction(prompt));
  };

  // "No, and tell <App> what to do differently": stay in Plan mode so the
  // user's next composer message refines the plan rather than executing it
  // (mirrors the TUI's free-text revise path), and dismiss the chooser for the
  // current plan so the buttons disappear and the user can type their revision.
  // Defensive: ensure Plan mode is still on in case it drifted.
  const keepRefiningPlan = async (planText: string) => {
    const key = draftKey;
    setDismissedPlanMap((prev) => ({ ...prev, [key]: planText }));
    setChatMode("plan");
    await setPlanMode(true);
  };

  const addFiles = async () => {
    const picked = await pickFiles();
    if (picked.length === 0) {
      return;
    }
    setSegments((prev) => {
      const existing = new Set(
        prev.filter((s) => s.kind === "attach").map((s) => s.value),
      );
      const additions: Segment[] = [];
      for (const p of picked) {
        if (!existing.has(p)) {
          existing.add(p);
          additions.push({ kind: "attach", value: p });
        }
      }
      if (additions.length === 0) return prev;
      // Insert new attachments at the very start so they show as a header of
      // pills, matching the visual convention from the previous chip strip.
      return [...additions, ...prev];
    });
  };

  const currentPolicy = state?.executionPolicy || "moderate";
  const currentModel = state?.model.current || "";
  const models = state?.model.available ?? [];
  const reasoningEfforts = state?.model.reasoningEfforts ?? [];
  const reasoningEffort = state?.model.reasoningEffort || "";

  // From-end genuine-user indices (e.g. -1 = last user turn) so Fork/Edit can
  // address a turn the same way the TUI `/chat fork|edit <index>` commands do.
  const histUserCount = historyTurns.reduce((n, h) => n + (h.userText ? 1 : 0), 0);
  const liveUserCount = turns.reduce((n, tn) => n + (tn.userText ? 1 : 0), 0);
  const totalUser = histUserCount + liveUserCount;
  const histNeg: number[] = [];
  {
    let seen = 0;
    for (const h of historyTurns) {
      histNeg.push(h.userText ? -(totalUser - seen) : 0);
      if (h.userText) {
        seen += 1;
      }
    }
  }
  const liveNeg: number[] = [];
  {
    let seen = histUserCount;
    for (const tn of turns) {
      liveNeg.push(tn.userText ? -(totalUser - seen) : 0);
      if (tn.userText) {
        seen += 1;
      }
    }
  }

  // While an ``request_user_input`` prompt is pending the agent is paused waiting
  // on the user's selection — it isn't actively working — so the action
  // button must revert to "send" (not the interrupt/stop affordance) even
  // though the backend busy flag is still set for the turn.
  const stopMode = busy && !askMoreInfo;

  const composer = (
    <div className="composer">
      <AttachmentStrip
        images={imageAttachments}
        onRemove={removeImageAttachment}
      />
      <RichComposer
        segments={segments}
        onChange={setSegments}
        onSubmit={() => void submit()}
        placeholder={t("chat.inputPlaceholder")}
        rows={3}
        onPasteImages={onPasteImages}
      />
      <div className="composer-toolbar">
        <div className="composer-left">
          <ComposerPlusMenu
            onAttach={() => void addFiles()}
            mode={chatMode}
            onChangeMode={(m) => {
              setChatMode(m);
              // Keep the backend's sticky flag aligned with the GUI's
              // per-chat mode so the runtime loop sees the right value
              // even before the user hits Send.
              void setPlanMode(m === "plan");
            }}
          />
          {chatMode === "plan" ? (
            <span className="mode-badge mode-badge-plan" title={t("composer.modePlanHint")}>
              {t("composer.modePlan")}
            </span>
          ) : (
            <span className="mode-badge mode-badge-agent" title={t("composer.modeAgentHint")}>
              {t("composer.modeAgent")}
            </span>
          )}
          <Dropdown
            trigger={
              <>
                <Icon name="shield" size={14} />
                <span>{t(`settings.policy.${currentPolicy}`)}</span>
              </>
            }
            className="policy-dropdown"
          >
            {(close) =>
              POLICIES.map((p) => (
                <button
                  key={p}
                  className={`dropdown-item ${p === currentPolicy ? "active" : ""}`}
                  onClick={() => {
                    close();
                    void setExecutionPolicy(p);
                  }}
                >
                  <span className="dropdown-check">
                    {p === currentPolicy && <Icon name="check" size={13} />}
                  </span>
                  <span>{t(`settings.policy.${p}`)}</span>
                </button>
              ))
            }
          </Dropdown>
        </div>
        <div className="composer-right">
          {models.length > 0 && (
            <ModelMenu
              models={models}
              currentModel={currentModel}
              reasoningEfforts={reasoningEfforts}
              reasoningEffort={reasoningEffort}
              onSelectModel={(selector) => void setModel(selector)}
              onSelectReasoning={(level) => void setReasoning(level)}
            />
          )}
          {state?.contextUsage && state.contextUsage.window > 0 && (
            <ContextUsageRing
              percent={state.contextUsage.percent}
              tokens={state.contextUsage.tokens}
              window={state.contextUsage.window}
              label={t("context.usage")}
            />
          )}
          <button
            className={`send-btn ${stopMode ? "is-stop" : ""}`}
            aria-label={stopMode ? t("chat.interrupt") : t("chat.send")}
            disabled={!stopMode && !canSend}
            onClick={() => (stopMode ? void interrupt() : void submit())}
          >
            <Icon name={stopMode ? "stop" : "send"} size={16} />
          </button>
        </div>
      </div>
    </div>
  );

  const showEmpty =
    draftMode ||
    (turns.length === 0 && historyTurns.length === 0 && !historyLoading);
  // In draft mode the greeting reflects the chosen draft workspace; otherwise
  // it reflects the active workspace. The Default workspace is not a real
  // project, so omit its name from the greeting.
  const emptyContent = (() => {
    const workspaces = state?.workspaces ?? [];
    const targetWs = draftMode
      ? workspaces.find((w) => w.id === draftWorkspaceId)
      : workspaces.find((w) => w.id === activeWorkspaceId);
    const inDefaultWs = !targetWs || targetWs.isDefault;
    const workspaceName = targetWs?.name || state?.workspace.name || "";
    const emptyTitle = inDefaultWs
      ? t("empty.promptNoWorkspace")
      : t("empty.prompt").replace("{workspace}", workspaceName);
    return (
      <div className="empty-state">
        <h1 className="empty-title">{emptyTitle}</h1>
        <div className="empty-composer">
          {composer}
          <WorkspaceSelector
            draft={draftMode}
            draftWorkspaceId={draftWorkspaceId}
            onPickDraft={setDraftWorkspace}
          />
        </div>
      </div>
    );
  })();

  return (
    <div className="chat-view">
      {showEmpty ? emptyContent : (
        <>
          <ChatTitleBar />
          <div className="transcript" ref={scrollRef} onScroll={onScroll}>
            {historyStart > 0 && (
              <div className="history-more">
                {historyLoading ? t("history.loading") : t("history.more")}
              </div>
            )}
            {historyTurns.map((turn, index) => (
              <HistoryTurnView
                key={`h-${index}`}
                turn={turn}
                negIndex={histNeg[index]}
                handlers={messageHandlers}
              />
            ))}
            {turns.map((turn, index) => (
              <TurnView
                key={turn.id}
                turn={turn}
                now={now}
                negIndex={liveNeg[index]}
                handlers={messageHandlers}
              />
            ))}
            <AskMoreInfoPanel />
            <ConfirmDialog />
            {(() => {
              // The Execute-now button represents "carry out the plan we just
              // drafted". It surfaces in BOTH Plan and Agent mode, but only when
              // the LAST assistant message still carries an unfinished
              // ``<proposed_plan>`` block (computed as ``latestPlanText`` below).
              // That "last message only" rule is what keeps an Agent-mode plan
              // artifact buried mid-conversation from spuriously showing the
              // button: once the agent replies again, the plan is no longer the
              // tail and the button disappears on its own.
              //
              // We still wait until any streaming turn has fully closed so the
              // button doesn't appear before the rendered plan content lands.
              // After an app restart there are no live ``turns`` (the plan turn
              // lives in ``historyTurns`` instead), so we must NOT require a live
              // turn — a rendered tail plan plus an idle agent is enough.
              if (busy) {
                return null;
              }
              const lastLive = turns.length > 0 ? turns[turns.length - 1] : null;
              if (lastLive && lastLive.endedAt === null) {
                return null;
              }
              if (turns.length === 0 && historyTurns.length === 0) {
                return null;
              }
              // A pending request_user_input prompt always wins: the agent is
              // waiting on the user's selection, so showing Execute-now
              // would misrepresent the state and let the user advance the
              // plan instead of answering the question.
              if (askMoreInfo) {
                return null;
              }
              // Plan-ready signal: the latest assistant text carries a finished
              // ``<proposed_plan>`` block (Plan mode no longer uses update_plan).
              const lastLiveText = (() => {
                for (let ti = turns.length - 1; ti >= 0; ti--) {
                  const rounds = turns[ti].rounds;
                  for (let ri = rounds.length - 1; ri >= 0; ri--) {
                    const ans = rounds[ri].segments
                      .filter((s) => s.kind === "answer")
                      .map((s) => s.text)
                      .join("");
                    if (ans.trim().length > 0) return ans;
                  }
                }
                return "";
              })();
              const lastHistText = (() => {
                for (let ti = historyTurns.length - 1; ti >= 0; ti--) {
                  const rounds = historyTurns[ti].rounds;
                  for (let ri = rounds.length - 1; ri >= 0; ri--) {
                    if (rounds[ri].text.trim().length > 0) return rounds[ri].text;
                  }
                }
                return "";
              })();
              // "Last message only": the button is tied strictly to the tail
              // assistant message. When a live turn has produced any answer it is
              // the tail, so a stale plan further back in history must NOT count;
              // only fall back to history text when there is no live answer at all.
              const tailText = lastLiveText.trim().length > 0 ? lastLiveText : lastHistText;
              const latestPlanText = hasProposedPlan(tailText) ? tailText : "";
              if (!latestPlanText) {
                return null;
              }
              // Hide the chooser once the user dismissed this exact plan via "No".
              if (dismissedPlanMap[draftKey] === latestPlanText) {
                return null;
              }
              return (
                <div className="plan-execute-row">
                  <button
                    type="button"
                    className="btn btn-primary plan-execute-btn"
                    onClick={() => void continueFromPlan()}
                  >
                    <Icon name="send" size={13} />
                    <span>{t("composer.implementPlan")}</span>
                  </button>
                  <button
                    type="button"
                    className="btn plan-revise-btn"
                    onClick={() => void keepRefiningPlan(latestPlanText)}
                  >
                    <span>
                      {t("composer.revisePlan").replace(
                        "{app}",
                        state?.app.name || "the assistant",
                      )}
                    </span>
                  </button>
                </div>
              );
            })()}
          </div>
          <div className="composer-dock">{composer}</div>
        </>
      )}
      <ConsoleDock open={consoleOpen} />
    </div>
  );
}

const CONSOLE_HEIGHT_KEY = "codewood.consoleHeight";
const CONSOLE_MIN_HEIGHT = 120;
const CONSOLE_MAX_HEIGHT = 720;

function loadConsoleHeight(): number {
  const raw = Number(window.localStorage.getItem(CONSOLE_HEIGHT_KEY));
  if (Number.isFinite(raw) && raw >= CONSOLE_MIN_HEIGHT && raw <= CONSOLE_MAX_HEIGHT) {
    return raw;
  }
  return 240;
}

/** The console dock below the transcript: a draggable top divider resizes it
 *  (dragging up grows it) and the persisted height survives reloads. */
function ConsoleDock({ open }: { open: boolean }) {
  const [height, setHeight] = useState(loadConsoleHeight);
  const [resizing, setResizing] = useState(false);

  useEffect(() => {
    window.localStorage.setItem(CONSOLE_HEIGHT_KEY, String(height));
  }, [height]);

  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = height;
    setResizing(true);
    document.body.classList.add("resizing-y");
    const onMove = (ev: MouseEvent) => {
      // Dragging up (negative delta) grows the console.
      const next = Math.min(
        CONSOLE_MAX_HEIGHT,
        Math.max(CONSOLE_MIN_HEIGHT, startHeight - (ev.clientY - startY)),
      );
      setHeight(next);
    };
    const onUp = () => {
      setResizing(false);
      document.body.classList.remove("resizing-y");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div className={`console-dock ${open ? "" : "collapsed"}`} style={{ height: open ? `${height}px` : "0px" }}>
      <div
        className={`console-resizer ${resizing ? "resizing" : ""}`}
        role="separator"
        aria-orientation="horizontal"
        onMouseDown={startResize}
      />
      <ConsolePanel dockOpen={open} />
    </div>
  );
}

/** One model round laid out in natural order. The round can render either as
 *  "answer first, then tools" (live streaming) or "collapsed details first,
 *  then the final paragraph" (history / completed rounds). */
export function RoundShell({
  timerText,
  expandedTimerText,
  running,
  showTimer,
  autoExpand,
  detailsBeforeText = false,
  detailsNode,
  textNode,
}: {
  timerText: string;
  expandedTimerText?: string;
  running: boolean;
  showTimer: boolean;
  autoExpand: boolean;
  detailsBeforeText?: boolean;
  detailsNode: ReactNode;
  textNode: ReactNode;
}) {
  const { t } = useApp();
  const hasDetails = Boolean(detailsNode);
  const [expanded, setExpanded] = useState(autoExpand && hasDetails);
  useEffect(() => {
    setExpanded(autoExpand && hasDetails);
  }, [autoExpand, hasDetails]);

  const timer = showTimer ? (
    <div className="activity">
      <button
        className={`activity-header ${running ? "running" : ""} ${hasDetails ? "has-details" : ""}`}
        onClick={() => hasDetails && setExpanded((v) => !v)}
        disabled={!hasDetails}
      >
        <span className={`activity-text ${running ? "marquee" : ""}`}>
          {hasDetails && expanded && expandedTimerText ? expandedTimerText : timerText}
        </span>
        {hasDetails && (
          <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
        )}
      </button>
      {hasDetails && expanded && (
        <>
          {detailsNode}
          <button
            className="activity-collapse"
            onClick={() => setExpanded(false)}
            title={t("activity.collapse")}
            aria-label={t("activity.collapse")}
          >
            <Icon name="chevron" size={14} className="chevron up" />
          </button>
        </>
      )}
    </div>
  ) : null;
  return (
    <div className="turn-round">
      {detailsBeforeText ? timer : textNode}
      {!detailsBeforeText && timer}
      {detailsBeforeText && textNode}
    </div>
  );
}

export function HistoryRoundDetailView({
  round,
  showText = true,
}: {
  round: HistoryRound;
  showText?: boolean;
}) {
  const { t } = useApp();
  const thinkingText = String(round.thinking || "");
  const toolText = String(round.tools || "");
  const toolCount = countToolCalls(toolText);
  const hasToolShell = toolText.trim().length > 0;
  const thinkingNode = thinkingText.trim().length > 0 ? (
    <ThinkingPanel
      thinkingText={thinkingText}
      running={false}
      timerText={`${t("activity.thoughtFor")} ${formatElapsed(round.waitSeconds * 1000)}`}
    />
  ) : null;
  const textNode = String(round.text || "").trim().length > 0 ? (
    <div className="answer">
      <MarkdownText text={String(round.text || "")} />
    </div>
  ) : null;
  const visibleTextNode = showText ? textNode : null;

  if (hasToolShell) {
    return (
      <>
        {thinkingNode}
        <RoundShell
          timerText={t("activity.toolCalls").replace("{count}", String(toolCount))}
          expandedTimerText={`${t("activity.working")} (${formatElapsed(round.waitSeconds * 1000)})`}
          running={false}
          showTimer={true}
          autoExpand={false}
          detailsNode={<StepsView text={toolText} />}
          textNode={null}
        />
        {visibleTextNode}
      </>
    );
  }

  if (!thinkingNode && !visibleTextNode) {
    return null;
  }

  return (
    <div className="worked-for-body worked-for-plain">
      {thinkingNode}
      {visibleTextNode}
    </div>
  );
}

export function splitCompletedTurn(turn: HistoryTurn): {
  detailRounds: HistoryRound[];
  finalAnswerText: string;
  workedForSeconds: number;
} {
  const rounds = turn.rounds || [];
  const lastRound = rounds.length > 0 ? rounds[rounds.length - 1] : null;
  const lastRoundText = String(lastRound?.text || "").trim();
  return {
    detailRounds: rounds.slice(0),
    finalAnswerText: lastRoundText,
    workedForSeconds: rounds.reduce(
      (sum, round) => sum + Math.max(0, Number(round.waitSeconds || 0)),
      0,
    ),
  };
}

function CompletedTurnView({
  turn,
  negIndex,
  handlers,
}: {
  turn: HistoryTurn;
  negIndex: number;
  handlers: MessageHandlers;
}) {
  const { t } = useApp();
  const { detailRounds, finalAnswerText, workedForSeconds } = splitCompletedTurn(turn);
  const detailNodes: ReactNode[] = [];
  detailRounds.forEach((round, index) => {
    if (round.selection && round.selection.trim().length > 0) {
      detailNodes.push(
        <div className="ask-selection" key={`selection-${index}`}>
          <Icon name="check" size={13} className="ask-selection-icon" />
          <span className="ask-selection-label">{t("askMoreInfo.selectedLabel")}</span>
          <span className="ask-selection-text">{round.selection}</span>
        </div>,
      );
      return;
    }
    const isFinalRoundWithAnswer = index === detailRounds.length - 1 && finalAnswerText.length > 0;
    detailNodes.push(
      <HistoryRoundDetailView
        key={`round-${index}`}
        round={round}
        showText={!isFinalRoundWithAnswer}
      />,
    );
  });
  const hasDetails = detailNodes.length > 0;
  const timerText = `${t("activity.workedFor")} ${formatElapsed(workedForSeconds * 1000)}`;
  const finalAnswer = finalAnswerText.length > 0 ? (
    <div className="answer">
      <MarkdownText text={finalAnswerText} />
    </div>
  ) : null;
  if (!hasDetails && !finalAnswer) {
    return (
      <div className="turn">
        {turn.userText && (
          <UserEntry
            text={turn.userText}
            timeMs={parseHistoryTime(turn.timestamp)}
            index={negIndex}
            handlers={handlers}
          />
        )}
      </div>
    );
  }
  return (
    <div className="turn">
      {turn.userText && (
        <UserEntry
          text={turn.userText}
          timeMs={parseHistoryTime(turn.timestamp)}
          index={negIndex}
          handlers={handlers}
        />
      )}
      {hasDetails ? (
        <>
          <RoundShell
            timerText={timerText}
            running={false}
            showTimer={true}
            autoExpand={false}
            detailsBeforeText={true}
            detailsNode={<div className="worked-for-body">{detailNodes}</div>}
            textNode={null}
          />
          {finalAnswer}
        </>
      ) : (
        finalAnswer
      )}
    </div>
  );
}

function ThinkingPanel({
  thinkingText,
  running,
  timerText,
}: {
  thinkingText: string;
  running: boolean;
  timerText?: string;
}) {
  const [expanded, setExpanded] = useState(running);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { t } = useApp();

  // Auto-expand when thinking starts, auto-collapse when thinking ends
  // (model moves on to visible text or tool calls).
  useEffect(() => {
    setExpanded(running);
  }, [running]);

  useEffect(() => {
    if (expanded && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [thinkingText, expanded]);

  if (!thinkingText.trim()) {
    return null;
  }

  const hasTimer = !!timerText;

  return (
    <div className={`thinking-panel ${!expanded ? "collapsed" : ""}`}>
      <div className="thinking-activity">
        <button
          className={`activity-header thinking-header ${running ? "running" : ""}`}
          onClick={() => setExpanded((v) => !v)}
        >
          <span className={`activity-text ${hasTimer && running ? "marquee" : ""}`}>
            {timerText ?? t("thinking.show")}
          </span>
          <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
        </button>
        {expanded && (
          <>
            <div className="thinking-scroll" ref={scrollRef}>
              <div className="thinking-content">
                <MarkdownText text={thinkingText} />
              </div>
            </div>
            <button
              className="activity-collapse"
              onClick={() => setExpanded(false)}
              title={t("activity.collapse")}
              aria-label={t("activity.collapse")}
            >
              <Icon name="chevron" size={14} className="chevron up" />
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function HistoryTurnView({
  turn,
  negIndex,
  handlers,
}: {
  turn: HistoryTurn;
  negIndex: number;
  handlers: MessageHandlers;
}) {
  return <CompletedTurnView turn={turn} negIndex={negIndex} handlers={handlers} />;
}

type LiveRoundGroup =
  | { kind: "tool"; rounds: TurnRound[] }
  | { kind: "other"; round: TurnRound };

function isLiveToolRound(round: TurnRound): boolean {
  const hasSteps = round.segments.some((segment) => segment.kind === "step" && segment.text.trim());
  const hasAnswer = round.segments.some((segment) => segment.kind === "answer" && segment.text.trim());
  return hasSteps && !hasAnswer;
}

export function groupLiveRounds(rounds: TurnRound[]): LiveRoundGroup[] {
  const groups: LiveRoundGroup[] = [];
  let toolRounds: TurnRound[] = [];
  const flushTools = () => {
    if (toolRounds.length > 0) {
      groups.push({ kind: "tool", rounds: toolRounds });
      toolRounds = [];
    }
  };

  for (const round of rounds) {
    const hasThinking = String(round.thinkingText || "").trim().length > 0;
    const hasSteps = round.segments.some((segment) => segment.kind === "step" && segment.text.trim());
    const hasAnswer = round.segments.some((segment) => segment.kind === "answer" && segment.text.trim());
    if (!hasThinking && !hasSteps && !hasAnswer) {
      continue;
    }
    if (isLiveToolRound(round)) {
      toolRounds.push(round);
      continue;
    }
    flushTools();
    groups.push({ kind: "other", round });
  }

  flushTools();
  return groups;
}

function hasVisibleRoundContent(round: TurnRound | undefined): boolean {
  if (!round) {
    return false;
  }
  const hasThinking = Boolean(round.thinkingText?.trim().length);
  const hasSegments = round.segments.some((segment) => segment.text.trim().length > 0);
  return hasThinking || hasSegments;
}

export function hasPendingInvisibleRound(
  turn: Pick<Turn, "endedAt" | "rounds">,
): boolean {
  const lastRound = turn.rounds[turn.rounds.length - 1];
  return Boolean(
    turn.endedAt === null &&
      lastRound &&
      lastRound.waitEndedAt === null &&
      !hasVisibleRoundContent(lastRound),
  );
}

export function shouldShowPendingWorking(
  turn: Pick<Turn, "startedAt" | "endedAt" | "rounds">,
  hasVisibleLiveGroups = false,
): boolean {
  return hasPendingInvisibleRound(turn) && !hasVisibleLiveGroups;
}

function LiveToolGroupView({
  rounds,
  now,
  isLatestGroup,
  waitingForContinuation,
  continuationElapsedMs,
}: {
  rounds: TurnRound[];
  now: number;
  isLatestGroup: boolean;
  waitingForContinuation: boolean;
  continuationElapsedMs: number;
}) {
  const { t } = useApp();
  const thinkingNodes = rounds.flatMap((round, index) => {
    const thinkingText = String(round.thinkingText || "");
    if (!thinkingText.trim()) {
      return [];
    }
    const thinkingRunning = round.waitEndedAt === null && !round.thinkingEndedAt;
    const startedAt = round.thinkingStartedAt ?? round.waitStartedAt;
    const endedAt = round.thinkingEndedAt ?? round.waitEndedAt ?? now;
    const elapsed = formatElapsed(Math.max(0, endedAt - startedAt));
    return [
      <ThinkingPanel
        key={`thinking-${round.id}-${index}`}
        thinkingText={thinkingText}
        running={thinkingRunning}
        timerText={
          thinkingRunning
            ? `${t("activity.thinking")} (${elapsed})`
            : `${t("activity.thoughtFor")} ${elapsed}`
        }
      />,
    ];
  });
  const toolText = rounds
    .map((round) =>
      round.segments
        .filter((segment) => segment.kind === "step")
        .map((segment) => segment.text)
        .join(""),
    )
    .join("\n");
  const toolCount = countToolCalls(toolText);
  const toolTitle = getLastToolPromptBody(toolText);
  const lastRound = rounds[rounds.length - 1];
  const lastRunning = lastRound?.waitEndedAt === null;
  const elapsedMs = rounds.reduce(
    (sum, round) => sum + Math.max(0, (round.waitEndedAt ?? now) - round.waitStartedAt),
    0,
  );
  const completedText = t("activity.toolCalls").replace("{count}", String(toolCount));
  const waitingText = `${t("activity.working")} (${formatElapsed(
    waitingForContinuation ? continuationElapsedMs : elapsedMs,
  )})`;
  const timerText = isLatestGroup
    ? lastRunning
      ? (toolTitle ?? completedText)
      : waitingForContinuation
        ? waitingText
        : completedText
    : completedText;
  const running = isLatestGroup && (lastRunning || waitingForContinuation);

  return (
    <>
      {thinkingNodes}
      <RoundShell
        timerText={timerText}
        expandedTimerText={waitingText}
        running={running}
        showTimer={true}
        autoExpand={false}
        detailsNode={<StepsView text={toolText} />}
        textNode={null}
      />
    </>
  );
}

function Dropdown({
  trigger,
  className,
  align = "left",
  children,
}: {
  trigger: ReactNode;
  className?: string;
  align?: "left" | "right";
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(open, () => setOpen(false));
  return (
    <div className={`dropdown ${className ?? ""}`} ref={ref}>
      <button className="dropdown-trigger" onClick={() => setOpen((v) => !v)}>
        {trigger}
      </button>
      {open && (
        <div className={`dropdown-menu ${align === "right" ? "align-right" : ""}`}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

const FIXED_REASONING_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"] as const;

/** Compact circular progress badge that mirrors the TUI status bar's context
 *  usage indicator. Sized to fit on the composer toolbar next to the model
 *  selector; uses CSS variables for stroke colors so it follows the theme. */
function ContextUsageRing({
  percent,
  tokens,
  window: ctxWindow,
  label,
}: {
  percent: number;
  tokens: number;
  window: number;
  label: string;
}) {
  // Clamp to a printable range; tokens can briefly overshoot the configured
  // window between turns when streaming usage updates arrive out of order, so
  // we cap the visual at 100% rather than overflowing the ring.
  const pct = Math.max(0, Math.min(100, Math.round(percent)));
  // Mirrors the SVG's coordinate system below: r=8 stroke=2 viewBox 0..20.
  const radius = 8;
  const circumference = 2 * Math.PI * radius;
  const dash = (pct / 100) * circumference;
  const title = `${label}: ${pct}% (${tokens.toLocaleString()} / ${ctxWindow.toLocaleString()})`;
  const danger = pct >= 90;
  const warn = !danger && pct >= 75;
  return (
    <span
      className={`context-ring ${danger ? "danger" : warn ? "warn" : ""}`}
      title={title}
      aria-label={title}
    >
      <svg viewBox="0 0 20 20" width={20} height={20} aria-hidden="true">
        <circle
          className="context-ring-track"
          cx="10"
          cy="10"
          r={radius}
          fill="none"
          strokeWidth="2"
        />
        <circle
          className="context-ring-bar"
          cx="10"
          cy="10"
          r={radius}
          fill="none"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          transform="rotate(-90 10 10)"
        />
      </svg>
    </span>
  );
}

/** Two-level model menu: the first level always lists the three reasoning
 *  effort levels (greying out ones the active model doesn't support) plus a
 *  "current model" entry. Hovering that entry flies the model list out to the
 *  side (right by default, flipping left when there isn't room). */
function ModelMenu({
  models,
  currentModel,
  reasoningEfforts,
  reasoningEffort,
  onSelectModel,
  onSelectReasoning,
}: {
  models: string[];
  currentModel: string;
  reasoningEfforts: string[];
  reasoningEffort: string;
  onSelectModel: (selector: string) => void;
  onSelectReasoning: (level: string) => void;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [flyoutOpen, setFlyoutOpen] = useState(false);
  // Fixed-position coordinates so the flyout escapes the parent menu's
  // overflow/scroll clipping and renders as a standalone panel beside it.
  const [flyoutPos, setFlyoutPos] = useState<{
    side: "right" | "left";
    left: number;
    top: number;
  } | null>(null);
  const ref = useOutsideClose(open, () => setOpen(false));
  const entryRef = useRef<HTMLDivElement>(null);
  const flyoutRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);
  const close = () => setOpen(false);

  const cancelClose = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };
  // Delay closing so the cursor can cross the small gap to the flyout.
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setFlyoutOpen(false), 140);
  };

  useEffect(() => {
    if (!open) setFlyoutOpen(false);
    return cancelClose;
  }, [open]);

  // After the flyout renders, clamp it within the viewport: if it would
  // overflow the bottom, shift it up so the whole menu stays visible.
  useLayoutEffect(() => {
    if (!flyoutOpen || !flyoutPos) return;
    const el = flyoutRef.current;
    if (!el) return;
    const margin = 8;
    const height = el.offsetHeight;
    const maxTop = window.innerHeight - height - margin;
    const clampedTop = Math.max(margin, Math.min(flyoutPos.top, maxTop));
    if (clampedTop !== flyoutPos.top) {
      setFlyoutPos((prev) => (prev ? { ...prev, top: clampedTop } : prev));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flyoutOpen, flyoutPos?.top, flyoutPos?.left]);

  const supported = new Set(reasoningEfforts.map((l) => l.toLowerCase()));
  const selectedLower = reasoningEffort.toLowerCase();
  const currentName = currentModel.includes(":")
    ? currentModel.slice(currentModel.indexOf(":") + 1)
    : currentModel;

  const FLYOUT_WIDTH = 220;
  const FLYOUT_GAP = 4;
  // Decide which side the flyout opens on based on available viewport space.
  const openFlyout = () => {
    const el = entryRef.current;
    if (!el) {
      setFlyoutOpen(true);
      return;
    }
    const rect = el.getBoundingClientRect();
    const spaceRight = window.innerWidth - rect.right;
    const side: "right" | "left" =
      spaceRight < FLYOUT_WIDTH + FLYOUT_GAP && rect.left > spaceRight ? "left" : "right";
    const left =
      side === "right"
        ? rect.right + FLYOUT_GAP
        : rect.left - FLYOUT_GAP - FLYOUT_WIDTH;
    cancelClose();
    setFlyoutPos({ side, left, top: rect.top });
    setFlyoutOpen(true);
  };

  return (
    <div className="dropdown model-dropdown" ref={ref}>
      <button className="dropdown-trigger" onClick={() => setOpen((v) => !v)}>
        <span>{currentName || t("model.label")}</span>
        {reasoningEffort && (
          <span className="model-reasoning-level">{reasoningEffort}</span>
        )}
        <Icon name="chevron" size={13} className="chevron down" />
      </button>
      {open && (
        <div className="dropdown-menu align-right">
          <div className="model-group">
            <div className="model-group-header">{t("reasoning.label")}</div>
            {supported.size > 0 && (
              <button
                className={`dropdown-item ${!reasoningEffort ? "active" : ""}`}
                onClick={() => {
                  close();
                  onSelectReasoning("");
                }}
              >
                <span className="dropdown-check">
                  {!reasoningEffort && <Icon name="check" size={13} />}
                </span>
                <span>{t("reasoning.effort.default")}</span>
              </button>
            )}
            {FIXED_REASONING_EFFORTS.filter((level) => supported.has(level)).map((level) => {
              const active = level === selectedLower;
              return (
                <button
                  key={level}
                  className={`dropdown-item ${active ? "active" : ""}`}
                  onClick={() => {
                    close();
                    onSelectReasoning(level);
                  }}
                >
                  <span className="dropdown-check">
                    {active && <Icon name="check" size={13} />}
                  </span>
                  <span>{t(`reasoning.effort.${level}`)}</span>
                </button>
              );
            })}
            {supported.size === 0 && (
              <button className="dropdown-item" disabled>
                <span className="dropdown-check" />
                <span>{t("reasoning.unavailable")}</span>
              </button>
            )}
          </div>
          <div className="model-group">
            <div className="model-group-header">{t("model.label")}</div>
            <div
              className="model-flyout-anchor"
              ref={entryRef}
              onMouseEnter={openFlyout}
              onMouseLeave={scheduleClose}
            >
              <button className="dropdown-item model-submenu-entry">
                <span className="dropdown-check" />
                <span>{currentName || t("model.label")}</span>
                <Icon name="arrow-right" size={13} className="model-submenu-arrow" />
              </button>
              {flyoutOpen && flyoutPos && (
                <div
                  ref={flyoutRef}
                  className={`model-flyout ${flyoutPos.side}`}
                  style={{
                    left: flyoutPos.left,
                    top: flyoutPos.top,
                    width: FLYOUT_WIDTH,
                  }}
                  onMouseEnter={cancelClose}
                  onMouseLeave={scheduleClose}
                >
                  {groupModelsByProvider(models).map((group) => (
                    <div className="model-group" key={group.provider}>
                      <div className="model-group-header">{group.provider}</div>
                      {group.items.map((item) => (
                        <button
                          key={item.selector}
                          className={`dropdown-item ${item.selector === currentModel ? "active" : ""}`}
                          onClick={() => {
                            close();
                            onSelectModel(item.selector);
                          }}
                        >
                          <span className="dropdown-check">
                            {item.selector === currentModel && <Icon name="check" size={13} />}
                          </span>
                          <span>{item.name}</span>
                        </button>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function LiveRoundView({
  round,
  now,
}: {
  round: TurnRound;
  now: number;
}) {
  const { t } = useApp();
  const running = round.waitEndedAt === null;
  const elapsedMs = (round.waitEndedAt ?? now) - round.waitStartedAt;
  const elapsed = formatElapsed(elapsedMs);
  const answer = round.segments
    .filter((s) => s.kind === "answer")
    .map((s) => s.text)
    .join("");
  const toolText = round.segments
    .filter((s) => s.kind === "step")
    .map((s) => s.text)
    .join("");
  const toolCount = countToolCalls(toolText);
  const toolTitle = getLastToolPromptBody(toolText);
  const hasAnswer = answer.trim().length > 0;
  const hasTools = toolText.trim().length > 0;
  const thinkingRunning = running && Boolean(round.thinkingText) && !round.thinkingEndedAt;
  const showTimer = shouldShowRoundTimer({
    running,
    hasTools,
    hasAnswer,
    thinkingRunning,
  });
  if (!running) {
    return (
      <HistoryRoundDetailView
        round={{
          waitSeconds: Math.max(0, Math.round(elapsedMs / 1000)),
          text: answer,
          tools: toolText,
          thinking: String(round.thinkingText || ""),
        }}
      />
    );
  }
  const timerText = hasTools
    ? toolTitle ?? t("activity.toolCalls").replace("{count}", String(toolCount))
    : `${t("activity.working")} (${elapsed})`;
  return (
    <>
      {round.thinkingText && (
        <ThinkingPanel
          thinkingText={round.thinkingText}
          running={thinkingRunning}
          timerText={(() => {
            const startedAt = round.thinkingStartedAt ?? round.waitStartedAt;
            const endedAt = round.thinkingEndedAt ?? round.waitEndedAt ?? now;
            const elapsed = formatElapsed(endedAt - startedAt);
            return thinkingRunning
              ? `${t("activity.thinking")} (${elapsed})`
              : `${t("activity.thoughtFor")} ${elapsed}`;
          })()}
        />
      )}
      <RoundShell
        timerText={timerText}
        expandedTimerText={`${t("activity.working")} (${elapsed})`}
        running={running}
        showTimer={showTimer}
        autoExpand={false}
        detailsNode={hasTools ? <StepsView text={toolText} /> : null}
        textNode={
          answer.trim().length > 0 ? (
            <div className="answer">
              <MarkdownText text={answer} />
            </div>
          ) : null
        }
      />
    </>
  );
}

function TurnView({
  turn,
  now,
  negIndex,
  handlers,
}: {
  turn: Turn;
  now: number;
  negIndex: number;
  handlers: MessageHandlers;
}) {
  const { t } = useApp();
  const liveGroups = groupLiveRounds(turn.rounds);
  const lastRound = turn.rounds[turn.rounds.length - 1];
  const hasPendingContinuation = hasPendingInvisibleRound(turn);
  const showPendingWorking = shouldShowPendingWorking(turn, liveGroups.length > 0);
  const pendingWorkingElapsed = lastRound
    ? formatElapsed(now - lastRound.waitStartedAt)
    : formatElapsed(now - turn.startedAt);
  return (
    <div className="turn">
      {turn.userText && (
        <UserEntry
          text={turn.userText}
          timeMs={turn.startedAt}
          index={negIndex}
          handlers={handlers}
        />
      )}
      {turn.rounds.length === 0 && turn.endedAt === null && (
        <div className="activity">
          <div className="activity-header running">
            <span className="activity-text marquee">
              {t("activity.working")} ({formatElapsed(now - turn.startedAt)})
            </span>
          </div>
        </div>
      )}
      {liveGroups.map((group, index) => {
        if (group.kind === "tool") {
          return (
            <LiveToolGroupView
              key={`tool-${group.rounds[0]?.id ?? index}`}
              rounds={group.rounds}
              now={now}
              isLatestGroup={index === liveGroups.length - 1}
              waitingForContinuation={index === liveGroups.length - 1 && hasPendingContinuation}
              continuationElapsedMs={
                index === liveGroups.length - 1 && lastRound
                  ? Math.max(0, now - lastRound.waitStartedAt)
                  : 0
              }
            />
          );
        }
        return <LiveRoundView key={group.round.id} round={group.round} now={now} />;
      })}
      {showPendingWorking && (
        <div className="activity">
          <div className="activity-header running">
            <span className="activity-text marquee">
              {t("activity.working")} ({pendingWorkingElapsed})
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

function WorkspaceSelector({
  draft = false,
  draftWorkspaceId = "",
  onPickDraft,
}: {
  draft?: boolean;
  draftWorkspaceId?: string;
  onPickDraft?: (id: string) => void;
}) {
  const { state, activeWorkspaceId, runCommand, clearTurns, selectWorkspace, pickFolder, t } = useApp();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [newPath, setNewPath] = useState("");
  const ref = useOutsideClose(open, () => setOpen(false));

  const workspaces = state?.workspaces ?? [];
  const defaultWs = workspaces.find((w) => w.isDefault);
  // The selected workspace differs between draft mode (the pending target) and
  // an existing chat (the active workspace).
  const selectedId = draft
    ? draftWorkspaceId
    : activeWorkspaceId;
  const selectedWs = workspaces.find((w) => w.id === selectedId);
  // The Default workspace is not a real project; surface it only through the
  // dedicated "Don't work in a workspace" entry, never in the workspace list.
  const filtered = workspaces.filter(
    (w) =>
      !w.isDefault &&
      w.name.toLowerCase().includes(search.trim().toLowerCase()),
  );

  const close = () => {
    setOpen(false);
    setAdding(false);
    setSearch("");
    setNewPath("");
  };

  const switchTo = async (id: string) => {
    close();
    // In draft mode just record the target; the backend switch + chat creation
    // happen when the first message is sent.
    if (draft) {
      onPickDraft?.(id);
      return;
    }
    await selectWorkspace(id);
  };

  const createFromPath = async (path: string) => {
    const p = path.trim();
    if (!p) {
      return;
    }
    close();
    clearTurns();
    await runCommand(`/workspace create ${quote(p)}`);
  };

  const pickExisting = async () => {
    const path = await pickFolder();
    if (path) {
      await createFromPath(path);
    }
  };

  return (
    <div className="ws-selector" ref={ref}>
      <button className="ws-selector-trigger" onClick={() => setOpen((v) => !v)}>
        <Icon name="folder" size={14} className="muted-icon" />
        <span className="ws-selector-label">{selectedWs && !selectedWs.isDefault ? selectedWs.name : t("workspace.selectorLabel")}</span>
        <Icon name="chevron" size={13} className={`chevron ${open ? "open" : ""}`} />
      </button>
      {open && (
        <div className="ws-selector-menu">
          <div className="ws-search">
            <Icon name="search" size={14} className="muted-icon" />
            <input
              className="ws-search-input"
              value={search}
              placeholder={t("workspace.search")}
              autoFocus
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <ul className="ws-selector-list">
            {filtered.map((ws) => (
              <li key={ws.id}>
                <button
                  className={`ws-selector-item ${ws.id === selectedId ? "active" : ""}`}
                  onClick={() => void switchTo(ws.id)}
                >
                  <span className="ws-selector-check">
                    {ws.id === selectedId && <Icon name="check" size={13} />}
                  </span>
                  <span>{ws.name}</span>
                </button>
              </li>
            ))}
            {filtered.length === 0 && <li className="tree-empty">{t("sidebar.noWorkspaces")}</li>}
          </ul>
          <div className="ws-selector-divider" />
          {adding ? (
            <div className="ws-add">
              <button className="ws-selector-item" onClick={() => void pickExisting()}>
                <Icon name="folder" size={14} />
                <span>{t("workspace.useExisting")}</span>
              </button>
              <div className="ws-add-row">
                <input
                  className="text-input"
                  value={newPath}
                  placeholder={t("workspace.createPathPlaceholder")}
                  onChange={(e) => setNewPath(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      void createFromPath(newPath);
                    }
                  }}
                />
                <button className="btn btn-small btn-primary" disabled={!newPath.trim()} onClick={() => void createFromPath(newPath)}>
                  {t("workspace.create")}
                </button>
              </div>
            </div>
          ) : (
            <button className="ws-selector-item add" onClick={() => setAdding(true)}>
              <Icon name="plus" size={14} />
              <span>{t("workspace.addNew")}</span>
            </button>
          )}
          {defaultWs && defaultWs.id !== selectedId && (
            <button className="ws-selector-item" onClick={() => void switchTo(defaultWs.id)}>
              <span className="ws-selector-check" />
              <span>{t("workspace.none")}</span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
