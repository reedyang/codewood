import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { groupModelsByProvider } from "./ChatView";
import type { ConfirmAllowlist } from "../api/types";

export function SecuritySettings() {
  const { getConfirmAllowlist, saveConfirmAllowlist, getSecurityAuditConfig, saveSecurityAuditConfig, getModelSelectors, t } = useApp();
  const [data, setData] = useState<ConfirmAllowlist | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [auditModel, setAuditModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [auditError, setAuditError] = useState("");
  const [newScriptPath, setNewScriptPath] = useState("");
  const [newExeToken, setNewExeToken] = useState("");
  const [confirmRemoveAll, setConfirmRemoveAll] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const result = await getConfirmAllowlist();
    if (result) {
      setData(result);
    } else {
      setData({
        version: 3,
        salt: "",
        shell_scripts: [],
        shell_exe_tokens: [],
      });
    }
    setLoaded(true);
    setLoading(false);
  }, [getConfirmAllowlist]);

  useEffect(() => {
    void load();
  }, [load]);

  // Load audit model config
  useEffect(() => {
    let alive = true;
    void getSecurityAuditConfig().then((cfg) => {
      if (!alive) return;
      if (cfg) {
        setAuditModel(cfg.security_audit_model ?? "");
      }
    });
    return () => {
      alive = false;
    };
  }, [getSecurityAuditConfig]);

  // Load model selectors for the dropdown
  useEffect(() => {
    let alive = true;
    void getModelSelectors().then((list) => {
      if (!alive) return;
      setModels(Array.isArray(list) ? list : []);
    });
    return () => { alive = false; };
  }, []);

  // Save audit model on dropdown change (no debounce needed)
  const handleAuditModelChange = (value: string) => {
    setAuditModel(value);
    setAuditError("");
    void saveSecurityAuditConfig({ security_audit_model: value }).then((ok) => {
      if (!ok) setAuditError(t("security.errSaveAuditModel"));
    });
  };

  // Auto-save on every data change after the initial load.
  const prevDataRef = useRef<string | null>(null);
  useEffect(() => {
    if (!loaded || !data) return;
    const snapshot = JSON.stringify(data);
    if (snapshot === prevDataRef.current) return;
    prevDataRef.current = snapshot;
    saveConfirmAllowlist(data).then((result) => {
      if (!result.ok) {
        setError(t("security.errSave"));
      } else if (result.allowlist) {
        setData(result.allowlist);
      }
    });
  });

  const addScript = () => {
    if (!data || !newScriptPath.trim()) return;
    const path = newScriptPath.trim();
    if (data.shell_scripts.some((s) => s.path === path)) return;
    prevDataRef.current = null;
    setData({
      ...data,
      shell_scripts: [
        ...data.shell_scripts,
        { path, hash: "" },
      ],
    });
    setNewScriptPath("");
  };

  const removeScript = (path: string) => {
    if (!data) return;
    prevDataRef.current = null;
    setData({
      ...data,
      shell_scripts: data.shell_scripts.filter((s) => s.path !== path),
    });
  };

  const addExeToken = () => {
    if (!data || !newExeToken.trim()) return;
    const token = newExeToken.trim();
    if (data.shell_exe_tokens.includes(token)) return;
    prevDataRef.current = null;
    setData({
      ...data,
      shell_exe_tokens: [...data.shell_exe_tokens, token],
    });
    setNewExeToken("");
  };

  const removeExeToken = (token: string) => {
    if (!data) return;
    prevDataRef.current = null;
    setData({
      ...data,
      shell_exe_tokens: data.shell_exe_tokens.filter((t) => t !== token),
    });
  };

  const handleRemoveAll = () => {
    if (!data) return;
    prevDataRef.current = null;
    setData({
      ...data,
      shell_scripts: [],
      shell_exe_tokens: [],
    });
    setConfirmRemoveAll(false);
  };

  if (loading) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.security")}</h2>
        <p className="setting-hint">Loading…</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.security")}</h2>
      <section>
        <h3 className="setting-section-title">
          {t("security.auditModel")}
        </h3>
        <p className="setting-hint" style={{ textAlign: "left", marginBottom: 8, marginTop: 4 }}>
          {t("security.auditModelHint")}
        </p>
        <select
          className="select"
          title={t("security.auditModel")}
          value={auditModel}
          onChange={(e) => handleAuditModelChange(e.target.value)}
        >
          <option value="">{t("security.auditModelDefault")}</option>
          {groupModelsByProvider(models).map((group) => (
            <optgroup key={group.provider} label={group.provider}>
              {group.items.map((item) => (
                <option key={item.selector} value={item.selector}>{item.name}</option>
              ))}
            </optgroup>
          ))}
        </select>
        {auditError && <p className="setting-error" style={{ marginTop: 8 }}>{auditError}</p>}
      </section>

      <section style={{ marginTop: 24 }}>
        <h3 className="setting-section-title">
          {t("security.confirmAllowlist")}
        </h3>
        <p className="setting-hint" style={{ textAlign: "left", marginBottom: 16 }}>
          {t("security.confirmAllowlistHint")}
        </p>
      </section>

      <section>
        <h4 className="setting-section-subtitle">
          {t("security.shellScripts")}
        </h4>
        <div className="security-list">
          {data?.shell_scripts.length === 0 ? (
            <p className="setting-hint" style={{ textAlign: "left" }}>
              {t("security.noScripts")}
            </p>
          ) : (
            data?.shell_scripts.map((s) => (
              <div key={s.path} className="security-list-item">
                <div className="security-list-item-content">
                  <span className="security-list-path">{s.path}</span>
                  {s.hash && (
                    <span className="security-list-hash">{s.hash.slice(0, 16)}…</span>
                  )}
                </div>
                <button
                  className="btn btn-icon"
                  title={t("common.remove")}
                  onClick={() => removeScript(s.path)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))
          )}
        </div>
        <div className="security-add-row">
          <input
            className="text-input"
            placeholder={t("security.scriptPathPlaceholder")}
            value={newScriptPath}
            onChange={(e) => setNewScriptPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addScript()}
          />
          <button className="btn" onClick={addScript} disabled={!newScriptPath.trim()}>
            Add
          </button>
        </div>
      </section>

      <section style={{ marginTop: 24 }}>
        <h4 className="setting-section-subtitle">
          {t("security.shellExeTokens")}
        </h4>
        <p
          className="setting-hint"
          style={{ textAlign: "left", marginBottom: 8 }}
        >
          {t("security.exeTokenHint")}
        </p>
        <div className="security-list">
          {data?.shell_exe_tokens.length === 0 ? (
            <p className="setting-hint" style={{ textAlign: "left" }}>
              {t("security.noExeTokens")}
            </p>
          ) : (
            data?.shell_exe_tokens.map((token) => (
              <div key={token} className="security-list-item">
                <span className="security-list-path">{token}</span>
                <button
                  className="btn btn-icon"
                  title={t("common.remove")}
                  onClick={() => removeExeToken(token)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            ))
          )}
        </div>
        <div className="security-add-row">
          <input
            className="text-input"
            placeholder={t("security.exeTokenPlaceholder")}
            value={newExeToken}
            onChange={(e) => setNewExeToken(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addExeToken()}
          />
          <button className="btn" onClick={addExeToken} disabled={!newExeToken.trim()}>
            Add
          </button>
        </div>
      </section>

      <div className="settings-action-row">
        <button
          className="btn btn-danger"
          onClick={() => setConfirmRemoveAll(true)}
        >
          {t("security.removeAll")}
        </button>
      </div>

      {error && <p className="setting-error" style={{ marginTop: 16 }}>{error}</p>}

      {confirmRemoveAll && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h3 className="modal-title">{t("security.removeAllConfirm")}</h3>
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirmRemoveAll(false)}>
                {t("common.cancel")}
              </button>
              <button className="btn btn-danger" onClick={handleRemoveAll}>
                {t("security.removeAll")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}