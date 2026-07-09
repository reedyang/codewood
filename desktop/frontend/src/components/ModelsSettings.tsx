import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import {
  MODEL_PRESETS,
  findPreset,
  toEditorProvider,
  toConfigProviders,
  type EditorProvider,
  type EditorModel,
  type EditorHeader,
  type ModelPreset,
} from "./modelPresets";

const FIXED_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"] as const;

interface ModelsSettingsProps {
  onDirtyChange?: (dirty: boolean) => void;
  saveSignal?: number;
}

export function ModelsSettings({ onDirtyChange, saveSignal }: ModelsSettingsProps) {
  const { getModelsConfig, saveModelsConfig, getModelPresets, fetchProviderModels, t } = useApp();
  const [providers, setProviders] = useState<EditorProvider[]>([]);
  const [presets, setPresets] = useState<ModelPreset[]>(MODEL_PRESETS);
  const [loading, setLoading] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [busyIdx, setBusyIdx] = useState<number | null>(null);
  const [errorByIdx, setErrorByIdx] = useState<Record<number, string>>({});
  const [revealKey, setRevealKey] = useState<Record<number, boolean>>({});
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({});
  const [expandModel, setExpandModel] = useState<Record<string, boolean>>({});
  const [confirmRemoveIdx, setConfirmRemoveIdx] = useState<number | null>(null);
  const [fetchContextAttr, setFetchContextAttr] = useState<Record<string, number | undefined>>({});
  const lastSaveSignal = useRef<number | undefined>(saveSignal);
  const getModelsConfigRef = useRef(getModelsConfig);
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const providersRef = useRef<EditorProvider[]>([]);
  providersRef.current = providers;
  const saveRef = useRef<() => Promise<boolean>>(() => Promise.resolve(true));
  getModelsConfigRef.current = getModelsConfig;
  const getModelPresetsRef = useRef(getModelPresets);
  getModelPresetsRef.current = getModelPresets;

  useEffect(() => {
    let alive = true;
    void getModelsConfigRef.current().then((raw) => {
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
// Also load merged presets from backend.
    void getModelPresetsRef.current().then((remote) => {
      if (!alive || !Array.isArray(remote) || remote.length === 0) return;
      const remotePresets = remote as ModelPreset[];
      const remoteById = new Map(
        remotePresets
          .filter((preset) => preset && typeof preset === "object")
          .map((preset) => [preset.id, preset]),
      );
      const merged = MODEL_PRESETS.map((preset) => ({
        ...preset,
        ...(remoteById.get(preset.id) ?? {}),
      }));
      for (const preset of remotePresets) {
        if (!preset?.id || merged.some((item) => item.id === preset.id)) continue;
        merged.push(preset);
      }
      // Reorder: remote presets in their original order, then built-in
      // presets that weren't in the remote list come after.
      const remoteOrder: ModelPreset[] = [];
      const addedIds = new Set<string>();
      for (const rp of remotePresets) {
        if (!rp.id) continue;
        const full = merged.find((m) => m.id === rp.id);
        if (full) {
          remoteOrder.push(full);
          addedIds.add(full.id);
        }
      }
      const builtinOnly = merged.filter((p) => !addedIds.has(p.id));
      const reordered = [...remoteOrder, ...builtinOnly];
      setPresets(reordered);
      // Re-map providers whose presetId is "custom" but match a newly loaded preset.
      setProviders((prev) => prev.map((p) => {
        if (p.presetId !== "custom") return p;
        const base = p.base_url.replace(/\/+$/, "");
        const match = reordered.find(
          (pre) => pre.kind === "openai" && pre.base_url.replace(/\/+$/, "") === base,
        );
        if (!match) return p;
        return { ...p, presetId: match.id, provider: match.provider };
      }));
    });
    return () => {
      alive = false;
    };
  }, []);

  // A provider freshly added but otherwise untouched needs no delete confirm.
  const isPristineProvider = (p: EditorProvider): boolean => {
    const preset = presets.find((pr) => pr.id === p.presetId);
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
    // Debounced auto-save on every change.
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      void saveRef.current();
    }, 500);
  };

  const update = (idx: number, patch: Partial<EditorProvider>) => {
    setProviders((prev) => prev.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
    markDirty();
  };

  const changePreset = (idx: number, presetId: string) => {
    const preset = presets.find((pr) => pr.id === presetId);
    if (!preset) return;
    update(idx, {
      presetId,
      api_mode: preset.api_mode,
      base_url: preset.kind === "openai" ? preset.base_url : "",
      include_thinking_in_messages: preset.include_thinking_in_messages ?? false,
      provider: preset.kind === "custom" ? providers[idx]?.provider || "" : preset.provider,
    });
  };

  const addProvider = () => {
    const preset = presets[0];
    if (!preset) return;
    setProviders((prev) => {
      const next: EditorProvider = {
        provider: preset.provider || "Custom",
        display_name: "",
        api_key: "",
        base_url: preset.base_url,
        api_mode: preset.api_mode,
        include_thinking_in_messages: preset.include_thinking_in_messages ?? false,
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

  const refreshModels = async (idx: number, silent?: boolean) => {
    const p = providers[idx];
    const preset = presets.find((pr) => pr.id === p.presetId);
    const contextAttr = preset?.context_length_attr_name || "";
    if (!silent) {
      setBusyIdx(idx);
      setErrorByIdx((e) => ({ ...e, [idx]: "" }));
    }
    const res = await fetchProviderModels({
      base_url: p.base_url,
      api_key: p.api_key,
      api_mode: p.api_mode,
      port: p.port,
      context_length_attr_name: contextAttr,
    });
    if (!silent) setBusyIdx(null);
    if (!res.ok) {
      if (!silent) setErrorByIdx((e) => ({ ...e, [idx]: res.error || "Fetch failed" }));
      return;
    }
    const fetched = res.models ?? [];
    const apiCtx: Record<string, number | undefined> = {};
    setProviders((prev) =>
      prev.map((prov, i) => {
        if (i !== idx) return prov;
        const existing = new Map(prov.models.map((m) => [m.name, m]));
        const merged = fetched.map(
          (entry) => {
            const name = typeof entry === "string" ? entry : entry.name;
            const ctx = typeof entry === "object" ? entry.context_window : undefined;
            const existingM = existing.get(name);
            // Track API-provided context_window for the reset button.
            apiCtx[name] = ctx;
            if (existingM) {
              // Fill in context_window for models that don't have one yet.
              if (ctx && (existingM.context_window === undefined || existingM.context_window === "")) {
                return { ...existingM, context_window: String(ctx) };
              }
              return existingM;
            }
            return {
              name,
              enabled: prov.auto_refresh ?? false,
              context_window: ctx ? String(ctx) : undefined,
              use_clean_content: false,
              reasoning_effort: [],
              extra_headers: [],
            };
          },
        );
        for (const m of prov.models) {
          if (!fetched.some((e) => (typeof e === "string" ? e : e.name) === m.name)) {
            merged.push(m);
          }
        }
        return { ...prov, models: merged };
      }),
    );
    setFetchContextAttr((prev) => ({ ...prev, ...Object.fromEntries(
      Object.entries(apiCtx).map(([name, val]) => [`${idx}:${name}`, val]),
    ) }));
    if (!silent) markDirty();
  };

  // On initial load, auto-refresh providers that have context_length_attr_name
  // so fetchContextAttr is populated from cache and reset buttons appear.
  const initialRefreshRef = useRef(false);
  useEffect(() => {
    if (loading || initialRefreshRef.current) return;
    initialRefreshRef.current = true;
    providers.forEach((p, idx) => {
      const pr = presets.find((pre) => pre.id === p.presetId);
      if (pr?.context_length_attr_name) {
        void refreshModels(idx, true);
      }
    });
  }, [loading, providers, presets]);

  const patchModel = (
    pIdx: number,
    mName: string,
    patch: Partial<EditorModel>,
  ) => {
    setProviders((prev) =>
      prev.map((p, i) =>
        i === pIdx
          ? {
              ...p,
              models: p.models.map((m) =>
                m.name === mName ? { ...m, ...patch } : m,
              ),
            }
          : p,
      ),
    );
    markDirty();
  };

  const toggleModel = (pIdx: number, mName: string) => {
    const m = providers[pIdx]?.models.find((x) => x.name === mName);
    if (m) patchModel(pIdx, mName, { enabled: !m.enabled });
  };

  const toggleModelEffort = (pIdx: number, mName: string, level: string) => {
    const m = providers[pIdx]?.models.find((x) => x.name === mName);
    if (!m) return;
    const has = m.reasoning_effort.includes(level);
    const next = has
      ? m.reasoning_effort.filter((l) => l !== level)
      : [...m.reasoning_effort, level];
    patchModel(pIdx, mName, { reasoning_effort: next });
  };

  const addModelHeader = (pIdx: number, mName: string) => {
    const m = providers[pIdx]?.models.find((x) => x.name === mName);
    if (!m) return;
    patchModel(pIdx, mName, {
      extra_headers: [...m.extra_headers, { key: "", value: "" }],
    });
  };

  const patchModelHeader = (
    pIdx: number,
    mName: string,
    hIdx: number,
    patch: Partial<EditorHeader>,
  ) => {
    const m = providers[pIdx]?.models.find((x) => x.name === mName);
    if (!m) return;
    patchModel(pIdx, mName, {
      extra_headers: m.extra_headers.map((h, i) => (i === hIdx ? { ...h, ...patch } : h)),
    });
  };

  const removeModelHeader = (pIdx: number, mName: string, hIdx: number) => {
    const m = providers[pIdx]?.models.find((x) => x.name === mName);
    if (!m) return;
    patchModel(pIdx, mName, {
      extra_headers: m.extra_headers.filter((_, i) => i !== hIdx),
    });
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
    const ok = await saveModelsConfig(toConfigProviders(providers));
    if (!ok) {
      window.alert(t("models.saveFailed"));
      return false;
    }
    setDirty(false);
    onDirtyChange?.(false);
    return true;
  };
  saveRef.current = save;

  // Allow the parent (leave-page prompt) to trigger a save.
  useEffect(() => {
    if (saveSignal !== undefined && saveSignal !== lastSaveSignal.current) {
      lastSaveSignal.current = saveSignal;
      void save();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveSignal]);

  // Cleanup: flush pending save on unmount (e.g. window close).
  useEffect(() => {
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      if (dirtyRef.current) {
        void saveModelsConfig(toConfigProviders(providersRef.current));
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (loading) {
    return <div className="settings-page">{t("models.loading")}</div>;
  }

  return (
    <div className="settings-page models-settings">
      <h2 className="settings-page-title">{t("settings.page.models")}</h2>

      {providers.map((p, idx) => {
        const preset = presets.find((pr) => pr.id === p.presetId);
        const isOllama = preset?.kind === "ollama" || p.api_mode === "ollama";
        const presetDefaultThinking =
          (preset ?? findPreset(p.presetId))?.include_thinking_in_messages === true;
        const showThinkingWarning =
          !isOllama &&
          presetDefaultThinking &&
          p.include_thinking_in_messages === false;
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
                  {presets.map((mp) => (
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

                    <div className="models-field">
                      <label>{t("models.apiMode")}</label>
                      <select
                        className="select"
                        aria-label={t("models.apiMode")}
                        value={p.api_mode}
                        onChange={(e) => update(idx, { api_mode: e.target.value })}
                      >
                        <option value="auto">{t("models.apiModeAuto")}</option>
                        <option value="chat">{t("models.apiModeChat")}</option>
                        <option value="responses">{t("models.apiModeResponses")}</option>
                      </select>
                    </div>

                    <div className="models-field">
                      <label className="models-effort">
                        <input
                          type="checkbox"
                          checked={p.include_thinking_in_messages === true}
                          onChange={(e) =>
                            update(idx, {
                              include_thinking_in_messages: e.target.checked,
                            })
                          }
                        />
                        {t("models.includeThinkingInMessages")}
                      </label>
                      <div className="models-hint">
                        {t("models.includeThinkingInMessagesHint")}
                      </div>
                      {showThinkingWarning && (
                        <div className="models-warning">
                          {t("models.includeThinkingInMessagesWarning")}
                        </div>
                      )}
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
                    p.models.map((m) => {
                      const mkey = `${idx}:${m.name}`;
                      // Track whether the API provided a context_window for this model.
                      const apiCtx = fetchContextAttr[mkey];
                      const hasApiCtx = apiCtx !== undefined;
                      // Ollama models can still tune context_window / multimodal
                      // (the backend reads both for every provider); only
                      // reasoning effort and custom headers stay hidden for them.
                      const showDetails = Boolean(expandModel[mkey]);
                      return (
                        <div className="models-model" key={m.name}>
                          <div className="models-item-row">
                            <label className="models-item">
                              <input
                                type="checkbox"
                                checked={m.enabled}
                                onChange={() => toggleModel(idx, m.name)}
                              />
                              <span>{m.name}</span>
                            </label>
                            <button
                              className="models-model-toggle"
                              aria-expanded={showDetails}
                              aria-label={t("models.modelOptions")}
                              title={t("models.modelOptions")}
                              onClick={() =>
                                setExpandModel((e) => ({ ...e, [mkey]: !e[mkey] }))
                              }
                            >
                              <Icon
                                name="chevron"
                                size={12}
                                className={`chevron ${showDetails ? "down" : ""}`}
                              />
                            </button>
                          </div>
                          {showDetails && (
                            <div className="models-model-details">
                              <div className="models-field">
                                <label>{t("models.contextWindow")}</label>
                                <div className="models-input-row">
                                <input
                                  className="text-input"
                                  placeholder={t("models.contextWindowPlaceholder")}
                                  value={
                                    m.context_window === undefined
                                      ? ""
                                      : String(m.context_window)
                                  }
                                  onChange={(e) =>
                                    patchModel(idx, m.name, {
                                      context_window:
                                        e.target.value.trim() === ""
                                          ? undefined
                                          : e.target.value,
                                    })
                                  }
                                />
                                {hasApiCtx && (
                                  <button
                                    className="icon-btn"
                                    title={t("models.resetContextWindow")}
                                    aria-label={t("models.resetContextWindow")}
                                    onClick={() =>
                                      patchModel(idx, m.name, {
                                        context_window: String(apiCtx),
                                      })
                                    }
                                  >
                                    <Icon name="sparkles" size={13} />
                                  </button>
                                )}
                                </div>
                              </div>
                              <div className="models-field">
                                <label className="models-effort">
                                  <input
                                    type="checkbox"
                                    checked={m.multimodal !== false}
                                    onChange={(e) =>
                                      patchModel(idx, m.name, {
                                        multimodal: e.target.checked,
                                      })
                                    }
                                  />
                                  {t("models.multimodal")}
                                </label>
                              </div>
                              <div className="models-field">
                                <label className="models-effort">
                                  <input
                                    type="checkbox"
                                    checked={m.streaming !== false}
                                    onChange={(e) =>
                                      patchModel(idx, m.name, {
                                        streaming: e.target.checked,
                                      })
                                    }
                                  />
                                  {t("models.streaming")}
                                </label>
                              </div>
                              <div className="models-field">
                                <label className="models-effort">
                                  <input
                                    type="checkbox"
                                    checked={m.use_clean_content === true}
                                    onChange={(e) =>
                                      patchModel(idx, m.name, {
                                        use_clean_content: e.target.checked,
                                      })
                                    }
                                  />
                                  {t("models.useCleanContent")}
                                </label>
                                <div className="models-hint">
                                  {t("models.useCleanContentHint")}
                                </div>
                              </div>
                              <div className="models-field">
                                <label className="models-effort">
                                  <input
                                    type="checkbox"
                                    checked={m.thinking !== false}
                                    onChange={(e) =>
                                      patchModel(idx, m.name, {
                                        thinking: e.target.checked,
                                      })
                                    }
                                  />
                                  {t("models.thinking")}
                                </label>
                                <div className="models-hint">{t("models.thinkingHint")}</div>
                              </div>
                              {!isOllama && (
                              <>
                              <div className="models-field">
                                <label>{t("reasoning.label")}</label>
                                <div className="models-effort-row">
                                  {FIXED_EFFORTS.map((level) => (
                                    <label className="models-effort" key={level}>
                                      <input
                                        type="checkbox"
                                        checked={m.reasoning_effort.includes(level)}
                                        onChange={() =>
                                          toggleModelEffort(idx, m.name, level)
                                        }
                                      />
                                      {t(`reasoning.effort.${level}`)}
                                    </label>
                                  ))}
                                </div>
                              </div>
                              <div className="models-field">
                                <label>{t("models.extraHeaders")}</label>
                                {m.extra_headers.map((h, hIdx) => (
                                  <div className="models-header-row" key={hIdx}>
                                    <input
                                      className="text-input"
                                      placeholder={t("models.headerName")}
                                      value={h.key}
                                      onChange={(e) =>
                                        patchModelHeader(idx, m.name, hIdx, {
                                          key: e.target.value,
                                        })
                                      }
                                    />
                                    <input
                                      className="text-input"
                                      placeholder={t("models.headerValue")}
                                      value={h.value}
                                      onChange={(e) =>
                                        patchModelHeader(idx, m.name, hIdx, {
                                          value: e.target.value,
                                        })
                                      }
                                    />
                                    <button
                                      className="icon-btn"
                                      aria-label={t("models.removeHeader")}
                                      title={t("models.removeHeader")}
                                      onClick={() => removeModelHeader(idx, m.name, hIdx)}
                                    >
                                      <Icon name="win-close" size={12} />
                                    </button>
                                  </div>
                                ))}
                                <button
                                  className="btn btn-small"
                                  onClick={() => addModelHeader(idx, m.name)}
                                >
                                  <Icon name="plus" size={12} />
                                  {t("models.addHeader")}
                                </button>
                              </div>
                              </>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })
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
