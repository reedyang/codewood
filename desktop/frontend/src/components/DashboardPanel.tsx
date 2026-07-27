import { useApp } from "../state/AppContext";
import type { CacheStats } from "../api/types";

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
      <CacheStatsView stats={state?.cacheStats} t={t} />
      <TokenStatsView stats={state?.tokenStats} t={t} />
    </div>
  );
}
