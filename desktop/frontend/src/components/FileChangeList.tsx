import { useState, useMemo, useCallback } from "react";
import { FileChangeSummary, FileChangeRecord } from "../api/types";
import { FileChangeDetails } from "./FileChangeDetails";
import { Collapsible } from "./Collapsible";
import { Icon } from "./Icon";
import { useApp } from "../state/AppContext";

interface FileChangeListProps {
  summary: FileChangeSummary;
  t: (key: string, params?: Record<string, string | number>) => string;
  workspaceRoot?: string;
}

function isUndoable(file: FileChangeRecord): boolean {
  if (file.changeType === "modify" && (file.patch?.length || file.backupPath)) return true;
  if (file.changeType === "create" && file.patch?.length) return true;
  if (file.changeType === "rename" && file.patch?.length) return true;
  if (file.changeType === "delete" && file.backupPath) return true;
  return false;
}

export function FileChangeList({ summary, t, workspaceRoot }: FileChangeListProps) {
  const { client, activeChatId, activeWorkspaceId } = useApp();
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());
  const [undoneFiles, setUndoneFiles] = useState<Set<string>>(
    () => new Set(summary.undoneFiles ?? []),
  );
  const [isProcessing, setIsProcessing] = useState(false);
  const [failDialog, setFailDialog] = useState<{ title: string; files: Array<{ path: string; error: string }> } | null>(null);

  const undoableFiles = useMemo(
    () => summary.files.filter(isUndoable),
    [summary.files],
  );
  const allUndone = undoableFiles.length > 0 && undoneFiles.size >= undoableFiles.length;
  const showButton = undoableFiles.length > 0 && !!summary.ref;

  const toggleFile = (filePath: string) => {
    setExpandedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(filePath)) {
        next.delete(filePath);
      } else {
        next.add(filePath);
      }
      return next;
    });
  };

  const handleUndo = useCallback(async () => {
    const filesToUndo = undoableFiles
      .filter((f) => !undoneFiles.has(f.filePath))
      .map((f) => f.filePath);
    if (filesToUndo.length === 0) return;
    setIsProcessing(true);
    try {
      const result = await client.undoFileChanges(activeChatId, summary.ref!, filesToUndo, activeWorkspaceId);
      const newUndone = new Set(undoneFiles);
      const failures: Array<{ path: string; error: string }> = [];
      for (const [path, res] of Object.entries(result.results)) {
        if (res.success) {
          newUndone.add(path);
        } else {
          failures.push({ path, error: res.error || "unknown error" });
        }
      }
      setUndoneFiles(newUndone);
      if (failures.length > 0) {
        setFailDialog({ title: t("fileChange.undoFail"), files: failures });
      }
    } finally {
      setIsProcessing(false);
    }
  }, [undoableFiles, undoneFiles, client, activeChatId, activeWorkspaceId, summary.ref, t]);

  const handleReapply = useCallback(async () => {
    const filesToReapply = undoableFiles
      .filter((f) => undoneFiles.has(f.filePath))
      .map((f) => f.filePath);
    if (filesToReapply.length === 0) return;
    setIsProcessing(true);
    try {
      const result = await client.reapplyFileChanges(activeChatId, summary.ref!, filesToReapply, activeWorkspaceId);
      const newUndone = new Set(undoneFiles);
      const failures: Array<{ path: string; error: string }> = [];
      for (const [path, res] of Object.entries(result.results)) {
        if (res.success) {
          newUndone.delete(path);
        } else {
          failures.push({ path, error: res.error || "unknown error" });
        }
      }
      setUndoneFiles(newUndone);
      if (failures.length > 0) {
        setFailDialog({ title: t("fileChange.reapplyFail"), files: failures });
      }
    } finally {
      setIsProcessing(false);
    }
  }, [undoableFiles, undoneFiles, client, activeChatId, activeWorkspaceId, summary.ref, t]);

  if (!summary || summary.totalFiles === 0) {
    return null;
  }

  const totals = useMemo(
    () => ({
      added: summary.files.reduce((sum, file) => sum + file.addedLines, 0),
      deleted: summary.files.reduce((sum, file) => sum + file.deletedLines, 0),
    }),
    [summary.files],
  );

  const btnLabel = allUndone ? t("fileChange.reapply") : t("fileChange.undo");

  return (
    <div className="file-change-list">
      <div className="file-change-header">
        <span className="file-change-title">
          {t("fileChange.header", { count: summary.totalFiles })}
        </span>
        <span className="file-change-stats">
          <span className="file-change-added">+{totals.added}</span>
          <span className="file-change-deleted">-{totals.deleted}</span>
        </span>
        {showButton && (
          <button
            className="btn btn-small file-change-undo-btn"
            disabled={isProcessing}
            onClick={allUndone ? handleReapply : handleUndo}
          >
            {btnLabel}
          </button>
        )}
      </div>
      <div className="file-change-items">
        {summary.files.map((file) => (
          <FileChangeItem
            key={file.filePath}
            file={file}
            isExpanded={expandedFiles.has(file.filePath)}
            isUndone={undoneFiles.has(file.filePath)}
            onToggle={() => toggleFile(file.filePath)}
            t={t}
            workspaceRoot={workspaceRoot}
          />
        ))}
      </div>
      {failDialog && (
        <div className="modal-backdrop" onClick={() => setFailDialog(null)}>
          <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">{failDialog.title}</h3>
            <div className="modal-body">
              {failDialog.files.map((f) => (
                <div key={f.path} className="file-change-fail-item">
                  <span className="file-change-fail-path">{f.path}</span>
                  {f.error && <span className="file-change-fail-error">{f.error}</span>}
                </div>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={() => setFailDialog(null)}>
                {t("common.ok")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

interface FileChangeItemProps {
  file: FileChangeRecord;
  isExpanded: boolean;
  isUndone: boolean;
  onToggle: () => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  workspaceRoot?: string;
}

function FileChangeItem({ file, isExpanded, isUndone, onToggle, t, workspaceRoot }: FileChangeItemProps) {
  const fileName = useMemo(() => {
    const parts = file.filePath.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1];
  }, [file.filePath]);
  const oldFileName = useMemo(() => {
    if (!file.oldPath) return "";
    const parts = file.oldPath.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1];
  }, [file.oldPath]);

  const isDelete = file.changeType === "delete";
  const isBinary = file.changeType === "modify" && file.addedLines === 0 && file.deletedLines === 0 && (!file.patch || file.patch.length === 0);
  const stats = useMemo(
    () => ({
      added: isBinary ? 0 : file.addedLines,
      deleted: isBinary ? 0 : file.deletedLines,
    }),
    [file.addedLines, file.deletedLines, isBinary],
  );

  const relativePath = useMemo(() => {
    const normalized = file.filePath.replace(/\\/g, "/");
    if (workspaceRoot) {
      const root = workspaceRoot.replace(/\\/g, "/").replace(/\/$/, "");
      if (normalized.startsWith(root + "/")) {
        return normalized.slice(root.length + 1);
      }
      if (normalized.startsWith(root)) {
        return normalized.slice(root.length);
      }
    }
    return normalized;
  }, [file.filePath, workspaceRoot]);

  return (
    <div
      className={`file-change-item ${isDelete ? "deleted" : ""} ${isBinary ? "binary" : ""} ${isExpanded ? "expanded" : ""} ${isUndone ? "undone" : ""}`}
    >
      <div
        className="file-change-item-header"
        onClick={isDelete || isBinary ? undefined : onToggle}
        title={relativePath}
      >
        <span className={`file-change-item-name ${isDelete ? "strikethrough" : ""}`}>{fileName}</span>
        {oldFileName && (
          <span className="file-change-old-path" title={file.oldPath}>
            ← {oldFileName}
          </span>
        )}
        {isUndone && <span className="file-change-undone-badge">{t("fileChange.undone")}</span>}
        <span className="file-change-item-stats">
          {isBinary ? (
            <span className="file-change-binary">Binary</span>
          ) : (
            <>
              <span className="file-change-added">+{stats.added}</span>
              <span className="file-change-deleted">-{stats.deleted}</span>
            </>
          )}
        </span>
        {!isDelete && !isBinary && (
          <Icon name="chevron" size={14} className={`chevron ${isExpanded ? "open" : ""}`} />
        )}
      </div>
      {!isDelete && !isBinary && file.patch && (
        <Collapsible open={isExpanded} className="file-change-collapse">
          <FileChangeDetails file={file} t={t} />
        </Collapsible>
      )}
    </div>
  );
}
