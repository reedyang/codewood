import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.localization import translate


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

        encodings = ["utf-8", "gbk", "gb2312", "utf-16", "latin1"]
        source = ""
        used_encoding = "utf-8"
        if file_exists:
            loaded = False
            for enc in encodings:
                try:
                    with open(abs_path, "r", encoding=enc, errors="replace") as f:
                        source = f.read()
                    used_encoding = enc
                    loaded = True
                    break
                except Exception:
                    continue
            if not loaded:
                return {"success": False, "error": "Unable to read text file; encoding may be unsupported"}

        newline = "\r\n" if "\r\n" in source else "\n"
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
            preview_lines = agent._format_side_by_side_change_preview_segments(preview_segments)
        result_lines.extend(old_lines[src_idx:])
        new_text = newline.join(result_lines)
        if had_trailing_newline and len(result_lines) > 0:
            new_text += newline
        if preview_lines and not skip_preview_and_confirm:
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
            ok = agent._prompt_confirm_yes_no_maybe_always(
                f"⚠️ Confirm applying patch to text file: {abs_path} ?",
                offer_always=False,
                kind="text_file",
            )
            if not ok:
                return {"success": False, "error": "Operation cancelled by user"}
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with open(abs_path, "w", encoding=used_encoding or "utf-8", errors="replace") as f:
            f.write(new_text)
        resolved = abs_path.resolve()
        agent._ai_created_path_keys.add(agent._ephemeral_path_key(resolved))
        agent._reload_skills_if_workspace_skill_changed([resolved])
        return {
            "success": True,
            "file": str(resolved),
            "hunk_count": len(hunks),
            "change_preview": preview_lines,
            "warnings": patch_warnings,
            "message": f"Successfully applied patch to '{resolved.name}'",
        }
    except Exception as e:
        return {"success": False, "error": f"apply_patch failed: {str(e)}"}


def action_read_image(agent: Any, file_path: str, prompt: str = "") -> Dict[str, Any]:
    try:
        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            p1 = agent.work_directory / file_path
            p_temp = agent.ai_workspace_temp_dir / file_path
            p2 = agent.workspace_config_dir / file_path
            if p1.is_file():
                abs_path = p1
            elif p_temp.is_file():
                abs_path = p_temp
            elif p2.is_file():
                abs_path = p2
            else:
                abs_path = p1
        if not abs_path.exists():
            return {"success": False, "error": f"Image file '{file_path}' does not exist"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' is not a file"}
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"]
        if abs_path.suffix.lower() not in image_exts:
            return {"success": False, "error": f"Unsupported file format: {abs_path.suffix}"}
        image_task_context = f"Image file path: {str(abs_path)}"
        image_user_prompt = prompt if prompt else "Please read this image first, then continue with the current task."
        analysis = agent.call_ai(image_user_prompt, context=image_task_context, image_path=str(abs_path))
        return {"success": True, "analysis": analysis, "file": str(abs_path)}
    except Exception as e:
        return {"success": False, "error": f"Image read failed: {str(e)}"}


