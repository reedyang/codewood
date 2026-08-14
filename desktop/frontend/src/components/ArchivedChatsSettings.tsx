import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import type { WorkspaceChatSummary } from "../api/types";
import { chatKey } from "./chatMenu";

interface ArchivedChat extends WorkspaceChatSummary {
  wsId: string;
}

interface WorkspaceGroup {
  wsId: string;
  wsName: string;
  wsArchived: boolean;
  chats: ArchivedChat[];
}

export function ArchivedChatsSettings() {
  const { state, workspaceChats, refreshWorkspaceChats, toggleChatArchive, deleteChat, t } = useApp();
  const [loading, setLoading] = useState(true);
  const [chatToDelete, setChatToDelete] = useState<ArchivedChat | null>(null);
  const [groupToClear, setGroupToClear] = useState<WorkspaceGroup | null>(null);
  const [clearing, setClearing] = useState(false);

  // Keep the latest workspaces in a ref so ``loadAll`` stays referentially
  // stable. Depending on ``state`` directly would recreate ``loadAll`` on every
  // state update (which happens constantly while a task streams), re-firing the
  // mount effect in a tight loop and making the page flicker/refresh forever.
  const workspacesRef = useRef(state?.workspaces ?? []);
  useEffect(() => {
    workspacesRef.current = state?.workspaces ?? [];
  }, [state?.workspaces]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    await Promise.allSettled(
      workspacesRef.current.map((ws) => refreshWorkspaceChats(ws.id)),
    );
    setLoading(false);
  }, [refreshWorkspaceChats]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const wsNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const ws of state?.workspaces ?? []) {
      map[ws.id] = ws.name;
    }
    return map;
  }, [state?.workspaces]);

  const wsArchivedMap = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const ws of state?.workspaces ?? []) {
      map[ws.id] = Boolean(ws.archived);
    }
    return map;
  }, [state?.workspaces]);

  const groups: WorkspaceGroup[] = useMemo(() => {
    const byWs = new Map<string, ArchivedChat[]>();
    for (const [wsId, chats] of Object.entries(workspaceChats)) {
      for (const c of chats) {
        if (c.archived) {
          const list = byWs.get(wsId) ?? [];
          list.push({ ...c, wsId });
          byWs.set(wsId, list);
        }
      }
    }
    if (state?.chats) {
      const activeWsId = state.workspace.id;
      const seenIds = new Set<string>();
      for (const list of byWs.values()) {
        for (const c of list) seenIds.add(c.id);
      }
      for (const c of state.chats) {
        if (c.archived && !seenIds.has(c.id)) {
          const list = byWs.get(activeWsId) ?? [];
          list.push({
            id: c.id,
            name: c.name,
            updatedAt: c.updatedAt,
            archived: c.archived,
            wsId: activeWsId,
          });
          byWs.set(activeWsId, list);
        }
      }
    }
    const result: WorkspaceGroup[] = [];
    for (const [wsId, chats] of byWs) {
      chats.sort((a, b) => (b.updatedAt ?? "").localeCompare(a.updatedAt ?? ""));
      result.push({
        wsId,
        wsName: wsNames[wsId] ?? wsId,
        wsArchived: Boolean(wsArchivedMap[wsId]),
        chats,
      });
    }
    result.sort((a, b) => a.wsName.localeCompare(b.wsName));
    return result;
  }, [workspaceChats, state, wsNames, wsArchivedMap]);

  const handleUnarchive = useCallback(
    (chat: ArchivedChat) => {
      void toggleChatArchive(chatKey(chat.wsId, chat.id));
    },
    [toggleChatArchive],
  );

  const handleDeleteConfirm = useCallback(
    (chat: ArchivedChat) => {
      void deleteChat(chat.id, chat.wsId);
      setChatToDelete(null);
    },
    [deleteChat],
  );

  const handleClearGroup = useCallback(async () => {
    if (!groupToClear) return;
    setClearing(true);
    try {
      for (const chat of groupToClear.chats) {
        await deleteChat(chat.id, chat.wsId);
      }
      await loadAll();
    } finally {
      setClearing(false);
      setGroupToClear(null);
    }
  }, [groupToClear, deleteChat, loadAll]);

  return (
    <div className="settings-page">
      <div className="archived-chats-header">
        <h2 className="settings-page-title">{t("settings.page.archivedChats")}</h2>
      </div>
      {loading ? (
        <p className="muted">{t("models.loading")}</p>
      ) : groups.length === 0 ? (
        <p className="muted">{t("archivedChats.empty")}</p>
      ) : (
        <div className="archived-chat-groups">
          {groups.map((group) => (
            <div key={group.wsId} className="archived-chat-group">
              <div className="archived-chat-group-header">
                <span className="archived-chat-group-name">
                  {group.wsName}
                  {group.wsArchived && (
                    <span
                      className="archived-chat-group-deleted"
                      title={t("archivedChats.deletedWorkspaceHint")}
                    >
                      {" "}
                      ({t("archivedChats.workspaceArchived")})
                    </span>
                  )}
                </span>
                <span className="archived-chat-group-count">{group.chats.length}</span>
                <button
                  className="archived-chat-group-remove-all"
                  onClick={() => setGroupToClear(group)}
                >
                  {t("archivedChats.removeWorkspaceAll")}
                </button>
              </div>
              <div className="archived-chats-list">
                {group.chats.map((chat) => (
                  <div key={chatKey(chat.wsId, chat.id)} className="archived-chat-row">
                    <div className="archived-chat-info">
                      <span className="archived-chat-name">{chat.name}</span>
                    </div>
                    {!group.wsArchived && (
                      <button
                        className="archived-chat-unarchive"
                        onClick={() => handleUnarchive(chat)}
                        title={t("archivedChats.unarchive")}
                      >
                        {t("archivedChats.unarchive")}
                      </button>
                    )}
                    <button
                      className="archived-chat-remove"
                      onClick={() => setChatToDelete(chat)}
                      title={t("common.remove")}
                    >
                      {t("common.remove")}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {chatToDelete && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("archivedChats.removeConfirm")}</h3>
            <p className="modal-body">{chatToDelete.name}</p>
            <div className="modal-actions">
              <button className="btn" onClick={() => setChatToDelete(null)}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => handleDeleteConfirm(chatToDelete)}
              >
                {t("common.delete")}
              </button>
            </div>
          </div>
        </div>
      )}

      {groupToClear && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">
              {t("archivedChats.removeWorkspaceAllTitle", { name: groupToClear.wsName })}
            </h3>
            <p className="modal-body">
              {t("archivedChats.removeWorkspaceAllConfirm", {
                name: groupToClear.wsName,
                count: String(groupToClear.chats.length),
              })}
            </p>
            <div className="modal-actions">
              <button className="btn" onClick={() => setGroupToClear(null)} disabled={clearing}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => void handleClearGroup()}
                disabled={clearing}
              >
                {t("common.remove")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
