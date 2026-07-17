import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { useApp } from "../state/AppContext";
import type { WorkspaceSummary } from "../api/types";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { Icon } from "./Icon";
import { HoverTooltip } from "./HoverTooltip";
import { buildChatMenuItems, chatKey } from "./chatMenu";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

function formatRelative(value?: string, now = Date.now()): string {
  if (!value) {
    return "";
  }
  const ts = Date.parse(value.replace(" ", "T"));
  if (Number.isNaN(ts)) {
    return "";
  }
  const s = Math.max(0, Math.floor((now - ts) / 1000));
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

function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
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
  archived?: boolean;
  /** True while this chat's agent loop is mid-turn (from the state snapshot).
   *  Used as a durable busy signal that survives focus changes/reloads, in
   *  addition to the transient SSE-driven ``busyByChat`` flags. */
  running?: boolean;
}

export function Sidebar({ collapsed, onOpenSettings }: { collapsed: boolean; onOpenSettings: () => void }) {
  const {
    state,
    activeWorkspaceId,
    activeChatId,
    activeChats,
    uiPrefs,
    workspaceChats,
    expandedWorkspaceIds,
    busyByChat,
    runningChatStartedAtByChat,
    unreadChatIds,
    now,
    t,
    runCommand,
    switchToChat,
    newChat,
    deleteChat,
    openWorkspaceInExplorer,
    deleteWorkspace,
    toggleWorkspacePin,
    toggleChatPin,
    toggleChatArchive,
    archiveChats,
    toggleWorkspaceExpanded,
    refreshWorkspaceChats,
    client,
    draftMode,
  } = useApp();

  const [menu, setMenu] = useState<MenuState | null>(null);
  const [rename, setRename] = useState<RenameTarget | null>(null);
  const [chatToDelete, setChatToDelete] = useState<{ id: string; wsId: string; name: string } | null>(null);
  const [chatLoadCounts, setChatLoadCounts] = useState<Record<string, number>>({});
  const CHAT_PAGE_SIZE = 5;

  const loadMoreChats = (wsId: string) => {
    setChatLoadCounts((prev) => ({
      ...prev,
      [wsId]: (prev[wsId] ?? CHAT_PAGE_SIZE) + CHAT_PAGE_SIZE,
    }));
  };

  const getVisibleCount = (wsId: string) => chatLoadCounts[wsId] ?? CHAT_PAGE_SIZE;

  const workspaces = state?.workspaces ?? [];
  const activeWsId = activeWorkspaceId;
  const backendWsId = state?.workspace.id ?? "";

  const pinnedWs = new Set(uiPrefs.pinnedWorkspaceIds);
  const pinnedChat = new Set(uiPrefs.pinnedChatIds);
  const expanded = new Set(expandedWorkspaceIds);

  const isPinnedChat = (wsId: string, chatId: string) => pinnedChat.has(chatKey(wsId, chatId));

  const workspaceNameById = useMemo(() => {
    const map: Record<string, string> = {};
    for (const ws of workspaces) {
      map[ws.id] = ws.name;
    }
    return map;
  }, [workspaces]);

  // Chats per workspace (active workspace sourced from live state).
  const chatsByWorkspace = useMemo(() => {
    const map: Record<string, ChatRow[]> = {};
    for (const [wsId, list] of Object.entries(workspaceChats)) {
      map[wsId] = [...list].sort((a, b) => {
        const aTime = a.updatedAt ? new Date(a.updatedAt).getTime() : 0;
        const bTime = b.updatedAt ? new Date(b.updatedAt).getTime() : 0;
        return bTime - aTime;
      });
    }
    if (activeWsId) {
      map[activeWsId] = [...activeChats].sort((a, b) => {
        const aTime = a.updatedAt ? new Date(a.updatedAt).getTime() : 0;
        const bTime = b.updatedAt ? new Date(b.updatedAt).getTime() : 0;
        return bTime - aTime;
      });
    }
    return map;
  }, [activeChats, workspaceChats, activeWsId]);
  // Freeze idle-chat relative timestamps until the chat data itself changes,
  // so only the actively running chat shows a live second-by-second timer.
  const relativeNow = useMemo(
    () => Date.now(),
    [activeChats, workspaceChats, state?.workspace.id, state?.activeChatId],
  );

  const pinnedWorkspaces = workspaces.filter((w) => pinnedWs.has(w.id) && !w.isDefault);
  const unpinnedWorkspaces = workspaces.filter((w) => !pinnedWs.has(w.id) && !w.isDefault);
  const pinnedChatEntries: { chat: ChatRow; wsId: string }[] = [];
  for (const [wsId, list] of Object.entries(chatsByWorkspace)) {
    for (const chat of list) {
      if (isPinnedChat(wsId, chat.id) && !chat.archived) {
        pinnedChatEntries.push({ chat, wsId });
      }
    }
  }
  const hasPinned = pinnedWorkspaces.length > 0 || pinnedChatEntries.length > 0;

  const defaultWs = workspaces.find((w) => w.isDefault);
  const defaultChats: ChatRow[] = defaultWs ? (chatsByWorkspace[defaultWs.id] ?? []) : [];
  const regularDefaultChats = defaultChats.filter(
    (c) => !c.archived && !isPinnedChat(defaultWs?.id ?? "", c.id),
  );
  const hasNonDefaultWorkspaces = workspaces.some((w) => !w.isDefault);
  const [chatsExpanded, setChatsExpanded] = useState(true);

  useEffect(() => {
    if (defaultWs) {
      void refreshWorkspaceChats(defaultWs.id);
    }
  }, [defaultWs?.id, refreshWorkspaceChats]);

  // Fetch chats for workspaces referenced by pinned chat keys that haven't
  // been loaded yet. Without this, the PINNED group stays empty when GUI
  // opens on a workspace that doesn't own any pinned chat entries.
  useEffect(() => {
    const needed = new Set<string>();
    for (const key of uiPrefs.pinnedChatIds) {
      const sep = key.indexOf("\0");
      if (sep > 0) {
        const wsId = key.slice(0, sep);
        if (chatsByWorkspace[wsId] === undefined) {
          needed.add(wsId);
        }
      }
    }
    for (const wsId of needed) {
      void refreshWorkspaceChats(wsId);
    }
  }, [uiPrefs.pinnedChatIds, chatsByWorkspace, refreshWorkspaceChats]);

  const chatsForWorkspace = (ws: WorkspaceSummary): ChatRow[] => {
    const list = chatsByWorkspace[ws.id] ?? [];
    return list.filter((c) => !c.archived && !isPinnedChat(ws.id, c.id));
  };

  const switchChat = async (wsId: string, chatId: string) => {
    await switchToChat(chatId, wsId);
  };

  const newChatInWorkspace = async (wsId: string) => {
    if (!expanded.has(wsId)) {
      toggleWorkspaceExpanded(wsId);
    }
    // Enter compose mode targeting this workspace; the chat (and any workspace
    // switch) is created only when the first message is sent.
    await newChat(wsId);
  };

  const runChatCommand = async (wsId: string, command: string) => {
    if (wsId && wsId !== backendWsId) {
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
    const archivableIds = ((ws.id === activeWsId ? activeChats : (chatsByWorkspace[ws.id] ?? [])) || [])
      .filter((c) => !c.archived)
      .map((c) => chatKey(ws.id, c.id));
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
        onSelect: () => { void deleteWorkspace(ws.id); },
      },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  const openChatMenu = (e: MouseEvent, chat: ChatRow, wsId: string) => {
    e.preventDefault();
    const items = buildChatMenuItems({
      t,
      isPinned: isPinnedChat(wsId, chat.id),
      isArchived: Boolean(chat.archived),
      onTogglePin: () => toggleChatPin(chatKey(wsId, chat.id)),
      onToggleArchive: () => toggleChatArchive(chatKey(wsId, chat.id)),
      onRename: () => startRename("chat", chat.id, wsId, chat.name),
      onRemove: () => {
        setChatToDelete({ id: chat.id, wsId, name: chat.name });
      },
      onExport: async () => {
        const api = (window as unknown as { pywebview?: { api?: { save_file_dialog?: () => string | Promise<string> } } }).pywebview?.api;
        if (!api?.save_file_dialog) return;
        const filePath = await api.save_file_dialog();
        if (!filePath) return;
        await client.exportChat(chat.id, wsId, filePath);
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
    const isActive = !draftMode && wsId === activeWsId && chat.id === activeChatId;
    const isPinned = isPinnedChat(wsId, chat.id);
    // Chat ids are only unique within a workspace, so the transient
    // ``busyByChat`` / ``unreadChatIds`` maps are keyed by a
    // workspace-qualified composite (``wsId\x00chatId``); build the same key
    // here so a chat's dot can't bleed onto a same-id chat in another
    // workspace. The composite lets a BACKGROUND chat in a non-focused
    // workspace keep its busy/unread dot too. ``chat.running`` from the
    // snapshot only describes the focused workspace's chats, so it stays
    // gated on ``wsId === activeWsId``.
    const rowKey = wsId ? `${wsId}\u0000${chat.id}` : chat.id;
    const isBusy =
      Boolean(busyByChat[rowKey]) ||
      (wsId === activeWsId && Boolean(chat.running));
    const startedAt = runningChatStartedAtByChat[rowKey];
    const metaText = isBusy && typeof startedAt === "number"
      ? formatElapsed(now - startedAt)
      : formatRelative(chat.updatedAt, relativeNow);
    // Unread: a turn finished while the user was elsewhere. Never show on the
    // chat currently being viewed.
    const isUnread = !isActive && !isBusy && Boolean(unreadChatIds[rowKey]);
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
            <HoverTooltip
              className="tree-label-tip"
              content={
                <>
                  <div className="hover-tooltip-topic">{chat.name}</div>
                  {isPinned && workspaceNameById[wsId] && (
                    <div className="hover-tooltip-workspace">{workspaceNameById[wsId]}</div>
                  )}
                </>
              }
            >
              <button className="tree-label" onClick={() => void switchChat(wsId, chat.id)}>
                <span className="tree-name">{chat.name}</span>
                {/* Running chats keep the pulsing busy dot and show their live
                    elapsed task time; idle chats show time since last update;
                    unread chats keep the steady dot so completion stands out
                    at a glance. */}
                {isUnread ? (
                  <span
                    className="chat-status-dot chat-unread-dot"
                    aria-label={t("chat.unread")}
                    title={t("chat.unread")}
                  />
                ) : isBusy ? (
                  <>
                    <span
                      className="chat-status-dot chat-busy-dot"
                      aria-label={t("chat.running")}
                      title={t("chat.running")}
                    />
                    {metaText && <span className="tree-meta">{metaText}</span>}
                  </>
                ) : (
                  metaText && <span className="tree-meta">{metaText}</span>
                )}
              </button>
            </HoverTooltip>
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
    const allChats = chatsForWorkspace(ws);
    const visibleCount = getVisibleCount(ws.id);
    const visibleChats = allChats.slice(0, visibleCount);
    const hasMore = allChats.length > visibleCount;
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
            {allChats.length === 0 ? (
              <li className="tree-empty">{t("sidebar.noChats")}</li>
            ) : (
              <>
                {visibleChats.map((chat) => renderChatRow(chat, ws.id))}
                {hasMore && (
                  <li>
                    <button
                      className="tree-subtitle as-button"
                      onClick={() => loadMoreChats(ws.id)}
                    >
                      {t("sidebar.loadMore")}
                    </button>
                  </li>
                )}
              </>
            )}
          </ul>
        )}
      </li>
    );
  };

  return (
    <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
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
            {!hasNonDefaultWorkspaces && <li className="tree-empty">{t("sidebar.noWorkspaces")}</li>}
          </ul>
        </section>

        {defaultWs && (
          <section className="tree-section">
            <div
              className="tree-row ws-row"
              onClick={() => setChatsExpanded((v) => !v)}
            >
              <span className="tree-label ws-label">
                <span className="tree-section-title-chats">{t("sidebar.chats")}</span>
                <Icon name="chevron" size={14} className={`chevron tree-inline-chevron ${chatsExpanded ? "open" : ""}`} />
              </span>
              <span className="tree-flex" />
              <button
                className="tree-more"
                aria-label={t("menu.more")}
                title={t("menu.more")}
                onClick={(e) => {
                  e.stopPropagation();
                  const archivableIds = (chatsByWorkspace[defaultWs.id] ?? [])
                    .filter((c) => !c.archived)
                    .map((c) => chatKey(defaultWs.id, c.id));
                  const items: MenuItem[] = [
                    {
                      id: "archive",
                      label: t("menu.archiveChats"),
                      disabled: archivableIds.length === 0,
                      onSelect: () => archiveChats(archivableIds),
                    },
                  ];
                  setMenu({ x: e.clientX, y: e.clientY, items });
                }}
              >
                <Icon name="dots" size={14} />
              </button>
              <button
                className="tree-more"
                aria-label={t("sidebar.newChat")}
                title={t("sidebar.newChat")}
                onClick={(e) => {
                  e.stopPropagation();
                  void newChatInWorkspace(defaultWs.id);
                }}
              >
                <Icon name="new-chat" size={14} />
              </button>
            </div>
            {chatsExpanded && (
              <ul className="tree-list tree-children">
                {regularDefaultChats.length === 0 ? (
                  <li className="tree-empty">{t("sidebar.noChats")}</li>
                ) : (
                  <>
                    {regularDefaultChats.slice(0, getVisibleCount(defaultWs.id)).map((chat) => renderChatRow(chat, defaultWs.id))}
                    {regularDefaultChats.length > getVisibleCount(defaultWs.id) && (
                      <li>
                        <button
                          className="tree-subtitle as-button"
                          onClick={() => loadMoreChats(defaultWs.id)}
                        >
                          {t("sidebar.loadMore")}
                        </button>
                      </li>
                    )}
                  </>
                )}
              </ul>
            )}
          </section>
        )}
      </div>

      <div className="sidebar-footer">
        <button className="settings-btn" onClick={onOpenSettings}>
          <Icon name="gear" size={16} />
          {t("nav.settings")}
        </button>
      </div>

      {menu && <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}

      {chatToDelete && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("chat.removeConfirm", { name: chatToDelete.name })}</h3>
            <div className="modal-actions">
              <button className="btn" onClick={() => setChatToDelete(null)}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  void deleteChat(chatToDelete.id, chatToDelete.wsId);
                  setChatToDelete(null);
                }}
              >
                {t("common.remove")}
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
