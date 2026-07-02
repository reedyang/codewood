import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { SUPPORTED_LANGS, normalizeLang } from "../i18n";
import type { GeneralConfig } from "../api/types";

const langLabel = (code: string) => (code === "zh-CN" ? "简体中文" : "English");

interface GeneralSettingsProps {
  /** Kept for compatibility with ``SettingsView`` even though the page now
   *  auto-saves: passing a no-op keeps the unsaved-changes guard quiet. */
  onDirtyChange?: (dirty: boolean) => void;
  saveSignal?: number;
}

/** General-runtime settings page. Every change auto-saves immediately to
 *  ``config.jsonc`` so there is no Save button and no leave-confirmation
 *  prompt — losing focus or navigating away can't lose state. */
export function GeneralSettings({ onDirtyChange }: GeneralSettingsProps) {
  const { state, setGuiLanguage, getGeneralConfig, saveGeneralConfig, t } = useApp();
  const [draft, setDraft] = useState<GeneralConfig | null>(null);
  // ``max_tool_rounds`` accepts an empty input meaning "unlimited"; we hold
  // the raw text so an in-progress edit doesn't get re-normalized while the
  // user is still typing.
  const [maxRoundsText, setMaxRoundsText] = useState("");
  const [error, setError] = useState("");
  const debounceRef = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    void getGeneralConfig().then((cfg) => {
      if (!alive || !cfg) {
        return;
      }
      setDraft(cfg);
      setMaxRoundsText(cfg.max_tool_rounds == null ? "" : String(cfg.max_tool_rounds));
    });
    return () => {
      alive = false;
    };
  }, [getGeneralConfig]);

  // The page no longer has a dirty state — every edit is committed
  // immediately. Tell the parent so its leave-confirmation can stay quiet.
  useEffect(() => {
    onDirtyChange?.(false);
  }, [onDirtyChange]);

  /** Persist a partial update. Numeric / range validation lives here so an
   *  out-of-range value is visibly rejected without bouncing the user's
   *  edit back to the previous value mid-keypress. */
  const persist = (next: GeneralConfig, mtrText: string) => {
    setError("");
    const pct = Number(next.auto_compact_trigger_percent);
    if (!Number.isFinite(pct) || pct < 0 || pct > 100) {
      setError(t("general.errAutoCompactRange"));
      return;
    }
    const text = mtrText.trim();
    let mtr: number | null = null;
    if (text !== "") {
      const parsed = Number(text);
      if (!Number.isFinite(parsed) || parsed < 0 || Math.floor(parsed) !== parsed) {
        setError(t("general.errMaxRoundsInt"));
        return;
      }
      mtr = parsed <= 0 ? null : parsed;
    }
    const payload: Partial<GeneralConfig> = {
      auto_compact_trigger_percent: Math.floor(pct),
      max_tool_rounds: mtr,
      memory_enabled: next.memory_enabled,
    };
    void saveGeneralConfig(payload).then((ok) => {
      if (!ok) setError(t("general.errSave"));
    });
  };

  /** Debounced text-input save: holds back the network call for ~280ms after
   *  the last keystroke so typing in the percent / max-rounds inputs doesn't
   *  fire one request per character. */
  const persistDebounced = (next: GeneralConfig, mtrText: string) => {
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
    }
    debounceRef.current = window.setTimeout(() => {
      debounceRef.current = null;
      persist(next, mtrText);
    }, 280);
  };

  useEffect(() => {
    return () => {
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
    };
  }, []);

  if (!draft) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.general")}</h2>
        <p className="muted">{t("models.loading")}</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.general")}</h2>

      <div className="setting-row">
        <label htmlFor="general-language">{t("settings.language")}</label>
        <select
          id="general-language"
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

      <div className="setting-row">
        <label htmlFor="general-auto-compact">{t("general.autoCompactTrigger")}</label>
        <div className="setting-control">
          <div className="setting-input-row">
            <input
              id="general-auto-compact"
              type="number"
              className="input"
              min={0}
              max={100}
              value={draft.auto_compact_trigger_percent}
              onChange={(e) => {
                const next = {
                  ...draft,
                  auto_compact_trigger_percent: Number(e.target.value) || 0,
                };
                setDraft(next);
                persistDebounced(next, maxRoundsText);
              }}
            />
            <span className="setting-unit">%</span>
          </div>
          <p className="setting-hint">{t("general.autoCompactHint")}</p>
        </div>
      </div>

      <div className="setting-row">
        <label htmlFor="general-max-rounds">{t("general.maxToolRounds")}</label>
        <div className="setting-control">
          <div className="setting-input-row">
            <input
              id="general-max-rounds"
              type="number"
              className="input"
              min={0}
              placeholder={t("general.maxToolRoundsUnlimited")}
              value={maxRoundsText}
              onChange={(e) => {
                setMaxRoundsText(e.target.value);
                persistDebounced(draft, e.target.value);
              }}
            />
          </div>
          <p className="setting-hint">{t("general.maxToolRoundsHint")}</p>
        </div>
      </div>

      <div className="setting-row">
        <label htmlFor="general-memory">{t("general.memoryEnabled")}</label>
        <div className="setting-control">
          <div className="setting-input-row">
            <input
              id="general-memory"
              type="checkbox"
              checked={draft.memory_enabled}
              onChange={(e) => {
                const next = { ...draft, memory_enabled: e.target.checked };
                setDraft(next);
                // Checkbox toggles are discrete: save without debouncing.
                persist(next, maxRoundsText);
              }}
            />
          </div>
          <p className="setting-hint">{t("general.memoryEnabledHint")}</p>
        </div>
      </div>

      {error && <p className="setting-error">{error}</p>}
    </div>
  );
}
