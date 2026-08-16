import { useApp } from "../state/AppContext";
import { useIndexStatus } from "../utils/useIndexStatus";

export function StatusBar() {
  const { client } = useApp();
  const status = useIndexStatus(client);

  const rgStatus = status?.rg_status ?? "";
  const rgMessage = status?.rg_message ?? "";
  const isRgActive = rgStatus === "downloading" || rgStatus === "extracting" || rgStatus === "success";
  const isRgFailed = rgStatus === "failed";

  if (status?.hidden && !isRgActive && !isRgFailed) return null;

  // Aggregated across every workspace: the status bar shows the total indexed
  // file count, and while ANY workspace is (re)indexing it shows that phase
  // with its progress instead of a stale idle count.
  const workspaces = status?.workspaces ?? [];
  const totalFiles = workspaces.reduce((sum, ws) => sum + (ws.files_total ?? 0), 0);
  const activeWs = workspaces.find((ws) =>
    ["scanning", "indexing", "saving"].includes(ws.refresh_phase),
  );
  const phase = activeWs?.refresh_phase ?? status?.refresh_phase ?? "";
  const isScanning = phase === "scanning";
  const isIndexing = phase === "indexing";
  const isSaving = phase === "saving";
  const percent = Math.max(
    0,
    Math.floor(activeWs?.refresh_progress_percent ?? status?.refresh_progress_percent ?? 0),
  );
  const shownFiles = totalFiles || (status?.files_total ?? 0);

  // The message area only shows transient setup progress; a failed rg
  // download/update is deliberately NOT surfaced as an error message here
  // (details are logged server-side).
  const centerContent = isRgActive
    ? rgMessage
    : "";

  return (
    <footer className="status-bar">
      <div className="status-bar-left">
        {isRgActive ? (
          <span className="status-label">Setup</span>
        ) : isSaving ? (
          <span className="status-label">Saving {percent}%</span>
        ) : isScanning ? (
          <span className="status-label">Scanning {percent}%</span>
        ) : isIndexing ? (
          <span className="status-label">Indexing {percent}%</span>
        ) : status && !status.hidden ? (
          <span className="status-label">
            Index: {shownFiles.toLocaleString()} file{shownFiles !== 1 ? "s" : ""}
          </span>
        ) : null}
      </div>
      <div className="status-bar-center">
        {centerContent}
      </div>
      <div className="status-bar-right" />
    </footer>
  );
}
