import { useApp } from "../state/AppContext";
import type { CacheStats, ContextUsagePart } from "../api/types";

type ContextUsage = {
  percent: number;
  tokens: number;
  window: number;
  parts?: ContextUsagePart[];
};

const CONTEXT_PART_LABELS: Record<string, string> = {
  system: "dashboard.part.system",
  skills: "dashboard.part.skills",
  agents_md: "dashboard.part.agentsMd",
  user_preferences: "dashboard.part.userPreferences",
  tools: "dashboard.part.tools",
  subagents: "dashboard.part.subagents",
  mcp: "dashboard.part.mcp",
  history: "dashboard.part.history",
};

// Fixed display order for the context-usage breakdown; history is always last.
const CONTEXT_PART_ORDER: string[] = [
  "system",
  "tools",
  "skills",
  "subagents",
  "mcp",
  "user_preferences",
  "agents_md",
  "history",
];

function contextPartLabel(key: string, t: (key: string) => string): string {
  const i18nKey = CONTEXT_PART_LABELS[key];
  return i18nKey ? t(i18nKey) : key;
}

function ContextUsageView({ usage, t }: { usage: ContextUsage | null | undefined; t: (key: string) => string }) {
  const windowSize = usage?.window && usage.window > 0 ? usage.window : 0;
  const usedTokens = usage?.tokens && usage.tokens > 0 ? usage.tokens : 0;
  const percent = windowSize > 0 ? Math.min(100, Math.max(0, (usedTokens * 100) / windowSize)) : 0;
  const orderIndex = (key: string) => {
    const i = CONTEXT_PART_ORDER.indexOf(key);
    return i === -1 ? CONTEXT_PART_ORDER.length : i;
  };
  const parts = (usage?.parts ?? [])
    .filter((p) => p && p.tokens > 0)
    .sort((a, b) => orderIndex(a.key) - orderIndex(b.key));

  return (
    <div className="cache-stats context-usage-section">
      <div className="cache-stats-title">
        {t("dashboard.context")}
      </div>
      <div className="cache-stats-row">
        <div className="cache-stat">
          <span className="cache-stat-value">{windowSize > 0 ? windowSize.toLocaleString() : "—"}</span>
          <span className="cache-stat-label">{t("dashboard.contextWindow")}</span>
        </div>
        <div className="cache-stat">
          <span className="cache-stat-value">{usedTokens > 0 ? usedTokens.toLocaleString() : "—"}</span>
          <span className="cache-stat-label">{t("dashboard.contextUsed")}</span>
        </div>
        <div className="cache-stat">
          <span className={`cache-stat-value ${percent > 80 ? "cache-miss" : percent > 0 ? "cache-hit" : ""}`}>
            {percent.toFixed(1)}%
          </span>
          <span className="cache-stat-label">{t("dashboard.contextPercent")}</span>
        </div>
      </div>
      {parts.length > 0 ? (
        <div className="context-usage-parts">
          {parts.map((p) => (
            <div className="context-usage-part" key={p.key}>
              <span className="context-usage-part-label" title={contextPartLabel(p.key, t)}>
                {contextPartLabel(p.key, t)}
              </span>
              <span className="context-usage-part-value">{p.tokens.toLocaleString()}</span>
              <span className="context-usage-part-percent">
                {windowSize > 0 ? `${((p.tokens * 100) / windowSize).toFixed(1)}%` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="context-usage-empty">{t("dashboard.contextEmpty")}</div>
      )}
    </div>
  );
}

function CacheStatsView({ stats, t }: { stats: CacheStats | null | undefined; t: (key: string) => string }) {
  if (!stats || !stats.supported) {
    return null;
  }
  const pct = stats.totalTokens > 0 ? stats.hitRate : 0;
  return (
    <div className="cache-stats">
      <div className="cache-stats-title">
        {t("dashboard.input")}
      </div>
      <div className="cache-stats-row">
        <div className="cache-stat">
          <span className="cache-stat-value">{stats.totalTokens.toLocaleString()}</span>
          <span className="cache-stat-label">{t("dashboard.inputTokens")}</span>
        </div>
        {stats.hasBreakdown ? (
          <>
            <div className="cache-stat">
              <span className="cache-stat-value">{stats.hitTokens.toLocaleString()}</span>
              <span className="cache-stat-label">{t("dashboard.cacheHits")}</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">{stats.missTokens.toLocaleString()}</span>
              <span className="cache-stat-label">{t("dashboard.cacheMisses")}</span>
            </div>
            <div className="cache-stat">
              <span className={`cache-stat-value ${pct > 0 ? "cache-hit" : "cache-miss"}`}>
                {pct}%
              </span>
              <span className="cache-stat-label">{t("dashboard.hitRate")}</span>
            </div>
          </>
        ) : (
          <>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">{t("dashboard.cacheHits")}</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">{t("dashboard.cacheMisses")}</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">{t("dashboard.hitRate")}</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function TokenStatsView({ stats, t }: { stats: import("../api/types").TokenStats | null | undefined; t: (key: string) => string }) {
  if (!stats || !stats.hasOutputTokens) {
    return null;
  }
  const effectiveOutput = stats.includesReasoning
    ? stats.outputTokens - stats.reasoningTokens
    : stats.outputTokens;
  return (
    <div className="cache-stats token-stats-section">
      <div className="cache-stats-title">
        {t("dashboard.output")}
      </div>
      <div className="cache-stats-row">
        <div className="cache-stat">
          <span className="cache-stat-value">{(effectiveOutput > 0 ? effectiveOutput : stats.outputTokens).toLocaleString()}</span>
          <span className="cache-stat-label">{t("dashboard.outputTokens")}</span>
        </div>
        {stats.hasReasoningTokens && (
          <div className="cache-stat">
            <span className="cache-stat-value">{stats.reasoningTokens.toLocaleString()}</span>
            <span className="cache-stat-label">{t("dashboard.reasoningTokens")}</span>
          </div>
        )}
      </div>
    </div>
  );
}

export function DashboardContent() {
  const { state, t } = useApp();
  return (
    <div className="dashboard-stats">
      <ContextUsageView usage={state?.contextUsage} t={t} />
      <CacheStatsView stats={state?.cacheStats} t={t} />
      <TokenStatsView stats={state?.tokenStats} t={t} />
    </div>
  );
}
