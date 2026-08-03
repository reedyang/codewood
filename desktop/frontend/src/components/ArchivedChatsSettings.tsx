import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import type { WorkspaceChatSummary } from "../api/types";
import { chatKey } from "./chatMenu";

interface ArchivedChat extends WorkspaceChatSummary {
  wsId: string;
  wsName: string;
}

export function ArchivedChatsSettings() {
  const { state, workspaceChats, refreshWorkspaceChats, toggleChatArchive, deleteChat, t } = useApp();
  const [loading, setLoading] = useState(true);
  const [chatToDelete, setChatToDelete] = useState<ArchivedChat | null>(null);
  const [confirmRemoveAll, setConfirmRemoveAll] = useState(false);

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

  const archivedChats: ArchivedChat[] = useMemo(() => {
    const result: ArchivedChat[] = [];
    for (const [wsId, chats] of Object.entries(workspaceChats)) {
      for (const c of chats) {
        if (c.archived) {
          result.push({ ...c, wsId, wsName: wsNames[wsId] ?? wsId });
        }
      }
    }
    if (state?.chats) {
      const activeWsId = state.workspace.id;
      const seenIds = new Set(result.map((r) => r.id));
      for (const c of state.chats) {
        if (c.archived && !seenIds.has(c.id)) {
          result.push({
            id: c.id,
            name: c.name,
            updatedAt: c.updatedAt,
            archived: c.archived,
            wsId: activeWsId,
            wsName: wsNames[activeWsId] ?? activeWsId,
          });
        }
      }
    }
    result.sort((a, b) => {
      const aTime = a.updatedAt ?? "";
      const bTime = b.updatedAt ?? "";
      return bTime.localeCompare(aTime);
    });
    return result;
  }, [workspaceChats, state, wsNames]);

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

  const handleRemoveAll = useCallback(async () => {
    for (const chat of archivedChats) {
      await deleteChat(chat.id, chat.wsId);
    }
    setConfirmRemoveAll(false);
    void loadAll();
  }, [archivedChats, deleteChat, loadAll]);

  return (
    <div className="settings-page">
      <div className="archived-chats-header">
        <h2 className="settings-page-title">{t("settings.page.archivedChats")}</h2>
        {!loading && archivedChats.length > 0 && (
          <button
            className="archived-chats-remove-all"
            onClick={() => setConfirmRemoveAll(true)}
          >
            {t("archivedChats.removeAll")}
          </button>
        )}
      </div>
      {loading ? (
        <p className="muted">{t("models.loading")}</p>
      ) : archivedChats.length === 0 ? (
        <p className="muted">{t("archivedChats.empty")}</p>
      ) : (
        <div className="archived-chats-list">
          {archivedChats.map((chat) => (
            <div key={chatKey(chat.wsId, chat.id)} className="archived-chat-row">
              <div className="archived-chat-info">
                <span className="archived-chat-name">{chat.name}</span>
                <span className="archived-chat-workspace">{chat.wsName}</span>
              </div>
              <button
                className="archived-chat-unarchive"
                onClick={() => handleUnarchive(chat)}
                title={t("archivedChats.unarchive")}
              >
                {t("archivedChats.unarchive")}
              </button>
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

      {confirmRemoveAll && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("archivedChats.removeAllConfirm")}</h3>
            <p className="modal-body">{t("archivedChats.removeAllConfirm")}</p>
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirmRemoveAll(false)}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => void handleRemoveAll()}
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
