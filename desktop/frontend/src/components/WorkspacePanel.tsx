import { useState } from "react";
import { useApp } from "../state/AppContext";

function quote(value: string): string {
  return `"${value.replace(/"/g, "")}"`;
}

export function WorkspacePanel() {
  const { state, runCommand, clearTranscript, t } = useApp();
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const workspaces = state?.workspaces ?? [];
  const current = state?.workspace;

  const createWorkspace = async () => {
    const p = path.trim();
    if (!p) {
      return;
    }
    const n = name.trim();
    setPath("");
    setName("");
    const cmd = n ? `/workspace create ${quote(p)} --name ${quote(n)}` : `/workspace create ${quote(p)}`;
    clearTranscript();
    await runCommand(cmd);
  };

  const switchWorkspace = async (id: string) => {
    clearTranscript();
    await runCommand(`/workspace switch ${id}`);
  };

  const startRename = (id: string, currentName: string) => {
    setRenamingId(id);
    setRenameValue(currentName);
  };

  const commitRename = async (id: string) => {
    const value = renameValue.trim();
    setRenamingId(null);
    if (value) {
      await runCommand(`/workspace rename ${id} ${quote(value)}`);
    }
  };

  return (
    <div className="panel workspace-panel">
      <div className="panel-header">
        <h2>{t("workspace.title")}</h2>
      </div>

      {current && (
        <div className="current-workspace">
          <div className="muted">{t("workspace.current")}</div>
          <div className="current-name">{current.name}</div>
          <div className="mono small muted">{current.root}</div>
        </div>
      )}

      <div className="new-row column">
        <input
          className="text-input"
          value={path}
          placeholder={t("workspace.createPathPlaceholder")}
          onChange={(e) => setPath(e.target.value)}
        />
        <div className="new-row">
          <input
            className="text-input"
            value={name}
            placeholder={t("workspace.createNamePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
          <button className="btn btn-primary btn-small" disabled={!path.trim()} onClick={() => void createWorkspace()}>
            {t("workspace.create")}
          </button>
        </div>
      </div>

      <ul className="item-list">
        {workspaces.map((ws) => (
          <li key={ws.id} className={`item ${ws.active ? "item-active" : ""}`}>
            {renamingId === ws.id ? (
              <div className="rename-row">
                <input
                  className="text-input"
                  value={renameValue}
                  autoFocus
                  onChange={(e) => setRenameValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      void commitRename(ws.id);
                    } else if (e.key === "Escape") {
                      setRenamingId(null);
                    }
                  }}
                />
                <button className="btn btn-small btn-primary" onClick={() => void commitRename(ws.id)}>
                  {t("common.ok")}
                </button>
                <button className="btn btn-small" onClick={() => setRenamingId(null)}>
                  {t("common.cancel")}
                </button>
              </div>
            ) : (
              <>
                <div className="item-main static">
                  <span className="item-name">
                    {ws.active ? "● " : ""}
                    {ws.name}
                    {ws.active ? ` (${t("workspace.active")})` : ""}
                  </span>
                  <span className="item-meta mono">{ws.root}</span>
                </div>
                <div className="item-actions">
                  {!ws.active && (
                    <button className="btn btn-small" onClick={() => void switchWorkspace(ws.id)}>
                      {t("workspace.switch")}
                    </button>
                  )}
                  <button className="btn btn-small" onClick={() => startRename(ws.id, ws.name)}>
                    {t("workspace.rename")}
                  </button>
                  <button
                    className="btn btn-small btn-danger"
                    onClick={() => void runCommand(`/workspace delete ${ws.id}`)}
                  >
                    {t("workspace.delete")}
                  </button>
                </div>
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
