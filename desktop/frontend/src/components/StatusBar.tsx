import { useState, useEffect, useRef } from "react";
import { useApp } from "../state/AppContext";
import type { IndexStatus } from "../api/types";

export function StatusBar() {
  const { state, client } = useApp();
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = () => {
      client.fetchIndexStatus().then((s) => {
        if (!cancelled) setStatus(s);
      }).catch(() => {});
    };

    poll();
    intervalRef.current = setInterval(poll, 2000);

    return () => {
      cancelled = true;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [client]);

  const isDefault = state?.workspace?.id === "default";
  if (status?.hidden) return null;

  const phase = status?.refresh_phase ?? "";
  const total = status?.refresh_progress_total ?? 0;
  const done = status?.refresh_progress_done ?? 0;
  const isScanning = phase === "scanning";
  const isIndexing = phase === "indexing";
  const isSaving = phase === "saving";
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  const barWidth = 80;
  const filledCount = Math.round((pct / 100) * (barWidth / 8));

  return (
    <footer className="status-bar">
      <div className="status-bar-left">
        {isDefault ? null : isSaving ? (
          <span className="status-label">Saving...</span>
        ) : isScanning ? (
          <span className="status-label">
            Found {done > 0 ? done.toLocaleString() : "..."} file{done !== 1 ? "s" : ""}
          </span>
        ) : isIndexing ? (
          <>
            <span className="status-label">Indexing</span>
            {done > 0 && total > 0 && (
              <span className="status-progress-text">
                {done.toLocaleString()} / {total.toLocaleString()} {"\u00b7"} {pct}%
              </span>
            )}
            <span className="status-progress-bar">
              {"\u2588".repeat(filledCount)}{"\u2591".repeat(barWidth / 8 - filledCount)}
            </span>
          </>
        ) : status ? (
          <span className="status-label">
            Index: {(status.files_total ?? 0).toLocaleString()} file{(status.files_total ?? 0) !== 1 ? "s" : ""}
          </span>
        ) : null}
      </div>
      <div className="status-bar-center">
        {isDefault ? "" : (status?.workspace_name || state?.workspace?.name || "")}
      </div>
      <div className="status-bar-right" />
    </footer>
  );
}
