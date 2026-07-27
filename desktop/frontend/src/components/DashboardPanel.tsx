import { useApp } from "../state/AppContext";
import type { CacheStats } from "../api/types";

function CacheStatsView({ stats }: { stats: CacheStats | null | undefined }) {
  if (!stats || !stats.supported) {
    return null;
  }
  const pct = stats.totalTokens > 0 ? stats.hitRate : 0;
  return (
    <div className="cache-stats">
      <div className="cache-stats-title">
        Cache — {stats.model}
      </div>
      <div className="cache-stats-row">
        <div className="cache-stat">
          <span className="cache-stat-value">{stats.totalTokens.toLocaleString()}</span>
          <span className="cache-stat-label">Input tokens</span>
        </div>
        {stats.hasBreakdown ? (
          <>
            <div className="cache-stat">
              <span className="cache-stat-value">{stats.hitTokens.toLocaleString()}</span>
              <span className="cache-stat-label">Cache hits</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">{stats.missTokens.toLocaleString()}</span>
              <span className="cache-stat-label">Cache misses</span>
            </div>
            <div className="cache-stat">
              <span className={`cache-stat-value ${pct > 0 ? "cache-hit" : "cache-miss"}`}>
                {pct}%
              </span>
              <span className="cache-stat-label">Hit rate</span>
            </div>
          </>
        ) : (
          <>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">Cache hits</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">Cache misses</span>
            </div>
            <div className="cache-stat">
              <span className="cache-stat-value">N/A</span>
              <span className="cache-stat-label">Hit rate</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** Dashboard panel: cache stats. */
export function DashboardContent() {
  const { state } = useApp();
  return (
    <CacheStatsView stats={state?.cacheStats} />
  );
}
