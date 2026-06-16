import { useApp, type Theme } from "../state/AppContext";
import { SUPPORTED_LANGS } from "../i18n";
import { Icon, type IconName } from "./Icon";

const THEME_OPTIONS: { value: Theme; icon: IconName }[] = [
  { value: "light", icon: "sun" },
  { value: "dark", icon: "moon" },
  { value: "system", icon: "monitor" },
];

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const { state, theme, setTheme, runCommand, t } = useApp();

  const langLabel = (code: string) => (code === "zh-CN" ? "简体中文" : "English");

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal settings-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">{t("settings.title")}</h3>

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

        <div className="modal-actions">
          <button className="btn btn-primary" onClick={onClose}>
            {t("settings.close")}
          </button>
        </div>
      </div>
    </div>
  );
}
