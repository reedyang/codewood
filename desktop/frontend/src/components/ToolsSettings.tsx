import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import type { ToolSummary } from "../api/types";

export function ToolsSettings() {
  const { getToolsOverview, setToolEnabled, setCompactMode, t } = useApp();
  const [tools, setTools] = useState<ToolSummary[]>([]);
  const [compact, setCompact] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busyTool, setBusyTool] = useState<Record<string, boolean>>({});
  const [busyCompact, setBusyCompact] = useState(false);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);

  const refresh = async (opts?: { silent?: boolean }) => {
    const silent = !!opts?.silent;
    if (!silent) {
      setLoading(true);
    }
    const overview = await getToolsOverview();
    if (!aliveRef.current) return;
    setTools(overview.tools);
    setCompact(overview.compactMode);
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

  const onToggleCompact = async () => {
    if (busyCompact) return;
    setError("");
    setBusyCompact(true);
    const ok = await setCompactMode(!compact);
    setBusyCompact(false);
    if (!ok) {
      setError(t("tools.errCompact"));
      return;
    }
    await refresh({ silent: true });
  };

  const onToggleTool = async (tool: ToolSummary) => {
    if (tool.locked || !compact || busyTool[tool.name]) return;
    setError("");
    setBusyTool((prev) => ({ ...prev, [tool.name]: true }));
    const ok = await setToolEnabled(tool.name, !tool.enabled);
    setBusyTool((prev) => {
      const next = { ...prev };
      delete next[tool.name];
      return next;
    });
    if (!ok) {
      setError(t("tools.errToggle"));
      return;
    }
    await refresh({ silent: true });
  };

  // Locked (always-on) tools come first, then the remaining tools in
  // registry order as returned by the backend.
  const locked = tools.filter((tl) => tl.locked);
  const optional = tools.filter((tl) => !tl.locked);
  const enabledCount = tools.filter((tl) => tl.enabled).length;

  const toggleTitle = (tool: ToolSummary): string => {
    if (tool.locked) return t("tools.lockedTitle");
    if (!compact) return t("tools.lockedByMode");
    return tool.enabled ? t("tools.enabled") : "";
  };

  const renderTool = (tool: ToolSummary) => {
    const interactive = !tool.locked && compact;
    return (
      <div className="mcp-server" key={tool.name}>
        <div className="mcp-server-head">
          <span className="mcp-collapse-placeholder" />
          <span className="mcp-server-name">{tool.name}</span>
          {tool.description && (
            <span
              className="muted"
              style={{
                marginLeft: 8,
                fontSize: 12,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                flex: 1,
              }}
              title={tool.description}
            >
              {tool.description}
            </span>
          )}
          {tool.locked && (
            <span className="tools-locked-badge">{t("tools.locked")}</span>
          )}
          <div className="mcp-server-actions">
            <button
              type="button"
              role="switch"
              aria-checked={tool.enabled}
              className={`mcp-toggle ${tool.enabled ? "is-on" : ""}`}
              disabled={!interactive || !!busyTool[tool.name]}
              title={toggleTitle(tool)}
              onClick={() => void onToggleTool(tool)}
            >
              <span className="mcp-toggle-thumb" />
            </button>
          </div>
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.tools")}</h2>
        <p className="muted">{t("models.loading")}</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.tools")}</h2>
      <div className="mcp-server tools-compact-row">
        <div className="mcp-server-head">
          <span className="mcp-collapse-placeholder" />
          <span className="mcp-server-name">{t("tools.compactMode")}</span>
          <span className="muted tools-compact-hint">
            {t("tools.compactModeHint")}
          </span>
          <div className="mcp-server-actions">
            <button
              type="button"
              role="switch"
              aria-checked={compact}
              className={`mcp-toggle ${compact ? "is-on" : ""}`}
              disabled={busyCompact}
              onClick={() => void onToggleCompact()}
            >
              <span className="mcp-toggle-thumb" />
            </button>
          </div>
        </div>
      </div>
      <p className="setting-hint">{t("tools.hint")}</p>
      {tools.length === 0 && <p className="muted">{t("tools.emptyHint")}</p>}
      {locked.length > 0 && (
        <div className="skills-group">
          <div className="skills-group-header">
            <span className="skills-group-chevron" />
            <span className="skills-group-label">{t("tools.groupCore")}</span>
            <span className="skills-group-count">
              {locked.length}/{locked.length} {t("tools.enabledCount")}
            </span>
          </div>
          <div className="skills-group-body">{locked.map(renderTool)}</div>
        </div>
      )}
      {optional.length > 0 && (
        <div className="skills-group">
          <div className="skills-group-header">
            <span className="skills-group-chevron" />
            <span className="skills-group-label">{t("tools.groupOptional")}</span>
            <span className="skills-group-count">
              {enabledCount - locked.length}/{optional.length}{" "}
              {t("tools.enabledCount")}
            </span>
          </div>
          <div className="skills-group-body">{optional.map(renderTool)}</div>
        </div>
      )}
      {error && <p className="setting-error">{error}</p>}
    </div>
  );
}
