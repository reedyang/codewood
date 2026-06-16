import { useEffect, useState } from "react";
import { AppProvider, useApp } from "./state/AppContext";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { SettingsDialog } from "./components/SettingsDialog";
import { AboutDialog } from "./components/AboutDialog";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { TitleBar } from "./components/TitleBar";

function Shell() {
  const {
    settingsOpen,
    closeSettings,
    openSettings,
    aboutOpen,
    closeAbout,
    clearTurns,
    runCommand,
  } = useApp();
  const [collapsed, setCollapsed] = useState(false);

  // JS-accessible shortcuts (the native menu shows the same accelerators).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) {
        return;
      }
      const key = e.key.toLowerCase();
      if (key === "n") {
        e.preventDefault();
        clearTurns();
        void runCommand("/chat new");
      } else if (key === ",") {
        e.preventDefault();
        openSettings();
      } else if (key === "w") {
        e.preventDefault();
        window.close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clearTurns, runCommand, openSettings]);

  return (
    <div className="window-root">
      <TitleBar onTogglePanel={() => setCollapsed((v) => !v)} />
      <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
        {!collapsed && <Sidebar onOpenSettings={openSettings} />}
        <main className="main">
          <ChatView />
        </main>
      </div>

      {settingsOpen && <SettingsDialog onClose={closeSettings} />}
      {aboutOpen && <AboutDialog onClose={closeAbout} />}
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
