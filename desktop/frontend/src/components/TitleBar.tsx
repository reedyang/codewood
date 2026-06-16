import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

interface HostWindowApi {
  minimize?: () => void;
  toggle_maximize?: () => boolean | Promise<boolean>;
  close_window?: () => void;
}

function hostApi(): HostWindowApi | undefined {
  return (window as unknown as { pywebview?: { api?: HostWindowApi } }).pywebview?.api;
}

type MenuEntry =
  | "separator"
  | { label: string; shortcut?: string; onSelect: () => void };

export function TitleBar({ onTogglePanel }: { onTogglePanel: () => void }) {
  const { t, clearTurns, runCommand, newChat, openSettings, openAbout, pickFolder } = useApp();
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [native, setNative] = useState<boolean>(() => Boolean(hostApi()));
  const [maximized, setMaximized] = useState(false);
  const barRef = useRef<HTMLDivElement | null>(null);

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

  const closeWindow = () => hostApi()?.close_window?.();

  const openFolder = async () => {
    const path = await pickFolder();
    if (path) {
      clearTurns();
      await runCommand(`/workspace create "${path.replace(/"/g, "")}"`);
    }
  };

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
        { label: t("menu.file.close"), shortcut: "Ctrl+W", onSelect: closeWindow },
        "separator",
        { label: t("menu.file.settings"), shortcut: "Ctrl+,", onSelect: openSettings },
        "separator",
        { label: t("menu.file.exit"), onSelect: closeWindow },
      ],
    },
    {
      id: "help",
      label: t("menu.help"),
      entries: [{ label: t("menu.help.about"), onSelect: openAbout }],
    },
  ];

  return (
    <div className="titlebar" ref={barRef}>
      <button
        className="icon-btn titlebar-toggle"
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
                {menu.entries.map((entry, idx) =>
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
                      <span>{entry.label}</span>
                      {entry.shortcut && <span className="menubar-shortcut">{entry.shortcut}</span>}
                    </button>
                  ),
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      <div
        className="titlebar-drag pywebview-drag-region"
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
