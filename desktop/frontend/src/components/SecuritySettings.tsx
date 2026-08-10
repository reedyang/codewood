import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { groupModelsByProvider } from "./ChatView";
import type { ConfirmAllowlist, SandboxConfig } from "../api/types";

export function SecuritySettings() {
  const { getConfirmAllowlist, saveConfirmAllowlist, getSecurityAuditConfig, saveSecurityAuditConfig, getModelSelectors, getSandboxConfig, saveSandboxConfig, setupSandbox, t } = useApp();
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
  const [sandbox, setSandbox] = useState<SandboxConfig | null>(null);
  const [sandboxLoaded, setSandboxLoaded] = useState(false);
  const [sandboxError, setSandboxError] = useState("");
  const [sandboxBusy, setSandboxBusy] = useState(false);

  const getConfirmAllowlistRef = useRef(getConfirmAllowlist);
  getConfirmAllowlistRef.current = getConfirmAllowlist;
  const getSecurityAuditConfigRef = useRef(getSecurityAuditConfig);
  getSecurityAuditConfigRef.current = getSecurityAuditConfig;
  const saveConfirmAllowlistRef = useRef(saveConfirmAllowlist);
  saveConfirmAllowlistRef.current = saveConfirmAllowlist;
  const getSandboxConfigRef = useRef(getSandboxConfig);
  getSandboxConfigRef.current = getSandboxConfig;
  const saveSandboxConfigRef = useRef(saveSandboxConfig);
  saveSandboxConfigRef.current = saveSandboxConfig;
  const setupSandboxRef = useRef(setupSandbox);
  setupSandboxRef.current = setupSandbox;

  useEffect(() => {
    let alive = true;
    void getSandboxConfigRef.current().then((cfg) => {
      if (!alive) return;
      setSandbox(cfg);
      setSandboxLoaded(true);
    });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      setError("");
      const result = await getConfirmAllowlistRef.current();
      if (!alive) return;
      setData(result ?? { version: 3, salt: "", shell_scripts: [], shell_exe_tokens: [] });
      setLoaded(true);
      setLoading(false);
    })();
    return () => { alive = false; };
  }, []);

  // Load audit model config
  useEffect(() => {
    let alive = true;
    void getSecurityAuditConfigRef.current().then((cfg) => {
      if (!alive) return;
      if (cfg) {
        setAuditModel(cfg.security_audit_model ?? "");
      }
    });
    return () => {
      alive = false;
    };
  }, []);

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

  const handleSandboxLevelChange = (level: string) => {
    if (!sandbox) return;
    setSandbox({ ...sandbox, sandbox_level: level });
    setSandboxError("");
    void saveSandboxConfigRef.current({ sandbox_level: level }).then((result) => {
      if (!result.ok) {
        setSandboxError(t("sandbox.errSave"));
        return;
      }
      if (result.sandbox) setSandbox(result.sandbox);
    });
  };

  const handleSandboxNetworkChange = (checked: boolean) => {
    if (!sandbox) return;
    setSandbox({ ...sandbox, sandbox_network: checked });
    setSandboxError("");
    void saveSandboxConfigRef.current({ sandbox_network: checked }).then(
      (result) => {
        if (!result.ok) {
          setSandboxError(t("sandbox.errSave"));
          return;
        }
        if (result.sandbox) setSandbox(result.sandbox);
      },
    );
  };

  const handleSetupSandbox = async () => {
    setSandboxBusy(true);
    setSandboxError("");
    try {
      const result = await setupSandboxRef.current();
      if (!result.ok) {
        setSandboxError(result.message || t("sandbox.setupFailed"));
        return;
      }
      // The elevated helper recreates users and can take up to a minute;
      // poll for completion and refresh the page state as soon as the
      // sandbox is provisioned with working credentials.
      for (let i = 0; i < 24; i++) {
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const cfg = await getSandboxConfigRef.current();
        if (cfg) {
          setSandbox(cfg);
          if (cfg.provisioned && cfg.passwords_ok !== false) break;
        }
      }
    } finally {
      setSandboxBusy(false);
    }
  };

  // Auto-save on every data change after the initial load.
  const prevDataRef = useRef<string | null>(null);
  useEffect(() => {
    if (!loaded || !data) return;
    const snapshot = JSON.stringify(data);
    if (snapshot === prevDataRef.current) return;
    prevDataRef.current = snapshot;
    saveConfirmAllowlistRef.current(data).then((result) => {
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

      <section style={{ marginBottom: 24 }}>
        <h3 className="setting-section-title">{t("sandbox.settings")}</h3>
        <p
          className="setting-hint"
          style={{ textAlign: "left", marginBottom: 12, marginTop: 4 }}
        >
          {t("sandbox.levelHint")}
        </p>
        {!sandboxLoaded ? (
          <p className="setting-hint">Loading…</p>
        ) : !sandbox ? (
          <p className="setting-error">{t("sandbox.loadFailed")}</p>
        ) : !sandbox.supported ? (
          <p className="setting-hint" style={{ textAlign: "left" }}>
            {t("sandbox.unsupported")}
          </p>
        ) : (
          <>
            <select
              className="select"
              title={t("sandbox.settings")}
              value={sandbox.sandbox_level}
              onChange={(e) => handleSandboxLevelChange(e.target.value)}
            >
              <option value="read_only">{t("sandbox.readOnly")}</option>
              <option value="workspace_write">{t("sandbox.workspaceWrite")}</option>
              <option value="full_access">{t("sandbox.fullAccess")}</option>
            </select>
            {sandbox.sandbox_level === "full_access" && (
              <p className="setting-error" style={{ marginTop: 8 }}>
                {t("sandbox.fullAccessWarning")}
              </p>
            )}
            {sandbox.sandbox_level === "workspace_write" && (
              <div className="setting-row" style={{ marginTop: 12 }}>
                <label htmlFor="sandbox-network">
                  {t("sandbox.allowNetwork")}
                </label>
                <div className="setting-control">
                  <div className="setting-input-row">
                    <input
                      id="sandbox-network"
                      type="checkbox"
                      checked={sandbox.sandbox_network}
                      onChange={(e) => handleSandboxNetworkChange(e.target.checked)}
                    />
                  </div>
                  <p className="setting-hint">{t("sandbox.allowNetworkHint")}</p>
                </div>
              </div>
            )}
            {(!sandbox.provisioned || sandbox.passwords_ok === false) && (
              <>
                {sandbox.passwords_ok === false && (
                  <p className="setting-error" style={{ marginTop: 8 }}>
                    {t("sandbox.passwordsMismatch")}
                  </p>
                )}
                <div className="sandbox-setup-row">
                  <button
                    className="btn"
                    onClick={handleSetupSandbox}
                    disabled={sandboxBusy}
                  >
                    {sandboxBusy ? t("sandbox.settingUp") : t("sandbox.setup")}
                  </button>
                  <span className="setting-hint">
                    {sandbox.passwords_ok === false
                      ? t("sandbox.passwordsMismatchHint")
                      : t("sandbox.notProvisioned")}
                  </span>
                </div>
              </>
            )}
            {sandboxError && (
              <p className="setting-error" style={{ marginTop: 8 }}>
                {sandboxError}
              </p>
            )}
          </>
        )}
      </section>

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