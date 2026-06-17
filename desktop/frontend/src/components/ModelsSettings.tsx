import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import {
  MODEL_PRESETS,
  findPreset,
  toEditorProvider,
  toConfigProviders,
  type EditorProvider,
} from "./modelPresets";

export function ModelsSettings() {
  const { getModelsConfig, saveModelsConfig, fetchProviderModels, t } = useApp();
  const [providers, setProviders] = useState<EditorProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [addPreset, setAddPreset] = useState("deepseek");
  const [busyIdx, setBusyIdx] = useState<number | null>(null);
  const [errorByIdx, setErrorByIdx] = useState<Record<number, string>>({});

  useEffect(() => {
    let alive = true;
    void getModelsConfig().then((raw) => {
      if (!alive) return;
      setProviders(raw.map(toEditorProvider));
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [getModelsConfig]);

  const update = (idx: number, patch: Partial<EditorProvider>) => {
    setProviders((prev) => prev.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
  };

  const addProvider = () => {
    const preset = findPreset(addPreset);
    if (!preset) return;
    setProviders((prev) => [
      ...prev,
      {
        provider: preset.provider || "Custom",
        api_key: "",
        base_url: preset.base_url,
        api_mode: preset.api_mode,
        models: [],
        auto_refresh: false,
      },
    ]);
  };

  const removeProvider = (idx: number) => {
    setProviders((prev) => prev.filter((_, i) => i !== idx));
  };

  const refreshModels = async (idx: number) => {
    const p = providers[idx];
    setBusyIdx(idx);
    setErrorByIdx((e) => ({ ...e, [idx]: "" }));
    const res = await fetchProviderModels({
      base_url: p.base_url,
      api_key: p.api_key,
      api_mode: p.api_mode,
    });
    setBusyIdx(null);
    if (!res.ok) {
      setErrorByIdx((e) => ({ ...e, [idx]: res.error || "Fetch failed" }));
      return;
    }
    const fetched = res.models ?? [];
    setProviders((prev) =>
      prev.map((prov, i) => {
        if (i !== idx) return prov;
        const existing = new Map(prov.models.map((m) => [m.name, m]));
        const merged = fetched.map((name) => existing.get(name) ?? { name, enabled: prov.auto_refresh ?? false });
        // Keep any manually-added models not present in the fetched list.
        for (const m of prov.models) {
          if (!fetched.includes(m.name)) merged.push(m);
        }
        return { ...prov, models: merged };
      }),
    );
  };

  const toggleModel = (pIdx: number, mName: string) => {
    setProviders((prev) =>
      prev.map((p, i) =>
        i === pIdx
          ? {
              ...p,
              models: p.models.map((m) =>
                m.name === mName ? { ...m, enabled: !m.enabled } : m,
              ),
            }
          : p,
      ),
    );
  };

  const save = async () => {
    setSaving(true);
    const ok = await saveModelsConfig(toConfigProviders(providers));
    setSaving(false);
    if (!ok) {
      window.alert(t("models.saveFailed"));
    }
  };

  if (loading) {
    return <div className="settings-page">{t("models.loading")}</div>;
  }

  return (
    <div className="settings-page models-settings">
      <h2 className="settings-page-title">{t("settings.page.models")}</h2>

      <div className="models-add">
        <select
          className="select"
          value={addPreset}
          aria-label={t("models.platform")}
          onChange={(e) => setAddPreset(e.target.value)}
        >
          {MODEL_PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        <button className="btn" onClick={addProvider}>
          <Icon name="plus" size={14} />
          {t("models.addProvider")}
        </button>
      </div>

      {providers.map((p, idx) => (
        <div className="models-provider" key={idx}>
          <div className="models-provider-head">
            <input
              className="text-input models-provider-name"
              aria-label={t("models.providerName")}
              placeholder={t("models.providerName")}
              value={p.provider}
              onChange={(e) => update(idx, { provider: e.target.value })}
            />
            <button
              className="icon-btn"
              aria-label={t("models.removeProvider")}
              title={t("models.removeProvider")}
              onClick={() => removeProvider(idx)}
            >
              <Icon name="win-close" size={14} />
            </button>
          </div>

          <div className="models-field">
            <label>{t("models.apiKey")}</label>
            <input
              className="text-input"
              type="password"
              aria-label={t("models.apiKey")}
              value={p.api_key}
              onChange={(e) => update(idx, { api_key: e.target.value })}
            />
          </div>

          <div className="models-field">
            <label>{t("models.baseUrl")}</label>
            <input
              className="text-input"
              aria-label={t("models.baseUrl")}
              value={p.base_url}
              onChange={(e) => update(idx, { base_url: e.target.value })}
            />
          </div>

          <div className="models-row">
            <button
              className="btn"
              disabled={busyIdx === idx}
              onClick={() => void refreshModels(idx)}
            >
              <Icon name={busyIdx === idx ? "spinner" : "list-check"} size={14} className={busyIdx === idx ? "icon-spin" : ""} />
              {t("models.refresh")}
            </button>
            <label className="models-auto">
              <input
                type="checkbox"
                checked={Boolean(p.auto_refresh)}
                onChange={(e) => update(idx, { auto_refresh: e.target.checked })}
              />
              {t("models.autoRefresh")}
            </label>
          </div>
          {errorByIdx[idx] && <div className="models-error">{errorByIdx[idx]}</div>}

          <div className="models-list">
            {p.models.length === 0 ? (
              <div className="models-empty">{t("models.noModels")}</div>
            ) : (
              p.models.map((m) => (
                <label className="models-item" key={m.name}>
                  <input
                    type="checkbox"
                    checked={m.enabled}
                    onChange={() => toggleModel(idx, m.name)}
                  />
                  <span>{m.name}</span>
                </label>
              ))
            )}
          </div>
        </div>
      ))}

      <div className="models-actions">
        <button className="btn btn-primary" disabled={saving} onClick={() => void save()}>
          {saving ? t("models.saving") : t("models.save")}
        </button>
      </div>
    </div>
  );
}
