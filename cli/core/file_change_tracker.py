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
    backup_path: Optional[str] = None  # relative backup filename for delete recovery

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "filePath": self.file_path,
            "changeType": self.change_type,
            "source": self.source,
            "timestamp": self.timestamp,
            "addedLines": self.added_lines,
            "deletedLines": self.deleted_lines,
            "patch": self.patch,
        }
        if self.backup_path:
            result["backupPath"] = self.backup_path
        return result


class FileChangeTracker:
    """Central manager for tracking all file changes during a session."""

    def __init__(self, session_id: Optional[str] = None, path_policy: Optional[Any] = None):
        self.session_id = session_id
        self._changes: List[FileChangeRecord] = []
        self._files_before: Dict[str, str] = {}  # cache for content before changes
        # Optional PathPolicy used to decide whether a path lives under the
        # workspace cache directory.  Changes under the cache are disposable
        # and must not surface in the file-change list.
        self._path_policy = path_policy

    def _is_ignored_cache_path(self, file_path: str) -> bool:
        """True iff ``file_path`` lives under the workspace cache directory."""
        if self._path_policy is None:
            return False
        try:
            return self._path_policy.is_workspace_cache_path(Path(file_path))
        except Exception:
            return False

    def record_change(
        self,
        file_path: str,
        change_type: str,
        source: str,
        content_before: Optional[str] = None,
        content_after: Optional[str] = None,
        patch: Optional[List[Dict[str, Any]]] = None,
        backup_path: Optional[str] = None,
    ) -> Optional[FileChangeRecord]:
        """Record a file change."""
        # Changes under the workspace cache directory are disposable and never
        # recorded in the file-change list.
        if self._is_ignored_cache_path(file_path):
            return None
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
            backup_path=backup_path,
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

    def record_delete(
        self,
        file_path: str,
        source: str,
        content_before: str,
        backup_path: Optional[str] = None,
    ) -> Optional[FileChangeRecord]:
        """Record a file deletion with the pre-deletion content."""
        # Changes under the workspace cache directory are disposable and never
        # recorded in the file-change list.
        if self._is_ignored_cache_path(file_path):
            return None
        deleted_lines = len(content_before.splitlines())
        record = FileChangeRecord(
            file_path=file_path,
            change_type="delete",
            source=source,
            content_before=content_before,
            content_after=None,
            patch=None,
            added_lines=0,
            deleted_lines=deleted_lines,
            backup_path=backup_path,
        )
        self._changes.append(record)
        return record

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
        # Lazy import to avoid circular dependencies at module level.
        try:
            from .change_preview_formatter import ChangePreviewFormatter
            _to_diff_rows = ChangePreviewFormatter.format_segments_structured
        except Exception:
            _to_diff_rows = None

        # Group by file.  When the same file is modified multiple times
        # within a task, each intermediate patch carries its own context
        # lines — splicing them naively would duplicate content.  Instead
        # compute a single diff from the very first content_before to the
        # very last content_after, giving a clean unified view.
        files: Dict[str, Dict[str, Any]] = {}
        for change in self._changes:
            path = change.file_path
            if path not in files:
                if change.change_type == "delete":
                    diff_rows: List[Dict[str, Any]] = []
                else:
                    diff_rows = _convert_segments_to_diff_rows(change.patch, _to_diff_rows)
                files[path] = {
                    "filePath": path,
                    "changeType": change.change_type,
                    "addedLines": change.added_lines,
                    "deletedLines": change.deleted_lines,
                    "patch": diff_rows,
                    "backupPath": change.backup_path,
                    "_first_change_type": change.change_type,
                    "_first_before": change.content_before,
                    "_last_after": change.content_after,
                }
            else:
                files[path]["addedLines"] += change.added_lines
                files[path]["deletedLines"] += change.deleted_lines
                files[path]["_last_after"] = change.content_after
                if change.change_type == "delete":
                    files[path]["changeType"] = "delete"
        for path, entry in files.items():
            _first_change_type = str(entry.pop("_first_change_type", "") or "")
            _first = entry.pop("_first_before", None)
            _last = entry.pop("_last_after", None)
            if entry["changeType"] == "delete":
                if _first_change_type == "create":
                    entry["addedLines"] = 0
                    entry["deletedLines"] = 0
                elif _first is not None:
                    entry["addedLines"] = 0
                    entry["deletedLines"] = len(str(_first).splitlines())
                continue

            final_patch: Optional[List[Dict[str, Any]]] = None
            if _first_change_type == "create" and _last is not None:
                final_patch = _compute_diff_rows("", _last)
            elif (
                _first is not None
                and _last is not None
                and _first != _last
            ):
                final_patch = _compute_diff_rows(_first, _last)

            if final_patch is not None:
                entry["patch"] = final_patch
                entry["addedLines"], entry["deletedLines"] = _count_diff_rows(final_patch)

        return {
            "totalFiles": len(files),
            "totalAdded": sum(int(f.get("addedLines", 0) or 0) for f in files.values()),
            "totalDeleted": sum(int(f.get("deletedLines", 0) or 0) for f in files.values()),
            "files": list(files.values()),
        }

    def cancel_create_for_deleted_file(self, file_path: str) -> bool:
        """If the file's first recorded change is *create* and its last is
        *delete*, the file went from non-existent back to non-existent — a
        net-zero change.  Remove all records for that file in this case.

        If the file already existed before the task (first change is not
        *create*), the deletion is a real change that must be shown even when
        intermediate create/delete pairs appear.  Returns True when records
        were removed."""
        normalized = self._normalize_path(file_path)
        records = [
            c for c in self._changes
            if self._normalize_path(c.file_path) == normalized
        ]
        if not records:
            return False
        if records[0].change_type == "create" and records[-1].change_type == "delete":
            self._changes = [
                c for c in self._changes
                if self._normalize_path(c.file_path) != normalized
            ]
            return True
        return False

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


def _compute_diff_rows(before: str, after: str) -> List[Dict[str, Any]]:
    """Compute frontend DiffRow[] from two content strings via difflib."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines)
    rows: List[Dict[str, Any]] = []
    old_no = 1
    new_no = 1
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append({
                    "type": "context",
                    "oldNo": old_no + k, "newNo": new_no + k,
                    "oldText": before_lines[i1 + k],
                    "newText": after_lines[j1 + k],
                })
            old_no += i2 - i1
            new_no += j2 - j1
        elif tag == "replace":
            shared = min(i2 - i1, j2 - j1)
            for k in range(shared):
                rows.append({
                    "type": "change",
                    "oldNo": old_no + k,
                    "newNo": new_no + k,
                    "oldText": before_lines[i1 + k],
                    "newText": after_lines[j1 + k],
                })
            for k in range(shared, i2 - i1):
                rows.append({
                    "type": "del",
                    "oldNo": old_no + k,
                    "newNo": None,
                    "oldText": before_lines[i1 + k],
                    "newText": "",
                })
            for k in range(shared, j2 - j1):
                rows.append({
                    "type": "add",
                    "oldNo": None,
                    "newNo": new_no + k,
                    "oldText": "",
                    "newText": after_lines[j1 + k],
                })
            old_no += i2 - i1
            new_no += j2 - j1
        elif tag == "delete":
            for k in range(i2 - i1):
                rows.append({
                    "type": "del",
                    "oldNo": old_no + k, "newNo": None,
                    "oldText": before_lines[i1 + k], "newText": "",
                })
            old_no += i2 - i1
        elif tag == "insert":
            for k in range(j2 - j1):
                rows.append({
                    "type": "add",
                    "oldNo": None, "newNo": new_no + k,
                    "oldText": "", "newText": after_lines[j1 + k],
                })
            new_no += j2 - j1
    return rows


def _convert_segments_to_diff_rows(
    segments: Optional[List[Dict[str, Any]]],
    converter: Any,
) -> List[Dict[str, Any]]:
    """Convert raw preview segments to frontend DiffRow[] format.

    ``segments`` is the list of dicts produced by apply_patch with keys
    ``old_lines``/``new_lines``/``old_start_line``/``new_start_line``,
    OR a list already in DiffRow format (with ``type`` key).
    ``converter`` is ``ChangePreviewFormatter.format_segments_structured``
    (or None when the import failed).
    """
    if not segments or not isinstance(segments, list) or len(segments) == 0:
        return []
    first = segments[0]
    if isinstance(first, dict) and "type" in first:
        return segments
    if not converter:
        return []
    try:
        rows = converter(segments)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _count_diff_rows(rows: List[Dict[str, Any]]) -> tuple[int, int]:
    """Return ``(added, deleted)`` counts for frontend DiffRow[] rows."""
    added = 0
    deleted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_type = str(row.get("type", "") or "")
        if row_type == "add":
            added += 1
        elif row_type == "del":
            deleted += 1
        elif row_type == "change":
            added += 1
            deleted += 1
    return added, deleted
