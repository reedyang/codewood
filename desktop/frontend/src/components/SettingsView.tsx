import { useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { useApp, type Theme } from "../state/AppContext";
import { Icon, type IconName } from "./Icon";
import { ModelsSettings } from "./ModelsSettings";
import { GeneralSettings } from "./GeneralSettings";
import { McpSettings } from "./McpSettings";
import { SubAgentsSettings } from "./SubAgentsSettings";
import { ConsoleSettings } from "./ConsoleSettings";
import { ArchivedChatsSettings } from "./ArchivedChatsSettings";

const THEME_OPTIONS: { value: Theme; icon: IconName }[] = [
  { value: "light", icon: "sun" },
  { value: "dark", icon: "moon" },
  { value: "system", icon: "monitor" },
];

const NAV_MIN = 160;
const NAV_MAX = 360;
const NAV_WIDTH_KEY = "codewood.settingsNavWidth";

type PageId = "appearance" | "general" | "models" | "mcp" | "subagents" | "console" | "archivedChats";

function loadNavWidth(): number {
  const raw = Number(window.localStorage.getItem(NAV_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= NAV_MIN && raw <= NAV_MAX) {
    return raw;
  }
  return 220;
}

export function SettingsView() {
  const {
    state,
    theme,
    setTheme,
    setBackgroundImage,
    clearBackgroundImage,
    setBackgroundOpacity,
    backgroundImageUrl,
    closeSettings,
    settingsInitialPage,
    t,
  } = useApp();
  const isPageId = (v: string | null): v is PageId =>
    v === "appearance" ||
    v === "general" ||
    v === "models" ||
    v === "mcp" ||
    v === "subagents" ||
    v === "console" ||
    v === "archivedChats";
  const [page, setPage] = useState<PageId>(
    isPageId(settingsInitialPage) ? settingsInitialPage : "general",
  );
  const bgHasImage = Boolean(state?.background?.hasImage);
  const bgOpacity = state?.background?.opacity ?? 85;
  const bgVersion = state?.background?.version ?? 0;
  // Local mirror so the slider drags smoothly; resynced when the server value
  // changes (e.g. after another window edits it).
  const [opacityDraft, setOpacityDraft] = useState(bgOpacity);
  useEffect(() => {
    setOpacityDraft(bgOpacity);
  }, [bgOpacity]);
  const [navWidth, setNavWidth] = useState(loadNavWidth);
  const [resizing, setResizing] = useState(false);
  const [modelsDirty, setModelsDirty] = useState(false);
  const [generalDirty, setGeneralDirty] = useState(false);
  const [saveSignal, setSaveSignal] = useState(0);
  // When true, the navigation runs once the page reports it's clean after auto-save.
  const [leaveAfterSave, setLeaveAfterSave] = useState(false);
  const pendingLeaveRef = useRef<PageId | "back" | null>(null);

  const performLeave = (target: PageId | "back") => {
    if (target === "back") {
      closeSettings();
    } else {
      setPage(target);
    }
  };

  // Pages that own their own dirty state. Whenever we try to leave one of
  // these pages while it's dirty, we trigger an auto-save before navigating away.
  const pageIsDirty = (id: PageId): boolean => {
    if (id === "models") return modelsDirty;
    if (id === "general") return generalDirty;
    return false;
  };

  const requestLeave = (target: PageId | "back") => {
    if (pageIsDirty(page)) {
      pendingLeaveRef.current = target;
      setLeaveAfterSave(true);
      setSaveSignal((n) => n + 1);
      return;
    }
    performLeave(target);
  };

  // Once a save clears the dirty flag, finish leaving.
  const handleModelsDirtyChange = (d: boolean) => {
    setModelsDirty(d);
    if (!d && leaveAfterSave) {
      setLeaveAfterSave(false);
      const target = pendingLeaveRef.current;
      pendingLeaveRef.current = null;
      if (target) performLeave(target);
    }
  };

  const handleGeneralDirtyChange = (d: boolean) => {
    setGeneralDirty(d);
    if (!d && leaveAfterSave) {
      setLeaveAfterSave(false);
      const target = pendingLeaveRef.current;
      pendingLeaveRef.current = null;
      if (target) performLeave(target);
    }
  };

  const goToPage = (id: PageId) => {
    if (id === page) return;
    requestLeave(id);
  };

  const back = () => {
    requestLeave("back");
  };

  useEffect(() => {
    window.localStorage.setItem(NAV_WIDTH_KEY, String(navWidth));
  }, [navWidth]);

  const startResize = (e: ReactMouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = navWidth;
    setResizing(true);
    document.body.classList.add("resizing-x");
    const onMove = (ev: MouseEvent) => {
      const next = Math.min(NAV_MAX, Math.max(NAV_MIN, startWidth + (ev.clientX - startX)));
      setNavWidth(next);
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

  const pages: { id: PageId; label: string; icon: IconName }[] = [
    { id: "general", label: t("settings.page.general"), icon: "gear" },
    { id: "appearance", label: t("settings.page.appearance"), icon: "sun" },
    { id: "models", label: t("settings.page.models"), icon: "cube" },
    { id: "mcp", label: t("settings.page.mcp"), icon: "plus" },
    { id: "subagents", label: t("settings.page.subagents"), icon: "robot" },
    { id: "console", label: t("settings.page.console"), icon: "terminal" },
    { id: "archivedChats", label: t("settings.page.archivedChats"), icon: "archive" },
  ];

  return (
    <div
      className="settings-view"
      style={{ "--settings-nav-width": `${navWidth}px` } as CSSProperties}
    >
      <aside className="settings-nav">
        <button className="settings-back" onClick={back}>
          <Icon name="arrow-left" size={15} />
          <span>{t("settings.back")}</span>
        </button>
        <nav className="settings-nav-list">
          {pages.map((p) => (
            <button
              key={p.id}
              className={`settings-nav-item ${page === p.id ? "active" : ""}`}
              onClick={() => goToPage(p.id)}
            >
              <Icon name={p.icon} size={15} />
              <span>{p.label}</span>
            </button>
          ))}
        </nav>
      </aside>
      <div
        className={`settings-resizer ${resizing ? "resizing" : ""}`}
        role="separator"
        aria-orientation="vertical"
        onMouseDown={startResize}
      />
      <section className="settings-content">
        {page === "appearance" && (
          <div className="settings-page">
            <h2 className="settings-page-title">{t("settings.page.appearance")}</h2>

            <div className="setting-row">
              <label>{t("settings.theme")}</label>
              <div className="segmented">
                {THEME_OPTIONS.map(({ value, icon }) => (
                  <button
                    key={value}
                    className={`segment ${theme === value ? "segment-active" : ""}`}
                    onClick={() => setTheme(value)}
                  >
                    <Icon name={icon} size={15} />
                    {t(`settings.theme.${value}`)}
                  </button>
                ))}
              </div>
            </div>

            <div className="setting-row">
              <label>{t("settings.background")}</label>
              <div className="setting-control">
                <div className="setting-input-row">
                  <button
                    className="btn"
                    onClick={() => void setBackgroundImage()}
                  >
                    {t("settings.background.choose")}
                  </button>
                  <button
                    className="btn"
                    disabled={!bgHasImage}
                    onClick={() => void clearBackgroundImage()}
                  >
                    {t("settings.background.clear")}
                  </button>
                </div>
                <p className="setting-hint">{t("settings.background.hint")}</p>
                {bgHasImage && (
                  <div className="setting-bg-preview">
                    <img
                      src={backgroundImageUrl(bgVersion)}
                      alt={t("settings.background")}
                      onError={(e) => {
                        (e.currentTarget as HTMLImageElement).dataset.error =
                          "1";
                      }}
                    />
                  </div>
                )}
              </div>
            </div>

            <div className="setting-row">
              <label htmlFor="settings-bg-opacity">
                {t("settings.background.opacity")}
              </label>
              <div className="setting-control">
                <div className="setting-input-row">
                  <input
                    id="settings-bg-opacity"
                    type="range"
                    min={0}
                    max={100}
                    step={1}
                    disabled={!bgHasImage}
                    value={opacityDraft}
                    onChange={(e) => {
                      const v = Number(e.target.value);
                      setOpacityDraft(v);
                      void setBackgroundOpacity(v);
                    }}
                  />
                  <span className="setting-unit">{opacityDraft}%</span>
                </div>
                <p className="setting-hint">
                  {t("settings.background.opacityHint")}
                </p>
              </div>
            </div>
          </div>
        )}
        {page === "general" && (
          <GeneralSettings onDirtyChange={handleGeneralDirtyChange} saveSignal={saveSignal} />
        )}
        {page === "models" && (
          <ModelsSettings onDirtyChange={handleModelsDirtyChange} saveSignal={saveSignal} />
        )}
        {page === "mcp" && <McpSettings />}
        {page === "subagents" && <SubAgentsSettings />}
        {page === "console" && <ConsoleSettings />}
        {page === "archivedChats" && <ArchivedChatsSettings />}
      </section>

    </div>
  );
}
