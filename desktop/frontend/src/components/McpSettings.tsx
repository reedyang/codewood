import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import type {
  McpServerConfigEntry,
  McpServerDetails,
  McpServerSummary,
} from "../api/types";

/** MCP settings page.
 *
 *  Mirrors the Models settings page UX:
 *   - One row per configured MCP server (collapsed by default).
 *   - Per-server enable toggle (writes ``skip_preload`` in ``mcp.jsonc`` and
 *     triggers a reconnect attempt when enabling).
 *   - When expanded the row lazily loads the cached tool/prompt catalog and
 *     surfaces a clickable chip per tool: enabled tools are highlighted; a
 *     click flips the per-tool disabled-tools policy.
 *
 *  This page never modifies the server entry's transport config (command/url
 *  args/etc.); that's still hand-edited in ``mcp.jsonc``. */
export function McpSettings() {
  const {
    getMcpOverview,
    getMcpServerDetails,
    setMcpServerEnabled,
    setMcpToolEnabled,
    setMcpToolsEnabled,
    getMcpServerConfig,
    addMcpServer,
    updateMcpServer,
    deleteMcpServer,
    mcpIconUrl,
    t,
  } = useApp();
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  // Editor modal state. ``originalName`` is empty for a new server (Add) and
  // set to the existing server's name when editing in-place.
  const [editor, setEditor] = useState<{
    open: boolean;
    originalName: string;
    initial: McpServerConfigEntry;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [details, setDetails] = useState<Record<string, McpServerDetails>>({});
  const [loadingDetails, setLoadingDetails] = useState<Record<string, boolean>>({});
  const [busyServer, setBusyServer] = useState<Record<string, boolean>>({});
  const [busyTool, setBusyTool] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const aliveRef = useRef(true);

  const refresh = async (opts?: { silent?: boolean }) => {
    const silent = !!opts?.silent;
    if (!silent) {
      setLoading(true);
    }
    const list = await getMcpOverview();
    if (!aliveRef.current) return;
    setServers(list);
    // Default all servers collapsed when the page opens.
    setCollapsed((prev) => {
      const next = { ...prev };
      for (const s of list) {
        if (!(s.name in next)) {
          next[s.name] = true;
        }
      }
      return next;
    });
    if (!silent) {
      setLoading(false);
    }
  };

  useEffect(() => {
    aliveRef.current = true;
    void refresh();
    return () => {
      aliveRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const hasLoadingServer = servers.some((server) => isServerLoading(server));
    if (!hasLoadingServer) return;
    const timer = window.setInterval(() => {
      void refresh({ silent: true });
    }, 1500);
    return () => {
      window.clearInterval(timer);
    };
    // ``refresh`` is intentionally recreated per render; resetting the polling
    // interval while MCP status changes is harmless here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [servers]);

  useEffect(() => {
    for (const server of servers) {
      if (!server.enabled || collapsed[server.name] !== false) continue;
      if (loadingDetails[server.name]) continue;
      const detail = details[server.name];
      if (!detail) {
        void loadDetails(server.name);
        continue;
      }
      if (detail.loading && !isServerLoading(server)) {
        void loadDetails(server.name);
      }
    }
    // Driven by expansion state and live MCP status only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [servers, collapsed, details, loadingDetails]);

  const toggleCollapsed = (name: string) => {
    const wasCollapsed = collapsed[name] !== false;
    setCollapsed((prev) => ({ ...prev, [name]: !wasCollapsed }));
    // Lazy-load on first expand.
    if (wasCollapsed && !details[name]) {
      void loadDetails(name);
    }
  };

  const loadDetails = async (name: string) => {
    setLoadingDetails((prev) => ({ ...prev, [name]: true }));
    const data = await getMcpServerDetails(name);
    if (!aliveRef.current) return;
    setLoadingDetails((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });
    if (data && data.ok) {
      setDetails((prev) => ({ ...prev, [name]: data }));
    }
  };

  const onToggleServer = async (server: McpServerSummary) => {
    if (busyServer[server.name]) return;
    setError("");
    setBusyServer((prev) => ({ ...prev, [server.name]: true }));
    const ok = await setMcpServerEnabled(server.name, !server.enabled);
    setBusyServer((prev) => {
      const next = { ...prev };
      delete next[server.name];
      return next;
    });
    if (!ok) {
      setError(t("mcp.errToggleServer"));
      return;
    }
    // Refresh the overview to pick up the new enabled state + status.
    await refresh({ silent: true });
    // If the server was just enabled and we have it expanded, also refresh
    // its tool/prompt catalog since reconnect may have repopulated the cache.
    if (!server.enabled && collapsed[server.name] === false) {
      await loadDetails(server.name);
    }
  };

  const openEditor = async (name: string) => {
    // Lazy-load the raw entry so the form is populated from disk (the
    // overview only carries summary fields like ``enabled`` and counts).
    const entry = await getMcpServerConfig(name);
    setEditor({ open: true, originalName: name, initial: entry || {} });
  };

  const openAddDialog = () => {
    setEditor({ open: true, originalName: "", initial: {} });
  };

  const submitEditor = async (
    nextName: string,
    entry: McpServerConfigEntry,
  ): Promise<string> => {
    const result = editor?.originalName
      ? await updateMcpServer(editor.originalName, nextName, entry)
      : await addMcpServer(nextName, entry);
    if (!result.ok) {
      return result.error || "save_failed";
    }
    setEditor(null);
    await refresh({ silent: true });
    return "";
  };

  const performDelete = async (name: string) => {
    const result = await deleteMcpServer(name);
    setConfirmDelete("");
    if (!result.ok) {
      setError(t("mcp.errDelete"));
      return;
    }
    await refresh({ silent: true });
  };

  const onToggleAllTools = async (server: string, targetEnabled: boolean) => {
    const detail = details[server];
    if (!detail || detail.tools.length === 0) return;
    const allKey = `${server}::__all__`;
    if (busyTool[allKey]) return;
    setBusyTool((prev) => ({ ...prev, [allKey]: true }));
    const toolNames = detail.tools.map((tool) => tool.name);
    const ok = await setMcpToolsEnabled(server, toolNames, targetEnabled);
    setBusyTool((prev) => {
      const next = { ...prev };
      delete next[allKey];
      return next;
    });
    if (!ok) {
      setError(t("mcp.errToggleTool"));
      return;
    }
    setDetails((prev) => {
      const entry = prev[server];
      if (!entry) return prev;
      const nextDisabled = targetEnabled ? [] : [...toolNames].sort();
      return {
        ...prev,
        [server]: { ...entry, disabledTools: nextDisabled },
      };
    });
    setServers((prev) =>
      prev.map((s) =>
        s.name === server
          ? { ...s, disabledTools: targetEnabled ? [] : [...toolNames].sort() }
          : s,
      ),
    );
  };

  const onToggleTool = async (server: string, tool: string, currentEnabled: boolean) => {
    const key = `${server}::${tool}`;
    if (busyTool[key]) return;
    setBusyTool((prev) => ({ ...prev, [key]: true }));
    const ok = await setMcpToolEnabled(server, tool, !currentEnabled);
    setBusyTool((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    if (!ok) {
      setError(t("mcp.errToggleTool"));
      return;
    }
    // Update local snapshot in-place so the highlight flips without a full
    // round-trip via overview/details refresh.
    setDetails((prev) => {
      const entry = prev[server];
      if (!entry) return prev;
      const newDisabled = new Set(entry.disabledTools);
      if (currentEnabled) {
        newDisabled.add(tool);
      } else {
        newDisabled.delete(tool);
      }
      return {
        ...prev,
        [server]: { ...entry, disabledTools: Array.from(newDisabled).sort() },
      };
    });
    setServers((prev) =>
      prev.map((s) =>
        s.name === server
          ? {
              ...s,
              disabledTools: (() => {
                const setNames = new Set(s.disabledTools);
                if (currentEnabled) {
                  setNames.add(tool);
                } else {
                  setNames.delete(tool);
                }
                return Array.from(setNames).sort();
              })(),
            }
          : s,
      ),
    );
  };

  if (loading) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.mcp")}</h2>
        <p className="muted">{t("models.loading")}</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.mcp")}</h2>
      {servers.length === 0 && (
        <p className="muted">{t("mcp.emptyHint")}</p>
      )}
      {servers.map((server) => {
        const isCollapsed = collapsed[server.name] !== false;
        const detail = details[server.name];
        const isLoadingDetail = !!loadingDetails[server.name];
        const isLoadingServer = isServerLoading(server);
        const isCatalogLoading =
          isLoadingDetail || !!detail?.loading || isLoadingServer;
        const disabledNames = new Set(server.disabledTools);
        return (
          <div className="mcp-server" key={server.name}>
            <div className="mcp-server-head">
              {server.enabled ? (
                <button
                  className="mcp-collapse"
                  onClick={() => toggleCollapsed(server.name)}
                  aria-label={isCollapsed ? t("models.toggleProvider") : t("models.toggleProvider")}
                >
                  <Icon name="chevron" size={13} className={isCollapsed ? "chevron right" : "chevron down"} />
                </button>
              ) : (
                // Disabled servers have nothing to expand, so the chevron
                // would just be a dead control; leave its slot blank to
                // keep the row's column layout intact.
                <span className="mcp-collapse-placeholder" />
              )}
              <McpServerIcon
                name={server.name}
                icon={mcpIconUrl(server.name, server.icon || "")}
                state={server.state}
                enabled={server.enabled}
              />
              <span className="mcp-server-name">{server.name}</span>
              {server.transport && (
                <span className="mcp-badge">{server.transport.toUpperCase()}</span>
              )}
              <div className="mcp-server-actions">
                {server.enabled && (
                  <span className="mcp-counts">
                    {t("mcp.toolsCount").replace("{n}", String(server.toolsCount))}
                    {" · "}
                    {t("mcp.promptsCount").replace("{n}", String(server.promptsCount))}
                  </span>
                )}
                <button
                  type="button"
                  role="switch"
                  aria-checked={server.enabled}
                  className={`mcp-toggle ${server.enabled ? "is-on" : ""}`}
                  disabled={!!busyServer[server.name] || isLoadingServer}
                  title={isLoadingServer ? t("mcp.stateLoading") : t("mcp.enabled")}
                  aria-busy={isLoadingServer}
                  onClick={() => void onToggleServer(server)}
                >
                  <span className="mcp-toggle-thumb" />
                </button>
                <button
                  type="button"
                  className="icon-btn ghost"
                  title={t("mcp.editServer")}
                  aria-label={t("mcp.editServer")}
                  onClick={() => void openEditor(server.name)}
                >
                  <Icon name="edit" size={14} />
                </button>
                <button
                  type="button"
                  className="icon-btn ghost"
                  title={t("mcp.deleteServer")}
                  aria-label={t("mcp.deleteServer")}
                  onClick={() => setConfirmDelete(server.name)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            </div>
            {server.enabled && !isCollapsed && (
              <div className="mcp-server-body">
                {isCatalogLoading ? (
                  <p className="muted">{t("models.loading")}</p>
                ) : detail ? (
                  <>
                    <div className="mcp-section-header">
                      <span className="mcp-section-label">{t("mcp.toolsLabel")}</span>
                      {detail.tools.length > 0 && (() => {
                        const allEnabled = detail.tools.every(
                          (tool) => !disabledNames.has(tool.name),
                        );
                        const allDisabled = detail.tools.every(
                          (tool) => disabledNames.has(tool.name),
                        );
                        const bulkKey = `${server.name}::__all__`;
                        return (
                          <span className="mcp-bulk-actions">
                            <button
                              type="button"
                              className="mcp-bulk-toggle"
                              disabled={allEnabled || !!busyTool[bulkKey] || !server.enabled}
                              onClick={() =>
                                void onToggleAllTools(server.name, true)
                              }
                              title={t("mcp.enableAllTools")}
                            >
                              {t("mcp.enableAllTools")}
                            </button>
                            <button
                              type="button"
                              className="mcp-bulk-toggle"
                              disabled={allDisabled || !!busyTool[bulkKey] || !server.enabled}
                              onClick={() =>
                                void onToggleAllTools(server.name, false)
                              }
                              title={t("mcp.disableAllTools")}
                            >
                              {t("mcp.disableAllTools")}
                            </button>
                          </span>
                        );
                      })()}
                    </div>
                    {detail.tools.length === 0 && !isCatalogLoading ? (
                      <p className="muted">{t("mcp.noTools")}</p>
                    ) : detail.tools.length > 0 ? (
                      <div className="mcp-chip-grid">
                        {detail.tools.map((tool) => {
                          const enabled = !disabledNames.has(tool.name);
                          const key = `${server.name}::${tool.name}`;
                          return (
                            <button
                              key={tool.name}
                              className={`mcp-tool-chip ${enabled ? "is-enabled" : ""}`}
                              title={tool.description || tool.name}
                              disabled={!server.enabled || !!busyTool[key]}
                              onClick={() => void onToggleTool(server.name, tool.name, enabled)}
                            >
                              {tool.name}
                            </button>
                          );
                        })}
                      </div>
                    ) : null}
                    <div className="mcp-section-label">{t("mcp.promptsLabel")}</div>
                    {detail.prompts.length === 0 && !isCatalogLoading ? (
                      <p className="muted">{t("mcp.noPrompts")}</p>
                    ) : detail.prompts.length > 0 ? (
                      <div className="mcp-chip-grid">
                        {detail.prompts.map((prompt) => (
                          <span
                            key={prompt.name}
                            className="mcp-prompt-chip"
                            title={prompt.description || prompt.name}
                          >
                            {prompt.name}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </>
                ) : null}
                {!isCatalogLoading && server.lastError && (
                  <p className="setting-error">{server.lastError}</p>
                )}
              </div>
            )}
          </div>
        );
      })}
      {error && <p className="setting-error">{error}</p>}
      <div className="settings-action-row">
        <button className="btn btn-primary" onClick={openAddDialog}>
          <Icon name="plus" size={13} />
          <span>{t("mcp.addServer")}</span>
        </button>
      </div>
      {editor?.open && (
        <McpServerEditor
          initialName={editor.originalName}
          initial={editor.initial}
          onCancel={() => setEditor(null)}
          onSubmit={submitEditor}
        />
      )}
      {confirmDelete && (
        <McpDeleteConfirm
          name={confirmDelete}
          onCancel={() => setConfirmDelete("")}
          onConfirm={() => void performDelete(confirmDelete)}
        />
      )}
    </div>
  );
}

/** Small modal that asks the user to confirm deleting an MCP server entry.
 *  Kept inline so the MCP page stays a single file; the styling reuses the
 *  generic ``.modal`` class shared with other GUI confirmations. */
function McpDeleteConfirm({
  name,
  onCancel,
  onConfirm,
}: {
  name: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useApp();
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">{t("mcp.deleteServer")}</h3>
        <p className="modal-body">
          {t("mcp.deleteConfirm").replace("{name}", name)}
        </p>
        <div className="modal-actions">
          <button className="btn" onClick={onCancel}>
            {t("common.cancel")}
          </button>
          <button className="btn btn-danger" onClick={onConfirm}>
            {t("common.delete")}
          </button>
        </div>
      </div>
    </div>
  );
}

type Transport = "stdio" | "http";

function detectTransport(entry: McpServerConfigEntry): Transport {
  if (entry.url) return "http";
  return "stdio";
}

function dictToLines(d: Record<string, string> | undefined): string {
  if (!d) return "";
  return Object.entries(d)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function linesToDict(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const k = line.slice(0, eq).trim();
    const v = line.slice(eq + 1);
    if (k) out[k] = v;
  }
  return out;
}

/** Edit / Add modal for an MCP server entry. The form is intentionally
 *  minimal: enough to add a new stdio or HTTP server from scratch without
 *  reaching for a text editor, but it avoids surfacing the long tail of
 *  experimental ``mcp.jsonc`` fields. Anything the form doesn't render is
 *  preserved on save by the backend's merge logic. */
function McpServerEditor({
  initialName,
  initial,
  onCancel,
  onSubmit,
}: {
  initialName: string;
  initial: McpServerConfigEntry;
  onCancel: () => void;
  onSubmit: (
    name: string,
    entry: McpServerConfigEntry,
  ) => Promise<string>;
}) {
  const { t } = useApp();
  const [name, setName] = useState(initialName);
  const [transport, setTransport] = useState<Transport>(detectTransport(initial));
  const [command, setCommand] = useState(String(initial.command ?? ""));
  const [argsText, setArgsText] = useState(
    Array.isArray(initial.args) ? initial.args.join("\n") : "",
  );
  const [envText, setEnvText] = useState(dictToLines(initial.env));
  const [url, setUrl] = useState(String(initial.url ?? ""));
  const [headersText, setHeadersText] = useState(dictToLines(initial.headers));
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async () => {
    setError("");
    const cleanName = name.trim();
    if (!cleanName) {
      setError(t("mcp.errInvalidName"));
      return;
    }
    const entry: McpServerConfigEntry = {};
    if (transport === "stdio") {
      const cmd = command.trim();
      if (!cmd) {
        setError(t("mcp.errCommandRequired"));
        return;
      }
      entry.command = cmd;
      const args = argsText
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      if (args.length > 0) entry.args = args;
      const env = linesToDict(envText);
      if (Object.keys(env).length > 0) entry.env = env;
    } else {
      const u = url.trim();
      if (!u || !/^https?:\/\//i.test(u)) {
        setError(t("mcp.errUrlRequired"));
        return;
      }
      entry.url = u;
      const headers = linesToDict(headersText);
      if (Object.keys(headers).length > 0) entry.headers = headers;
    }
    setSubmitting(true);
    const err = await onSubmit(cleanName, entry);
    setSubmitting(false);
    if (err) {
      setError(`${t("mcp.errSavePrefix")}${err}`);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal mcp-editor" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">
          {initialName ? t("mcp.editServer") : t("mcp.addServer")}
        </h3>
        <div className="modal-body">
          <div className="setting-row">
            <label>{t("mcp.fieldName")}</label>
            <input
              className="input"
              value={name}
              spellCheck={false}
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="setting-row">
            <label>{t("mcp.fieldTransport")}</label>
            <div className="setting-input-row">
              <label className="radio">
                <input
                  type="radio"
                  name="mcp-transport"
                  checked={transport === "stdio"}
                  onChange={() => setTransport("stdio")}
                />
                <span>{t("mcp.transportStdio")}</span>
              </label>
              <label className="radio">
                <input
                  type="radio"
                  name="mcp-transport"
                  checked={transport === "http"}
                  onChange={() => setTransport("http")}
                />
                <span>{t("mcp.transportHttp")}</span>
              </label>
            </div>
          </div>
          {transport === "stdio" ? (
            <>
              <div className="setting-row">
                <label>{t("mcp.fieldCommand")}</label>
                <input
                  className="input"
                  value={command}
                  spellCheck={false}
                  placeholder="npx"
                  onChange={(e) => setCommand(e.target.value)}
                />
              </div>
              <div className="setting-row">
                <label>{t("mcp.fieldArgs")}</label>
                <textarea
                  className="input"
                  rows={4}
                  value={argsText}
                  spellCheck={false}
                  placeholder={t("mcp.argsPlaceholder")}
                  onChange={(e) => setArgsText(e.target.value)}
                />
              </div>
              <div className="setting-row">
                <label>{t("mcp.fieldEnv")}</label>
                <textarea
                  className="input"
                  rows={3}
                  value={envText}
                  spellCheck={false}
                  placeholder="KEY=value"
                  onChange={(e) => setEnvText(e.target.value)}
                />
              </div>
            </>
          ) : (
            <>
              <div className="setting-row">
                <label>{t("mcp.fieldUrl")}</label>
                <input
                  className="input"
                  value={url}
                  spellCheck={false}
                  placeholder="https://example.com/mcp"
                  onChange={(e) => setUrl(e.target.value)}
                />
              </div>
              <div className="setting-row">
                <label>{t("mcp.fieldHeaders")}</label>
                <textarea
                  className="input"
                  rows={3}
                  value={headersText}
                  spellCheck={false}
                  placeholder="Authorization=Bearer ..."
                  onChange={(e) => setHeadersText(e.target.value)}
                />
              </div>
            </>
          )}
          {error && <p className="setting-error">{error}</p>}
        </div>
        <div className="modal-actions">
          <button className="btn" onClick={onCancel} disabled={submitting}>
            {t("common.cancel")}
          </button>
          <button
            className="btn btn-primary"
            onClick={() => void handleSubmit()}
            disabled={submitting}
          >
            {submitting ? t("models.saving") : t("common.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Avatar-style icon for an MCP server, with a status dot overlay.
 *  Renders the server's icon image when available, falling back to
 *  the first letter of the server name. */
function McpServerIcon({
  name,
  icon,
  state,
  enabled,
}: {
  name: string;
  icon?: string;
  state: string;
  enabled: boolean;
}) {
  const [imgFailed, setImgFailed] = useState(false);
  useEffect(() => {
    setImgFailed(false);
  }, [icon]);
  const initial = (name || "?").charAt(0).toUpperCase();
  const dotClass = !enabled ? "" : (() => {
    const s = (state || "").toLowerCase();
    if (s === "success") return "dot-ok";
    if (s === "loading" || s === "pending") return "dot-loading";
    return "dot-error";
  })();
  const useImg = icon && !imgFailed;
  return (
    <span className="mcp-server-icon">
      {useImg ? (
        <img className="mcp-server-icon-img" src={icon} alt="" onError={() => setImgFailed(true)} />
      ) : (
        <span className="mcp-server-icon-char">{initial}</span>
      )}
      {enabled && dotClass && (
        <span className={`mcp-server-icon-dot ${dotClass}`} />
      )}
    </span>
  );
}

function isServerLoading(server: McpServerSummary): boolean {
  const s = (server.state || "").toLowerCase();
  return s === "loading" || s === "pending";
}
