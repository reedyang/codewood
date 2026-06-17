import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import {
  MODEL_PRESETS,
  findPreset,
  toEditorProvider,
  toConfigProviders,
  type EditorProvider,
} from "./modelPresets";

interface ModelsSettingsProps {
  onDirtyChange?: (dirty: boolean) => void;
  saveSignal?: number;
}

export function ModelsSettings({ onDirtyChange, saveSignal }: ModelsSettingsProps) {
  const { getModelsConfig, saveModelsConfig, fetchProviderModels, t } = useApp();
  const [providers, setProviders] = useState<EditorProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busyIdx, setBusyIdx] = useState<number | null>(null);
  const [errorByIdx, setErrorByIdx] = useState<Record<number, string>>({});
  const [revealKey, setRevealKey] = useState<Record<number, boolean>>({});
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});
  const [confirmRemoveIdx, setConfirmRemoveIdx] = useState<number | null>(null);
  const lastSaveSignal = useRef<number | undefined>(saveSignal);

  useEffect(() => {
    let alive = true;
    void getModelsConfig().then((raw) => {
      if (!alive) return;
      const loaded = raw.map(toEditorProvider);
      setProviders(loaded);
      // Default to all providers collapsed when the page opens.
      const allCollapsed: Record<number, boolean> = {};
      loaded.forEach((_, i) => {
        allCollapsed[i] = true;
      });
      setCollapsed(allCollapsed);
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [getModelsConfig]);

  // A provider freshly added but otherwise untouched needs no delete confirm.
  const isPristineProvider = (p: EditorProvider): boolean => {
    const preset = findPreset(p.presetId);
    const defaultBase = preset?.base_url ?? "";
    return (
      !p.display_name.trim() &&
      !p.api_key.trim() &&
      p.models.length === 0 &&
      !p.auto_refresh &&
      p.base_url === defaultBase
    );
  };

  const markDirty = () => {
    if (!dirty) {
      setDirty(true);
      onDirtyChange?.(true);
    }
  };

  const update = (idx: number, patch: Partial<EditorProvider>) => {
    setProviders((prev) => prev.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
    markDirty();
  };

  const changePreset = (idx: number, presetId: string) => {
    const preset = findPreset(presetId);
    if (!preset) return;
    update(idx, {
      presetId,
      api_mode: preset.api_mode,
      base_url: preset.kind === "openai" ? preset.base_url : "",
      provider: preset.kind === "custom" ? providers[idx]?.provider || "" : preset.provider,
    });
  };

  const addProvider = () => {
    const preset = MODEL_PRESETS[0];
    if (!preset) return;
    setProviders((prev) => {
      const next: EditorProvider = {
        provider: preset.provider || "Custom",
        display_name: "",
        api_key: "",
        base_url: preset.base_url,
        api_mode: preset.api_mode,
        models: [],
        auto_refresh: false,
        presetId: preset.id,
      };
      // Expand the newly added provider so the user can edit it right away.
      setCollapsed((c) => ({ ...c, [prev.length]: false }));
      return [...prev, next];
    });
    markDirty();
  };

  const doRemove = (idx: number) => {
    setProviders((prev) => prev.filter((_, i) => i !== idx));
    markDirty();
  };

  const removeProvider = (idx: number) => {
    const p = providers[idx];
    if (p && !isPristineProvider(p)) {
      setConfirmRemoveIdx(idx);
      return;
    }
    doRemove(idx);
  };

  const refreshModels = async (idx: number) => {
    const p = providers[idx];
    setBusyIdx(idx);
    setErrorByIdx((e) => ({ ...e, [idx]: "" }));
    const res = await fetchProviderModels({
      base_url: p.base_url,
      api_key: p.api_key,
      api_mode: p.api_mode,
      port: p.port,
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
        const merged = fetched.map(
          (name) => existing.get(name) ?? { name, enabled: prov.auto_refresh ?? false },
        );
        for (const m of prov.models) {
          if (!fetched.includes(m.name)) merged.push(m);
        }
        return { ...prov, models: merged };
      }),
    );
    markDirty();
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
    markDirty();
  };

  /** Returns a validation error message, or "" when valid. */
  const validate = (): string => {
    // Group providers by their provider id; when 2+ share one, every member
    // must have a non-empty, unique display name.
    const groups = new Map<string, number[]>();
    providers.forEach((p, i) => {
      const key = p.provider.trim();
      const arr = groups.get(key) ?? [];
      arr.push(i);
      groups.set(key, arr);
    });
    for (const [, idxs] of groups) {
      if (idxs.length < 2) continue;
      const names = idxs.map((i) => providers[i].display_name.trim());
      if (names.some((n) => !n)) {
        return t("models.dupNeedDisplayName");
      }
      if (new Set(names).size !== names.length) {
        return t("models.dupDisplayNameUnique");
      }
    }
    return "";
  };

  const save = async (): Promise<boolean> => {
    const err = validate();
    if (err) {
      window.alert(err);
      return false;
    }
    setSaving(true);
    const ok = await saveModelsConfig(toConfigProviders(providers));
    setSaving(false);
    if (!ok) {
      window.alert(t("models.saveFailed"));
      return false;
    }
    setDirty(false);
    onDirtyChange?.(false);
    return true;
  };

  // Allow the parent (leave-page prompt) to trigger a save.
  useEffect(() => {
    if (saveSignal !== undefined && saveSignal !== lastSaveSignal.current) {
      lastSaveSignal.current = saveSignal;
      void save();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveSignal]);

  if (loading) {
    return <div className="settings-page">{t("models.loading")}</div>;
  }

  return (
    <div className="settings-page models-settings">
      <h2 className="settings-page-title">{t("settings.page.models")}</h2>

      {providers.map((p, idx) => {
        const preset = findPreset(p.presetId);
        const isOllama = preset?.kind === "ollama" || p.api_mode === "ollama";
        const isCollapsed = Boolean(collapsed[idx]);
        const collapsedLabel =
          p.display_name.trim() || p.provider.trim() || preset?.label || t("models.provider");
        return (
          <div className={`models-provider ${isCollapsed ? "collapsed" : ""}`} key={idx}>
            <div className="models-provider-head">
              <button
                className="models-collapse"
                aria-expanded={!isCollapsed}
                aria-label={t("models.toggleProvider")}
                onClick={() => setCollapsed((c) => ({ ...c, [idx]: !c[idx] }))}
              >
                <Icon
                  name="chevron"
                  size={13}
                  className={`chevron ${isCollapsed ? "" : "down"}`}
                />
              </button>
              {isCollapsed ? (
                <span className="models-provider-label">{collapsedLabel}</span>
              ) : (
                <select
                  className="select models-provider-name"
                  aria-label={t("models.platform")}
                  value={p.presetId}
                  onChange={(e) => changePreset(idx, e.target.value)}
                >
                  {MODEL_PRESETS.map((mp) => (
                    <option key={mp.id} value={mp.id}>
                      {mp.label}
                    </option>
                  ))}
                </select>
              )}
              <button
                className="icon-btn"
                aria-label={t("models.removeProvider")}
                title={t("models.removeProvider")}
                onClick={() => removeProvider(idx)}
              >
                <Icon name="win-close" size={14} />
              </button>
            </div>

            {!isCollapsed && (
              <>
                {!isOllama && (
                  <div className="models-field">
                    <label>{t("models.displayName")}</label>
                    <input
                      className="text-input"
                      aria-label={t("models.displayName")}
                      placeholder={t("models.displayNamePlaceholder")}
                      value={p.display_name}
                      onChange={(e) => update(idx, { display_name: e.target.value })}
                    />
                  </div>
                )}

                {!isOllama && (
                  <>
                    <div className="models-field">
                      <label>{t("models.apiKey")}</label>
                      <div className="models-key">
                        <input
                          className="text-input"
                          type={revealKey[idx] ? "text" : "password"}
                          aria-label={t("models.apiKey")}
                          value={p.api_key}
                          onChange={(e) => update(idx, { api_key: e.target.value })}
                        />
                        <button
                          className="icon-btn models-key-eye"
                          aria-label={
                            revealKey[idx] ? t("models.hideKey") : t("models.showKey")
                          }
                          title={revealKey[idx] ? t("models.hideKey") : t("models.showKey")}
                          onClick={() => setRevealKey((r) => ({ ...r, [idx]: !r[idx] }))}
                        >
                          <Icon name={revealKey[idx] ? "eye-off" : "eye"} size={15} />
                        </button>
                      </div>
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
                  </>
                )}

                <div className="models-row">
                  <button
                    className="btn"
                    disabled={busyIdx === idx}
                    onClick={() => void refreshModels(idx)}
                  >
                    <Icon
                      name={busyIdx === idx ? "spinner" : "list-check"}
                      size={14}
                      className={busyIdx === idx ? "icon-spin" : ""}
                    />
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
              </>
            )}
          </div>
        );
      })}

      <div className="models-add">
        <button className="btn" onClick={addProvider}>
          <Icon name="plus" size={14} />
          {t("models.add")}
        </button>
        <button
          className="btn btn-primary"
          disabled={saving || !dirty}
          onClick={() => void save()}
        >
          {saving ? t("models.saving") : t("models.save")}
        </button>
      </div>

      {confirmRemoveIdx !== null && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("models.removeTitle")}</h3>
            <p className="modal-body">{t("models.removeConfirm")}</p>
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirmRemoveIdx(null)}>
                {t("models.leaveCancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => {
                  doRemove(confirmRemoveIdx);
                  setConfirmRemoveIdx(null);
                }}
              >
                {t("models.removeOk")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
