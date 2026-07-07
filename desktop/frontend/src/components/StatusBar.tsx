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
    intervalRef.current = setInterval(poll, 500);

    return () => {
      cancelled = true;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [client]);

  const isDefault = state?.workspace?.id === "default";
  if (status?.hidden) return null;

  const phase = status?.refresh_phase ?? "";
  const isScanning = phase === "scanning";
  const isIndexing = phase === "indexing";
  const isSaving = phase === "saving";
  const percent = Math.max(0, Math.floor(status?.refresh_progress_percent ?? 0));

  return (
    <footer className="status-bar">
      <div className="status-bar-left">
        {isDefault ? null : isSaving ? (
          <span className="status-label">Saving {percent}%</span>
        ) : isScanning ? (
          <span className="status-label">Scanning {percent}%</span>
        ) : isIndexing ? (
          <span className="status-label">Indexing {percent}%</span>
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
