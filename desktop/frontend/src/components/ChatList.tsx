import { useState } from "react";
import { useApp } from "../state/AppContext";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

export function ChatList() {
  const { state, runCommand, clearTranscript, t } = useApp();
  const [newName, setNewName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const chats = state?.chats ?? [];

  const reloadingRun = async (command: string) => {
    clearTranscript();
    await runCommand(command);
  };

  const createChat = async () => {
    const name = newName.trim();
    setNewName("");
    await reloadingRun(name ? `/chat new ${quote(name)}` : "/chat new");
  };

  const startRename = (id: string, current: string) => {
    setRenamingId(id);
    setRenameValue(current);
  };

  const commitRename = async (id: string) => {
    const name = renameValue.trim();
    setRenamingId(null);
    if (name) {
      await runCommand(`/chat rename ${id} ${quote(name)}`);
    }
  };

  return (
    <div className="panel chat-list">
      <div className="panel-header">
        <h2>{t("chat.sessions")}</h2>
        <button className="btn btn-small btn-danger" onClick={() => void reloadingRun("/chat delete all")}>
          {t("chat.deleteAll")}
        </button>
      </div>

      <div className="new-row">
        <input
          className="text-input"
          value={newName}
          placeholder={t("chat.newPlaceholder")}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              void createChat();
            }
          }}
        />
        <button className="btn btn-primary btn-small" onClick={() => void createChat()}>
          {t("chat.new")}
        </button>
      </div>

      <ul className="item-list">
        {chats.map((chat) => (
          <li key={chat.id} className={`item ${chat.active ? "item-active" : ""}`}>
            {renamingId === chat.id ? (
              <div className="rename-row">
                <input
                  className="text-input"
                  value={renameValue}
                  autoFocus
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      void commitRename(chat.id);
                    } else if (e.key === "Escape") {
                      setRenamingId(null);
                    }
                  }}
                />
                <button className="btn btn-small btn-primary" onClick={() => void commitRename(chat.id)}>
                  {t("common.ok")}
                </button>
                <button className="btn btn-small" onClick={() => setRenamingId(null)}>
                  {t("common.cancel")}
                </button>
              </div>
            ) : (
              <>
                <button
                  className="item-main"
                  title={chat.id}
                  onClick={() => void reloadingRun(`/chat switch ${chat.id}`)}
                >
                  <span className="item-name">
                    {chat.active ? "● " : ""}
                    {chat.name}
                  </span>
                  <span className="item-meta">
                    #{chat.index} · {chat.messageCount} {t("chat.messages")}
                  </span>
                </button>
                <div className="item-actions">
                  <button className="btn btn-small" onClick={() => startRename(chat.id, chat.name)}>
                    {t("chat.rename")}
                  </button>
                  {chat.active && (
                    <button className="btn btn-small" onClick={() => void runCommand("/chat fork")}>
                      {t("chat.fork")}
                    </button>
                  )}
                  <button
                    className="btn btn-small btn-danger"
                    onClick={() => void reloadingRun(`/chat delete ${chat.id}`)}
                  >
                    {t("chat.delete")}
                  </button>
                </div>
              </>
            )}
          </li>
        ))}
      </ul>

      <div className="panel-footer">
        <button className="btn btn-small" onClick={() => void reloadingRun("/chat reload")}>
          {t("chat.reload")}
        </button>
      </div>
    </div>
  );
}
