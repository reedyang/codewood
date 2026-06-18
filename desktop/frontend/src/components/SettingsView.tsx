import { useEffect, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { useApp, type Theme } from "../state/AppContext";
import { SUPPORTED_LANGS, normalizeLang } from "../i18n";
import { Icon, type IconName } from "./Icon";
import { ModelsSettings } from "./ModelsSettings";
import { GeneralSettings } from "./GeneralSettings";
import { McpSettings } from "./McpSettings";
import { SubAgentsSettings } from "./SubAgentsSettings";

const THEME_OPTIONS: { value: Theme; icon: IconName }[] = [
  { value: "light", icon: "sun" },
  { value: "dark", icon: "moon" },
  { value: "system", icon: "monitor" },
];

const NAV_MIN = 160;
const NAV_MAX = 360;
const NAV_WIDTH_KEY = "codewood.settingsNavWidth";

type PageId = "appearance" | "general" | "models" | "mcp" | "subagents";

function loadNavWidth(): number {
  const raw = Number(window.localStorage.getItem(NAV_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= NAV_MIN && raw <= NAV_MAX) {
    return raw;
  }
  return 220;
}

export function SettingsView() {
  const { state, theme, setTheme, setGuiLanguage, closeSettings, t } = useApp();
  const [page, setPage] = useState<PageId>("general");
  const [navWidth, setNavWidth] = useState(loadNavWidth);
  const [resizing, setResizing] = useState(false);
  const [modelsDirty, setModelsDirty] = useState(false);
  const [generalDirty, setGeneralDirty] = useState(false);
  const [saveSignal, setSaveSignal] = useState(0);
  // A queued navigation that is waiting on the unsaved-changes prompt.
  const [pendingLeave, setPendingLeave] = useState<PageId | "back" | null>(null);
  // When true, the pending navigation runs once the page reports it's clean.
  const [leaveAfterSave, setLeaveAfterSave] = useState(false);

  const performLeave = (target: PageId | "back") => {
    if (target === "back") {
      closeSettings();
    } else {
      setPage(target);
    }
  };

  // Pages that own their own dirty state. Whenever we try to leave one of
  // these pages while it's dirty, we open the leave-confirmation prompt
  // instead of navigating away immediately.
  const pageIsDirty = (id: PageId): boolean => {
    if (id === "models") return modelsDirty;
    if (id === "general") return generalDirty;
    return false;
  };

  const requestLeave = (target: PageId | "back") => {
    if (pageIsDirty(page)) {
      setPendingLeave(target);
      return;
    }
    performLeave(target);
  };

  const onDialogSave = () => {
    setLeaveAfterSave(true);
    setSaveSignal((n) => n + 1);
  };

  const onDialogDiscard = () => {
    const target = pendingLeave;
    setPendingLeave(null);
    // Drop dirty flags for any page that might have triggered the dialog so we
    // don't reopen it immediately on the next navigation attempt.
    setModelsDirty(false);
    setGeneralDirty(false);
    if (target) performLeave(target);
  };

  const onDialogCancel = () => {
    setPendingLeave(null);
    setLeaveAfterSave(false);
  };

  // Once a save triggered by the dialog clears the dirty flag, finish leaving.
  const handleModelsDirtyChange = (d: boolean) => {
    setModelsDirty(d);
    if (!d && leaveAfterSave) {
      setLeaveAfterSave(false);
      const target = pendingLeave;
      setPendingLeave(null);
      if (target) performLeave(target);
    }
  };

  const handleGeneralDirtyChange = (d: boolean) => {
    setGeneralDirty(d);
    if (!d && leaveAfterSave) {
      setLeaveAfterSave(false);
      const target = pendingLeave;
      setPendingLeave(null);
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

  const langLabel = (code: string) => (code === "zh-CN" ? "简体中文" : "English");

  const pages: { id: PageId; label: string; icon: IconName }[] = [
    { id: "general", label: t("settings.page.general"), icon: "gear" },
    { id: "appearance", label: t("settings.page.appearance"), icon: "sun" },
    { id: "models", label: t("settings.page.models"), icon: "cube" },
    { id: "mcp", label: t("settings.page.mcp"), icon: "plus" },
    { id: "subagents", label: t("settings.page.subagents"), icon: "robot" },
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
              <label>{t("settings.language")}</label>
              <select
                className="select"
                aria-label={t("settings.language")}
                value={normalizeLang(state?.language)}
                onChange={(e) => void setGuiLanguage(e.target.value)}
              >
                {SUPPORTED_LANGS.map((code) => (
                  <option key={code} value={code}>
                    {langLabel(code)}
                  </option>
                ))}
              </select>
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
      </section>

      {pendingLeave !== null && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("models.leaveTitle")}</h3>
            <p className="modal-body">{t("models.leavePrompt")}</p>
            <div className="modal-actions">
              <button className="btn" onClick={onDialogCancel}>
                {t("models.leaveCancel")}
              </button>
              <button className="btn" onClick={onDialogDiscard}>
                {t("models.leaveDiscard")}
              </button>
              <button className="btn btn-primary" onClick={onDialogSave}>
                {t("models.leaveSave")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
