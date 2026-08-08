import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent as ReactClipboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import appIconUrl from "../assets/app_icon_mark.svg";
import { useApp } from "../state/AppContext";
import { ConsolePanel } from "./ConsolePanel";
import type { CompactNoticeData, HistoryRound, HistoryTurn, PlanStep, RetryCountdownState, SubAgentMessage, Turn, TurnRound } from "../api/types";
import { normalizeLang } from "../i18n";
import { Icon, type IconName } from "./Icon";
import { MarkdownText } from "./Markdown";
import { StepsView, countToolCalls, textContainsSubAgentSession } from "./Steps";
import { Collapsible } from "./Collapsible";
import { ChatTitleBar } from "./ChatTitleBar";
import { AskMoreInfoPanel } from "./AskMoreInfoPanel";
import { ConfirmDialog } from "./ConfirmDialog";
import { FileChangeList } from "./FileChangeList";
import { chatKey } from "./chatMenu";
import { decodeAttachments } from "../utils/attachments";
import {
  appendImageRefs,
  parseImageRefs,
} from "../utils/imageRefs";
import { getSubAgentMessageToolRounds } from "../utils/subagentToolRounds";
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
  stripHiddenAssistantMarkers,
  stripPlanModePrefix,
} from "../utils/tokens";
import type { Segment, TokenKind } from "../utils/tokens";
import { RichComposer } from "./RichComposer";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

function ChatLoadingSplash() {
  return (
    <div className="chat-loading-splash" aria-hidden="true">
      <div className="chat-loading-splash-orb">
        <div className="chat-loading-splash-ripple" />
        <img
          className="chat-loading-splash-icon"
          src={appIconUrl}
          alt=""
          draggable={false}
        />
      </div>
    </div>
  );
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

function CompactNoticeView({
  title,
  body = "",
  stage = "",
}: {
  title: string;
  body?: string;
  stage?: string;
}) {
  const noticeTitle = String(title || "").trim();
  const noticeBody = String(body || "").trim();
  if (!noticeTitle && !noticeBody) {
    return null;
  }
  const inProgress = stage === "start" || stage === "stream";
  return (
    <div className="compact-notice-block">
      {noticeTitle && (
        <div className="compact-notice-banner">
          <span
            className={`compact-notice-text${inProgress ? " shimmer-text" : ""}`}
          >
            {noticeTitle}
          </span>
        </div>
      )}
      {noticeBody && (
        <div className="compact-notice-body">
          <MarkdownText text={noticeBody} />
        </div>
      )}
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
    const idx = sel.indexOf("/");
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

const DIFF_BEGIN = "\uE006";
const CMD_OUTPUT_BEGIN = "\uE000";

export function toolTextHasVisibleOutput(toolText: string): boolean {
  return toolText.includes(CMD_OUTPUT_BEGIN) || toolText.includes(DIFF_BEGIN);
}

function roundHasToolSteps(
  round: Pick<TurnRound, "segments"> | undefined,
): boolean {
  return Boolean(
    round?.segments.some(
      (segment) => segment.kind === "step" && segment.text.trim().length > 0,
    ),
  );
}

const DIFF_END = "\uE007";

function extractFilesFromToolText(toolText: string): string[] {
  const files: string[] = [];
  let start = 0;
  while (true) {
    const s = toolText.indexOf(DIFF_BEGIN, start);
    if (s === -1) break;
    const e = toolText.indexOf(DIFF_END, s + 1);
    if (e === -1) break;
    try {
      const parsed = JSON.parse(toolText.slice(s + 1, e));
      if (parsed.file) files.push(parsed.file);
    } catch { /* ignore malformed payloads */ }
    start = e + 1;
  }
  return [...new Set(files)];
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

type TranscriptEntry =
  | { source: "history"; index: number; turn: HistoryTurn; timestamp: number | undefined; order: number }
  | { source: "live"; index: number; turn: Turn; timestamp: number | undefined; order: number };

/**
 * A history reload can race a terminal SSE event: the newly persisted turn is
 * then in history while an older settled turn is still kept in the live bucket
 * until its duplicate check can be retried.  Do not let the storage buckets
 * dictate transcript order in that window; use their message times instead.
 */
export function orderTranscriptEntries(
  historyTurns: HistoryTurn[],
  liveTurns: Turn[],
): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [
    ...historyTurns.map((turn, index) => ({
      source: "history" as const,
      index,
      turn,
      timestamp: parseHistoryTime(turn.timestamp),
      order: index,
    })),
    ...liveTurns.map((turn, index) => ({
      source: "live" as const,
      index,
      turn,
      timestamp: turn.startedAt,
      order: historyTurns.length + index,
    })),
  ];
  return entries.sort((left, right) => {
    // Preserve the previous history-then-live order when either legacy record
    // has no reliable time rather than guessing and causing a fresh reorder.
    if (left.timestamp === undefined || right.timestamp === undefined) {
      return left.order - right.order;
    }
    return left.timestamp - right.timestamp || left.order - right.order;
  });
}

export function compactNoticeInsertionIndex(
  entries: TranscriptEntry[],
  createdAt: number | undefined,
): number {
  if (!Number.isFinite(createdAt)) {
    return entries.length;
  }
  const nextEntryIndex = entries.findIndex(
    (entry) => entry.timestamp !== undefined && entry.timestamp > createdAt!,
  );
  return nextEntryIndex === -1 ? entries.length : nextEntryIndex;
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

/** A read-only view of a sub-agent session's conversation history,
 *  styled to match the main chat transcript. Rendered as a single
 *  "user turn" with the sub-agent's prompt followed by the work block.
 *
 *  When the session is still streaming (``endedAt === null``), each
 *  assistant round is rendered inline with live thinking/tool states and
 *  a trailing "Working..." indicator — matching the ``TurnView``
 *  structure of the main conversation.
 *
 *  When the session has finished, all assistant rounds are wrapped in a
 *  single collapsible ``RoundShell`` ("Worked for Xs") whose expanded
 *  body mirrors ``HistoryRoundDetailView`` — matching the
 *  ``CompletedTurnView`` structure of the main conversation.
 */
function SubAgentSessionView({ session, now }: { session: import("../api/types").SubAgentSession; now: number }) {
  const { t, state } = useApp();
  const lang = normalizeLang(state?.language);
  const scrollRef = useRef<HTMLDivElement>(null);
  const isLive = !session.endedAt;

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [session.messages.length, session.output, session.messages[session.messages.length - 1]?.content]);
  // Merge consecutive tool-only assistant messages into a single group.
  const mergedMessages = useMemo(() => {
    const result: SubAgentMessage[] = [];
    let i = 0;
    const visibleTextOf = (m: SubAgentMessage): string => {
      return stripHiddenAssistantMarkers(m.content || "");
    };
    while (i < session.messages.length) {
      const msg = session.messages[i];
      const msgRounds = getSubAgentMessageToolRounds(msg, { lang });
      if (msg.role !== "assistant" || msgRounds.length === 0) {
        result.push(msg);
        i++;
        continue;
      }
      const allRounds: string[] = [...msgRounds];
      let j = i + 1;
      while (j < session.messages.length) {
        const next = session.messages[j];
        if (
          next.role === "assistant" &&
          !visibleTextOf(next).trim() &&
          getSubAgentMessageToolRounds(next, { lang }).length > 0
        ) {
          const nt = String((next as unknown as { _thinking?: string })._thinking || "").trim();
          if (nt) break;
          // Don't absorb messages that carry their own tool_calls: they came
          // from a separate sub_agent_tool_call SSE event and represent an
          // independent tool invocation. Only absorb adjacent messages whose
          // tool_rounds were split across rows by the backend history renderer.
          if (Array.isArray(next.tool_calls) && next.tool_calls.length > 0) break;
          allRounds.push(...getSubAgentMessageToolRounds(next, { lang }));
          j++;
        } else if (next.role === "tool") {
          j++;
        } else {
          break;
        }
      }
      const merged: SubAgentMessage = { ...msg, tool_rounds: allRounds };
      result.push(merged);
      i = j;
    }
    return result;
  }, [session.messages, lang]);

  // Build HistoryRound-compatible objects from merged assistant messages.
  const { rounds, workedForSeconds } = useMemo(() => {
    const out: HistoryRound[] = [];
    let totalWait = 0;
    for (const msg of mergedMessages) {
      if (msg.role !== "assistant") continue;
      const thinking = String((msg as unknown as { _thinking?: string })._thinking || "").trim();
      const tools = getSubAgentMessageToolRounds(msg, { lang }).join("\n\n");
      const text = stripHiddenAssistantMarkers(msg.content || "").trim();
      const waitSeconds = (msg as unknown as { _thinking_elapsed_seconds?: number })._thinking_elapsed_seconds ?? 0;
      totalWait += waitSeconds;
      if (!thinking && !tools && !text) continue;
      out.push({ waitSeconds, text, tools, thinking });
    }
    return { rounds: out, workedForSeconds: totalWait };
  }, [mergedMessages, lang]);

  const userPrompt = session.messages.find((m) => m.role === "user");
  const startedAt = session.startedAt ? new Date(session.startedAt).getTime() : 0;
  const finalAnswerText = session.output || "";
  const liveStartedAt = startedAt || now;
  const liveTurnRounds = useMemo<TurnRound[]>(() => {
    return rounds.map((round, index) => {
      const isLast = index === rounds.length - 1;
      const roundStartedAt = liveStartedAt;
      const backendElapsedMs = Math.max(0, Number(round.waitSeconds || 0) * 1000);
      const settledAt = roundStartedAt + backendElapsedMs;
      const waitEndedAt = isLive && isLast ? null : settledAt;
      const thinkingText = String(round.thinking || "");
      const hasThinking = thinkingText.trim().length > 0;
      const hasTools = round.tools.trim().length > 0;
      const hasAnswer = round.text.trim().length > 0;
      const thinkingStillRunning = Boolean(isLive && isLast && hasThinking && !hasTools && !hasAnswer);
      const segments: TurnRound["segments"] = [];
      if (hasTools) {
        segments.push({ id: index * 2 + 1, kind: "step", text: round.tools });
      }
      if (hasAnswer) {
        segments.push({ id: index * 2 + 2, kind: "answer", text: round.text });
      }
      return {
        id: index + 1,
        waitStartedAt: roundStartedAt,
        waitEndedAt,
        segments,
        thinkingText: hasThinking ? thinkingText : undefined,
        thinkingStartedAt: hasThinking ? roundStartedAt : undefined,
        thinkingEndedAt: hasThinking && !thinkingStillRunning ? settledAt : undefined,
        backendElapsedMs: backendElapsedMs > 0 ? backendElapsedMs : undefined,
      };
    });
  }, [rounds, isLive, liveStartedAt, now]);

  const messageHandlers: MessageHandlers = {
    onCopy: (text: string) => {
      void navigator.clipboard?.writeText(text);
    },
    onFork: () => {},
    onEdit: () => {},
  };

  // ── History mode (session has finished) ──────────────────────────
  if (!isLive) {
    const detailNodes = rounds.map((round, index) => {
      const isFinalAnswer =
        finalAnswerText.length > 0 &&
        index === rounds.length - 1 &&
        round.text === finalAnswerText;
      return (
        <HistoryRoundDetailView
          key={`round-${index}`}
          round={round}
          showText={!isFinalAnswer}
        />
      );
    });
    const hasDetails = detailNodes.length > 0;
    const timerText = `${t("activity.workedFor")} ${formatElapsed(workedForSeconds * 1000)}`;

    return (
      <div className="transcript" ref={scrollRef}>
        <div className="transcript-inner">
          <div className="turn">
            {userPrompt && (
              <UserEntry
                text={userPrompt.content}
                timeMs={startedAt}
                index={0}
                handlers={messageHandlers}
              />
            )}
            {hasDetails && (
              <RoundShell
                timerText={timerText}
                running={false}
                showTimer={true}
                autoExpand={false}
                detailsBeforeText={true}
                detailsNode={
                  <div className="worked-for-body">{detailNodes}</div>
                }
                textNode={null}
              />
            )}
            {finalAnswerText && (
              <div className="answer">
                <MarkdownText text={finalAnswerText} />
              </div>
            )}
            {session.endedAt && session.startedAt && (
              <div
                style={{
                  opacity: 0.5,
                  fontSize: 12,
                  textAlign: "center",
                  marginTop: 16,
                }}
              >
                {formatDuration(
                  new Date(session.endedAt).getTime() -
                    new Date(session.startedAt).getTime(),
                )}
                {session.success !== null && (
                  <span style={{ marginLeft: 8 }}>
                    {session.success ? "✓" : "✗"}
                  </span>
                )}
              </div>
            )}
            {session.maxRoundsReached && (
              <div
                style={{
                  color: "var(--warning)",
                  fontSize: 13,
                  textAlign: "center",
                  marginTop: 8,
                }}
              >
                {t("subagents.error.max_rounds") || "Maximum rounds reached"}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── Live mode (session is still streaming) ───────────────────────
  const liveGroups = groupLiveRounds(liveTurnRounds);
  const liveTurn = {
    startedAt: liveStartedAt,
    endedAt: null,
    rounds: liveTurnRounds,
  };
  const { lastRound, hasPendingContinuation, showWorking } =
    getLiveTurnDisplayState(liveTurn, liveGroups);
  const workingElapsed = lastRound
    ? formatElapsed(now - lastRound.waitStartedAt)
    : formatElapsed(now - liveStartedAt);

  return (
    <div className="transcript" ref={scrollRef}>
      <div className="transcript-inner">
        <div className="turn">
          {userPrompt && (
            <UserEntry
              text={userPrompt.content}
              timeMs={startedAt}
              index={0}
              handlers={messageHandlers}
            />
          )}
          {liveGroups.map((group, index) => {
            if (group.kind === "tool") {
              return (
                <LiveToolGroupView
                  key={`subagent-tool-${group.rounds[0]?.id ?? index}`}
                  rounds={group.rounds}
                  now={now}
                  waitingForContinuation={index === liveGroups.length - 1 && hasPendingContinuation}
                  continuationElapsedMs={
                    index === liveGroups.length - 1 && lastRound
                      ? Math.max(0, now - lastRound.waitStartedAt)
                      : 0
                  }
                />
              );
            }
            return (
              <LiveRoundView
                key={`subagent-round-${group.round.id}`}
                round={group.round}
                now={now}
                forceSettled={index < liveGroups.length - 1}
              />
            );
          })}
          {session.output && (
            <div className="answer">
              <MarkdownText text={session.output} />
            </div>
          )}
          {showWorking && (
            <div className="activity">
              <div className="activity-header running">
                <span className="activity-text marquee">
                  {t("activity.working")} ({workingElapsed})
                </span>
              </div>
            </div>
          )}
          {session.maxRoundsReached && (
            <div
              style={{
                color: "var(--warning)",
                fontSize: 13,
                textAlign: "center",
                marginTop: 8,
              }}
            >
              {t("subagents.error.max_rounds") || "Maximum rounds reached"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

function statusIcon(status: string): { name: IconName; className: string } {
  if (status === "completed") {
    return { name: "check-circle", className: "plan-step-icon completed" };
  }
  if (status === "in_progress") {
    return { name: "circle-square", className: "plan-step-icon in-progress" };
  }
  return { name: "circle", className: "plan-step-icon pending" };
}


function TodoDock({
  steps,
  visible,
}: {
  steps: PlanStep[];
  visible: boolean;
}) {
  const { t } = useApp();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(true);

  const total = steps.length;
  const completed = steps.filter((s) => s.status === "completed").length;

  // Auto-expand when the dock becomes visible (new plan arrived).
  useEffect(() => {
    if (visible && total > 0) {
      setExpanded(true);
    }
  }, [visible, total]);

  if (total === 0 || !visible) {
    return null;
  }

  return (
    <div className="todo-dock">
      <div className="todo-dock-header">
        <button
          className="todo-dock-toggle"
          onClick={() => setExpanded((v) => !v)}
          title={expanded ? t("todo.collapse") : t("todo.expand")}
        >
          <Icon name="chevron" size={12} className={`chevron ${expanded ? "down" : "right"}`} />
          <span className="todo-dock-title">{t("plan.title")}</span>
          <span className="todo-dock-count">{completed}/{total}</span>
        </button>
      </div>
      <Collapsible open={expanded} className="todo-dock-collapse">
        <div className="todo-dock-body">
          <div className="todo-dock-steps" ref={scrollRef}>
            {steps.map((step, idx) => {
              const { name, className } = statusIcon(step.status);
              return (
                <div
                  key={idx}
                  className={`todo-dock-step ${step.status === "completed" ? "completed" : ""}`}
                >
                  <Icon name={name} size={14} className={className} />
                  <span className="todo-dock-step-text">{step.step}</span>
                </div>
              );
            })}
          </div>
        </div>
      </Collapsible>
    </div>
  );
}

// ── Global chat search highlighting ─────────────────────────────────────
// Elements whose text must never be wrapped with <mark> (UI chrome,
// tool-step transcripts and thinking blocks — those were never indexed, so
// highlighting them would only produce noise). Code blocks ARE highlighted:
// hits often land inside code examples, so skipping them would hide matches.
const SEARCH_SKIP_SELECTOR =
  "button, textarea, input, select, .steps, .thinking-panel, .file-change-list";

/** Unwrap every ``mark.search-term`` under *root* (idempotent). */
export function removeSearchMarks(root: Element | null | undefined): void {
  if (!root) {
    return;
  }
  root.querySelectorAll("mark.search-term").forEach((m) => {
    const parent = m.parentNode;
    if (!parent) {
      return;
    }
    parent.replaceChild(document.createTextNode(m.textContent ?? ""), m);
    parent.normalize();
  });
}

/** Wrap every occurrence of *keywords* in *root* with ``<mark.search-term>``.
 *  Idempotent: previous marks are unwrapped first so re-runs never nest. */
export function applySearchHighlights(root: HTMLElement, keywords: string[]): void {
  if (!keywords || keywords.length === 0) {
    return;
  }
  removeSearchMarks(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node: Node): number {
      const el = node.parentElement;
      if (!el || el.closest(SEARCH_SKIP_SELECTOR)) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const textNodes: Text[] = [];
  while (walker.nextNode()) {
    textNodes.push(walker.currentNode as Text);
  }
  const lowerKeywords = keywords
    .map((k) => k.toLowerCase())
    .filter((k) => k.length > 0);
  if (lowerKeywords.length === 0) {
    return;
  }
  for (const node of textNodes) {
    const text = node.data;
    if (!text) {
      continue;
    }
    const lower = text.toLowerCase();
    const intervals: Array<[number, number]> = [];
    for (const kw of lowerKeywords) {
      let idx = lower.indexOf(kw);
      while (idx >= 0) {
        intervals.push([idx, idx + kw.length]);
        idx = lower.indexOf(kw, idx + kw.length);
      }
    }
    if (intervals.length === 0) {
      continue;
    }
    intervals.sort((a, b) => a[0] - b[0]);
    const merged: Array<[number, number]> = [];
    for (const [s, e] of intervals) {
      const last = merged[merged.length - 1];
      if (last && s <= last[1]) {
        last[1] = Math.max(last[1], e);
      } else {
        merged.push([s, e]);
      }
    }
    const frag = document.createDocumentFragment();
    let cursor = 0;
    for (const [s, e] of merged) {
      if (s > cursor) {
        frag.appendChild(document.createTextNode(text.slice(cursor, s)));
      }
      const mark = document.createElement("mark");
      mark.className = "search-term";
      mark.textContent = text.slice(s, e);
      frag.appendChild(mark);
      cursor = e;
    }
    if (cursor < text.length) {
      frag.appendChild(document.createTextNode(text.slice(cursor)));
    }
    node.parentNode?.replaceChild(frag, node);
  }
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
    historyTotal,
    historyLoading,
    loadOlderHistory,
    busy,
    now,
    sendInput,
    pasteImage,
    saveDroppedFile,
    chatImageUrl,
    interrupt,
    compactContext,
    compactNotice,
    retryCountdownByChat,
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
    setDraftHasContent,
    askMoreInfo,
    answerAskMoreInfo,
    confirmRequest,
    consoleOpen,
    activeSubAgentSession,
    subAgentSessionLoading,
    pendingInputs,
    pendingAutoSend,
    startPendingInputs,
    cancelPendingInput,
    sendPendingInputNow,
    todoDockVisible,
    setTodoDockVisible,
    t,
    activeSearchHit,
  } = useApp();
  // Drafts (in-progress composer segments) are kept per chat so switching
  // between chats never bleeds an unsent message into a sibling. A synthetic
  // key is used while we're still in "draft mode" (no chat exists yet) so
  // that first composition survives until the user sends or discards.
  const DRAFT_KEY = "__draft__";
  const draftKey = draftMode ? DRAFT_KEY : chatKey(activeWorkspaceId, activeChatId);
  const draftKeyRef = useRef(draftKey);
  draftKeyRef.current = draftKey;
  // Live 429/503 retry countdown for the active chat, rendered below the last
  // message and left-aligned with the message column.
  const retryCountdown: RetryCountdownState | null =
    retryCountdownByChat[chatKey(activeWorkspaceId, activeChatId)] ?? null;
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
  // Report to AppContext whether the draft composer holds any content (typed
  // text, file attachments, or pasted images) so ``newChat`` can preserve the
  // draft's model/reasoning when the user returns to an existing draft after
  // switching chats. The check targets the synthetic draft bucket directly —
  // not the currently-active key — so it stays accurate even while another
  // chat is focused.
  const draftSegments = segmentsByChat[DRAFT_KEY] ?? [];
  const draftImages = imageAttachmentsByChat[DRAFT_KEY] ?? [];
  const draftHasContent = draftSegments.length > 0 || draftImages.length > 0;
  useEffect(() => {
    setDraftHasContent(draftHasContent);
    return () => {
      // ChatView unmounted: the local draft state is gone, nothing to preserve.
      setDraftHasContent(false);
    };
  }, [draftHasContent, setDraftHasContent]);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const prevHeightRef = useRef<number | null>(null);
  // ── Global chat search: scroll to the hit turn + highlight keywords ──
  const appliedSearchRef = useRef<string>("");
  useEffect(() => {
    if (!activeSearchHit) {
      appliedSearchRef.current = "";
      removeSearchMarks(scrollRef.current);
      return;
    }
    const key = chatKey(activeWorkspaceId, activeChatId);
    if (activeSearchHit.chatKey !== key || historyLoading) {
      return;
    }
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    const target = container.querySelector<HTMLElement>(
      `[data-turn-abs="${activeSearchHit.turnIdx}"]`,
    );
    if (!target) {
      return;
    }
    const sig = `${key}:${activeSearchHit.turnIdx}:${activeSearchHit.keywords.join(
      "\u0001",
    )}`;
    const first = appliedSearchRef.current !== sig;
    // Re-apply marks every time the transcript re-renders (React reconciliation
    // can drop the DOM-level marks), then scroll precisely to the first hit.
    applySearchHighlights(target, activeSearchHit.keywords);
    if (first) {
      appliedSearchRef.current = sig;
      const hit = target.querySelector<HTMLElement>("mark.search-term");
      (hit ?? target).scrollIntoView({ block: "center", behavior: "smooth" });
      target.classList.add("search-hit-flash");
      window.setTimeout(() => {
        target.classList.remove("search-hit-flash");
      }, 2000);
    }
  }, [
    activeSearchHit,
    historyTurns,
    historyLoading,
    activeWorkspaceId,
    activeChatId,
  ]);
  // Whether the transcript should auto-stick to the bottom on live updates.
  // Starts true (a fresh/switched chat opens pinned to the latest message)
  // and flips off the moment the user scrolls away from the bottom, so an
  // in-flight streaming turn can't yank them back down while they read
  // earlier messages. Re-pins as soon as they scroll back to the bottom.
  const stickToBottomRef = useRef<boolean>(true);
  // Once history has loaded at least one turn (or a live turn appeared), the
  // splash should not reappear during subsequent refreshes (e.g. after editing
  // the first message and sending a new one). Reset when switching to a
  // different chat so the splash still shows on the first load of a new chat.
  const historyEverHadContentRef = useRef(false);
  // Track whether a history load has ever completed (historyLoading
  // transitioned true→false) for the current chat. Detected synchronously
  // during render so the transition frame correctly hides the splash.
  const historyLoadCompletedRef = useRef(false);
  const prevHistoryLoadingRef = useRef(false);
  if (prevHistoryLoadingRef.current && !historyLoading) {
    historyLoadCompletedRef.current = true;
  }
  prevHistoryLoadingRef.current = historyLoading;
  // When the first load on startup returns empty before the SSE idle has
  // arrived (server persistence not yet caught up), defer showing the empty
  // state for a grace period so the imminent idle-triggered reload can take
  // over without a flash.
  const [emptyDeferred, setEmptyDeferred] = useState(false);
  const emptyGraceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevLoadCompletedRef = useRef(false);
  if (historyLoadCompletedRef.current && !prevLoadCompletedRef.current) {
    prevLoadCompletedRef.current = true;
  }
  if (historyLoadCompletedRef.current && historyTotal === 0 && historyTurns.length === 0 && !historyEverHadContentRef.current) {
    if (!emptyDeferred && !emptyGraceTimerRef.current) {
      emptyGraceTimerRef.current = setTimeout(() => {
        emptyGraceTimerRef.current = null;
        setEmptyDeferred(true);
      }, 1500);
    }
  } else {
    if (emptyGraceTimerRef.current) {
      clearTimeout(emptyGraceTimerRef.current);
      emptyGraceTimerRef.current = null;
    }
    if (emptyDeferred && (historyEverHadContentRef.current || historyTotal > 0)) {
      setEmptyDeferred(false);
    }
  }
  // If a second load starts (historyLoading flips true after having completed),
  // cancel the grace timer and keep the splash.
  if (prevLoadCompletedRef.current && historyLoading && emptyGraceTimerRef.current) {
    clearTimeout(emptyGraceTimerRef.current);
    emptyGraceTimerRef.current = null;
  }
  // Cleanup on unmount or draftKey change.
  const prevDraftKeyRef = useRef(draftKey);
  if (draftKey !== prevDraftKeyRef.current) {
    // When materializing a draft chat (New Chat → real chat), there is no
    // history to load — suppress the splash that would flash during the
    // idle-triggered history reload.
    const wasDraft = prevDraftKeyRef.current === DRAFT_KEY;
    prevDraftKeyRef.current = draftKey;
    historyEverHadContentRef.current = wasDraft;
    historyLoadCompletedRef.current = wasDraft;
    prevHistoryLoadingRef.current = false;
    prevLoadCompletedRef.current = wasDraft;
    if (emptyGraceTimerRef.current) {
      clearTimeout(emptyGraceTimerRef.current);
      emptyGraceTimerRef.current = null;
    }
  }
  if ((historyTurns.length > 0 || turns.length > 0) && !historyEverHadContentRef.current) {
    historyEverHadContentRef.current = true;
  }
  // Reset deferred-empty flag when switching chats.
  useEffect(() => { setEmptyDeferred(false); }, [draftKey]);
  // Cleanup on unmount.
  useEffect(() => () => {
    if (emptyGraceTimerRef.current) {
      clearTimeout(emptyGraceTimerRef.current);
      emptyGraceTimerRef.current = null;
    }
  }, []);

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
        .join("")
        .replace(/\s+$/, "");
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
  // Also includes confirmRequest so the confirm dialog is fully scrolled
  // into view (the dialog may contain a tall command block or diff preview
  // that needs a full layout pass before scrollHeight is accurate).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) {
      // Defer to rAF so the browser has laid out the full dialog content
      // (command code block, diff preview) before we read scrollHeight.
      requestAnimationFrame(() => {
        if (el && stickToBottomRef.current) {
          el.scrollTop = el.scrollHeight;
        }
      });
    }
  }, [turns, now, compactNotice, confirmRequest]);

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

  // Drag-and-drop files into the composer.
  const addAttachSegment = (path: string) => {
    setSegmentsByChat((prev) => {
      const key = draftKeyRef.current;
      const current = prev[key] ?? [];
      const existing = new Set(current.filter((s) => s.kind === "attach").map((s) => s.value));
      if (existing.has(path)) return prev;
      return { ...prev, [key]: [...current, { kind: "attach", value: path }] };
    });
  };
  const handleComposerDropFiles = (files: FileList) => {
    console.log("[drag-drop] handleComposerDropFiles called, fileCount=" + files.length);
    const imageDataUrls: string[] = [];
    const nonImageFiles: File[] = [];
    let handled = 0;
    const total = files.length;
    const tryFinish = () => {
      if (handled >= total) {
        console.log("[drag-drop] tryFinish, imageDataUrls=" + imageDataUrls.length + " nonImageFiles=" + nonImageFiles.length);
        if (imageDataUrls.length > 0) onPasteImages(imageDataUrls);
        for (const file of nonImageFiles) {
          const reader = new FileReader();
          reader.onload = () => {
            const url = String(reader.result || "");
            console.log("[drag-drop] nonImage file read, urlLen=" + url.length + " name=" + file.name);
            void saveDroppedFile(url, file.name).then((saved) => {
              console.log("[drag-drop] saveDroppedFile result for " + file.name + ":", saved);
              if (saved) {
                addAttachSegment(saved.path);
              }
            });
          };
          reader.onerror = () => { console.log("[drag-drop] FileReader error for " + file.name); };
          reader.readAsDataURL(file);
        }
      }
    };
    for (let i = 0; i < total; i++) {
      const file = files[i];
      console.log("[drag-drop] file[" + i + "]: name=" + file.name + " type=" + file.type + " size=" + file.size + " hasPath=" + (!!((file as unknown as { path?: string }).path)));
      const osPath = (file as unknown as { path?: string }).path;
      if (osPath) {
        console.log("[drag-drop]  -> using osPath: " + osPath);
        addAttachSegment(osPath);
        handled++;
        tryFinish();
        continue;
      }
      if (file.type.startsWith("image/")) {
        console.log("[drag-drop]  -> image file, reading as data URL");
        const reader = new FileReader();
        reader.onload = () => {
          const url = String(reader.result || "");
          if (url.startsWith("data:image/")) imageDataUrls.push(url);
          handled++;
          console.log("[drag-drop]  -> image read, handled=" + handled + "/" + total);
          tryFinish();
        };
        reader.onerror = () => { console.log("[drag-drop]  -> image read error"); handled++; tryFinish(); };
        reader.readAsDataURL(file);
      } else {
        console.log("[drag-drop]  -> non-image, will send to backend");
        nonImageFiles.push(file);
        handled++;
      }
    }
    tryFinish();
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
      stickToBottomRef.current = true;
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
    stickToBottomRef.current = true;
    // Only dismiss the todos list when the message is actually being sent
    // (model is idle), not when it's being queued as a pending task.
    if (!busy) {
      setTodoDockVisible(false);
    }
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
    stickToBottomRef.current = true;
    setTodoDockVisible(false);
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
  const orderedTranscriptEntries = useMemo(
    () => orderTranscriptEntries(historyTurns, turns),
    [historyTurns, turns],
  );
  const standaloneCompactNotice =
    compactNotice?.anchorTurnId === undefined ? compactNotice : null;
  const standaloneCompactNoticeIndex = compactNoticeInsertionIndex(
    orderedTranscriptEntries,
    standaloneCompactNotice?.createdAt,
  );
  const standaloneCompactNoticeNode = standaloneCompactNotice ? (
    <div className="turn compact-notice-turn" role="alert" aria-live="polite">
      <CompactNoticeView
        title={standaloneCompactNotice.title}
        body={standaloneCompactNotice.body}
        stage={standaloneCompactNotice.stage}
      />
    </div>
  ) : null;

  // While an ``request_user_input`` prompt is pending the agent is paused waiting
  // on the user's selection — it isn't actively working — so the action
  // button must revert to "send" (not the interrupt/stop affordance) even
  // though the backend busy flag is still set for the turn.
  // Also switch to "send" when the composer has text while busy: the message
  // will be queued rather than interrupting the running task.
  const stopMode = busy && !askMoreInfo && !canSend;

  // Pending task list: shown when there are queued messages waiting to be sent.
  const [pendingHoverIdx, setPendingHoverIdx] = useState<number | null>(null);
  const pendingListRef = useRef<HTMLDivElement>(null);
  const hasPending = pendingInputs.length > 0;

  const pendingList = hasPending ? (
    <div className="pending-list" ref={pendingListRef}>
      <div className="pending-list-header">
        <span className="pending-list-title">
          {t("chat.pendingListCount", { count: pendingInputs.length })}
        </span>
        {!pendingAutoSend && (
          <button
            className="pending-list-send"
            title={t("chat.pendingListSendTip")}
            onClick={() => void startPendingInputs()}
          >
            <Icon name="send" size={14} />
          </button>
        )}
      </div>
      <div className="pending-list-items">
        {pendingInputs.map((text, i) => (
          <div
            key={i}
            className={`pending-list-item${i === 0 && pendingAutoSend ? " is-next" : ""}`}
            onMouseEnter={() => setPendingHoverIdx(i)}
            onMouseLeave={() => setPendingHoverIdx(null)}
            title={text}
          >
            <span className="pending-list-item-text">{text}</span>
            <button
              className={`pending-list-jump${pendingHoverIdx === i ? " visible" : ""}`}
              title={t("chat.pendingListSendNow")}
              aria-label={t("chat.pendingListSendNow")}
              onClick={() => void sendPendingInputNow(i)}
            >
              <Icon name="send" size={12} />
            </button>
            <button
              className={`pending-list-cancel${pendingHoverIdx === i ? " visible" : ""}`}
              title={t("chat.pendingListCancel")}
              aria-label={t("chat.pendingListCancel")}
              onClick={() => {
                const removed = cancelPendingInput(i);
                if (removed && draftText.trim().length === 0) {
                  setSegments([{ kind: "text", value: removed }]);
                }
              }}
            >
              <Icon name="trash" size={12} />
            </button>
            {pendingHoverIdx === i && (
              <div className="pending-list-tooltip">{text}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  ) : null;

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
        onCompact={() => void compactContext()}
        onDropFiles={handleComposerDropFiles}
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
    (state !== null && !historyStart && turns.length === 0 && historyTurns.length === 0 && historyLoadCompletedRef.current && !emptyGraceTimerRef.current);
  const showChatLoadingSplash =
    !draftMode &&
    !(historyTurns.length > 0 || turns.length > 0) &&
    (state === null || (historyLoading && !historyEverHadContentRef.current) || !historyLoadCompletedRef.current || Boolean(emptyGraceTimerRef.current));
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
      <ChatTitleBar />
      {activeSubAgentSession ? (
        <SubAgentSessionView session={activeSubAgentSession} now={now} />
      ) : subAgentSessionLoading ? (
        <div className="subagent-session-loading">
          <div className="subagent-session-loading-spinner" />
          <span>{t("subagents.loading")}</span>
        </div>
      ) : showChatLoadingSplash ? (
        <ChatLoadingSplash />
      ) : showEmpty ? emptyContent : (
        <>
          <TranscriptMinimap
            scrollRef={scrollRef}
            historyTurns={historyTurns}
            liveTurns={turns}
            unloadedCount={historyStart}
            loadOlderHistory={() => void loadOlderHistory()}
            historyLoading={historyLoading}
          />
          <div className="transcript" ref={scrollRef} onScroll={onScroll}>
            <div className="transcript-inner">
            {historyStart > 0 && (
              <div className="history-more">
                {historyLoading ? t("history.loading") : t("history.more")}
              </div>
            )}
            {orderedTranscriptEntries.map((entry, renderIndex) => (
              <Fragment key={`${entry.source}-${entry.index}`}>
                {standaloneCompactNoticeNode && renderIndex === standaloneCompactNoticeIndex &&
                  standaloneCompactNoticeNode}
                {entry.source === "history" ? (
                  <div
                    data-turn-abs={historyStart + entry.index}
                    className="search-turn-anchor"
                  >
                    <HistoryTurnView
                      turn={entry.turn}
                      negIndex={histNeg[entry.index]}
                      handlers={messageHandlers}
                      searchTarget={
                        activeSearchHit !== null &&
                        activeSearchHit.chatKey ===
                          chatKey(activeWorkspaceId, activeChatId) &&
                        activeSearchHit.turnIdx === historyStart + entry.index
                      }
                    />
                  </div>
                ) : (() => {
                  const turn = entry.turn;
                  return (
                    <TurnView
                      turn={turn}
                      now={now}
                      negIndex={liveNeg[entry.index]}
                      handlers={messageHandlers}
                      compactNotice={compactNotice?.anchorTurnId === turn.id ? compactNotice : null}
                    />
                  );
                })()}
              </Fragment>
            ))}
            {standaloneCompactNoticeNode &&
              standaloneCompactNoticeIndex === orderedTranscriptEntries.length &&
              standaloneCompactNoticeNode}
            {retryCountdown && (
              <div className="retry-countdown" role="status" aria-live="polite">
                <span className="retry-countdown-icon">⏳</span>
                <span className="retry-countdown-text">
                  {t("retry.countdown", {
                    label:
                      (retryCountdown.message || "").trim() ||
                      t("retry.httpError", {
                        code: String(retryCountdown.code),
                      }),
                    n: String(retryCountdown.retryNumber),
                    seconds: String(Math.max(1, Math.ceil(retryCountdown.remainingSeconds))),
                  })}
                </span>
              </div>
            )}
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
          </div>
          <div className="composer-dock">
            <div className="composer-dock-inner">
              <TodoDock
                steps={state?.plan?.plan ?? []}
                visible={todoDockVisible}
              />
              {pendingList}
              {composer}
            </div>
          </div>
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

/** How long a just-settled live turn's "Worked for" shell stays expanded
 *  before it auto-collapses (the task end / terminate / pause transition). */
const SETTLE_COLLAPSE_DELAY_MS = 900;

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
  autoCollapseDelayMs = 0,
}: {
  timerText: string;
  expandedTimerText?: string;
  running: boolean;
  showTimer: boolean;
  autoExpand: boolean;
  detailsBeforeText?: boolean;
  detailsNode: ReactNode;
  textNode: ReactNode;
  /** When > 0 the shell mounts expanded and auto-collapses (with animation)
   *  after this many ms — used for the task-settle transition. One-shot per
   *  mount; manual toggling never re-arms it. */
  autoCollapseDelayMs?: number;
}) {
  const hasDetails = Boolean(detailsNode);
  const [expanded, setExpanded] = useState(autoExpand && hasDetails);
  const autoCollapseFiredRef = useRef(false);
  // Expand (but never auto-collapse) when autoExpand is requested.
  useEffect(() => {
    if (autoExpand && hasDetails) {
      setExpanded(true);
    }
  }, [autoExpand, hasDetails]);

  // One-shot delayed auto-collapse after a task settles (ended / interrupted /
  // paused): show the expanded "Worked for" block first, then animate closed.
  useEffect(() => {
    if (autoCollapseDelayMs <= 0 || !hasDetails || !expanded || autoCollapseFiredRef.current) {
      return;
    }
    autoCollapseFiredRef.current = true;
    const timer = setTimeout(() => setExpanded(false), autoCollapseDelayMs);
    return () => clearTimeout(timer);
  }, [autoCollapseDelayMs, hasDetails, expanded]);

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
      {hasDetails && <div className="activity-divider" />}
      {hasDetails && (
        <Collapsible open={expanded} className="round-collapse">
          {detailsNode}
        </Collapsible>
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
  const compactNoticeTitle = String(round.compactNoticeTitle || "");
  const compactNoticeBody = String(round.compactNoticeBody || "");
  if (compactNoticeTitle.trim().length > 0 || compactNoticeBody.trim().length > 0) {
    return (
      <div className="turn compact-notice-turn">
        <CompactNoticeView title={compactNoticeTitle} body={compactNoticeBody} />
      </div>
    );
  }
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
  const textNode = showText && String(round.text || "").trim().length > 0 ? (
    <div className="answer">
      <MarkdownText text={String(round.text || "")} />
    </div>
  ) : null;

  if (hasToolShell) {
    // A round can carry thinking + a visible reply + tool steps (e.g. the model
    // explains the next action, then calls the tool). The visible reply must be
    // rendered after the thinking block and before the tool steps — previously
    // it was dropped entirely whenever tools were present, so reloading a chat
    // showed "Thought → tool call" with the answer text missing.
    const toolBlock = (
      <div className="turn-round">
        {toolCount === 0 ? (
          <div className="activity-centered">
            <StepsView text={toolText} />
          </div>
        ) : (
          <StepsView text={toolText} />
        )}
      </div>
    );
    return (
      <>
        {thinkingNode}
        {textNode}
        {toolBlock}
      </>
    );
  }

  if (!thinkingNode && !textNode) {
    return null;
  }

  return (
    <div className="worked-for-body worked-for-plain">
      {thinkingNode}
      {showText && textNode}
    </div>
  );
}

/** Convert a (settled) live turn into the history-turn shape so it can be
 *  rendered by ``CompletedTurnView`` (collapsed "Worked for" shell + final
 *  answer), matching what a chat reload produces. */
export function liveTurnToHistoryTurn(turn: Turn): HistoryTurn {
  const rounds: HistoryRound[] = turn.rounds.map((r) => {
    const answer = r.segments
      .filter((s) => s.kind === "answer")
      .map((s) => s.text)
      .join("");
    const tools = r.segments
      .filter((s) => s.kind === "step")
      .map((s) => s.text)
      .join("");
    const waitEndedAt = r.waitEndedAt ?? Date.now();
    const elapsedMs = Math.max(0, waitEndedAt - r.waitStartedAt);
    const backendMs = (r as unknown as { backendElapsedMs?: number }).backendElapsedMs;
    return {
      waitSeconds:
        backendMs != null && backendMs > 0
          ? Math.max(0, Math.round(backendMs / 1000))
          : Math.max(0, Math.round(elapsedMs / 1000)),
      text: answer,
      tools,
      selection: String(r.selection || ""),
      thinking: String(r.thinkingText || ""),
    };
  });
  return {
    userText: turn.userText || "",
    rounds,
    timestamp: new Date(turn.startedAt).toISOString(),
    fileChanges: turn.fileChanges,
  };
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

const MINIMAP_LINE_MIN = 6;
const MINIMAP_LINE_MAX = 24;
const MINIMAP_LINE_HEIGHT = 3;
const MINIMAP_LINE_GAP = 6;

function snapToDevicePixel(value: number) {
  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  return Math.round(value * dpr) / dpr;
}

function TranscriptMinimap({
  scrollRef,
  historyTurns,
  liveTurns,
  unloadedCount,
  loadOlderHistory,
  historyLoading,
}: {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  historyTurns: HistoryTurn[];
  liveTurns: Turn[];
  unloadedCount: number;
  loadOlderHistory: () => void;
  historyLoading: boolean;
}) {
  const { t, zoomLevel } = useApp();
  const minimapRef = useRef<HTMLDivElement | null>(null);
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const [visible, setVisible] = useState(false);
  const [minimapHeight, setMinimapHeight] = useState(0);
  const [minimapTop, setMinimapTop] = useState(0);
  const [visibleRange, setVisibleRange] = useState<{ start: number; end: number }>({ start: 0, end: 0 });
  const loadTriggeredRef = useRef(false);

  // Build filtered list: only user turns get minimap lines.
  // Use ref-based memoization to avoid recalculating when context provides
  // new array references with the same content.
  const userTurnsRef = useRef<Array<{ userText: string; answerText: string; files: string[] }>>([]);
  const domIndicesRef = useRef<number[]>([]);
  const prevKeyRef = useRef("");

  const curKey = `${unloadedCount}:${historyTurns.length}:${historyTurns.map((h) => h.userText?.length).join(",")}:${liveTurns.length}:${liveTurns.map((l) => `${l.id}:${l.userText?.length}`).join(",")}`;
  if (curKey !== prevKeyRef.current) {
    prevKeyRef.current = curKey;
    const ut: Array<{ userText: string; answerText: string; files: string[] }> = [];
    const di: number[] = [];
    let domIdx = 0;
    for (let i = 0; i < unloadedCount; i++) {
      ut.push({ userText: "", answerText: "", files: [] });
      di.push(domIdx);
      domIdx++;
    }
    for (const turn of historyTurns) {
      if (turn.userText?.trim()) {
        const lastRound = turn.rounds[turn.rounds.length - 1];
        const answerText = lastRound ? String(lastRound.text || "").trim() : "";
        const files = turn.rounds.flatMap((r) => extractFilesFromToolText(String(r.tools || "")));
        ut.push({ userText: turn.userText.trim(), answerText, files: [...new Set(files)] });
        di.push(domIdx);
      }
      domIdx++;
    }
    for (const turn of liveTurns) {
      if (turn.userText?.trim()) {
        let answerText = "";
        const files: string[] = [];
        for (const round of turn.rounds) {
          for (const seg of round.segments) {
            if (seg.kind === "answer" && seg.text.trim()) {
              answerText = seg.text.trim();
            }
          }
          const stepTexts = round.segments
            .filter((s) => s.kind === "step")
            .map((s) => s.text);
          files.push(...stepTexts.flatMap(extractFilesFromToolText));
        }
        ut.push({ userText: turn.userText.trim(), answerText, files: [...new Set(files)] });
        di.push(domIdx);
      }
      domIdx++;
    }
    userTurnsRef.current = ut;
    domIndicesRef.current = di;
  }
  const userTurns = userTurnsRef.current;
  const domIndices = domIndicesRef.current;

  const totalLines = userTurns.length;
  const lineStep = MINIMAP_LINE_HEIGHT + MINIMAP_LINE_GAP;
  const lineHeight = snapToDevicePixel(MINIMAP_LINE_HEIGHT);
  const getLineTop = (idx: number) => snapToDevicePixel(idx * lineStep);

  const measureLayout = () => {
    const container = scrollRef.current;
    if (!container) return;
    const ut = userTurnsRef.current;
    const userCount = ut.length;
    const contentExceeds3Screens = container.scrollHeight > container.clientHeight * 3;
    const nextVisible = userCount >= 3 && contentExceeds3Screens;
    setVisible((prev) => prev === nextVisible ? prev : nextVisible);

    const chatView = container.closest('.chat-view');
    if (chatView) {
      const containerRect = container.getBoundingClientRect();
      const chatViewRect = chatView.getBoundingClientRect();
      const h = containerRect.height;
      const contentHeight = Math.min(h, userCount * lineStep);
      const nextTop = (containerRect.top - chatViewRect.top) + (h - contentHeight) / 2;
      setMinimapHeight(contentHeight);
      setMinimapTop(nextTop);
    }
  };

  const measureScroll = () => {
    const container = scrollRef.current;
    if (!container) return;
    const ut = userTurnsRef.current;
    const di = domIndicesRef.current;
    const userCount = ut.length;

    const turnEls = Array.from(container.querySelectorAll(':scope > .transcript-inner > .turn'));
    const scrollTop = container.scrollTop;
    const viewBottom = scrollTop + container.clientHeight;
    let domStart = turnEls.length;
    let domEnd = 0;
    for (let i = 0; i < turnEls.length; i++) {
      const el = turnEls[i] as HTMLElement;
      const elTop = el.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
      const elBottom = elTop + el.offsetHeight;
      if (elBottom > scrollTop && elTop < viewBottom) {
        if (i < domStart) domStart = i;
        if (i > domEnd) domEnd = i;
      }
    }
    if (domStart <= domEnd) {
      let lineStart = userCount;
      let lineEnd = -1;
      for (let li = 0; li < di.length; li++) {
        if (di[li] >= domStart && di[li] <= domEnd) {
          if (li < lineStart) lineStart = li;
          if (li > lineEnd) lineEnd = li;
        }
      }
      if (lineStart <= lineEnd) {
        setVisibleRange((prev) =>
          prev.start === lineStart && prev.end === lineEnd ? prev : { start: lineStart, end: lineEnd }
        );
      }
    }
  };

  useEffect(() => {
    measureLayout();
    const container = scrollRef.current;
    if (!container) return;
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = 0; measureScroll(); });
    };
    container.addEventListener('scroll', onScroll, { passive: true });
    const ro = new ResizeObserver(() => {
      measureLayout();
      measureScroll();
    });
    ro.observe(container);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      container.removeEventListener('scroll', onScroll);
      ro.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyTurns, liveTurns, unloadedCount, scrollRef]);

  const handleMouseMove = (e: React.MouseEvent) => {
    const rect = minimapRef.current?.getBoundingClientRect();
    if (!rect || totalLines === 0) return;
    const y = e.clientY - rect.top;
    const idx = Math.round(y / lineStep);
    setHoveredIdx(Math.max(0, Math.min(idx, totalLines - 1)));
  };

  const handleMouseLeave = () => {
    setHoveredIdx(null);
    loadTriggeredRef.current = false;
  };

  // Trigger history load when hovering over unloaded lines
  if (hoveredIdx !== null && hoveredIdx < unloadedCount && !historyLoading && !loadTriggeredRef.current) {
    loadTriggeredRef.current = true;
    loadOlderHistory();
  }

  const getLineOpacity = (idx: number) => {
    if (hoveredIdx !== null) {
      return idx === hoveredIdx ? 0.9 : 0.3;
    }
    if (idx >= visibleRange.start && idx <= visibleRange.end) return 0.7;
    return 0.25;
  };

  const getLineWidth = (idx: number) => {
    if (hoveredIdx === null) return MINIMAP_LINE_MIN;
    const dist = Math.abs(idx - hoveredIdx);
    if (dist >= 4) return MINIMAP_LINE_MIN;
    const t = dist / 4;
    return Math.round(MINIMAP_LINE_MAX - t * (MINIMAP_LINE_MAX - MINIMAP_LINE_MIN));
  };

  const handleClick = (e: React.MouseEvent) => {
    const rect = minimapRef.current?.getBoundingClientRect();
    if (!rect || totalLines === 0) return;
    const y = e.clientY - rect.top;
    const idx = Math.round(y / lineStep);
    const clickedIdx = Math.max(0, Math.min(idx, totalLines - 1));
    const container = scrollRef.current;
    if (!container) return;
    const domIdx = domIndices[clickedIdx];
    if (domIdx === undefined) return;
    if (clickedIdx < unloadedCount) {
      container.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    const turnEls = Array.from(container.querySelectorAll(':scope > .transcript-inner > .turn'));
    const target = turnEls[domIdx] as HTMLElement | undefined;
    if (target) {
      const scrollTop = target.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
      container.scrollTo({ top: scrollTop, behavior: 'smooth' });
    }
  };

  const tooltip = (() => {
    if (hoveredIdx === null) return null;
    const rect = minimapRef.current?.getBoundingClientRect();
    if (!rect) return null;
    // Same zoom-frame conversion as ContextMenu: getBoundingClientRect and the
    // viewport clamps below are in scaled viewport pixels while the fixed
    // tooltip lives in the unscaled local frame of .window-root, so only the
    // final placement is divided by the zoom factor.
    const lineCenterY = (getLineTop(hoveredIdx) + lineHeight / 2) * zoomLevel;
    const tooltipY = Math.min(
      Math.max(rect.top + lineCenterY - 30, 8),
      window.innerHeight - 120,
    );
    const tooltipX = rect.right + MINIMAP_LINE_MAX * zoomLevel;

    // Unloaded line: show loading or nothing
    if (hoveredIdx < unloadedCount) {
      if (historyLoading) {
        return (
          <div className="minimap-tooltip" style={{ top: tooltipY / zoomLevel, left: tooltipX / zoomLevel, position: 'fixed' }}>
            <div className="minimap-tooltip-answer">{t('minimap.loading')}</div>
          </div>
        );
      }
      return null;
    }

    const preview = userTurns[hoveredIdx];
    if (!preview || (!preview.userText && !preview.answerText && preview.files.length === 0)) {
      return null;
    }
    return (
      <div className="minimap-tooltip" style={{ top: tooltipY / zoomLevel, left: tooltipX / zoomLevel, position: 'fixed' }}>
        {preview.userText && (
          <div className="minimap-tooltip-user">
            {preview.userText.slice(0, 80)}{preview.userText.length > 80 ? '…' : ''}
          </div>
        )}
        {preview.answerText && (
          <div className="minimap-tooltip-answer">
            {preview.answerText.slice(0, 120)}{preview.answerText.length > 120 ? '…' : ''}
          </div>
        )}
        {preview.files.length > 0 && (
          <div className="minimap-tooltip-files">
            {preview.files.map(baseName).join(', ')}
          </div>
        )}
      </div>
    );
  })();

  if (!visible || totalLines === 0 || unloadedCount > 0) return null;

  return (
    <div
      ref={minimapRef}
      className="transcript-minimap"
      style={{ top: minimapTop, height: minimapHeight }}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      onClick={handleClick}
    >
      {Array.from({ length: totalLines }, (_, i) => (
        <div
          key={i}
          className="minimap-line"
          style={{
            top: getLineTop(i),
            height: lineHeight,
            width: getLineWidth(i),
            opacity: getLineOpacity(i),
          }}
        />
      ))}
      {tooltip}
    </div>
  );
}

function CompletedTurnView({
  turn,
  negIndex,
  handlers,
  settle = false,
  searchTarget = false,
}: {
  turn: HistoryTurn;
  negIndex: number;
  handlers: MessageHandlers;
  /** True while a live turn has just settled (finished / interrupted /
   *  paused): the "Worked for" shell is shown expanded first, then
   *  auto-collapses with an animation. */
  settle?: boolean;
  /** True when this turn is the destination of a global chat search hit:
   *  its collapsed "Worked for" sections are force-expanded so the matched
   *  message content is visible before the jump/highlight runs. */
  searchTarget?: boolean;
}) {
  const { t, pendingExpandSubAgentId, state } = useApp();
  const { detailRounds, finalAnswerText, workedForSeconds } = splitCompletedTurn(turn);
  const turnHasTarget = pendingExpandSubAgentId !== "" &&
    detailRounds.some((r) => textContainsSubAgentSession(String(r.tools || ""), pendingExpandSubAgentId));
  const detailNodes: ReactNode[] = [];
  const compactNoticeNodes: ReactNode[] = [];
  const interruptedNodes: ReactNode[] = [];
  const modelErrorNodes: ReactNode[] = [];
  detailRounds.forEach((round, index) => {
    const interruptedText = String(round.interrupted || "").trim();
    const modelErrorText = String(round.modelError || "").trim();
    const compactNoticeTitle = String(round.compactNoticeTitle || "").trim();
    const compactNoticeBody = String(round.compactNoticeBody || "").trim();
    if (modelErrorText.length > 0) {
      modelErrorNodes.push(
        <div className="turn-round" key={`model-error-${index}`}>
          <div className="activity-centered">
            <span className="model-error-banner">{modelErrorText}</span>
          </div>
        </div>,
      );
      // When there's also an interrupted banner for the same round,
      // skip it — the model error is the primary reason for stopping.
    } else if (interruptedText.length > 0) {
      interruptedNodes.push(
        <div className="turn-round" key={`interrupted-${index}`}>
          <div className="activity-centered">
            <span className="interrupted-banner">{interruptedText}</span>
          </div>
        </div>,
      );
    }
    if (compactNoticeTitle.length > 0 || compactNoticeBody.length > 0) {
      compactNoticeNodes.push(
        <div className="turn compact-notice-turn" key={`compact-notice-${index}`}>
          <CompactNoticeView title={compactNoticeTitle} body={compactNoticeBody} />
        </div>,
      );
      return;
    }
    if (round.selection && round.selection.trim().length > 0) {
      detailNodes.push(
        <div className="ask-selection" key={`selection-${index}`}>
          <Icon name="check" size={13} className="ask-selection-icon" />
          <span className="ask-selection-label">{t("askMoreInfo.answerLabel")}</span>
          <span className="ask-selection-text">{round.selection}</span>
        </div>,
      );
      return;
    }
    const isFinalRoundWithAnswer = index === detailRounds.length - 1 && finalAnswerText.length > 0;
    const showText = !isFinalRoundWithAnswer;
    const roundText = String(round.text || "").trim();
    const roundTools = String(round.tools || "").trim();
    const roundThinking = String(round.thinking || "").trim();
    if (roundTools.length > 0 || roundThinking.length > 0 || (showText && roundText.length > 0)) {
      detailNodes.push(
        <HistoryRoundDetailView
          key={`round-${index}`}
          round={round}
          showText={showText}
        />,
      );
    }
  });
  const hasDetails = detailNodes.length > 0;
  const hasCompactNotices = compactNoticeNodes.length > 0;
  const timerText = `${t("activity.workedFor")} ${formatElapsed(workedForSeconds * 1000)}`;
  const finalAnswer = finalAnswerText.length > 0 ? (
    <div className="answer">
      <MarkdownText text={finalAnswerText} />
    </div>
  ) : null;
  if (!hasDetails && !finalAnswer && !hasCompactNotices && interruptedNodes.length === 0 && modelErrorNodes.length === 0) {
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
      {compactNoticeNodes}
      {hasDetails ? (
        <>
          <RoundShell
            timerText={timerText}
            running={false}
            showTimer={true}
            autoExpand={turnHasTarget || settle || searchTarget}
            autoCollapseDelayMs={settle ? SETTLE_COLLAPSE_DELAY_MS : 0}
            detailsBeforeText={true}
            detailsNode={<div className="worked-for-body">{detailNodes}</div>}
            textNode={null}
          />
          {finalAnswer}
        </>
      ) : (
        finalAnswer
      )}
      {interruptedNodes}
      {modelErrorNodes}
      {(() => {
        return turn.fileChanges ? <FileChangeList summary={turn.fileChanges} t={t} workspaceRoot={state?.workspace?.root} /> : null;
      })()}
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
  const [expanded, setExpanded] = useState(false);
  const { t } = useApp();
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (expanded && running && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [thinkingText, expanded, running]);

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
        <Collapsible
          open={expanded}
          className="thinking-collapse"
          onInnerMount={() => {
            // Content mounts one render after the header click; scroll to the
            // newest line so a freshly expanded streaming thought shows its tail.
            if (expanded && running && scrollRef.current) {
              scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            }
          }}
        >
          <div className="thinking-scroll" ref={scrollRef}>
            <div className="thinking-content">
              <MarkdownText text={thinkingText} />
            </div>
          </div>
        </Collapsible>
      </div>
    </div>
  );
}

function HistoryTurnView({
  turn,
  negIndex,
  handlers,
  searchTarget = false,
}: {
  turn: HistoryTurn;
  negIndex: number;
  handlers: MessageHandlers;
  searchTarget?: boolean;
}) {
  return (
    <CompletedTurnView
      turn={turn}
      negIndex={negIndex}
      handlers={handlers}
      searchTarget={searchTarget}
    />
  );
}

type LiveRoundGroup =
  | { kind: "tool"; rounds: TurnRound[] }
  | { kind: "other"; round: TurnRound };

function isLiveToolRound(round: TurnRound): boolean {
  const hasSteps = round.segments.some((segment) => segment.kind === "step" && segment.text.trim());
  const hasAnswer = round.segments.some((segment) => segment.kind === "answer" && segment.text.trim());
  const hasThinking = Boolean(String(round.thinkingText || "").trim());
  const hasSelection = Boolean(String(round.selection || "").trim());
  // Only pure tool calls (no thinking, no visible answer) merge into tool groups.
  // Rounds with their own thinking stay separate so each "Thought for" block
  // matches its corresponding tool calls.
  return hasSteps && !hasAnswer && !hasThinking && !hasSelection;
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
    const hasSelection = String(round.selection || "").trim().length > 0;
    if (!hasThinking && !hasSteps && !hasAnswer && !hasSelection) {
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
  return hasThinking || hasSegments || Boolean(round.selection?.trim());
}

function liveGroupShowsOwnWorking(
  group: LiveRoundGroup | undefined,
  waitingForContinuation: boolean,
): boolean {
  if (!(group && group.kind === "tool" && waitingForContinuation)) {
    return false;
  }
  const lastRound = group.rounds[group.rounds.length - 1];
  return Boolean(lastRound && lastRound.waitEndedAt === null);
}

export function shouldShowStreamingWorkingForRound(
  round: Pick<TurnRound, "segments" | "thinkingText" | "waitEndedAt"> | undefined,
): boolean {
  if (!round || round.waitEndedAt !== null) {
    return false;
  }
  const hasTools = round.segments.some(
    (segment) => segment.kind === "step" && segment.text.trim().length > 0,
  );
  if (hasTools) {
    return false;
  }
  const hasAnswer = round.segments.some(
    (segment) => segment.kind === "answer" && segment.text.trim().length > 0,
  );
  if (hasAnswer) {
    return true;
  }
  const hasThinking = Boolean(round.thinkingText?.trim().length);
  if (hasThinking) {
    return false;
  }
  return true;
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
  if (turn.endedAt !== null || hasVisibleLiveGroups) {
    return false;
  }
  const lastRound = turn.rounds[turn.rounds.length - 1];
  if (shouldShowStreamingWorkingForRound(lastRound)) {
    return true;
  }
  return hasPendingInvisibleRound(turn) && !hasVisibleLiveGroups;
}

export function getLiveTurnDisplayState(
  turn: Pick<Turn, "startedAt" | "endedAt" | "rounds">,
  liveGroups: LiveRoundGroup[] = groupLiveRounds(turn.rounds),
) {
  const lastRound = turn.rounds[turn.rounds.length - 1];
  const hasPendingContinuation = hasPendingInvisibleRound(turn);
  const isRunning = turn.endedAt === null;
  const lastVisibleGroup = liveGroups[liveGroups.length - 1];
  const hasActiveThinking = turn.rounds.some(
    (round) => Boolean(round.thinkingText?.trim()) && round.waitEndedAt === null,
  );
  const hasRunningToolRound = Boolean(
    lastRound &&
    lastRound.waitEndedAt === null &&
    roundHasToolSteps(lastRound),
  );
  const hasToolGroupWorking =
    liveGroupShowsOwnWorking(lastVisibleGroup, hasPendingContinuation);
  const hasInvisibleRunningRound = hasPendingInvisibleRound(turn);
  const showWorking =
    isRunning &&
    !hasActiveThinking &&
    !hasRunningToolRound &&
    !hasToolGroupWorking &&
    (
      turn.rounds.length === 0 ||
      Boolean(lastRound && lastRound.waitEndedAt !== null) ||
      liveGroups.length === 0 ||
      hasInvisibleRunningRound ||
      shouldShowStreamingWorkingForRound(lastRound)
    );

  return {
    lastRound,
    hasPendingContinuation,
    showWorking,
  };
}

function LiveToolGroupView({
  rounds,
  now,
  waitingForContinuation,
  continuationElapsedMs,
}: {
  rounds: TurnRound[];
  now: number;
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
    const clientElapsed = formatElapsed(Math.max(0, endedAt - startedAt));
    const backendMs = (round as unknown as { backendElapsedMs?: number }).backendElapsedMs;
    const thoughtForElapsed = backendMs != null && backendMs > 0
      ? formatElapsed(backendMs) : clientElapsed;
    return [
      <ThinkingPanel
        key={`thinking-${round.id}-${index}`}
        thinkingText={thinkingText}
        running={thinkingRunning}
        timerText={
          thinkingRunning
            ? `${t("activity.thinking")} (${clientElapsed})`
            : `${t("activity.thoughtFor")} ${thoughtForElapsed}`
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
  const lastRound = rounds[rounds.length - 1];
  const lastRunning = lastRound?.waitEndedAt === null;
  const toolRunning = Boolean(lastRunning) && toolText.trim().length > 0;
  const elapsedMs = rounds.reduce(
    (sum, round) => sum + Math.max(0, (round.waitEndedAt ?? now) - round.waitStartedAt),
    0,
  );
  const waitingText = `${t("activity.working")} (${formatElapsed(
    waitingForContinuation ? continuationElapsedMs : elapsedMs,
  )})`;
  if (toolCount === 0 && toolText.trim().length > 0) {
    return (
      <>
        {thinkingNodes}
        <div className="turn-round">
          <div className="activity-centered">
            <StepsView text={toolText} />
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      {thinkingNodes}
      <div className="turn-round">
        <div className="activity live-tool-activity">
          <StepsView
            key={`tool-group-${rounds[0]?.id ?? "none"}-${lastRound?.id ?? "none"}`}
            text={toolText}
            running={toolRunning}
            trailingStatusText={
              lastRunning
                ? (!toolRunning && thinkingNodes.length === 0 ? waitingText : undefined)
                : undefined
            }
          />
        </div>
      </div>
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
  const { t, zoomLevel } = useApp();
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
    // offsetHeight is in the unscaled local frame; viewport clamps are scaled.
    const height = el.offsetHeight * zoomLevel;
    const maxTop = window.innerHeight - height - margin;
    const clampedTop = Math.max(margin, Math.min(flyoutPos.top, maxTop));
    if (clampedTop !== flyoutPos.top) {
      setFlyoutPos((prev) => (prev ? { ...prev, top: clampedTop } : prev));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flyoutOpen, flyoutPos?.top, flyoutPos?.left, zoomLevel]);

  const supported = new Set(reasoningEfforts.map((l) => l.toLowerCase()));
  const selectedLower = reasoningEffort.toLowerCase();
  const currentSeparator = currentModel.indexOf("/");
  const currentName = currentSeparator > 0
    ? currentModel.slice(currentSeparator + 1)
    : currentModel;
  const currentProvider = currentSeparator > 0
    ? currentModel.slice(0, currentSeparator)
    : "";
  const currentLabel = currentName || t("model.label");

  const FLYOUT_WIDTH = 220;
  const FLYOUT_GAP = 4;
  // Decide which side the flyout opens on based on available viewport space.
  // Same zoom-frame conversion as ContextMenu: rects and viewport clamps are
  // in scaled viewport pixels while the fixed flyout lives in the unscaled
  // local frame of .window-root, so only the final placement is divided by
  // the zoom factor.
  const openFlyout = () => {
    const el = entryRef.current;
    if (!el) {
      setFlyoutOpen(true);
      return;
    }
    const rect = el.getBoundingClientRect();
    const spaceRight = window.innerWidth - rect.right;
    const flyoutW = FLYOUT_WIDTH * zoomLevel;
    const gap = FLYOUT_GAP * zoomLevel;
    const side: "right" | "left" =
      spaceRight < flyoutW + gap && rect.left > spaceRight ? "left" : "right";
    const left =
      side === "right"
        ? rect.right + gap
        : rect.left - gap - flyoutW;
    cancelClose();
    setFlyoutPos({ side, left, top: rect.top });
    setFlyoutOpen(true);
  };

  return (
    <div className="dropdown model-dropdown" ref={ref}>
      <button className="dropdown-trigger" onClick={() => setOpen((v) => !v)}>
        <span className="model-trigger-label">
          <span className="model-trigger-name">{currentLabel}</span>
          {currentProvider && (
            <span className="model-provider-hint"> ({currentProvider})</span>
          )}
        </span>
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
                <span className="model-submenu-label">
                  <span>{currentLabel}</span>
                  {currentProvider && (
                    <span className="model-provider-sub">{currentProvider}</span>
                  )}
                </span>
                <Icon name="arrow-right" size={13} className="model-submenu-arrow" />
              </button>
              {flyoutOpen && flyoutPos && (
                <div
                  ref={flyoutRef}
                  className={`model-flyout ${flyoutPos.side}`}
                  style={{
                    left: flyoutPos.left / zoomLevel,
                    top: flyoutPos.top / zoomLevel,
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

export function LiveRoundView({
  round,
  now,
  forceSettled = false,
}: {
  round: TurnRound;
  now: number;
  forceSettled?: boolean;
}) {
  const { t } = useApp();
  const selection = String(round.selection || "").trim();
  if (selection) {
    return (
      <div className="ask-selection">
        <Icon name="check" size={13} className="ask-selection-icon" />
        <span className="ask-selection-label">{t("askMoreInfo.answerLabel")}</span>
        <span className="ask-selection-text">{selection}</span>
      </div>
    );
  }
  const running = !forceSettled && round.waitEndedAt === null;
  const effectiveEndedAt = forceSettled ? round.waitEndedAt ?? now : round.waitEndedAt;
  const elapsedMs = (effectiveEndedAt ?? now) - round.waitStartedAt;
  const elapsed = formatElapsed(elapsedMs);
  const answer = round.segments
    .filter((s) => s.kind === "answer")
    .map((s) => s.text)
    .join("");
  const toolText = round.segments
    .filter((s) => s.kind === "step")
    .map((s) => s.text)
    .join("");
  const hasAnswer = answer.trim().length > 0;
  const hasTools = toolText.trim().length > 0;
  const toolRunning = running && hasTools;
  const toolWaitingText =
    running && hasTools && !toolRunning && !Boolean(round.thinkingText)
      ? `${t("activity.working")} (${elapsed})`
      : undefined;
  const thinkingRunning = running && Boolean(round.thinkingText) && !round.thinkingEndedAt;
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
  return (
    <>
      {round.thinkingText && (
        <ThinkingPanel
          thinkingText={round.thinkingText}
          running={thinkingRunning}
          timerText={(() => {
            const startedAt = round.thinkingStartedAt ?? round.waitStartedAt;
            const endedAt = round.thinkingEndedAt ?? round.waitEndedAt ?? now;
            const clientElapsed = formatElapsed(endedAt - startedAt);
            const backendMs = (round as unknown as { backendElapsedMs?: number }).backendElapsedMs;
            const thoughtForElapsed = backendMs != null && backendMs > 0
              ? formatElapsed(backendMs) : clientElapsed;
            return thinkingRunning
              ? `${t("activity.thinking")} (${clientElapsed})`
              : `${t("activity.thoughtFor")} ${thoughtForElapsed}`;
          })()}
        />
      )}
      {hasTools && (
        <div className="turn-round">
          <div className="activity">
            <StepsView
              key={`live-round-${round.id}`}
              text={toolText}
              running={toolRunning}
              trailingStatusText={toolWaitingText}
            />
          </div>
        </div>
      )}
      {answer.trim().length > 0 && (
        <div className="answer">
          <MarkdownText text={answer} />
        </div>
      )}
      {!hasTools && !hasAnswer && !Boolean(round.thinkingText) && running && (
        <div className="activity">
          <div className="activity-header running">
            <span className="activity-text marquee">
              {t("activity.working")} ({elapsed})
            </span>
          </div>
        </div>
      )}
    </>
  );
}

export function TurnView({
  turn,
  now,
  negIndex,
  handlers,
  compactNotice,
}: {
  turn: Turn;
  now: number;
  negIndex: number;
  handlers: MessageHandlers;
  compactNotice: CompactNoticeData | null;
}) {
  const { t, state } = useApp();
  const [settle, setSettle] = useState(false);
  const wasRunningRef = useRef(turn.endedAt === null);

  // When a running live turn settles (task finished / interrupted / paused),
  // render the "Worked for" shell expanded first, then let RoundShell
  // auto-collapse it with an animation. useLayoutEffect keeps the transition
  // from flashing the collapsed state for a frame, and the ref makes it a
  // one-shot so later re-renders (timer ticks, etc.) don't re-trigger it.
  useLayoutEffect(() => {
    if (wasRunningRef.current && turn.endedAt !== null) {
      wasRunningRef.current = false;
      setSettle(true);
    }
  }, [turn.endedAt]);

  // A finished live turn (``endedAt`` set) must render collapsed into the
  // "Worked for" shell exactly like a reloaded history turn — otherwise the
  // finished task's tool calls stay expanded in the live layout until a manual
  // reload. This happens when the settled turn is still held in the live bucket
  // (e.g. the post-idle history reload came back with an empty page yet).
  if (turn.endedAt !== null) {
    return (
      <>
        <CompletedTurnView
          turn={liveTurnToHistoryTurn(turn)}
          negIndex={negIndex}
          handlers={handlers}
          settle={settle}
        />
        {compactNotice && (
          <div className="turn compact-notice-turn" role="alert" aria-live="polite">
            <CompactNoticeView
              title={compactNotice.title}
              body={compactNotice.body}
              stage={compactNotice.stage}
            />
          </div>
        )}
      </>
    );
  }
  const liveGroups = groupLiveRounds(turn.rounds);
  const { lastRound, hasPendingContinuation, showWorking } =
    getLiveTurnDisplayState(turn, liveGroups);
  const workingElapsed = lastRound
    ? formatElapsed(now - lastRound.waitStartedAt)
    : formatElapsed(now - turn.startedAt);
  
  const fileChanges = turn.fileChanges;
  
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
      {compactNotice && (
        <div className="turn compact-notice-turn" role="alert" aria-live="polite">
          <CompactNoticeView
            title={compactNotice.title}
            body={compactNotice.body}
            stage={compactNotice.stage}
          />
        </div>
      )}
      {liveGroups.map((group, index) => {
        if (group.kind === "tool") {
          return (
            <LiveToolGroupView
              key={`tool-${group.rounds[0]?.id ?? index}`}
              rounds={group.rounds}
              now={now}
              waitingForContinuation={index === liveGroups.length - 1 && hasPendingContinuation}
              continuationElapsedMs={
                index === liveGroups.length - 1 && lastRound
                  ? Math.max(0, now - lastRound.waitStartedAt)
                  : 0
              }
            />
          );
        }
        return (
          <LiveRoundView
            key={group.round.id}
            round={group.round}
            now={now}
            forceSettled={index < liveGroups.length - 1}
          />
        );
      })}
      {showWorking && (
        <div className="activity">
          <div className="activity-header running">
            <span className="activity-text marquee">
              {t("activity.working")} ({workingElapsed})
            </span>
          </div>
        </div>
      )}
      {fileChanges && <FileChangeList summary={fileChanges} t={t} workspaceRoot={state?.workspace?.root} />}
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
