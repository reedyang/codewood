import { useMemo, useState, type MouseEvent } from "react";
import { useApp } from "../state/AppContext";
import type { WorkspaceSummary } from "../api/types";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon } from "./Icon";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

function formatRelative(value?: string): string {
  if (!value) {
    return "";
  }
  const ts = Date.parse(value.replace(" ", "T"));
  if (Number.isNaN(ts)) {
    return "";
  }
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d`;
  const w = Math.floor(d / 7);
  if (w < 5) return `${w}w`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo`;
  return `${Math.floor(d / 365)}y`;
}

interface MenuState {
  x: number;
  y: number;
  items: MenuItem[];
}

type RenameTarget = { kind: "workspace" | "chat"; id: string; wsId: string; value: string };

interface ChatRow {
  id: string;
  name: string;
  updatedAt?: string;
  active?: boolean;
}

export function Sidebar({ onOpenSettings }: { onOpenSettings: () => void }) {
  const {
    state,
    connected,
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    t,
    runCommand,
    clearTurns,
    openWorkspaceInExplorer,
    toggleWorkspacePin,
    toggleChatPin,
    toggleChatArchive,
    archiveChats,
    toggleWorkspaceExpanded,
  } = useApp();

  const [menu, setMenu] = useState<MenuState | null>(null);
  const [rename, setRename] = useState<RenameTarget | null>(null);

  const workspaces = state?.workspaces ?? [];
  const activeChats: ChatRow[] = state?.chats ?? [];
  const activeWsId = state?.workspace.id ?? "";

  const pinnedWs = new Set(uiPrefs.pinnedWorkspaceIds);
  const pinnedChat = new Set(uiPrefs.pinnedChatIds);
  const archivedChat = new Set(uiPrefs.archivedChatIds);
  const expanded = new Set(expandedWorkspaceIds.length > 0 ? expandedWorkspaceIds : [activeWsId]);

  // Resolve every known chat (active workspace + loaded ones) for pin lookup.
  const allKnownChats = useMemo(() => {
    const map = new Map<string, ChatRow>();
    for (const c of activeChats) {
      map.set(c.id, c);
    }
    for (const list of Object.values(workspaceChats)) {
      for (const c of list) {
        if (!map.has(c.id)) {
          map.set(c.id, c);
        }
      }
    }
    return map;
  }, [activeChats, workspaceChats]);

  const pinnedWorkspaces = workspaces.filter((w) => pinnedWs.has(w.id));
  const pinnedChats = Array.from(allKnownChats.values()).filter(
    (c) => pinnedChat.has(c.id) && !archivedChat.has(c.id),
  );
  const hasPinned = pinnedWorkspaces.length > 0 || pinnedChats.length > 0;

  const chatsForWorkspace = (ws: WorkspaceSummary): ChatRow[] => {
    const list = ws.id === activeWsId ? activeChats : workspaceChats[ws.id] ?? [];
    return list.filter((c) => !archivedChat.has(c.id) && !pinnedChat.has(c.id));
  };

  const reloadingRun = async (command: string) => {
    clearTurns();
    await runCommand(command);
  };

  const switchChat = async (wsId: string, chatId: string) => {
    clearTurns();
    if (wsId && wsId !== activeWsId) {
      await runCommand(`/workspace switch ${wsId}`);
    }
    await runCommand(`/chat switch ${chatId}`);
  };

  const runChatCommand = async (wsId: string, command: string) => {
    if (wsId && wsId !== activeWsId) {
      await runCommand(`/workspace switch ${wsId}`);
    }
    await runCommand(command);
  };

  const startRename = (kind: "workspace" | "chat", id: string, wsId: string, value: string) =>
    setRename({ kind, id, wsId, value });

  const commitRename = async () => {
    const target = rename;
    setRename(null);
    if (!target) {
      return;
    }
    const value = target.value.trim();
    if (!value) {
      return;
    }
    if (target.kind === "workspace") {
      await runCommand(`/workspace rename ${target.id} ${quote(value)}`);
    } else {
      await runChatCommand(target.wsId, `/chat rename ${target.id} ${quote(value)}`);
    }
  };

  const openWorkspaceMenu = (e: MouseEvent, ws: WorkspaceSummary) => {
    e.preventDefault();
    const archivableIds = ws.id === activeWsId ? activeChats.map((c) => c.id) : [];
    const items: MenuItem[] = [
      {
        id: "pin",
        label: pinnedWs.has(ws.id) ? t("menu.unpinProject") : t("menu.pinProject"),
        onSelect: () => toggleWorkspacePin(ws.id),
      },
      {
        id: "open",
        label: t("menu.openInExplorer"),
        onSelect: () => void openWorkspaceInExplorer(ws.id),
      },
      {
        id: "rename",
        label: t("menu.renameProject"),
        onSelect: () => startRename("workspace", ws.id, ws.id, ws.name),
      },
      {
        id: "archive",
        label: t("menu.archiveChats"),
        disabled: archivableIds.length === 0,
        onSelect: () => archiveChats(archivableIds),
      },
      {
        id: "remove",
        label: t("menu.remove"),
        danger: true,
        onSelect: () => void reloadingRun(`/workspace delete ${ws.id}`),
      },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  const openChatMenu = (e: MouseEvent, chat: ChatRow, wsId: string) => {
    e.preventDefault();
    const items: MenuItem[] = [
      {
        id: "pin",
        label: pinnedChat.has(chat.id) ? t("menu.unpinChat") : t("menu.pinChat"),
        onSelect: () => toggleChatPin(chat.id),
      },
      {
        id: "archive",
        label: archivedChat.has(chat.id) ? t("menu.unarchiveChat") : t("menu.archiveChat"),
        onSelect: () => toggleChatArchive(chat.id),
      },
      {
        id: "rename",
        label: t("menu.rename"),
        onSelect: () => startRename("chat", chat.id, wsId, chat.name),
      },
      {
        id: "remove",
        label: t("menu.remove"),
        danger: true,
        onSelect: () => {
          clearTurns();
          void runChatCommand(wsId, `/chat delete ${chat.id}`);
        },
      },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  const renderRenameRow = () => (
    <div className="tree-rename">
      <input
        className="text-input"
        aria-label={t("menu.rename")}
        value={rename?.value ?? ""}
        autoFocus
        onChange={(e) => setRename((r) => (r ? { ...r, value: e.target.value } : r))}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            void commitRename();
          } else if (e.key === "Escape") {
            setRename(null);
          }
        }}
        onBlur={() => void commitRename()}
      />
    </div>
  );

  const isRenaming = (kind: "workspace" | "chat", id: string) =>
    rename?.kind === kind && rename.id === id;

  const renderChatRow = (chat: ChatRow, wsId: string) => {
    const isActive = wsId === activeWsId && chat.active;
    const rel = formatRelative(chat.updatedAt);
    return (
      <li
        key={`${wsId}-${chat.id}`}
        className={`tree-row chat-row ${isActive ? "active" : ""}`}
        onContextMenu={(e) => openChatMenu(e, chat, wsId)}
      >
        {isRenaming("chat", chat.id) ? (
          renderRenameRow()
        ) : (
          <>
            <button className="tree-label" title={chat.id} onClick={() => void switchChat(wsId, chat.id)}>
              {pinnedChat.has(chat.id) && <Icon name="pin" size={12} className="muted-icon" />}
              <span className="tree-name">{chat.name}</span>
              {rel && <span className="tree-meta">{rel}</span>}
            </button>
            <button
              className="tree-more"
              aria-label={t("menu.more")}
              onClick={(e) => openChatMenu(e, chat, wsId)}
            >
              <Icon name="dots" size={14} />
            </button>
          </>
        )}
      </li>
    );
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="btn-newchat" onClick={() => void reloadingRun("/chat new")}>
          <Icon name="new-chat" size={16} />
          {t("sidebar.newChat")}
        </button>
      </div>

      <div className="sidebar-scroll">
        {hasPinned && (
          <section className="tree-section">
            <div className="tree-section-title">{t("sidebar.pinned")}</div>
            <ul className="tree-list">
              {pinnedWorkspaces.map((ws) => (
                <li
                  key={`pw-${ws.id}`}
                  className={`tree-row ws-row ${ws.active ? "active" : ""}`}
                  onContextMenu={(e) => openWorkspaceMenu(e, ws)}
                >
                  <button className="tree-label" title={ws.root} onClick={() => toggleWorkspaceExpanded(ws.id)}>
                    <Icon name="pin" size={12} className="muted-icon" />
                    <span className="tree-name">{ws.name}</span>
                  </button>
                </li>
              ))}
              {pinnedChats.map((chat) => renderChatRow(chat, activeWsId))}
            </ul>
          </section>
        )}

        <section className="tree-section">
          <div className="tree-section-title">{t("sidebar.workspaces")}</div>
          <ul className="tree-list">
            {workspaces.map((ws) => {
              const open = expanded.has(ws.id);
              const chats = chatsForWorkspace(ws);
              return (
                <li key={ws.id} className="tree-group">
                  <div
                    className={`tree-row ws-row ${ws.active ? "active" : ""}`}
                    onContextMenu={(e) => openWorkspaceMenu(e, ws)}
                  >
                    {isRenaming("workspace", ws.id) ? (
                      renderRenameRow()
                    ) : (
                      <>
                        <button
                          className="tree-label"
                          title={ws.root}
                          onClick={() => toggleWorkspaceExpanded(ws.id)}
                        >
                          <Icon name="chevron" size={14} className={`chevron ${open ? "open" : ""}`} />
                          <Icon name="folder" size={15} className="muted-icon" />
                          <span className="tree-name">{ws.name}</span>
                        </button>
                        <button
                          className="tree-more"
                          aria-label={t("menu.more")}
                          onClick={(e) => openWorkspaceMenu(e, ws)}
                        >
                          <Icon name="dots" size={14} />
                        </button>
                      </>
                    )}
                  </div>

                  {open && (
                    <ul className="tree-list tree-children">
                      {chats.length === 0 ? (
                        <li className="tree-empty">{t("sidebar.noChats")}</li>
                      ) : (
                        chats.map((chat) => renderChatRow(chat, ws.id))
                      )}
                    </ul>
                  )}
                </li>
              );
            })}
            {workspaces.length === 0 && <li className="tree-empty">{t("sidebar.noWorkspaces")}</li>}
          </ul>
        </section>
      </div>

      <div className="sidebar-footer">
        <button className="settings-btn" onClick={onOpenSettings}>
          <span className={`status-dot ${connected ? "online" : "offline"}`} />
          <Icon name="gear" size={16} />
          {t("nav.settings")}
        </button>
      </div>

      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
    </aside>
  );
}
