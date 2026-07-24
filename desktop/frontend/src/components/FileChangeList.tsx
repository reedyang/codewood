import { useState, useMemo } from "react";
import { FileChangeSummary, FileChangeRecord, DiffRow } from "../api/types";
import { FileChangeDetails } from "./FileChangeDetails";
import { Icon } from "./Icon";

function computeStats(patch?: DiffRow[]): { added: number; deleted: number } {
  let added = 0;
  let deleted = 0;
  if (patch) {
    for (const row of patch) {
      if (row.type === "add") added++;
      else if (row.type === "del") deleted++;
      else if (row.type === "change") { added++; deleted++; }
    }
  }
  return { added, deleted };
}

interface FileChangeListProps {
  summary: FileChangeSummary;
  t: (key: string, params?: Record<string, string | number>) => string;
  workspaceRoot?: string;
}

export function FileChangeList({ summary, t, workspaceRoot }: FileChangeListProps) {
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());

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

  if (!summary || summary.totalFiles === 0) {
    return null;
  }

  const totals = useMemo(() => {
    let added = 0;
    let deleted = 0;
    for (const file of summary.files) {
      if (file.changeType === "delete") {
        deleted += file.deletedLines;
      } else {
        const s = computeStats(file.patch);
        added += s.added;
        deleted += s.deleted;
      }
    }
    return { added, deleted };
  }, [summary.files]);

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
      </div>
      <div className="file-change-items">
        {summary.files.map((file) => (
          <FileChangeItem
            key={file.filePath}
            file={file}
            isExpanded={expandedFiles.has(file.filePath)}
            onToggle={() => toggleFile(file.filePath)}
            t={t}
            workspaceRoot={workspaceRoot}
          />
        ))}
      </div>
    </div>
  );
}

interface FileChangeItemProps {
  file: FileChangeRecord;
  isExpanded: boolean;
  onToggle: () => void;
  t: (key: string, params?: Record<string, string | number>) => string;
  workspaceRoot?: string;
}

function FileChangeItem({ file, isExpanded, onToggle, t, workspaceRoot }: FileChangeItemProps) {
  const fileName = useMemo(() => {
    const parts = file.filePath.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1];
  }, [file.filePath]);

  const isDelete = file.changeType === "delete";
  const isBinary = file.changeType === "modify" && file.addedLines === 0 && file.deletedLines === 0 && (!file.patch || file.patch.length === 0);
  const stats = useMemo(() => {
    if (isDelete) {
      return { added: 0, deleted: file.deletedLines };
    }
    if (isBinary) {
      return { added: 0, deleted: 0 };
    }
    return computeStats(file.patch);
  }, [file.patch, file.deletedLines, isDelete, isBinary]);

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
    <div className={`file-change-item ${isDelete ? "deleted" : ""} ${isBinary ? "binary" : ""} ${isExpanded ? "expanded" : ""}`}>
      <div
        className="file-change-item-header"
        onClick={isDelete || isBinary ? undefined : onToggle}
        title={relativePath}
      >
        <span className={`file-change-item-name ${isDelete ? "strikethrough" : ""}`}>{fileName}</span>
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
      {!isDelete && !isBinary && isExpanded && file.patch && (
        <FileChangeDetails file={file} t={t} />
      )}
    </div>
  );
}
