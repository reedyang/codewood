import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../state/AppContext";
import type { HistoryTurn, Turn } from "../api/types";
import { Icon } from "./Icon";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
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
    t,
  } = useApp();
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const prevHeightRef = useRef<number | null>(null);

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
    const message = [...attachments, text].filter(Boolean).join("\n");
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
                  {p === currentPolicy && <Icon name="check" size={13} />}
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
                models.map((m) => (
                  <button
                    key={m}
                    className={`dropdown-item ${m === currentModel ? "active" : ""}`}
                    onClick={() => {
                      close();
                      void setModel(m);
                    }}
                  >
                    {m === currentModel && <Icon name="check" size={13} />}
                    <span>{m}</span>
                  </button>
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

  if (turns.length === 0 && historyTurns.length === 0 && !historyLoading) {
    const workspaceName = state?.workspace.name || t("workspace.none");
    return (
      <div className="chat-view">
        <div className="empty-state">
          <h1 className="empty-title">{t("empty.prompt").replace("{workspace}", workspaceName)}</h1>
          <div className="empty-composer">
            {composer}
            <WorkspaceSelector />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-view">
      <div className="transcript" ref={scrollRef} onScroll={onScroll}>
        {historyStart > 0 && (
          <div className="history-more">
            {historyLoading ? t("history.loading") : t("history.more")}
          </div>
        )}
        {historyTurns.map((turn, index) => (
          <HistoryTurnView key={`h-${index}`} turn={turn} />
        ))}
        {turns.map((turn, index) => (
          <TurnView
            key={turn.id}
            turn={turn}
            now={now}
            active={busy && index === turns.length - 1 && turn.endedAt === null}
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

function HistoryTurnView({ turn }: { turn: HistoryTurn }) {
  const { t } = useApp();
  const [expanded, setExpanded] = useState(false);
  const hasSteps = turn.steps.trim().length > 0;
  const hasAnswer = turn.answer.trim().length > 0;

  return (
    <div className="turn">
      {turn.userText && (
        <div className="entry entry-input">
          <span className="entry-label">{t("chat.you")}</span>
          <div className="entry-text">{turn.userText}</div>
        </div>
      )}

      {hasSteps && (
        <div className="activity">
          <button className="activity-header" onClick={() => setExpanded((v) => !v)}>
            <span className="activity-text">
              {`${t("activity.workedFor")} ${formatElapsed((turn.elapsedSeconds ?? 0) * 1000)}`}
            </span>
            <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
          </button>
          {expanded && <pre className="activity-steps">{turn.steps}</pre>}
        </div>
      )}

      {hasAnswer && <pre className="answer">{turn.answer}</pre>}
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

function TurnView({ turn, now, active }: { turn: Turn; now: number; active: boolean }) {
  const { t } = useApp();
  const [expanded, setExpanded] = useState(active);

  // Collapse the steps automatically once the turn finishes.
  useEffect(() => {
    setExpanded(active);
  }, [active]);

  const steps = turn.segments.filter((s) => s.kind === "step");
  const answer = turn.segments
    .filter((s) => s.kind === "answer")
    .map((s) => s.text)
    .join("");
  const stepText = steps.map((s) => s.text).join("");
  const elapsedMs = (turn.endedAt ?? now) - turn.startedAt;
  const elapsed = formatElapsed(elapsedMs);
  const hasSteps = stepText.trim().length > 0;

  return (
    <div className="turn">
      {turn.userText && (
        <div className="entry entry-input">
          <span className="entry-label">{t("chat.you")}</span>
          <div className="entry-text">{turn.userText}</div>
        </div>
      )}

      {(hasSteps || active) && (
        <div className="activity">
          <button
            className={`activity-header ${active ? "running" : ""}`}
            onClick={() => hasSteps && setExpanded((v) => !v)}
            disabled={!hasSteps}
          >
            <span className={`activity-text ${active ? "marquee" : ""}`}>
              {active
                ? `${t("activity.working")} (${elapsed})`
                : `${t("activity.workedFor")} ${elapsed}`}
            </span>
            {hasSteps && <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />}
          </button>
          {hasSteps && expanded && <pre className="activity-steps">{stepText}</pre>}
        </div>
      )}

      {answer.trim().length > 0 && <pre className="answer">{answer}</pre>}
    </div>
  );
}

function WorkspaceSelector() {
  const { state, runCommand, clearTurns, selectWorkspace, pickFolder, t } = useApp();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [newPath, setNewPath] = useState("");
  const ref = useOutsideClose(open, () => setOpen(false));

  const workspaces = state?.workspaces ?? [];
  const current = state?.workspace.name || t("workspace.none");
  const defaultWs = workspaces.find((w) => w.isDefault);
  const filtered = workspaces.filter((w) =>
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
        <span className="ws-selector-label">{current}</span>
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
                  className={`ws-selector-item ${ws.active ? "active" : ""}`}
                  onClick={() => void switchTo(ws.id)}
                >
                  {ws.active && <Icon name="check" size={13} />}
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
          {defaultWs && !defaultWs.active && (
            <button className="ws-selector-item" onClick={() => void switchTo(defaultWs.id)}>
              {t("workspace.none")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
