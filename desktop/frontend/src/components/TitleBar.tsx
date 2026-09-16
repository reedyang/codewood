import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { GlobalChatSearch } from "./GlobalChatSearch";
import { Icon } from "./Icon";
import { UpdateButton } from "./UpdateButton";

interface HostWindowApi {
  minimize?: () => void;
  toggle_maximize?: () => boolean | Promise<boolean>;
  close_window?: () => void;
  open_external?: (url: string) => boolean | Promise<boolean>;
  start_window_drag?: () => boolean | Promise<boolean>;
  toggle_always_on_top?: () => boolean | Promise<boolean>;
  host_platform?: () => string | Promise<string>;
}

const GITHUB_URL = "https://github.com/reedyang/codewood";

function openExternal(url: string): void {
  const api = hostApi();
  if (api?.open_external) {
    void api.open_external(url);
    return;
  }
  // Browser/dev fallback when the native bridge is unavailable.
  window.open(url, "_blank", "noopener,noreferrer");
}

function hostApi(): HostWindowApi | undefined {
  return (window as unknown as { pywebview?: { api?: HostWindowApi } }).pywebview?.api;
}

type MenuEntry =
  | "separator"
  | { label: string; shortcut?: string; checked?: boolean; onSelect: () => void };

export function TitleBar({ collapsed, onTogglePanel }: { collapsed: boolean; onTogglePanel: () => void }) {
  const { t, pickAndOpenFolder, newChat, openSettings, openAbout, showBrowserTab, hideBrowserTab, showConsole, hideConsole, consoleOpen, browserOpen, closeSettings, settingsOpen, zoomLevel, setZoomLevel } = useApp();

  const ZOOM_LEVELS = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0];
  const zoomIn = () => { const next = ZOOM_LEVELS.findIndex((z) => z > zoomLevel); if (next >= 0) setZoomLevel(ZOOM_LEVELS[next]); };
  const zoomOut = () => { for (let i = ZOOM_LEVELS.length - 1; i >= 0; i--) { if (ZOOM_LEVELS[i] < zoomLevel) { setZoomLevel(ZOOM_LEVELS[i]); break; } } };
  const zoomReset = () => setZoomLevel(1);

  const [alwaysOnTop, setAlwaysOnTop] = useState<boolean>(false);

  const toggleAlwaysOnTop = async () => {
    const api = hostApi();
    if (api?.toggle_always_on_top) {
      try {
        const newState = await api.toggle_always_on_top();
        setAlwaysOnTop(Boolean(newState));
      } catch {
        setAlwaysOnTop((v) => !v);
      }
    }
  };

  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [native, setNative] = useState<boolean>(() => Boolean(hostApi()));
  const [maximized, setMaximized] = useState(false);
  // Default to the era-agnostic guess so the correct chrome (drag mode,
  // traffic-light title bar, absent in-window menubar) renders on the very
  // first paint until the host reports the real platform: only Windows uses
  // the pywebview-drag-region; macOS drags via the host's native handoff and
  // GTK/Linux/WSL via the window manager (see the drag strip below).
  const [hostOs, setHostOs] = useState<string>(() => {
    const plat = (navigator.platform || "").toLowerCase();
    if (plat.includes("mac")) return "darwin";
    if (plat.includes("win")) return "win32";
    return "win32";
  });
  const barRef = useRef<HTMLDivElement | null>(null);
  const isMac = hostOs === "darwin";

  useEffect(() => {
    const api = hostApi();
    if (!api?.host_platform) {
      return;
    }
    let cancelled = false;
    void Promise.resolve(api.host_platform())
      .then((os) => {
        if (!cancelled && typeof os === "string" && os) {
          setHostOs(os);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [native]);

  const toggleMaximize = async () => {
    const api = hostApi();
    if (!api?.toggle_maximize) {
      return;
    }
    try {
      const result = await api.toggle_maximize();
      setMaximized(Boolean(result));
    } catch {
      setMaximized((v) => !v);
    }
  };

  useEffect(() => {
    const onReady = () => setNative(true);
    window.addEventListener("pywebviewready", onReady);
    return () => window.removeEventListener("pywebviewready", onReady);
  }, []);

  // Close any open menu when clicking elsewhere.
  useEffect(() => {
    if (!openMenu) {
      return;
    }
    const onPointer = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setOpenMenu(null);
      }
    };
    window.addEventListener("mousedown", onPointer);
    return () => window.removeEventListener("mousedown", onPointer);
  }, [openMenu]);

  const closeWindow = () => {
    // If settings are open, close them first so pending auto-saves can fire.
    if (settingsOpen) {
      closeSettings();
    }
    hostApi()?.close_window?.();
  };

  const openFolder = () => void pickAndOpenFolder();
  const menus: { id: string; label: string; entries: MenuEntry[] }[] = [
    {
      id: "file",
      label: t("menu.file"),
      entries: [
        {
          label: t("menu.file.newChat"),
          shortcut: "Ctrl+N",
          onSelect: () => void newChat(),
        },
        { label: t("menu.file.openFolder"), shortcut: "Ctrl+O", onSelect: () => void openFolder() },
        "separator",
        { label: t("menu.file.settings"), shortcut: "Ctrl+,", onSelect: openSettings },
        "separator",
        { label: t("menu.file.exit"), onSelect: closeWindow },
      ],
    },
    {
      id: "view",
      label: t("menu.view"),
      entries: [
        { label: t("menu.view.alwaysOnTop"), checked: alwaysOnTop, onSelect: () => void toggleAlwaysOnTop() },
        "separator",
        { label: t("menu.view.browser"), checked: browserOpen, onSelect: () => browserOpen ? hideBrowserTab() : showBrowserTab() },
        { label: t("menu.view.console"), checked: consoleOpen, onSelect: () => consoleOpen ? hideConsole() : showConsole() },
        "separator",
        { label: t("menu.view.zoomIn"), shortcut: "Ctrl++", onSelect: zoomIn },
        { label: t("menu.view.zoomOut"), shortcut: "Ctrl+-", onSelect: zoomOut },
        { label: t("menu.view.actualSize"), shortcut: "Ctrl+0", onSelect: zoomReset },
      ],
    },
    {
      id: "help",
      label: t("menu.help"),
      entries: [
        {
          label: t("menu.help.github"),
          onSelect: () => openExternal(GITHUB_URL),
        },
        "separator",
        { label: t("menu.help.about"), onSelect: openAbout },
      ],
    },
  ];

  return (
    <div className={`titlebar${isMac ? " mac" : ""}`} ref={barRef}>
      {/* macOS: the native traffic lights (host applies fullSizeContentView)
          float over this strip's left side; CSS reserves padding for them. */}

      <button
        className={`icon-btn titlebar-toggle ${collapsed ? "" : "active"}`}
        aria-label={t("panel.toggle")}
        title={t("panel.toggle")}
        onClick={onTogglePanel}
      >
        <Icon name="panel" size={18} />
      </button>

      {!isMac && (
        <div className="menubar">
          {menus.map((menu) => (
            <div className="menubar-item" key={menu.id}>
              <button
                className={`menubar-button ${openMenu === menu.id ? "open" : ""}`}
                onClick={() => setOpenMenu((cur) => (cur === menu.id ? null : menu.id))}
                onMouseEnter={() => setOpenMenu((cur) => (cur ? menu.id : cur))}
              >
                {menu.label}
              </button>
              {openMenu === menu.id && (
                <div className="menubar-menu" role="menu">
                  {(() => {
                    const hasCheckColumn = (menu.entries as MenuEntry[]).some(
                      (e): e is Exclude<MenuEntry, "separator"> => e !== "separator" && "checked" in e,
                    );
                    return menu.entries.map((entry, idx) =>
                      entry === "separator" ? (
                        <div className="menubar-separator" key={`sep-${idx}`} />
                      ) : (
                        <button
                          key={entry.label}
                          className="menubar-menu-item"
                          role="menuitem"
                          onClick={() => {
                            setOpenMenu(null);
                            entry.onSelect();
                          }}
                        >
                          <span className="menubar-item-label">
                            {hasCheckColumn && (
                              <span className="dropdown-check">
                                {entry.checked && <Icon name="check" size={13} />}
                              </span>
                            )}
                            <span>{entry.label}</span>
                          </span>
                          {entry.shortcut && <span className="menubar-shortcut">{entry.shortcut}</span>}
                        </button>
                      ),
                    );
                  })()}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <GlobalChatSearch />

      <div
        className={`titlebar-drag ${hostOs === "win32" ? "pywebview-drag-region" : ""}`}
        onMouseDown={(e) => {
          // Left button only; let double-clicks fall through to maximize.
          if (e.button !== 0 || e.detail > 1) {
            return;
          }
          // Stop the browser from starting a text-selection session here:
          // with the native drag loop consuming the event stream, any
          // mousemove that leaks into the page would otherwise highlight
          // text while the window is being dragged.
          e.preventDefault();
          // GTK/WSL: hand the drag to the window manager so the window
          // follows the cursor across mixed-DPI monitors. macOS: the host
          // hands the move to AppKit's native drag loop (standard Dock /
          // menu-bar constraining for free); the pywebview-drag-region
          // must NOT also be active there or the two movers fight and the
          // window jitters. Windows returns false, leaving the drag
          // region as the driver.
          const api = hostApi();
          if (!api?.start_window_drag) {
            return;
          }
          void Promise.resolve(api.start_window_drag()).then((handled) => {
            void handled;
          });
        }}
        onDoubleClick={() => void toggleMaximize()}
      />

      {/* Sits immediately before the window controls so on Windows/Linux it
          lands just left of Minimize, and on macOS at the far right. */}
      <UpdateButton />

      {native && !isMac && (
        <div className="win-controls">
          <button className="win-btn" aria-label={t("win.minimize")} title={t("win.minimize")} onClick={() => hostApi()?.minimize?.()}>
            <Icon name="win-min" size={14} />
          </button>
          <button
            className="win-btn"
            aria-label={maximized ? t("win.restore") : t("win.maximize")}
            title={maximized ? t("win.restore") : t("win.maximize")}
            onClick={() => void toggleMaximize()}
          >
            <Icon name={maximized ? "win-restore" : "win-max"} size={13} />
          </button>
          <button className="win-btn close" aria-label={t("win.close")} title={t("win.close")} onClick={closeWindow}>
            <Icon name="win-close" size={14} />
          </button>
        </div>
      )}
    </div>
  );
}
