import { useState, useEffect, useRef } from "react";
import { useApp } from "../state/AppContext";
import type { IndexStatus } from "../api/types";

function Dots() {
  const [n, setN] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setN((x) => (x + 1) % 4), 600);
    return () => clearInterval(t);
  }, []);
  return <>{Array(n).fill(".").join("")}</>;
}

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
  const isScanning = phase === "scanning";
  const isIndexing = phase === "indexing";
  const isSaving = phase === "saving";

  return (
    <footer className="status-bar">
      <div className="status-bar-left">
        {isDefault ? null : isSaving ? (
          <span className="status-label">Saving...</span>
        ) : isScanning ? (
          <span className="status-label">
            Found {(status?.files_total ?? 0).toLocaleString()} file{(status?.files_total ?? 0) !== 1 ? "s" : ""}
          </span>
        ) : isIndexing ? (
          <span className="status-label">
            Indexing<Dots />
          </span>
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
