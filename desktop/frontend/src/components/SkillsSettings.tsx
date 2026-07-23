import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import type { SkillSummary } from "../api/types";

const SOURCE_ORDER = ["global", "agents", "builtin"] as const;

const SOURCE_LABEL_KEYS: Record<string, string> = {
  builtin: "skills.sourceBuiltin",
  agents: "skills.sourceAgents",
  global: "skills.sourceGlobal",
  workspace: "skills.sourceWorkspace",
};

export function SkillsSettings() {
  const { getSkillsOverview, setSkillEnabled, t } = useApp();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [busySkill, setBusySkill] = useState<Record<string, boolean>>({});
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const aliveRef = useRef(true);

  const refresh = async (opts?: { silent?: boolean }) => {
    const silent = !!opts?.silent;
    if (!silent) {
      setLoading(true);
    }
    const list = await getSkillsOverview();
    if (!aliveRef.current) return;
    setSkills(list);
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
    const timer = window.setInterval(() => {
      void refresh({ silent: true });
    }, 3000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onToggleSkill = async (skill: SkillSummary) => {
    if (busySkill[skill.skillId]) return;
    setError("");
    setBusySkill((prev) => ({ ...prev, [skill.skillId]: true }));
    const ok = await setSkillEnabled(skill.skillId, !skill.enabled);
    setBusySkill((prev) => {
      const next = { ...prev };
      delete next[skill.skillId];
      return next;
    });
    if (!ok) {
      setError(t("skills.errToggle"));
      return;
    }
    await refresh({ silent: true });
  };

  const toggleGroup = (source: string) => {
    setCollapsedGroups((prev) => ({ ...prev, [source]: !prev[source] }));
  };

  const grouped = useMemo(() => {
    const groups: Record<string, SkillSummary[]> = {};
    for (const s of skills) {
      const src = s.source || "builtin";
      if (!groups[src]) groups[src] = [];
      groups[src].push(s);
    }
    for (const key of Object.keys(groups)) {
      groups[key].sort((a, b) => a.name.localeCompare(b.name));
    }
    return groups;
  }, [skills]);

  const sourceLabel = (source: string): string => {
    return t(SOURCE_LABEL_KEYS[source] || source);
  };

  if (loading) {
    return (
      <div className="settings-page">
        <h2 className="settings-page-title">{t("settings.page.skills")}</h2>
        <p className="muted">{t("models.loading")}</p>
      </div>
    );
  }

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.skills")}</h2>
      {skills.length === 0 && (
        <p className="muted">{t("skills.emptyHint")}</p>
      )}
      {SOURCE_ORDER.map((source) => {
        const items = grouped[source];
        if (!items || items.length === 0) return null;
        const isCollapsed = collapsedGroups[source] === true;
        const enabledCount = items.filter((s) => s.enabled).length;
        return (
          <div className="skills-group" key={source}>
            <button
              className="skills-group-header"
              onClick={() => toggleGroup(source)}
            >
              <span className="skills-group-chevron">
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 12 12"
                  style={{
                    transform: isCollapsed ? "rotate(-90deg)" : "rotate(0deg)",
                    transition: "transform 0.15s",
                  }}
                >
                  <path
                    d="M4 2l4 4-4 4"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    fill="none"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="skills-group-label">{sourceLabel(source)}</span>
              <span className="skills-group-count">
                {enabledCount}/{items.length} {t("skills.enabledCount")}
              </span>
            </button>
            {!isCollapsed && (
              <div className="skills-group-body">
                {items.map((skill) => (
                  <div className="mcp-server" key={skill.skillId}>
                    <div className="mcp-server-head">
                      <span className="mcp-collapse-placeholder" />
                      <span className="mcp-server-name">{skill.name}</span>
                      {skill.description && (
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
                          title={skill.description}
                        >
                          {skill.description}
                        </span>
                      )}
                      <div className="mcp-server-actions">
                        <button
                          type="button"
                          role="switch"
                          aria-checked={skill.enabled}
                          className={`mcp-toggle ${skill.enabled ? "is-on" : ""}`}
                          disabled={!!busySkill[skill.skillId]}
                          title={t("skills.enabled")}
                          onClick={() => void onToggleSkill(skill)}
                        >
                          <span className="mcp-toggle-thumb" />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
      {error && <p className="setting-error">{error}</p>}
    </div>
  );
}
