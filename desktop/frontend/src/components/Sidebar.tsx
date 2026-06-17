import { useMemo, useState, type MouseEvent } from "react";
import { useApp } from "../state/AppContext";
import type { WorkspaceSummary } from "../api/types";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon } from "./Icon";
import { buildChatMenuItems, chatKey } from "./chatMenu";

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
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    busyByChat,
    t,
    runCommand,
    clearTurns,
    switchToChat,
    newChat,
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
  const expanded = new Set(expandedWorkspaceIds);

  // Chat ids are only unique within a workspace, so pin/archive state must be
  // keyed by workspace + chat to avoid hiding same-id chats in other workspaces.
  const isPinnedChat = (wsId: string, chatId: string) => pinnedChat.has(chatKey(wsId, chatId));
  const isArchivedChat = (wsId: string, chatId: string) => archivedChat.has(chatKey(wsId, chatId));

  // Chats per workspace (active workspace sourced from live state).
  const chatsByWorkspace = useMemo(() => {
    const map: Record<string, ChatRow[]> = { ...workspaceChats };
    if (activeWsId) {
      map[activeWsId] = activeChats;
    }
    return map;
  }, [activeChats, workspaceChats, activeWsId]);

  const pinnedWorkspaces = workspaces.filter((w) => pinnedWs.has(w.id));
  const unpinnedWorkspaces = workspaces.filter((w) => !pinnedWs.has(w.id));
  const pinnedChatEntries: { chat: ChatRow; wsId: string }[] = [];
  for (const [wsId, list] of Object.entries(chatsByWorkspace)) {
    for (const chat of list) {
      if (isPinnedChat(wsId, chat.id) && !isArchivedChat(wsId, chat.id)) {
        pinnedChatEntries.push({ chat, wsId });
      }
    }
  }
  const hasPinned = pinnedWorkspaces.length > 0 || pinnedChatEntries.length > 0;

  const chatsForWorkspace = (ws: WorkspaceSummary): ChatRow[] => {
    const list = chatsByWorkspace[ws.id] ?? [];
    return list.filter((c) => !isArchivedChat(ws.id, c.id) && !isPinnedChat(ws.id, c.id));
  };

  const reloadingRun = async (command: string) => {
    clearTurns();
    await runCommand(command);
  };

  const switchChat = async (wsId: string, chatId: string) => {
    await switchToChat(chatId, wsId && wsId !== activeWsId ? wsId : "");
  };

  const newChatInWorkspace = async (wsId: string) => {
    if (!expanded.has(wsId)) {
      toggleWorkspaceExpanded(wsId);
    }
    if (wsId && wsId !== activeWsId) {
      clearTurns();
      await runCommand(`/workspace switch ${wsId}`);
    }
    await newChat();
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
    const archivableIds = ws.id === activeWsId ? activeChats.map((c) => chatKey(ws.id, c.id)) : [];
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
      // The Default workspace cannot be renamed.
      ...(ws.isDefault
        ? []
        : [
            {
              id: "rename",
              label: t("menu.renameProject"),
              onSelect: () => startRename("workspace", ws.id, ws.id, ws.name),
            },
          ]),
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
    const items = buildChatMenuItems({
      t,
      isPinned: isPinnedChat(wsId, chat.id),
      isArchived: isArchivedChat(wsId, chat.id),
      onTogglePin: () => toggleChatPin(chatKey(wsId, chat.id)),
      onToggleArchive: () => toggleChatArchive(chatKey(wsId, chat.id)),
      onRename: () => startRename("chat", chat.id, wsId, chat.name),
      onRemove: () => {
        clearTurns();
        void runChatCommand(wsId, `/chat delete ${chat.id}`);
      },
    });
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

  // Chat ids repeat across workspaces, so the rename target must match the
  // exact workspace too, otherwise the input would open on a same-id sibling.
  const isRenamingChat = (wsId: string, id: string) =>
    rename?.kind === "chat" && rename.wsId === wsId && rename.id === id;

  const renderChatRow = (chat: ChatRow, wsId: string) => {
    const isActive = wsId === activeWsId && chat.active;
    const rel = formatRelative(chat.updatedAt);
    const isPinned = isPinnedChat(wsId, chat.id);
    // Chat ids are only unique within a workspace, so only trust the running
    // marker for chats in the active workspace to avoid false positives.
    const isBusy = wsId === activeWsId && Boolean(busyByChat[chat.id]);
    return (
      <li
        key={`${wsId}-${chat.id}`}
        className={`tree-row chat-row ${isPinned ? "chat-row-pinned" : ""} ${isActive ? "active" : ""} ${isBusy ? "chat-row-busy" : ""}`}
        onContextMenu={(e) => openChatMenu(e, chat, wsId)}
      >
        {isRenamingChat(wsId, chat.id) ? (
          renderRenameRow()
        ) : (
          <>
            <button className="tree-label" title={chat.id} onClick={() => void switchChat(wsId, chat.id)}>
              <span className="tree-name">{chat.name}</span>
              {isBusy && <span className="chat-busy-dot" aria-label={t("chat.busy")} title={t("chat.busy")} />}
              {rel && <span className="tree-meta">{rel}</span>}
            </button>
            <button
              className="chat-pin-btn"
              aria-label={isPinned ? t("menu.unpinChat") : t("menu.pinChat")}
              title={isPinned ? t("menu.unpinChat") : t("menu.pinChat")}
              onClick={(e) => {
                e.stopPropagation();
                toggleChatPin(chatKey(wsId, chat.id));
              }}
            >
              <Icon name={isPinned ? "pin-filled" : "pin"} size={15} />
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

  const renderWorkspaceGroup = (ws: WorkspaceSummary) => {
    const open = expanded.has(ws.id);
    const chats = chatsForWorkspace(ws);
    return (
      <li key={ws.id} className="tree-group">
        <div className="tree-row ws-row" onContextMenu={(e) => openWorkspaceMenu(e, ws)}>
          {isRenaming("workspace", ws.id) ? (
            renderRenameRow()
          ) : (
            <>
              <button
                className="tree-label ws-label"
                title={ws.root}
                onClick={() => toggleWorkspaceExpanded(ws.id)}
              >
                <Icon name={open ? "folder-open" : "folder"} size={15} className="muted-icon" />
                <span className="tree-name">{ws.name}</span>
                <Icon name="chevron" size={14} className={`chevron tree-inline-chevron ${open ? "open" : ""}`} />
              </button>
              <span className="tree-flex" />
              <button
                className="tree-more"
                aria-label={t("menu.more")}
                onClick={(e) => openWorkspaceMenu(e, ws)}
              >
                <Icon name="dots" size={14} />
              </button>
              <button
                className="tree-more"
                aria-label={t("sidebar.newChat")}
                title={t("sidebar.newChat")}
                onClick={(e) => {
                  e.stopPropagation();
                  void newChatInWorkspace(ws.id);
                }}
              >
                <Icon name="new-chat" size={14} />
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
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="btn-newchat" onClick={() => void newChat()}>
          <Icon name="new-chat" size={16} />
          {t("sidebar.newChat")}
        </button>
      </div>

      <div className="sidebar-scroll">
        {hasPinned && (
          <section className="tree-section">
            <div className="tree-section-title">{t("sidebar.pinned")}</div>
            <ul className="tree-list">
              {pinnedWorkspaces.map((ws) => renderWorkspaceGroup(ws))}
              {pinnedChatEntries.map(({ chat, wsId }) => renderChatRow(chat, wsId))}
            </ul>
          </section>
        )}

        <section className="tree-section">
          <div className="tree-section-title">{t("sidebar.workspaces")}</div>
          <ul className="tree-list">
            {unpinnedWorkspaces.map((ws) => renderWorkspaceGroup(ws))}
            {workspaces.length === 0 && <li className="tree-empty">{t("sidebar.noWorkspaces")}</li>}
          </ul>
        </section>
      </div>

      <div className="sidebar-footer">
        <button className="settings-btn" onClick={onOpenSettings}>
          <Icon name="gear" size={16} />
          {t("nav.settings")}
        </button>
      </div>

      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
    </aside>
  );
}
