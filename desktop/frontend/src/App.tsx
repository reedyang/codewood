import { useState } from "react";
import { AppProvider, useApp } from "./state/AppContext";
import { ChatView } from "./components/ChatView";
import { ChatList } from "./components/ChatList";
import { WorkspacePanel } from "./components/WorkspacePanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { ConfirmDialog } from "./components/ConfirmDialog";

type Tab = "chat" | "workspace";

function Shell() {
  const { state, connected, t } = useApp();
  const [tab, setTab] = useState<Tab>("chat");
  const [settingsOpen, setSettingsOpen] = useState(false);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">{state?.app.name ?? "Code Wood"}</div>
          <div className="brand-sub">{t("app.subtitle")}</div>
        </div>
        <nav className="nav">
          <button className={`nav-item ${tab === "chat" ? "nav-active" : ""}`} onClick={() => setTab("chat")}>
            {t("nav.chat")}
          </button>
          <button
            className={`nav-item ${tab === "workspace" ? "nav-active" : ""}`}
            onClick={() => setTab("workspace")}
          >
            {t("nav.workspace")}
          </button>
        </nav>
        <div className="sidebar-footer">
          <div className={`status-dot ${connected ? "online" : "offline"}`} />
          <button className="btn btn-small" onClick={() => setSettingsOpen(true)}>
            {t("nav.settings")}
          </button>
        </div>
      </aside>

      <main className="main">
        {tab === "chat" ? (
          <div className="chat-layout">
            <ChatList />
            <ChatView />
          </div>
        ) : (
          <WorkspacePanel />
        )}
      </main>

      {settingsOpen && <SettingsDialog onClose={() => setSettingsOpen(false)} />}
      <ConfirmDialog />
    </div>
  );
}

export function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
