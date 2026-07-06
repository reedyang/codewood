import { useState, type MouseEvent } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { ContextMenu, type MenuItem } from "./ContextMenu";
import { buildChatMenuItems, chatKey } from "./chatMenu";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

interface MenuState {
  x: number;
  y: number;
  items: MenuItem[];
}

/** Fixed bar above the transcript showing the active chat name, a pinned
 *  marker when applicable, and a "more" menu identical to the sidebar's. */
export function ChatTitleBar() {
  const {
    state,
    uiPrefs,
    t,
    runCommand,
    deleteChat,
    toggleChatPin,
    toggleChatArchive,
    planOpen,
    togglePlan,
    consoleOpen,
    showConsole,
    hideConsole,
    client,
  } = useApp();
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const wsId = state?.workspace.id ?? "";
  const activeChat = state?.chats.find((c) => c.active);
  if (!activeChat) {
    return null;
  }

  const key = chatKey(wsId, activeChat.id);
  const isPinned = uiPrefs.pinnedChatIds.includes(key);
  const isArchived = Boolean(activeChat.archived);

  const commitRename = async () => {
    const value = renameValue.trim();
    setRenaming(false);
    if (value && value !== activeChat.name) {
      await runCommand(`/chat rename ${activeChat.id} ${quote(value)}`);
    }
  };

  const openMenu = (e: MouseEvent) => {
    e.preventDefault();
    const items = buildChatMenuItems({
      t,
      isPinned,
      isArchived,
      onTogglePin: () => toggleChatPin(key),
      onToggleArchive: () => toggleChatArchive(key),
      onRename: () => {
        setRenameValue(activeChat.name);
        setRenaming(true);
      },
      onRemove: () => {
        setConfirmDelete(true);
      },
      onExport: async () => {
        const api = (window as unknown as { pywebview?: { api?: { save_file_dialog?: () => string | Promise<string> } } }).pywebview?.api;
        if (!api?.save_file_dialog) return;
        const filePath = await api.save_file_dialog();
        if (!filePath) return;
        await client.exportChat(activeChat.id, wsId, filePath);
      },
    });
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  return (
    <div className="chat-titlebar">
      {isPinned && (
        <Icon name="pin-filled" size={14} className="chat-titlebar-pin" />
      )}
      {renaming ? (
        <input
          className="chat-titlebar-rename text-input"
          aria-label={t("menu.rename")}
          value={renameValue}
          autoFocus
          onChange={(e) => setRenameValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              void commitRename();
            } else if (e.key === "Escape") {
              setRenaming(false);
            }
          }}
          onBlur={() => void commitRename()}
        />
      ) : (
        <span className="chat-titlebar-name" title={activeChat.name}>
          {activeChat.name}
        </span>
      )}
      <button
        className="chat-titlebar-more"
        aria-label={t("menu.more")}
        onClick={openMenu}
      >
        <Icon name="dots" size={16} />
      </button>
      <button
        className={`chat-titlebar-console ${consoleOpen ? "active" : ""}`}
        aria-label={consoleOpen ? t("console.hide") : t("console.show")}
        aria-pressed={consoleOpen}
        title={consoleOpen ? t("console.hide") : t("console.show")}
        onClick={() => { consoleOpen ? hideConsole() : showConsole(); }}
      >
        <Icon name="panel-bottom" size={18} />
      </button>
      {!planOpen && (
        <button
          className="chat-titlebar-plan"
          aria-label={t("rightpanel.toggle")}
          aria-pressed={false}
          title={t("rightpanel.toggle")}
          onClick={togglePlan}
        >
          <Icon name="panel-right" size={18} />
        </button>
      )}
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menu.items}
          onClose={() => setMenu(null)}
        />
      )}

      {confirmDelete && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("chat.removeConfirm")}</h3>
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirmDelete(false)}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  void deleteChat(activeChat.id);
                  setConfirmDelete(false);
                }}
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
