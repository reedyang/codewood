import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { groupModelsByProvider } from "./ChatView";
import type { SubAgentConfig } from "../api/types";

/** Sub Agents settings page.
 *
 *  Mirrors the MCP settings page UX:
 *   - One collapsed row per configured sub-agent (only the toggle, edit and
 *     delete controls show when collapsed).
 *   - A per-agent enable toggle (writes the ``enabled`` frontmatter flag).
 *   - Expanding a row reveals the read-only property summary.
 *   - Add / Edit open a modal where every property is editable: ``model`` and
 *     ``tools`` use dropdowns sourced from the backend option catalog.
 *
 *  All writes target the GLOBAL ``<config_dir>/subagents/<name>.md`` files. */
export function SubAgentsSettings() {
  const {
    getSubAgentsOverview,
    saveSubAgent,
    deleteSubAgent,
    setSubAgentEnabled,
    t,
  } = useApp();
  const [agents, setAgents] = useState<SubAgentConfig[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [editor, setEditor] = useState<{
    open: boolean;
    originalName: string;
    initial: SubAgentConfig;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string>("");
  const [error, setError] = useState("");
  const aliveRef = useRef(true);

  const refresh = async () => {
    setLoading(true);
    const data = await getSubAgentsOverview();
    if (!aliveRef.current) return;
    setAgents(data.subagents);
    setModels(data.models);
    setTools(data.tools);
    setCollapsed((prev) => {
      const next = { ...prev };
      for (const a of data.subagents) {
        if (!(a.name in next)) next[a.name] = true;
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
    setCollapsed((prev) => ({ ...prev, [name]: prev[name] === false }));
  };

  const onToggleEnabled = async (agent: SubAgentConfig) => {
    if (busy[agent.name]) return;
    setError("");
    setBusy((prev) => ({ ...prev, [agent.name]: true }));
    const result = await setSubAgentEnabled(agent.name, !agent.enabled);
    setBusy((prev) => {
      const next = { ...prev };
      delete next[agent.name];
      return next;
    });
    if (!result.ok) {
      setError(t("subagents.errToggle"));
      return;
    }
    await refresh();
  };

  const blankAgent = (): SubAgentConfig => ({
    name: "",
    description: "",
    instructions: "",
    model: "",
    tools: [],
    toolsSpecified: false,
    maxRounds: 20,
    enabled: true,
    sourcePath: "",
    global: true,
  });

  const openAdd = () => {
    setEditor({ open: true, originalName: "", initial: blankAgent() });
  };

  const openEdit = (agent: SubAgentConfig) => {
    setEditor({ open: true, originalName: agent.name, initial: agent });
  };

  const submitEditor = async (
    next: SubAgentConfig,
  ): Promise<string> => {
    const result = await saveSubAgent({
      originalName: editor?.originalName || "",
      name: next.name,
      description: next.description,
      instructions: next.instructions,
      model: next.model,
      tools: next.tools,
      toolsSpecified: next.toolsSpecified,
      maxRounds: next.maxRounds,
      enabled: next.enabled,
    });
    if (!result.ok) {
      return result.error || "save_failed";
    }
    setEditor(null);
    await refresh();
    return "";
  };

  const performDelete = async (name: string) => {
    const result = await deleteSubAgent(name);
    setConfirmDelete("");
    if (!result.ok) {
      setError(t("subagents.errDelete"));
      return;
    }
    await refresh();
  };

  if (loading) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.subagents")}</h2>
        <p className="muted">{t("models.loading")}</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.subagents")}</h2>
      {agents.length === 0 && <p className="muted">{t("subagents.emptyHint")}</p>}
      {agents.map((agent) => {
        const isCollapsed = collapsed[agent.name] !== false;
        return (
          <div className="mcp-server" key={agent.name}>
            <div className="mcp-server-head">
              <button
                className="mcp-collapse"
                onClick={() => toggleCollapsed(agent.name)}
                aria-label={t("models.toggleProvider")}
              >
                <Icon
                  name="chevron"
                  size={13}
                  className={isCollapsed ? "chevron right" : "chevron down"}
                />
              </button>
              <span className="mcp-server-name">{agent.name}</span>
              {agent.model && <span className="mcp-badge">{agent.model}</span>}
              <div className="mcp-server-actions">
                <button
                  type="button"
                  role="switch"
                  aria-checked={agent.enabled}
                  className={`mcp-toggle ${agent.enabled ? "is-on" : ""}`}
                  disabled={!!busy[agent.name]}
                  title={t("subagents.enabled")}
                  onClick={() => void onToggleEnabled(agent)}
                >
                  <span className="mcp-toggle-thumb" />
                </button>
                <button
                  type="button"
                  className="icon-btn ghost"
                  title={t("subagents.edit")}
                  aria-label={t("subagents.edit")}
                  onClick={() => openEdit(agent)}
                >
                  <Icon name="edit" size={14} />
                </button>
                <button
                  type="button"
                  className="icon-btn ghost"
                  title={t("subagents.delete")}
                  aria-label={t("subagents.delete")}
                  onClick={() => setConfirmDelete(agent.name)}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
            </div>
            {!isCollapsed && (
              <div className="mcp-server-body">
                <div className="subagent-prop">
                  <span className="subagent-prop-label">
                    {t("subagents.fieldDescription")}
                  </span>
                  <span className="subagent-prop-value">{agent.description}</span>
                </div>
                <div className="subagent-prop">
                  <span className="subagent-prop-label">
                    {t("subagents.fieldModel")}
                  </span>
                  <span className="subagent-prop-value">
                    {agent.model || t("subagents.modelDefault")}
                  </span>
                </div>
                <div className="subagent-prop">
                  <span className="subagent-prop-label">
                    {t("subagents.fieldTools")}
                  </span>
                  <span className="subagent-prop-value">
                    {agent.toolsSpecified
                      ? agent.tools.length > 0
                        ? agent.tools.join(", ")
                        : t("subagents.toolsNone")
                      : t("subagents.toolsDefault")}
                  </span>
                </div>
                <div className="subagent-prop">
                  <span className="subagent-prop-label">
                    {t("subagents.fieldMaxRounds")}
                  </span>
                  <span className="subagent-prop-value">{agent.maxRounds}</span>
                </div>
              </div>
            )}
          </div>
        );
      })}
      {error && <p className="setting-error">{error}</p>}
      <div className="settings-action-row">
        <button className="btn btn-primary" onClick={openAdd}>
          <Icon name="plus" size={13} />
          <span>{t("subagents.add")}</span>
        </button>
      </div>
      {editor?.open && (
        <SubAgentEditor
          initial={editor.initial}
          isNew={!editor.originalName}
          models={models}
          tools={tools}
          onCancel={() => setEditor(null)}
          onSubmit={submitEditor}
        />
      )}
      {confirmDelete && (
        <div
          className="modal-backdrop"
          onClick={() => setConfirmDelete("")}
        >
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">{t("subagents.delete")}</h3>
            <p className="modal-body">
              {t("subagents.deleteConfirm").replace("{name}", confirmDelete)}
            </p>
            <div className="modal-actions">
              <button className="btn" onClick={() => setConfirmDelete("")}>
                {t("common.cancel")}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => void performDelete(confirmDelete)}
              >
                {t("common.delete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function SubAgentEditor({
  initial,
  isNew,
  models,
  tools,
  onCancel,
  onSubmit,
}: {
  initial: SubAgentConfig;
  isNew: boolean;
  models: string[];
  tools: string[];
  onCancel: () => void;
  onSubmit: (next: SubAgentConfig) => Promise<string>;
}) {
  const { t } = useApp();
  const [name, setName] = useState(initial.name);
  const [description, setDescription] = useState(initial.description);
  const [instructions, setInstructions] = useState(initial.instructions);
  const [model, setModel] = useState(initial.model);
  const [toolsSpecified, setToolsSpecified] = useState(initial.toolsSpecified);
  const [selectedTools, setSelectedTools] = useState<string[]>(initial.tools);
  const [maxRounds, setMaxRounds] = useState(String(initial.maxRounds));
  const [enabled, setEnabled] = useState(initial.enabled);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const toggleTool = (tool: string) => {
    setSelectedTools((prev) =>
      prev.includes(tool) ? prev.filter((x) => x !== tool) : [...prev, tool],
    );
  };

  const handleSubmit = async () => {
    setError("");
    const cleanName = name.trim();
    if (!cleanName) {
      setError(t("subagents.errName"));
      return;
    }
    if (!description.trim()) {
      setError(t("subagents.errDescription"));
      return;
    }
    if (!instructions.trim()) {
      setError(t("subagents.errInstructions"));
      return;
    }
    const rounds = Number.parseInt(maxRounds, 10);
    setSubmitting(true);
    const err = await onSubmit({
      ...initial,
      name: cleanName,
      description: description.trim(),
      instructions,
      model,
      tools: toolsSpecified ? selectedTools : [],
      toolsSpecified,
      maxRounds: Number.isFinite(rounds) && rounds > 0 ? rounds : 20,
      enabled,
    });
    setSubmitting(false);
    if (err) {
      setError(`${t("subagents.errSavePrefix")}${err}`);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal subagent-editor" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">
          {isNew ? t("subagents.add") : t("subagents.edit")}
        </h3>
        <div className="modal-body">
          <div className="setting-row">
            <label>{t("subagents.fieldName")}</label>
            <input
              className="input"
              value={name}
              spellCheck={false}
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldDescription")}</label>
            <textarea
              className="input"
              rows={2}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldModel")}</label>
            <select
              className="select"
              title={t("subagents.fieldModel")}
              value={model}
              onChange={(e) => setModel(e.target.value)}
            >
              <option value="">{t("subagents.modelDefault")}</option>
              {groupModelsByProvider(models).map((group) => (
                <optgroup key={group.provider} label={group.provider}>
                  {group.items.map((item) => (
                    <option key={item.selector} value={item.selector}>
                      {item.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldTools")}</label>
            <div className="setting-input-col">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={toolsSpecified}
                  onChange={(e) => setToolsSpecified(e.target.checked)}
                />
                <span>{t("subagents.toolsCustomize")}</span>
              </label>
              {toolsSpecified && (
                <div className="subagent-tools-grid">
                  {tools.map((tool) => {
                    const on = selectedTools.includes(tool);
                    return (
                      <button
                        key={tool}
                        type="button"
                        className={`mcp-tool-chip ${on ? "is-enabled" : ""}`}
                        onClick={() => toggleTool(tool)}
                      >
                        {tool}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldMaxRounds")}</label>
            <input
              className="input input-narrow"
              type="number"
              min={1}
              value={maxRounds}
              onChange={(e) => setMaxRounds(e.target.value)}
            />
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldEnabled")}</label>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              title={t("subagents.fieldEnabled")}
              className={`mcp-toggle ${enabled ? "is-on" : ""}`}
              onClick={() => setEnabled((v) => !v)}
            >
              <span className="mcp-toggle-thumb" />
            </button>
          </div>
          <div className="setting-row">
            <label>{t("subagents.fieldInstructions")}</label>
            <textarea
              className="input"
              rows={8}
              value={instructions}
              spellCheck={false}
              placeholder={t("subagents.instructionsPlaceholder")}
              onChange={(e) => setInstructions(e.target.value)}
            />
          </div>
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
