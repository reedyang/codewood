import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import type { GeneralConfig } from "../api/types";

interface GeneralSettingsProps {
  onDirtyChange?: (dirty: boolean) => void;
  saveSignal?: number;
}

/** General-runtime settings: a thin form over the top-level ``config.jsonc``
 *  fields the user is most likely to want to flip from the GUI without
 *  hand-editing the file. Mirrors ``ModelsSettings``' load/dirty/save shape so
 *  ``SettingsView`` can apply its unsaved-changes prompt the same way. */
export function GeneralSettings({ onDirtyChange, saveSignal }: GeneralSettingsProps) {
  const { getGeneralConfig, saveGeneralConfig, t } = useApp();
  const [loaded, setLoaded] = useState<GeneralConfig | null>(null);
  const [draft, setDraft] = useState<GeneralConfig | null>(null);
  // The text input lets the user clear ``max_tool_rounds`` to mean "unlimited";
  // we keep the raw string so an in-progress edit doesn't get re-normalized
  // back to a number while typing.
  const [maxRoundsText, setMaxRoundsText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const lastSaveSignal = useRef<number | undefined>(saveSignal);

  useEffect(() => {
    let alive = true;
    void getGeneralConfig().then((cfg) => {
      if (!alive || !cfg) {
        return;
      }
      setLoaded(cfg);
      setDraft(cfg);
      setMaxRoundsText(cfg.max_tool_rounds == null ? "" : String(cfg.max_tool_rounds));
    });
    return () => {
      alive = false;
    };
  }, [getGeneralConfig]);

  const dirty = (() => {
    if (!loaded || !draft) return false;
    if (draft.auto_compact_trigger_percent !== loaded.auto_compact_trigger_percent) return true;
    if (draft.memory_enabled !== loaded.memory_enabled) return true;
    if (draft.mcp_tools_enabled !== loaded.mcp_tools_enabled) return true;
    // Treat empty/whitespace as "unlimited" (null).
    const text = maxRoundsText.trim();
    const parsed = text === "" ? null : Number(text);
    const draftMtr = parsed === null || !Number.isFinite(parsed) || parsed <= 0 ? null : Math.floor(parsed);
    if (draftMtr !== loaded.max_tool_rounds) return true;
    return false;
  })();

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  const handleSave = async () => {
    if (!draft || saving) return;
    setError("");
    // Validate before submitting; the backend repeats these checks but giving
    // immediate feedback keeps the dialog interaction snappy.
    const pct = Number(draft.auto_compact_trigger_percent);
    if (!Number.isFinite(pct) || pct < 0 || pct > 100) {
      setError(t("general.errAutoCompactRange"));
      return;
    }
    const text = maxRoundsText.trim();
    let mtr: number | null = null;
    if (text !== "") {
      const parsed = Number(text);
      if (!Number.isFinite(parsed) || parsed < 0 || Math.floor(parsed) !== parsed) {
        setError(t("general.errMaxRoundsInt"));
        return;
      }
      mtr = parsed <= 0 ? null : parsed;
    }
    setSaving(true);
    const payload: Partial<GeneralConfig> = {
      auto_compact_trigger_percent: Math.floor(pct),
      max_tool_rounds: mtr,
      memory_enabled: draft.memory_enabled,
      mcp_tools_enabled: draft.mcp_tools_enabled,
    };
    const ok = await saveGeneralConfig(payload);
    setSaving(false);
    if (!ok) {
      setError(t("general.errSave"));
      return;
    }
    const next: GeneralConfig = {
      auto_compact_trigger_percent: payload.auto_compact_trigger_percent!,
      max_tool_rounds: payload.max_tool_rounds!,
      memory_enabled: payload.memory_enabled!,
      mcp_tools_enabled: payload.mcp_tools_enabled!,
    };
    setLoaded(next);
    setDraft(next);
    setMaxRoundsText(next.max_tool_rounds == null ? "" : String(next.max_tool_rounds));
  };

  // External save trigger from the SettingsView leave-confirmation dialog.
  useEffect(() => {
    if (saveSignal !== undefined && saveSignal !== lastSaveSignal.current) {
      lastSaveSignal.current = saveSignal;
      if (dirty) {
        void handleSave();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveSignal]);

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
              onChange={(e) =>
                setDraft({
                  ...draft,
                  auto_compact_trigger_percent: Number(e.target.value) || 0,
                })
              }
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
              onChange={(e) => setMaxRoundsText(e.target.value)}
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
              onChange={(e) => setDraft({ ...draft, memory_enabled: e.target.checked })}
            />
          </div>
          <p className="setting-hint">{t("general.memoryEnabledHint")}</p>
        </div>
      </div>

      <div className="setting-row">
        <label htmlFor="general-mcp">{t("general.mcpToolsEnabled")}</label>
        <div className="setting-control">
          <div className="setting-input-row">
            <input
              id="general-mcp"
              type="checkbox"
              checked={draft.mcp_tools_enabled}
              onChange={(e) => setDraft({ ...draft, mcp_tools_enabled: e.target.checked })}
            />
          </div>
          <p className="setting-hint">{t("general.mcpToolsEnabledHint")}</p>
        </div>
      </div>

      {error && <p className="setting-error">{error}</p>}

      <div className="settings-action-row">
        <button
          className="btn btn-primary"
          disabled={!dirty || saving}
          onClick={() => void handleSave()}
        >
          {saving ? t("models.saving") : t("models.save")}
        </button>
      </div>
    </div>
  );
}
