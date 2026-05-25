import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def action_ffmpeg(agent: Any, source: str, target: str, options: Optional[str] = None) -> Dict[str, Any]:
    if not source or not target:
        print("⚠️ Missing 'source' or 'target' parameter")
        return {"success": False, "error": "Missing 'source' or 'target' parameter"}
    source_path = agent.work_directory / source
    if not source_path.exists():
        print(f"⚠️ Source file '{source}' does not exist")
        return {"success": False, "error": f"Source file '{source}' does not exist"}
    ffmpeg_cmd = ["ffmpeg", "-y", "-i", source]
    if options:
        ffmpeg_cmd += options.split()
    ffmpeg_cmd.append(target)
    print(f"🔄 Running ffmpeg command: {' '.join(ffmpeg_cmd)}")
    try:
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            return {"success": True, "message": "Media file processed successfully"}
        return {"success": False, "error": f"ffmpeg execution failed: {result.stderr}"}
    except FileNotFoundError:
        return {"success": False, "error": "ffmpeg was not found. Please install it and ensure it is available in PATH."}
    except Exception as e:
        return {"success": False, "error": f"ffmpeg execution error: {str(e)}"}


def action_apply_unified_patch(agent: Any, file_path: str, patch: str, confirmed: bool = False) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        abs_path = agent._resolve_user_path(str(file_path))
        if not abs_path.exists():
            return {"success": False, "error": f"File '{file_path}' does not exist"}
        if not abs_path.is_file():
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
        source = None
        used_encoding = None
        for enc in encodings:
            try:
                with open(abs_path, "r", encoding=enc, errors="replace") as f:
                    source = f.read()
                used_encoding = enc
                break
            except Exception:
                continue
        if source is None:
            return {"success": False, "error": "Unable to read text file; encoding may be unsupported"}

        newline = "\r\n" if "\r\n" in source else "\n"
        had_trailing_newline = source.endswith("\n") or source.endswith("\r")
        old_lines = source.splitlines()
        patch_lines = str(patch or "").splitlines()
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
            target_idx = src_idx if old_start is None else int(old_start) - 1
            if target_idx < src_idx or target_idx > len(old_lines):
                return {"success": False, "error": f"Hunk start line out of range: {old_start}"}
            if old_start is None:
                anchor = None
                for hl in hunk["lines"]:
                    if hl and hl[0] in (" ", "-"):
                        anchor = hl[1:]
                        break
                if anchor is not None:
                    found_idx = None
                    for probe in range(src_idx, len(old_lines)):
                        if old_lines[probe] == anchor:
                            found_idx = probe
                            break
                    if found_idx is None:
                        return {"success": False, "error": "Patch anchor not found; unable to locate hunk"}
                    target_idx = found_idx
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
            print("Change preview (old ││ new):")
            print("   Markers: '=' unchanged, '-' removed, '+' added")
            for ln in preview_lines:
                print(ln)
        if need_confirm:
            ok = agent._prompt_confirm_yes_no_maybe_always(
                f"⚠️ Confirm applying patch to text file: {abs_path} ?",
                offer_always=False,
                kind="text_file",
            )
            if not ok:
                return {"success": False, "error": "Operation cancelled by user"}
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
            p2 = agent.ai_workspace_dir / file_path
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


def action_diff(agent: Any, file1: str, file2: str, options: Optional[str] = None) -> Dict[str, Any]:
    try:
        file1_path = Path(file1)
        file2_path = Path(file2)
        if not file1_path.exists():
            return {"success": False, "error": f"File does not exist: {file1}"}
        if not file2_path.exists():
            return {"success": False, "error": f"File does not exist: {file2}"}
        if platform.system() == "Windows":
            if shutil.which("diff.exe"):
                full_command = f'diff.exe {options} "{file1}" "{file2}"' if options else f'diff.exe "{file1}" "{file2}"'
                command_type = "diff.exe"
            else:
                full_command = f'cmd /c fc {options} "{file1}" "{file2}"' if options else f'cmd /c fc "{file1}" "{file2}"'
                command_type = "fc"
        else:
            full_command = f'diff {options} "{file1}" "{file2}"' if options else f'diff "{file1}" "{file2}"'
            command_type = "diff"

        process = subprocess.Popen(
            full_command,
            shell=True,
            stdin=sys.stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(agent.work_directory),
        )
        stdout, stderr = process.communicate()
        return_code = process.returncode
        if command_type == "fc":
            if return_code in [0, 1]:
                return {
                    "success": True,
                    "command": full_command,
                    "command_type": command_type,
                    "output": stdout.strip() if stdout else "",
                    "has_differences": return_code == 1,
                    "message": "File comparison completed" + (", differences found" if return_code == 1 else ", files are identical"),
                }
            return {
                "success": False,
                "command": full_command,
                "command_type": command_type,
                "error": stderr.strip() if stderr else f"fc command failed, exit code: {return_code}",
                "output": stdout.strip() if stdout else "",
            }
        if return_code in [0, 1]:
            return {
                "success": True,
                "command": full_command,
                "command_type": command_type,
                "output": stdout.strip() if stdout else "",
                "has_differences": return_code == 1,
                "message": "File comparison completed" + (", differences found" if return_code == 1 else ", files are identical"),
            }
        return {
            "success": False,
            "command": full_command,
            "command_type": command_type,
            "error": stderr.strip() if stderr else f"{command_type} command failed, exit code: {return_code}",
            "output": stdout.strip() if stdout else "",
        }
    except Exception as e:
        return {"success": False, "error": f"File comparison command error: {str(e)}"}
