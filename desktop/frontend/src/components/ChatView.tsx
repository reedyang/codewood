import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../state/AppContext";
import type { HistoryRound, HistoryTurn, Turn, TurnRound } from "../api/types";
import { Icon } from "./Icon";
import { MarkdownText } from "./Markdown";
import { StepsView } from "./Steps";
import { ChatTitleBar } from "./ChatTitleBar";

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
  return (
    <div className="user-message">
      <div className="entry-input">
        <span className="entry-label">{t("chat.you")}</span>
        <div className="entry-text">{text}</div>
      </div>
      <div className="entry-actions">
        <span className="entry-time">{time}</span>
        <div className="entry-action-buttons">
          <button
            className="entry-action-btn"
            title={t("msg.copy")}
            aria-label={t("msg.copy")}
            onClick={() => {
              handlers.onCopy(text);
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
    pickFiles,
    forkChat,
    editChat,
    draftMode,
    draftWorkspaceId,
    setDraftWorkspace,
    t,
  } = useApp();
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const prevHeightRef = useRef<number | null>(null);

  const messageHandlers: MessageHandlers = {
    onCopy: (text) => {
      void navigator.clipboard?.writeText(text);
    },
    onEdit: (index, text) => {
      setDraft(text);
      composerRef.current?.focus();
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

  const canSend = Boolean(draft.trim()) || attachments.length > 0;

  const submit = async () => {
    const text = draft.trim();
    if (!text && attachments.length === 0) {
      return;
    }
    // Attach files by reference (path), not by inlining content. This mirrors
    // Codex's approach: the agent reads each file with its tools and decides how
    // much to load, which avoids blowing the context window on large files.
    let message = text;
    if (attachments.length > 0) {
      const list = attachments.map((p) => `- ${p}`).join("\n");
      const block = `${t("attach.contextHeader")}\n${list}`;
      message = text ? `${block}\n\n${text}` : block;
    }
    setDraft("");
    setAttachments([]);
    await sendInput(message);
  };

  const addFiles = async () => {
    const picked = await pickFiles();
    if (picked.length === 0) {
      return;
    }
    setAttachments((prev) => {
      const seen = new Set(prev);
      const merged = [...prev];
      for (const p of picked) {
        if (!seen.has(p)) {
          seen.add(p);
          merged.push(p);
        }
      }
      return merged;
    });
  };

  const removeAttachment = (path: string) =>
    setAttachments((prev) => prev.filter((p) => p !== path));

  const currentPolicy = state?.executionPolicy || "moderate";
  const currentModel = state?.model.current || "";
  const models = state?.model.available ?? [];

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
      {attachments.length > 0 && (
        <div className="attachments">
          {attachments.map((path) => (
            <span className="attachment-chip" key={path} title={path}>
              <Icon name="info" size={13} className="muted-icon" />
              <span className="attachment-name">{baseName(path)}</span>
              <span className="attachment-ext">{fileExt(path)}</span>
              <button
                className="attachment-remove"
                aria-label={t("attach.remove")}
                onClick={() => removeAttachment(path)}
              >
                <Icon name="win-close" size={10} />
              </button>
            </span>
          ))}
        </div>
      )}
      <textarea
        ref={composerRef}
        className="composer-input"
        value={draft}
        placeholder={t("chat.inputPlaceholder")}
        rows={3}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void submit();
          }
        }}
      />
      <div className="composer-toolbar">
        <div className="composer-left">
          <button className="icon-btn round" aria-label={t("attach.add")} title={t("attach.add")} onClick={() => void addFiles()}>
            <Icon name="plus" size={16} />
          </button>
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
            <Dropdown
              trigger={
                <>
                  <span>{currentModel || t("model.label")}</span>
                  <Icon name="chevron" size={13} className="chevron down" />
                </>
              }
              className="model-dropdown"
              align="right"
            >
              {(close) =>
                groupModelsByProvider(models).map((group) => (
                  <div className="model-group" key={group.provider}>
                    <div className="model-group-header">{group.provider}</div>
                    {group.items.map((item) => (
                      <button
                        key={item.selector}
                        className={`dropdown-item ${item.selector === currentModel ? "active" : ""}`}
                        onClick={() => {
                          close();
                          void setModel(item.selector);
                        }}
                      >
                        <span className="dropdown-check">
                          {item.selector === currentModel && <Icon name="check" size={13} />}
                        </span>
                        <span>{item.name}</span>
                      </button>
                    ))}
                  </div>
                ))
              }
            </Dropdown>
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
      </div>
      <div className="composer-dock">
        {composer}
        <WorkspaceSelector />
      </div>
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
  return (
    <div className="turn-round">
      {showTimer && (
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
      )}
      {textNode}
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
