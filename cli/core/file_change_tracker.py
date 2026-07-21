"""File change tracker for recording all file modifications during a session."""

import difflib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FileChangeRecord:
    """Record of a single file change."""
    file_path: str
    change_type: str  # "create", "modify", "delete", "rename"
    source: str  # "apply_patch", "shell", "write", etc.
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    content_before: Optional[str] = None
    content_after: Optional[str] = None
    patch: Optional[List[Dict[str, Any]]] = None  # structured diff segments
    added_lines: int = 0
    deleted_lines: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filePath": self.file_path,
            "changeType": self.change_type,
            "source": self.source,
            "timestamp": self.timestamp,
            "addedLines": self.added_lines,
            "deletedLines": self.deleted_lines,
            "patch": self.patch,
        }


class FileChangeTracker:
    """Central manager for tracking all file changes during a session."""

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id
        self._changes: List[FileChangeRecord] = []
        self._files_before: Dict[str, str] = {}  # cache for content before changes

    def record_change(
        self,
        file_path: str,
        change_type: str,
        source: str,
        content_before: Optional[str] = None,
        content_after: Optional[str] = None,
        patch: Optional[List[Dict[str, Any]]] = None,
    ) -> FileChangeRecord:
        """Record a file change."""
        # Calculate line changes
        added_lines = 0
        deleted_lines = 0
        if content_before is not None and content_after is not None:
            before_lines = content_before.splitlines()
            after_lines = content_after.splitlines()
            matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "delete":
                    deleted_lines += i2 - i1
                elif tag == "insert":
                    added_lines += j2 - j1
                elif tag == "replace":
                    deleted_lines += i2 - i1
                    added_lines += j2 - j1

        record = FileChangeRecord(
            file_path=file_path,
            change_type=change_type,
            source=source,
            content_before=content_before,
            content_after=content_after,
            patch=patch,
            added_lines=added_lines,
            deleted_lines=deleted_lines,
        )
        self._changes.append(record)
        return record

    def record_patch_change(
        self,
        file_path: str,
        source: str,
        segments: List[Dict[str, Any]],
        content_before: Optional[str] = None,
        content_after: Optional[str] = None,
    ) -> FileChangeRecord:
        """Record a patch-based file change with structured segments."""
        return self.record_change(
            file_path=file_path,
            change_type="modify" if content_before is not None else "create",
            source=source,
            content_before=content_before,
            content_after=content_after,
            patch=segments,
        )

    def get_changes(self) -> List[FileChangeRecord]:
        """Get all recorded changes."""
        return list(self._changes)

    def get_changes_for_file(self, file_path: str) -> List[FileChangeRecord]:
        """Get changes for a specific file."""
        normalized = self._normalize_path(file_path)
        return [c for c in self._changes if self._normalize_path(c.file_path) == normalized]

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all changes.

        The ``patch`` field in each file entry is converted from raw preview
        segments (``old_lines``/``new_lines`` dicts) to the frontend
        ``DiffRow[]`` format expected by ``FileChangeDetails``.
        """
        total_added = sum(c.added_lines for c in self._changes)
        total_deleted = sum(c.deleted_lines for c in self._changes)

        # Lazy import to avoid circular dependencies at module level.
        try:
            from .change_preview_formatter import ChangePreviewFormatter
            _to_diff_rows = ChangePreviewFormatter.format_segments_structured
        except Exception:
            _to_diff_rows = None

        # Group by file, keeping patch from the last change per file
        files: Dict[str, Dict[str, Any]] = {}
        for change in self._changes:
            path = change.file_path
            if path not in files:
                diff_rows = _convert_segments_to_diff_rows(change.patch, _to_diff_rows)
                files[path] = {
                    "filePath": path,
                    "changeType": change.change_type,
                    "addedLines": 0,
                    "deletedLines": 0,
                    "patch": diff_rows,
                }
            else:
                files[path]["addedLines"] += change.added_lines
                files[path]["deletedLines"] += change.deleted_lines
                if change.patch:
                    files[path]["patch"] = _convert_segments_to_diff_rows(
                        change.patch, _to_diff_rows
                    )

        return {
            "totalFiles": len(files),
            "totalAdded": total_added,
            "totalDeleted": total_deleted,
            "files": list(files.values()),
        }

    def clear(self) -> None:
        """Clear all recorded changes."""
        self._changes.clear()
        self._files_before.clear()

    def snapshot_file(self, file_path: str) -> None:
        """Snapshot a file's content before modification."""
        try:
            path = Path(file_path)
            if path.exists():
                self._files_before[file_path] = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    def get_snapshot(self, file_path: str) -> Optional[str]:
        """Get the snapshot of a file's content before modification."""
        return self._files_before.get(file_path)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path for comparison."""
        return os.path.normcase(os.path.normpath(path))


def _convert_segments_to_diff_rows(
    segments: Optional[List[Dict[str, Any]]],
    converter: Any,
) -> List[Dict[str, Any]]:
    """Convert raw preview segments to frontend DiffRow[] format.

    ``segments`` is the list of dicts produced by apply_patch with keys
    ``old_lines``/``new_lines``/``old_start_line``/``new_start_line``.
    ``converter`` is ``ChangePreviewFormatter.format_segments_structured``
    (or None when the import failed).
    """
    if not segments or not converter:
        return []
    try:
        rows = converter(segments)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []
