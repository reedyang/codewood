import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import type { McpServerDetails, McpServerSummary } from "../api/types";

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
    t,
  } = useApp();
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [details, setDetails] = useState<Record<string, McpServerDetails>>({});
  const [loadingDetails, setLoadingDetails] = useState<Record<string, boolean>>({});
  const [busyServer, setBusyServer] = useState<Record<string, boolean>>({});
  const [busyTool, setBusyTool] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const aliveRef = useRef(true);

  const refresh = async () => {
    setLoading(true);
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
    setLoading(false);
  };

  useEffect(() => {
    aliveRef.current = true;
    void refresh();
    return () => {
      aliveRef.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    await refresh();
    // If the server was just enabled and we have it expanded, also refresh
    // its tool/prompt catalog since reconnect may have repopulated the cache.
    if (!server.enabled && collapsed[server.name] === false) {
      await loadDetails(server.name);
    }
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
        const disabledNames = new Set(server.disabledTools);
        // Only surface a textual status while the server is enabled — when
        // disabled, the toggle below already conveys that state, so an
        // extra "Disabled" pill would be redundant noise.
        const showStatus = server.enabled;
        const stateLabel = showStatus ? serverStateLabel(server, t) : "";
        return (
          <div className="mcp-server" key={server.name}>
            <div className="mcp-server-head">
              <button
                className="mcp-collapse"
                onClick={() => toggleCollapsed(server.name)}
                aria-label={isCollapsed ? t("models.toggleProvider") : t("models.toggleProvider")}
              >
                <Icon name="chevron" size={13} className={isCollapsed ? "chevron right" : "chevron down"} />
              </button>
              <span className="mcp-server-name">{server.name}</span>
              {server.transport && (
                <span className="mcp-badge">{server.transport.toUpperCase()}</span>
              )}
              {showStatus && (
                <span className={`mcp-status mcp-status-${serverStateClass(server)}`}>
                  {stateLabel}
                </span>
              )}
              <span className="mcp-counts">
                {t("mcp.toolsCount").replace("{n}", String(server.toolsCount))}
                {" · "}
                {t("mcp.promptsCount").replace("{n}", String(server.promptsCount))}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={server.enabled}
                className={`mcp-toggle ${server.enabled ? "is-on" : ""}`}
                disabled={!!busyServer[server.name]}
                title={t("mcp.enabled")}
                onClick={() => void onToggleServer(server)}
              >
                <span className="mcp-toggle-thumb" />
              </button>
            </div>
            {!isCollapsed && (
              <div className="mcp-server-body">
                {isLoadingDetail && <p className="muted">{t("models.loading")}</p>}
                {detail && (
                  <>
                    <div className="mcp-section-label">{t("mcp.toolsLabel")}</div>
                    {detail.tools.length === 0 ? (
                      <p className="muted">{t("mcp.noTools")}</p>
                    ) : (
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
                    )}
                    <div className="mcp-section-label">{t("mcp.promptsLabel")}</div>
                    {detail.prompts.length === 0 ? (
                      <p className="muted">{t("mcp.noPrompts")}</p>
                    ) : (
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
                    )}
                  </>
                )}
                {server.lastError && (
                  <p className="setting-error">{server.lastError}</p>
                )}
              </div>
            )}
          </div>
        );
      })}
      {error && <p className="setting-error">{error}</p>}
    </div>
  );
}

function serverStateClass(server: McpServerSummary): string {
  if (!server.enabled) return "off";
  const s = (server.state || "").toLowerCase();
  if (s === "success") return "ok";
  if (s === "loading" || s === "pending") return "loading";
  if (s === "failed" || s === "error") return "error";
  return "idle";
}

function serverStateLabel(
  server: McpServerSummary,
  t: (key: string) => string,
): string {
  if (!server.enabled) return t("mcp.stateDisabled");
  const s = (server.state || "").toLowerCase();
  if (s === "success") return t("mcp.stateConnected");
  if (s === "loading" || s === "pending") return t("mcp.stateLoading");
  if (s === "failed" || s === "error") return t("mcp.stateFailed");
  return t("mcp.stateIdle");
}
