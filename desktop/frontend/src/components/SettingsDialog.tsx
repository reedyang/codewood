import { useApp, type Theme } from "../state/AppContext";
import { SUPPORTED_LANGS } from "../i18n";

const POLICIES = ["unlimited", "moderate", "confirmation"] as const;

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
            {(["light", "dark"] as Theme[]).map((value) => (
              <button
                key={value}
                className={`segment ${theme === value ? "segment-active" : ""}`}
                onClick={() => setTheme(value)}
              >
                {t(`settings.theme.${value}`)}
              </button>
            ))}
          </div>
        </div>

        <div className="setting-row">
          <label>{t("settings.language")}</label>
          <select
            className="select"
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

        <div className="setting-row">
          <label>{t("settings.model")}</label>
          <select
            className="select"
            value={state?.model.current ?? ""}
            onChange={(e) => void runCommand(`/model ${e.target.value}`)}
          >
            {(state?.model.available ?? []).map((selector) => (
              <option key={selector} value={selector}>
                {selector}
              </option>
            ))}
          </select>
        </div>

        <div className="setting-row">
          <label>{t("settings.executionPolicy")}</label>
          <select
            className="select"
            value={state?.executionPolicy ?? "moderate"}
            onChange={(e) => void runCommand(`/execution-policy ${e.target.value}`)}
          >
            {POLICIES.map((policy) => (
              <option key={policy} value={policy}>
                {t(`settings.policy.${policy}`)}
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
