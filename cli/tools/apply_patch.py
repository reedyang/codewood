"""Tool: apply_patch.

Hosts the unified-diff application pipeline (formerly part of
cli/actions/filesystem_actions.py) plus the ApplyPatchTool class.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.localization import translate

# BOM signatures, checked longest-first so utf-32 is not misread as utf-16.
# Each entry maps the leading bytes to the base codec used to decode/encode the
# remaining content (the BOM itself is preserved separately).
_BOM_SIGNATURES: List[Tuple[bytes, str]] = [
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

# Candidate encodings tried (strict) for files without a BOM. ``latin1`` maps
# every byte, so it is the final catch-all.
_TEXT_DECODE_CANDIDATES: List[str] = ["utf-8", "gbk", "gb2312", "utf-16", "latin1"]


def _gui_mode_active(agent: Any) -> bool:
    """True when running under the desktop GUI (structured confirm provider)."""
    return callable(getattr(agent, "_confirm_choice_provider", None))


def _emit_gui_diff_block(file_path: str, preview_segments: List[Dict[str, Any]]) -> None:
    """Print a sentinel-wrapped structured diff payload for the GUI transcript.

    The desktop frontend splits step text on the GUI_DIFF_* sentinels and renders
    a collapsible, syntax-highlighted diff block. The payload travels inside the
    normal step output so it is persisted with the chat and re-rendered on reload.
    """
    try:
        import json as _json

        from ..core.change_preview_formatter import ChangePreviewFormatter
        from ..core.console_utils import GUI_DIFF_BEGIN, GUI_DIFF_END

        rows = ChangePreviewFormatter.format_segments_structured(preview_segments)
        if not rows:
            return
        payload = _json.dumps(
            {"file": file_path, "diffRows": rows}, ensure_ascii=False
        )
        print(f"{GUI_DIFF_BEGIN}{payload}{GUI_DIFF_END}")
    except Exception:
        # Never let preview rendering break the patch application.
        pass


def _interactive_selector_available(agent: Any) -> bool:
    """Return True when an interactive UI will render the confirm prompt (and
    thus the change preview) itself, so apply_patch should hand over the
    structured diff segments and skip the static ANSI text print.

    Covers both the GUI structured confirm provider and the TUI arrow-key
    selector on an attached terminal.
    """
    try:
        # GUI: a structured confirm provider renders the preview in the frontend.
        if callable(getattr(agent, "_confirm_choice_provider", None)):
            return True
        input_handler = getattr(agent, "input_handler", None)
        interactive = getattr(input_handler, "prompt_request_user_input_selection", None)
        if not callable(interactive):
            return False
        import sys

        return bool(
            sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()
        )
    except Exception:
        return False


def _read_text_preserving_encoding(abs_path: Path) -> Tuple[str, str, bytes]:
    """Read a text file as ``(content_without_bom, base_codec, bom_bytes)``.

    The encoding and any byte-order mark are detected so the file can later be
    rewritten byte-for-byte in the same encoding. Decoding is attempted strictly
    (so a non-UTF-8 file is not silently mojibake'd to UTF-8); only the final
    fallback uses lossy replacement.
    """
    raw_bytes = abs_path.read_bytes()
    for bom, codec in _BOM_SIGNATURES:
        if raw_bytes.startswith(bom):
            content = raw_bytes[len(bom):].decode(codec, errors="replace")
            return content, codec, bom
    for codec in _TEXT_DECODE_CANDIDATES:
        try:
            return raw_bytes.decode(codec), codec, b""
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8", b""


def _normalize_apply_patch_text(raw_patch: str, file_path: str) -> tuple[str, List[str]]:
    patch_text = str(raw_patch or "")
    warnings: List[str] = []
    if not patch_text.strip():
        return patch_text, warnings

    lines = patch_text.splitlines()
    has_begin_marker = any(str(ln).strip() == "*** Begin Patch" for ln in lines)
    if not has_begin_marker:
        return patch_text, warnings

    end_patch_count = sum(1 for ln in lines if str(ln).strip() == "*** End Patch")
    if end_patch_count > 1:
        warnings.append(
            f"Detected repeated '*** End Patch' markers ({end_patch_count}); extra markers were ignored."
        )

    if any(str(ln).startswith("@@") for ln in lines):
        return patch_text, warnings

    add_file_idx: Optional[int] = None
    add_file_declared: Optional[str] = None
    for idx, ln in enumerate(lines):
        if str(ln).startswith("*** Add File:"):
            add_file_idx = idx
            add_file_declared = str(ln)[len("*** Add File:") :].strip()
            break
    if add_file_idx is None:
        return patch_text, warnings

    requested_name = Path(str(file_path or "")).name
    declared_name = Path(str(add_file_declared or "")).name
    if requested_name and declared_name and requested_name != declared_name:
        warnings.append(
            f"Add-file declaration '{add_file_declared}' does not match requested path '{file_path}'; used requested path."
        )

    add_lines: List[str] = []
    for ln in lines[add_file_idx + 1 :]:
        stripped = str(ln).strip()
        if stripped == "*** End Patch":
            continue
        if str(ln).startswith("*** "):
            continue
        if str(ln).startswith("+"):
            add_lines.append(str(ln))
            continue
        if str(ln) == "":
            warnings.append(
                "Found a blank line without '+' prefix in Add File patch body; ignored that line."
            )
            continue
        warnings.append(
            "Found non-addition line in Add File patch body; ignored lines without '+' prefix."
        )

    if not add_lines:
        return patch_text, warnings

    normalized_lines = [f"@@ -0,0 +1,{len(add_lines)} @@"]
    normalized_lines.extend(add_lines)
    normalized = "\n".join(normalized_lines)
    if patch_text.endswith("\n"):
        normalized += "\n"

    return normalized, warnings


def _hunk_matches_at(old_lines: List[str], start_idx: int, hunk_lines: List[str]) -> bool:
    if start_idx < 0 or start_idx > len(old_lines):
        return False
    cur = start_idx
    for hl in hunk_lines:
        if hl.startswith("*** "):
            continue
        if hl.startswith("\\ No newline at end of file"):
            continue
        if not hl:
            return False
        prefix = hl[0]
        text = hl[1:]
        if prefix in (" ", "-"):
            if cur >= len(old_lines) or old_lines[cur] != text:
                return False
            cur += 1
        elif prefix == "+":
            continue
        else:
            return False
    return True


def _locate_hunk_start(
    old_lines: List[str], src_idx: int, target_idx: int, hunk_lines: List[str]
) -> Optional[int]:
    if _hunk_matches_at(old_lines, target_idx, hunk_lines):
        return target_idx

    anchor: Optional[str] = None
    for hl in hunk_lines:
        if hl and hl[0] in (" ", "-"):
            anchor = hl[1:]
            break
    if anchor is None:
        return None

    candidates: List[int] = []
    for probe in range(src_idx, len(old_lines)):
        if old_lines[probe] == anchor:
            candidates.append(probe)
    if not candidates:
        return None

    candidates.sort(key=lambda idx: abs(idx - target_idx))
    for probe in candidates:
        if _hunk_matches_at(old_lines, probe, hunk_lines):
            return probe
    return None


def action_apply_unified_patch(agent: Any, file_path: str, patch: str, confirmed: bool = False) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        abs_path = agent._resolve_user_path(str(file_path))
        file_exists = abs_path.exists()
        if file_exists and (not abs_path.is_file()):
            return {"success": False, "error": f"'{file_path}' is not a file"}
        decision = policy.can_write_path(abs_path, "apply_patch")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        execution_policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
        in_workspace_root = False
        raw_workspace_root = getattr(agent, "workspace_root", None)
        if raw_workspace_root:
            try:
                in_workspace_root = bool(
                    agent._is_path_under(abs_path, Path(str(raw_workspace_root)))
                )
            except Exception:
                in_workspace_root = False
        skip_preview_and_confirm = (
            execution_policy in ("moderate", "unlimited") and in_workspace_root
        )
        need_confirm = not skip_preview_and_confirm

        # New files are always created as UTF-8 without BOM and "\n" newlines.
        # Existing files keep their original encoding, BOM, and newline style.
        source = ""
        base_codec = "utf-8"
        bom_bytes = b""
        if file_exists:
            try:
                source, base_codec, bom_bytes = _read_text_preserving_encoding(abs_path)
            except Exception:
                return {"success": False, "error": "Unable to read text file; encoding may be unsupported"}

        if not file_exists:
            newline = "\n"
        elif "\r\n" in source:
            newline = "\r\n"
        elif "\r" in source:
            newline = "\r"
        else:
            newline = "\n"
        had_trailing_newline = (
            (source.endswith("\n") or source.endswith("\r"))
            if file_exists
            else str(patch or "").endswith("\n")
        )
        old_lines = source.splitlines()
        normalized_patch, patch_warnings = _normalize_apply_patch_text(
            str(patch or ""), str(file_path or "")
        )
        patch_lines = normalized_patch.splitlines()
        if not patch_lines:
            return {"success": False, "error": "Patch content cannot be empty"}

        hunks: List[Dict[str, Any]] = []
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            if not line.startswith("@@"):
                i += 1
                continue
            if line.strip() == "@@":
                old_start = None
            else:
                m = re.match(r"^@@(?:\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?)?\s*@@", line)
                if not m:
                    return {"success": False, "error": f"Invalid hunk header: {line}"}
                old_start = int(m.group(1)) if m.group(1) else None
            hunk_lines: List[str] = []
            i += 1
            while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                hunk_lines.append(patch_lines[i])
                i += 1
            hunks.append({"old_start": old_start, "lines": hunk_lines})
        if not hunks:
            return {"success": False, "error": "No applicable hunks found (expected '@@ ... @@' sections)"}

        result_lines: List[str] = []
        src_idx = 0
        preview_lines: List[str] = []
        preview_segments: List[Dict[str, Any]] = []
        for hunk in hunks:
            old_start = hunk["old_start"]
            if old_start is None:
                target_idx = src_idx
            else:
                old_start_no = int(old_start)
                target_idx = 0 if old_start_no <= 0 else old_start_no - 1
            if target_idx < src_idx or target_idx > len(old_lines):
                return {"success": False, "error": f"Hunk start line out of range: {old_start}"}
            located_idx = _locate_hunk_start(old_lines, src_idx, target_idx, hunk["lines"])
            if located_idx is None:
                if old_start is None:
                    return {"success": False, "error": "Patch anchor not found; unable to locate hunk"}
                return {
                    "success": False,
                    "error": f"Patch context mismatch (line {target_idx + 1})",
                }
            target_idx = located_idx
            result_lines.extend(old_lines[src_idx:target_idx])
            new_target_idx = len(result_lines)
            cur = target_idx
            hunk_old_fragment: List[str] = []
            hunk_new_fragment: List[str] = []
            has_change = False
            for hl in hunk["lines"]:
                if hl.startswith("*** "):
                    continue
                if hl.startswith("\\ No newline at end of file"):
                    continue
                if not hl:
                    return {"success": False, "error": "Invalid hunk line format (missing prefix)"}
                prefix = hl[0]
                text = hl[1:]
                if prefix == " ":
                    if cur >= len(old_lines) or old_lines[cur] != text:
                        return {"success": False, "error": f"Patch context mismatch (line {cur + 1})"}
                    result_lines.append(old_lines[cur])
                    hunk_old_fragment.append(old_lines[cur])
                    hunk_new_fragment.append(old_lines[cur])
                    cur += 1
                elif prefix == "-":
                    if cur >= len(old_lines) or old_lines[cur] != text:
                        return {"success": False, "error": f"Patch deletion mismatch (line {cur + 1})"}
                    hunk_old_fragment.append(old_lines[cur])
                    has_change = True
                    cur += 1
                elif prefix == "+":
                    result_lines.append(text)
                    hunk_new_fragment.append(text)
                    has_change = True
                else:
                    return {"success": False, "error": f"Unsupported hunk line prefix: {prefix}"}
            src_idx = cur
            if has_change:
                context_before = old_lines[max(0, target_idx - 2):target_idx]
                context_after = old_lines[cur:min(len(old_lines), cur + 2)]
                preview_old = context_before + hunk_old_fragment + context_after
                preview_new = context_before + hunk_new_fragment + context_after
                preview_old_start = max(1, target_idx - len(context_before) + 1)
                preview_new_start = max(1, new_target_idx - len(context_before) + 1)
                preview_segments.append(
                    {
                        "old_lines": preview_old,
                        "new_lines": preview_new,
                        "old_start_line": preview_old_start,
                        "new_start_line": preview_new_start,
                    }
                )
        if preview_segments:
            preview_lines = agent._format_side_by_side_change_preview_segments(
                preview_segments, file_path=abs_path
            )
        result_lines.extend(old_lines[src_idx:])
        new_text = newline.join(result_lines)
        if had_trailing_newline and len(result_lines) > 0:
            new_text += newline
        # When the interactive TUI selector will render the change preview
        # itself (so it can re-layout live on terminal resize), skip the static
        # text print here and hand the structured segments to the confirm call.
        # Under the GUI we always skip the static ANSI print: the diff is shown
        # either in the confirm panel (confirmation mode) or as a collapsible
        # transcript diff block emitted below (non-confirmation mode).
        gui_mode = _gui_mode_active(agent)
        interactive_preview = bool(need_confirm) and _interactive_selector_available(agent)
        confirm_preview_segments = preview_segments if interactive_preview else None
        if (
            preview_lines
            and not skip_preview_and_confirm
            and not interactive_preview
            and not gui_mode
        ):
            try:
                lang = agent._ui_language()
            except AttributeError:
                lang = getattr(agent, "display_language", None) or "en"
            print(translate("change_preview.header", lang))
            print(translate("change_preview.markers", lang))
            for ln in preview_lines:
                print(ln)
        for warn in patch_warnings:
            try:
                print(f"⚠️ {warn}")
            except Exception:
                pass
        if need_confirm:
            from ..core.change_preview_formatter import ChangePreviewFormatter

            try:
                _lang = agent._ui_language()
            except AttributeError:
                _lang = getattr(agent, "display_language", None) or "en"
            ok = agent._prompt_confirm_yes_no_maybe_always(
                translate(
                    "confirm.apply_patch_text_file",
                    _lang,
                    fallback="⚠️ Confirm applying patch to text file: {path} ?",
                    path=str(abs_path),
                ),
                offer_always=False,
                kind="text_file",
                preview_segments=confirm_preview_segments,
                code_language=ChangePreviewFormatter.language_from_path(abs_path),
            )
            if not ok:
                return {"success": False, "error": "Operation cancelled by user"}
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Snapshot file content before modification for change tracking
        content_before = source if file_exists else None
        # Write raw bytes so the text-mode universal-newline translation does not
        # rewrite "\n" to the OS separator. This keeps the chosen ``newline`` and
        # encoding/BOM exactly as intended.
        if file_exists:
            data = bom_bytes + new_text.encode(base_codec or "utf-8", errors="replace")
        else:
            data = new_text.encode("utf-8", errors="replace")
        abs_path.write_bytes(data)
        resolved = abs_path.resolve()
        agent._ai_created_path_keys.add(agent._ephemeral_path_key(resolved))
        agent._reload_skills_if_workspace_skill_changed([resolved])
        # Record file change for the change tracker
        try:
            from ..core.logging.app_logging import get_logger
            _fc_logger = get_logger("codewood.file_change")
            tracker = getattr(agent, "file_change_tracker", None)
            if tracker is not None:
                tracker.record_patch_change(
                    file_path=str(resolved),
                    source="apply_patch",
                    segments=preview_segments or [],
                    content_before=content_before,
                    content_after=new_text,
                )
                _fc_logger.debug(f"[file_changes] recorded patch change for {resolved} (segments={len(preview_segments or [])})")
            else:
                _fc_logger.debug("[file_changes] tracker is None in apply_patch")
        except Exception as _e:
            from ..core.logging.app_logging import get_logger
            get_logger("codewood.file_change").debug(f"[file_changes] apply_patch record error: {_e}")
            pass
        # GUI: render the change preview as a collapsible, highlighted diff
        # block in the transcript.  Print the tool-call prompt line FIRST so
        # the frontend orders them prompt-then-preview during live execution.
        if gui_mode and preview_segments:
            try:
                formatter = getattr(agent, "_format_tool_call_feedback_line", None)
                if callable(formatter):
                    _call_args = {"path": file_path, "patch": patch}
                    print(formatter("apply_patch", _call_args, failed=False))
            except Exception:
                pass
            _emit_gui_diff_block(str(resolved), preview_segments)
        change_preview_rows: List[Dict[str, Any]] = []
        if preview_segments:
            try:
                from ..core.change_preview_formatter import ChangePreviewFormatter

                change_preview_rows = ChangePreviewFormatter.format_segments_structured(
                    preview_segments
                )
            except Exception:
                change_preview_rows = []
        return {
            "success": True,
            "file": str(resolved),
            "hunk_count": len(hunks),
            "change_preview": preview_lines,
            "change_preview_rows": change_preview_rows,
            "warnings": patch_warnings,
            "message": f"Successfully applied patch to '{resolved.name}'",
        }
    except Exception as e:
        return {"success": False, "error": f"apply_patch failed: {str(e)}"}


from .base import BaseTool  # noqa: E402


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = "Apply a unified diff patch to a text file."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "patch": {
                "type": "string",
                "description": "Prefer a standard patch/git apply unified diff, including ---/+++ and at least one @@ ... @@ hunk.",
            },
        },
        "required": ["path", "patch"],
    }

    def execute(self, agent: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        file_path = params.get("path")
        patch = params.get("patch")
        if file_path and patch is not None:
            patch_cmd = {"action": "apply_patch", "params": {"path": file_path}}
            confirmed = agent._freedom_auto_confirm(patch_cmd)
            return action_apply_unified_patch(
                agent, file_path=file_path, patch=str(patch), confirmed=confirmed
            )
        missing = []
        if not file_path:
            missing.append("path")
        if patch is None:
            missing.append("patch")
        missing_text = ", ".join(missing) if missing else "path/patch"
        return {
            "success": False,
            "error": f"apply_patch requires both path and patch; missing: {missing_text}",
        }
