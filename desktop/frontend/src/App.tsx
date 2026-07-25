import { useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { AppProvider, useApp } from "./state/AppContext";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./components/ChatView";
import { RightPanel } from "./components/RightPanel";
import { SettingsView } from "./components/SettingsView";
import { AboutDialog } from "./components/AboutDialog";
import { TitleBar } from "./components/TitleBar";
import { ResizeGrips } from "./components/ResizeGrips";
import { StatusBar } from "./components/StatusBar";

const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 480;
const SIDEBAR_WIDTH_KEY = "codewood.sidebarWidth";

const RIGHT_PANEL_MIN = 240;
const RIGHT_PANEL_MAX = 720;
const RIGHT_PANEL_WIDTH_KEY = "codewood.rightPanelWidth";

function loadSidebarWidth(): number {
  const raw = Number(window.localStorage.getItem(SIDEBAR_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= SIDEBAR_MIN && raw <= SIDEBAR_MAX) {
    return raw;
  }
  return 267;
}

function loadRightPanelWidth(): number {
  const raw = Number(window.localStorage.getItem(RIGHT_PANEL_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= RIGHT_PANEL_MIN && raw <= RIGHT_PANEL_MAX) {
    return raw;
  }
  return 300;
}

function NoModelGuide() {
  const { openSettings, t } = useApp();
  return (
    <div className="modal-backdrop no-model-overlay">
      <div className="modal no-model-guide" role="dialog" aria-modal="true">
        <h2 className="modal-title">{t("noModel.title")}</h2>
        <p className="modal-body">{t("noModel.body")}</p>
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={() => openSettings("models")}>
            {t("noModel.openSettings")}
          </button>
        </div>
      </div>
    </div>
  );
}

function Shell() {
  const {
    state,
    settingsOpen,
    openSettings,
    aboutOpen,
    closeAbout,
    newChat,
    planOpen,
    pickAndOpenFolder,
    zoomLevel,
    setZoomLevel,
  } = useApp();
  // The backend serves a state even when no usable model is configured (e.g.
  // first launch where only the placeholder template config exists). The
  // backend's ``model.ready`` flag is the authoritative signal: it is false
  // until a real, non-template model is configured. Surface a centered modal
  // that guides the user into Model settings whenever it is not ready.
  const noModelConfigured = !!state && state.model?.ready === false;
  const [collapsed, setCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const [resizing, setResizing] = useState(false);
  const [rightPanelWidth, setRightPanelWidth] = useState(loadRightPanelWidth);
  const [resizingRight, setResizingRight] = useState(false);
  const zoomLevelRef = useRef(zoomLevel);
  zoomLevelRef.current = zoomLevel;

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
  }, [sidebarWidth]);

  useEffect(() => {
    window.localStorage.setItem(RIGHT_PANEL_WIDTH_KEY, String(rightPanelWidth));
  }, [rightPanelWidth]);

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

  const startResizeRight = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = rightPanelWidth;
    setResizingRight(true);
    document.body.classList.add("resizing-x");
    const onMove = (ev: MouseEvent) => {
      // Dragging left widens the right panel, so subtract the delta.
      const next = Math.min(
        RIGHT_PANEL_MAX,
        Math.max(RIGHT_PANEL_MIN, startWidth - (ev.clientX - startX)),
      );
      setRightPanelWidth(next);
    };
    const onUp = () => {
      setResizingRight(false);
      document.body.classList.remove("resizing-x");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  // JS-accessible shortcuts (the native menu shows the same accelerators).
  useEffect(() => {
    const ZOOM_LEVELS = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0];
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) {
        return;
      }
      const key = e.key.toLowerCase();
      if (key === "=" || key === "+") {
        e.preventDefault();
        const next = ZOOM_LEVELS.findIndex((z) => z > zoomLevelRef.current);
        if (next >= 0) setZoomLevel(ZOOM_LEVELS[next]);
      } else if (key === "-") {
        e.preventDefault();
        for (let i = ZOOM_LEVELS.length - 1; i >= 0; i--) { if (ZOOM_LEVELS[i] < zoomLevelRef.current) { setZoomLevel(ZOOM_LEVELS[i]); break; } }
      } else if (key === "0") {
        e.preventDefault();
        setZoomLevel(1);
      } else if (key === "n") {
        e.preventDefault();
        void newChat();
      } else if (key === "o") {
        e.preventDefault();
        void pickAndOpenFolder();
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
  }, [newChat, openSettings, pickAndOpenFolder, setZoomLevel]);

  return (
    <div className="window-root" style={{ transform: `scale(${zoomLevel})`, transformOrigin: "top left", width: `${100 / zoomLevel}vw`, height: `${100 / zoomLevel}vh` }}>
      <TitleBar collapsed={collapsed} onTogglePanel={() => setCollapsed((v) => !v)} />
      {settingsOpen ? (
        <div className="app-shell">
          <SettingsView />
        </div>
      ) : (
        <div
          className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}
          style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}
        >
          <Sidebar collapsed={collapsed} onOpenSettings={openSettings} />
          <div
            className={`sidebar-resizer ${resizing ? "resizing" : ""} ${collapsed ? "collapsed" : ""}`}
            role="separator"
            aria-orientation="vertical"
            onMouseDown={startResize}
          />
          <div className="main-shadow-wrap">
            <main
              className="main"
              style={{ "--right-panel-width": `${rightPanelWidth}px` } as CSSProperties}
            >
              <ChatView />
              <div
                className={`right-panel-resizer ${resizingRight ? "resizing" : ""} ${planOpen ? "" : "collapsed"}`}
                role="separator"
                aria-orientation="vertical"
                onMouseDown={startResizeRight}
              />
              <RightPanel />
            </main>
          </div>
        </div>
      )}

      <StatusBar />

      {aboutOpen && <AboutDialog onClose={closeAbout} />}
      {!settingsOpen && noModelConfigured && <NoModelGuide />}
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
