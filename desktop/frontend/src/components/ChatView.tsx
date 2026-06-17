import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../state/AppContext";
import type { HistoryRound, HistoryTurn, Turn, TurnRound } from "../api/types";
import { Icon } from "./Icon";
import { MarkdownText } from "./Markdown";
import { StepsView } from "./Steps";
import { ChatTitleBar } from "./ChatTitleBar";
import { AskMoreInfoPanel } from "./AskMoreInfoPanel";
import { decodeAttachments } from "../utils/attachments";
import {
  composeMessageText,
  encodeHiddenInstruction,
  parseMessageToSegments,
  stripHiddenControl,
  stripPlanModePrefix,
} from "../utils/tokens";
import type { Segment } from "../utils/tokens";
import { RichComposer } from "./RichComposer";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

interface ModelGroup {
  provider: string;
  items: { selector: string; name: string }[];
}

/** Group "provider:name" model selectors under their provider, preserving order. */
function groupModelsByProvider(selectors: string[]): ModelGroup[] {
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
  const { paths: attachedPaths, body } = decodeAttachments(text);
  // Backend prepends a Plan-mode directive to outgoing user messages while
  // ``_plan_mode_sticky`` is on; it lives in chat history so the model sees
  // the same instruction context on reload, but the GUI should never show
  // it as if the user typed it. ``stripHiddenControl`` then unwraps the
  // CONTROL envelope used by the GUI's own "Execute now" nudge.
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
        {visibleBody && <div className="entry-text">{visibleBody}</div>}
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
    turns,
    historyTurns,
    historyStart,
    historyLoading,
    loadOlderHistory,
    busy,
    now,
    sendInput,
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
    t,
  } = useApp();
  // Drafts (in-progress composer segments) are kept per chat so switching
  // between chats never bleeds an unsent message into a sibling. A synthetic
  // key is used while we're still in "draft mode" (no chat exists yet) so
  // that first composition survives until the user sends or discards.
  const DRAFT_KEY = "__draft__";
  const draftKey = draftMode ? DRAFT_KEY : state?.activeChatId || "";
  const [segmentsByChat, setSegmentsByChat] = useState<Record<string, Segment[]>>({});
  const segments = segmentsByChat[draftKey] ?? [];
  // Per-chat compose mode (Agent or Plan). Stored only in-memory: switching
  // chats restores the last-known mode for that chat without persisting it
  // across restarts (so re-opening a project doesn't trap the user in Plan
  // mode they forgot to leave). The Plan mode is conveyed to the agent by
  // prepending a planning instruction to the outgoing message; the backend
  // doesn't need a dedicated flag for this minimal implementation.
  const [chatModeMap, setChatModeMap] = useState<Record<string, ChatMode>>({});
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

  // Sync the agent's sticky plan-mode flag with the active chat's mode so
  // that switching back into a chat that was last left in Plan mode keeps
  // the runtime injection in step with the visible "Plan" badge. We
  // intentionally don't persist the per-chat mode across restarts — the
  // sticky flag itself is process-scoped — so a fresh launch starts every
  // chat in Agent mode.
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
      // Restore the original segment list so the composer re-renders the
      // same attachment chips and prose that the user originally sent.
      setSegments(parseMessageToSegments(text));
      void editChat(index);
    },
    onFork: (index) => {
      void forkChat(index);
    },
  };

  // Live updates and chat switches stick to the bottom.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) {
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
      el.scrollTop = el.scrollHeight - prevHeightRef.current;
      prevHeightRef.current = null;
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }, [historyTurns]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el || historyLoading || historyStart <= 0) {
      return;
    }
    if (el.scrollTop < 80) {
      prevHeightRef.current = el.scrollHeight;
      void loadOlderHistory();
    }
  };

  const canSend = draftText.trim().length > 0 || attachmentPaths.length > 0;

  const submit = async () => {
    if (!canSend) {
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
    const message = composeMessageText(segments);
    setSegments([]);
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
  const reasoningLevels = state?.model.reasoningLevels ?? [];
  const reasoningLevel = state?.model.reasoningLevel || "";

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

  const composer = (
    <div className="composer">
      <RichComposer
        segments={segments}
        onChange={setSegments}
        onSubmit={() => void submit()}
        placeholder={t("chat.inputPlaceholder")}
        rows={3}
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
              reasoningLevels={reasoningLevels}
              reasoningLevel={reasoningLevel}
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
            className={`send-btn ${busy ? "is-stop" : ""}`}
            aria-label={busy ? t("chat.interrupt") : t("chat.send")}
            disabled={!busy && !canSend}
            onClick={() => (busy ? void interrupt() : void submit())}
          >
            <Icon name={busy ? "stop" : "send"} size={16} />
          </button>
        </div>
      </div>
    </div>
  );

  const showEmpty =
    draftMode ||
    (turns.length === 0 && historyTurns.length === 0 && !historyLoading);
  if (showEmpty) {
    // In draft mode the greeting reflects the chosen draft workspace; otherwise
    // it reflects the active workspace. The Default workspace is not a real
    // project, so omit its name from the greeting.
    const workspaces = state?.workspaces ?? [];
    const targetWs = draftMode
      ? workspaces.find((w) => w.id === draftWorkspaceId)
      : workspaces.find((w) => w.active);
    const inDefaultWs = !targetWs || targetWs.isDefault;
    const workspaceName = targetWs?.name || state?.workspace.name || "";
    const emptyTitle = inDefaultWs
      ? t("empty.promptNoWorkspace")
      : t("empty.prompt").replace("{workspace}", workspaceName);
    return (
      <div className="chat-view">
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
      </div>
    );
  }

  return (
    <div className="chat-view">
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
        {(() => {
          // The Execute-now button represents "carry out the plan we just
          // drafted". Gate it on the actual plan rather than the composer's
          // mode so:
          //   * Switching the composer to Agent mode after seeing the plan
          //     does NOT make the button vanish — the plan is still the
          //     latest thing the model produced and the user might still
          //     want to advance it.
          //   * Chats with no plan (or a fully-completed plan) never see
          //     the button, even in Plan mode.
          // We also wait until the streaming turn has fully closed so the
          // button doesn't appear before the rendered plan content lands.
          //
          // After an app restart there are no live ``turns`` (the plan turn
          // lives in ``historyTurns`` instead), so we must NOT require a live
          // turn — the presence of an unfinished plan in ``state`` plus an
          // idle agent and at least one rendered turn is enough. We only
          // suppress the button while a LIVE turn is mid-stream.
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
          const planSteps = state?.plan?.plan ?? [];
          const hasUnfinished = planSteps.some(
            (s) => s.status !== "completed",
          );
          if (!hasUnfinished) {
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
                <span>{t("composer.executeNow")}</span>
              </button>
              <span className="plan-execute-hint">{t("composer.executeNowHint")}</span>
            </div>
          );
        })()}
      </div>
      <div className="composer-dock">{composer}</div>
    </div>
  );
}

/** One model round laid out in natural order: the tool group first (its wait
 *  timer + collapsible tool output), then the model's natural-language reply.
 *  `running` marks a live, in-flight round so the timer animates. `autoExpand`
 *  keeps the tool output open while the turn is still streaming — it stays open
 *  across the brief round_end→round_start gap between back-to-back tool calls,
 *  so the tools don't collapse-then-reopen. It only auto-collapses once the
 *  whole turn settles. The timer shows only for a tool group (or while live). */
function RoundShell({
  timerText,
  running,
  autoExpand,
  toolText,
  textNode,
}: {
  timerText: string;
  running: boolean;
  autoExpand: boolean;
  toolText: string;
  textNode: ReactNode;
}) {
  const hasTools = toolText.trim().length > 0;
  const [expanded, setExpanded] = useState(autoExpand);
  useEffect(() => {
    setExpanded(autoExpand);
  }, [autoExpand]);

  const showTimer = hasTools || running;
  // For a pure answer round (no tools) that is still live, the model has
  // already produced this reply and is now thinking about the next step — so
  // the running "Working" timer reads more naturally BELOW the answer text.
  const timerBelow = running && !hasTools;
  const timer = showTimer ? (
    <div className="activity">
      <button
        className={`activity-header ${running ? "running" : ""}`}
        onClick={() => hasTools && setExpanded((v) => !v)}
        disabled={!hasTools}
      >
        <span className={`activity-text ${running ? "marquee" : ""}`}>{timerText}</span>
        {hasTools && (
          <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
        )}
      </button>
      {hasTools && expanded && <StepsView text={toolText} />}
    </div>
  ) : null;
  return (
    <div className="turn-round">
      {!timerBelow && timer}
      {textNode}
      {timerBelow && timer}
    </div>
  );
}

function HistoryRoundView({ round }: { round: HistoryRound }) {
  const { t } = useApp();
  const timerText = `${t("activity.workedFor")} ${formatElapsed(round.waitSeconds * 1000)}`;
  return (
    <RoundShell
      timerText={timerText}
      running={false}
      autoExpand={false}
      toolText={round.tools}
      textNode={
        round.text.trim().length > 0 ? (
          <div className="answer">
            <MarkdownText text={round.text} />
          </div>
        ) : null
      }
    />
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
      {turn.rounds.map((round, index) => (
        <HistoryRoundView key={index} round={round} />
      ))}
    </div>
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

const FIXED_REASONING_EFFORTS = ["low", "medium", "high"] as const;

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
  const title = `${label}: ${pct}% (${tokens} / ${ctxWindow})`;
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
  reasoningLevels,
  reasoningLevel,
  onSelectModel,
  onSelectReasoning,
}: {
  models: string[];
  currentModel: string;
  reasoningLevels: string[];
  reasoningLevel: string;
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

  const supported = new Set(reasoningLevels.map((l) => l.toLowerCase()));
  const selectedLower = reasoningLevel.toLowerCase();
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
        {reasoningLevel && (
          <span className="model-reasoning-level">{reasoningLevel}</span>
        )}
        <Icon name="chevron" size={13} className="chevron down" />
      </button>
      {open && (
        <div className="dropdown-menu align-right">
          <div className="model-group">
            <div className="model-group-header">{t("reasoning.label")}</div>
            {FIXED_REASONING_EFFORTS.map((level) => {
              const enabled = supported.has(level);
              const active = enabled && level === selectedLower;
              return (
                <button
                  key={level}
                  className={`dropdown-item ${active ? "active" : ""}`}
                  disabled={!enabled}
                  onClick={() => {
                    if (!enabled) return;
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
  turnActive,
}: {
  round: TurnRound;
  now: number;
  turnActive: boolean;
}) {
  const { t } = useApp();
  const running = round.waitEndedAt === null;
  const elapsedMs = (round.waitEndedAt ?? now) - round.waitStartedAt;
  const elapsed = formatElapsed(elapsedMs);
  const timerText = running
    ? `${t("activity.working")} (${elapsed})`
    : `${t("activity.workedFor")} ${elapsed}`;
  const answer = round.segments
    .filter((s) => s.kind === "answer")
    .map((s) => s.text)
    .join("");
  const toolText = round.segments
    .filter((s) => s.kind === "step")
    .map((s) => s.text)
    .join("");
  return (
    <RoundShell
      timerText={timerText}
      running={running}
      // Keep tools expanded while the turn is still streaming so back-to-back
      // tool calls (round_end then round_start on the same merged group) don't
      // collapse and immediately re-open. Collapse only once the turn settles.
      autoExpand={turnActive}
      toolText={toolText}
      textNode={
        answer.trim().length > 0 ? (
          <div className="answer">
            <MarkdownText text={answer} />
          </div>
        ) : null
      }
    />
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
      {turn.rounds.map((round) => (
        <LiveRoundView
          key={round.id}
          round={round}
          now={now}
          turnActive={turn.endedAt === null}
        />
      ))}
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
  const { state, runCommand, clearTurns, selectWorkspace, pickFolder, t } = useApp();
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
    : workspaces.find((w) => w.active)?.id || "";
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
        <span className="ws-selector-label">{t("workspace.selectorLabel")}</span>
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
