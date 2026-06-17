import { useEffect, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from "react";
import { useApp, type Theme } from "../state/AppContext";
import { SUPPORTED_LANGS } from "../i18n";
import { Icon, type IconName } from "./Icon";
import { ModelsSettings } from "./ModelsSettings";

const THEME_OPTIONS: { value: Theme; icon: IconName }[] = [
  { value: "light", icon: "sun" },
  { value: "dark", icon: "moon" },
  { value: "system", icon: "monitor" },
];

const NAV_MIN = 160;
const NAV_MAX = 360;
const NAV_WIDTH_KEY = "codewood.settingsNavWidth";

type PageId = "appearance" | "models";

function loadNavWidth(): number {
  const raw = Number(window.localStorage.getItem(NAV_WIDTH_KEY));
  if (Number.isFinite(raw) && raw >= NAV_MIN && raw <= NAV_MAX) {
    return raw;
  }
  return 220;
}

export function SettingsView() {
  const { state, theme, setTheme, runCommand, closeSettings, t } = useApp();
  const [page, setPage] = useState<PageId>("appearance");
  const [navWidth, setNavWidth] = useState(loadNavWidth);
  const [resizing, setResizing] = useState(false);

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

  const pages: { id: PageId; label: string }[] = [
    { id: "appearance", label: t("settings.page.appearance") },
    { id: "models", label: t("settings.page.models") },
  ];

  return (
    <div
      className="settings-view"
      style={{ "--settings-nav-width": `${navWidth}px` } as CSSProperties}
    >
      <aside className="settings-nav">
        <button className="settings-back" onClick={closeSettings}>
          <Icon name="arrow-left" size={15} />
          <span>{t("settings.back")}</span>
        </button>
        <nav className="settings-nav-list">
          {pages.map((p) => (
            <button
              key={p.id}
              className={`settings-nav-item ${page === p.id ? "active" : ""}`}
              onClick={() => setPage(p.id)}
            >
              {p.label}
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
                value={state?.language ?? "en"}
                onChange={(e) => void runCommand(`/language ${e.target.value}`)}
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
        {page === "models" && <ModelsSettings />}
      </section>
    </div>
  );
}
