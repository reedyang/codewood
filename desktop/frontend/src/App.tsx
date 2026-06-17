import { useEffect, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { AppProvider, useApp } from "./state/AppContext";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { SettingsDialog } from "./components/SettingsDialog";
import { AboutDialog } from "./components/AboutDialog";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { TitleBar } from "./components/TitleBar";
import { ResizeGrips } from "./components/ResizeGrips";

const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 480;
const SIDEBAR_WIDTH_KEY = "codewood.sidebarWidth";

function loadSidebarWidth(): number {
  const raw = Number(window.localStorage.getItem(SIDEBAR_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= SIDEBAR_MIN && raw <= SIDEBAR_MAX) {
    return raw;
  }
  return 200;
}

function Shell() {
  const {
    settingsOpen,
    closeSettings,
    openSettings,
    aboutOpen,
    closeAbout,
    newChat,
  } = useApp();
  const [collapsed, setCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const [resizing, setResizing] = useState(false);

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
  }, [sidebarWidth]);

  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = sidebarWidth;
    setResizing(true);
    document.body.classList.add("resizing-x");
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(
        SIDEBAR_MAX,
        Math.max(SIDEBAR_MIN, startWidth + (ev.clientX - startX)),
      );
      setSidebarWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      document.body.classList.remove("resizing-x");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  // JS-accessible shortcuts (the native menu shows the same accelerators).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) {
        return;
      }
      const key = e.key.toLowerCase();
      if (key === "n") {
        e.preventDefault();
        void newChat();
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
  }, [newChat, openSettings]);

  return (
    <div className="window-root">
      <TitleBar onTogglePanel={() => setCollapsed((v) => !v)} />
      <div
        className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}
        style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}
      >
        {!collapsed && <Sidebar onOpenSettings={openSettings} />}
        {!collapsed && (
          <div
            className={`sidebar-resizer ${resizing ? "resizing" : ""}`}
            role="separator"
            aria-orientation="vertical"
            onMouseDown={startResize}
          />
        )}
        <main className="main">
          <ChatView />
        </main>
      </div>

      {settingsOpen && <SettingsDialog onClose={closeSettings} />}
      {aboutOpen && <AboutDialog onClose={closeAbout} />}
      <ConfirmDialog />
      <ResizeGrips />
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
