import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

interface HostWindowApi {
  minimize?: () => void;
  toggle_maximize?: () => boolean | Promise<boolean>;
  close_window?: () => void;
  open_external?: (url: string) => boolean | Promise<boolean>;
  start_window_drag?: () => boolean | Promise<boolean>;
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
  const { t, pickAndOpenFolder, newChat, openSettings, openAbout, showBrowserTab, hideBrowserTab, showConsole, hideConsole, consoleOpen, browserOpen, closeSettings, settingsOpen } = useApp();

  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [native, setNative] = useState<boolean>(() => Boolean(hostApi()));
  const [maximized, setMaximized] = useState(false);
  // Defaults to "win32" so the pywebview-drag-region is present on the very
  // first paint (matching prior behavior) until the host reports otherwise.
  const [hostOs, setHostOs] = useState<string>("win32");
  const barRef = useRef<HTMLDivElement | null>(null);

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
        { label: t("menu.view.browser"), checked: browserOpen, onSelect: () => browserOpen ? hideBrowserTab() : showBrowserTab() },
        { label: t("menu.view.console"), checked: consoleOpen, onSelect: () => consoleOpen ? hideConsole() : showConsole() },
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
    <div className="titlebar" ref={barRef}>
      <button
        className={`icon-btn titlebar-toggle ${collapsed ? "" : "active"}`}
        aria-label={t("panel.toggle")}
        title={t("panel.toggle")}
        onClick={onTogglePanel}
      >
        <Icon name="panel" size={18} />
      </button>

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

      <div
        className={`titlebar-drag ${hostOs === "win32" ? "pywebview-drag-region" : ""}`}
        onMouseDown={(e) => {
          // Left button only; let double-clicks fall through to maximize.
          if (e.button !== 0 || e.detail > 1) {
            return;
          }
          // GTK/WSL: hand the drag to the window manager so the window
          // follows the cursor across mixed-DPI monitors. The host returns
          // false on Windows, where the native pywebview-drag-region handles
          // it instead — so we only suppress that default when GTK took over.
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

      {native && (
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
