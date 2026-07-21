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
}

export function FileChangeList({ summary, t }: FileChangeListProps) {
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
      const s = computeStats(file.patch);
      added += s.added;
      deleted += s.deleted;
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
}

function FileChangeItem({ file, isExpanded, onToggle, t }: FileChangeItemProps) {
  const fileName = useMemo(() => {
    const parts = file.filePath.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1];
  }, [file.filePath]);

  const stats = useMemo(() => computeStats(file.patch), [file.patch]);

  return (
    <div className={`file-change-item ${isExpanded ? "expanded" : ""}`}>
      <div className="file-change-item-header" onClick={onToggle}>
        <span className="file-change-item-name">{fileName}</span>
        <span className="file-change-item-stats">
          <span className="file-change-added">+{stats.added}</span>
          <span className="file-change-deleted">-{stats.deleted}</span>
        </span>
        <Icon name="chevron" size={14} className={`chevron ${isExpanded ? "open" : ""}`} />
      </div>
      {isExpanded && file.patch && (
        <FileChangeDetails file={file} t={t} />
      )}
    </div>
  );
}
