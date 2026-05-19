import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def action_ffmpeg(agent: Any, source: str, target: str, options: Optional[str] = None) -> Dict[str, Any]:
    if not source or not target:
        print("⚠️ 缺少 source 或 target 参数")
        return {"success": False, "error": "缺少 source 或 target 参数"}
    source_path = agent.work_directory / source
    if not source_path.exists():
        print(f"⚠️ 源文件 '{source}' 不存在")
        return {"success": False, "error": f"源文件 '{source}' 不存在"}
    ffmpeg_cmd = ["ffmpeg", "-y", "-i", source]
    if options:
        ffmpeg_cmd += options.split()
    ffmpeg_cmd.append(target)
    print(f"🔄 正在执行 ffmpeg 命令: {' '.join(ffmpeg_cmd)}")
    try:
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            return {"success": True, "message": "媒体文件处理成功"}
        return {"success": False, "error": f"ffmpeg 执行失败: {result.stderr}"}
    except FileNotFoundError:
        return {"success": False, "error": "未检测到 ffmpeg，请确保已安装并配置好 PATH 环境变量"}
    except Exception as e:
        return {"success": False, "error": f"ffmpeg 执行异常: {str(e)}"}


def action_apply_unified_patch(agent: Any, file_path: str, patch: str, confirmed: bool = False) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        abs_path = agent._resolve_user_path(str(file_path))
        if not abs_path.exists():
            return {"success": False, "error": f"文件 '{file_path}' 不存在"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' 不是一个文件"}
        decision = policy.can_write_path(abs_path, "apply_patch")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        need_confirm = (not confirmed) and (not agent._is_path_under(abs_path, agent.ai_workspace_dir))

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
            return {"success": False, "error": "无法读取文本文件，可能编码不受支持"}

        newline = "\r\n" if "\r\n" in source else "\n"
        had_trailing_newline = source.endswith("\n") or source.endswith("\r")
        old_lines = source.splitlines()
        patch_lines = str(patch or "").splitlines()
        if not patch_lines:
            return {"success": False, "error": "patch 不能为空"}

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
                    return {"success": False, "error": f"非法 hunk 头: {line}"}
                old_start = int(m.group(1)) if m.group(1) else None
            hunk_lines: List[str] = []
            i += 1
            while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                hunk_lines.append(patch_lines[i])
                i += 1
            hunks.append({"old_start": old_start, "lines": hunk_lines})
        if not hunks:
            return {"success": False, "error": "未发现可应用的 hunk（需要 @@ ... @@ 段）"}

        result_lines: List[str] = []
        src_idx = 0
        preview_lines: List[str] = []
        for hunk in hunks:
            old_start = hunk["old_start"]
            target_idx = src_idx if old_start is None else int(old_start) - 1
            if target_idx < src_idx or target_idx > len(old_lines):
                return {"success": False, "error": f"hunk 起始行越界: {old_start}"}
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
                        return {"success": False, "error": "patch 锚点未命中，无法定位 hunk"}
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
                    return {"success": False, "error": "hunk 行格式无效（缺少前缀）"}
                prefix = hl[0]
                text = hl[1:]
                if prefix == " ":
                    if cur >= len(old_lines) or old_lines[cur] != text:
                        return {"success": False, "error": f"patch 上下文不匹配（行 {cur + 1}）"}
                    result_lines.append(old_lines[cur])
                    hunk_old_fragment.append(old_lines[cur])
                    hunk_new_fragment.append(old_lines[cur])
                    cur += 1
                elif prefix == "-":
                    if cur >= len(old_lines) or old_lines[cur] != text:
                        return {"success": False, "error": f"patch 删除行不匹配（行 {cur + 1}）"}
                    hunk_old_fragment.append(old_lines[cur])
                    has_change = True
                    cur += 1
                elif prefix == "+":
                    result_lines.append(text)
                    hunk_new_fragment.append(text)
                    has_change = True
                else:
                    return {"success": False, "error": f"不支持的 hunk 行前缀: {prefix}"}
            src_idx = cur
            if has_change:
                context_before = old_lines[max(0, target_idx - 2):target_idx]
                context_after = old_lines[cur:min(len(old_lines), cur + 2)]
                preview_old = context_before + hunk_old_fragment + context_after
                preview_new = context_before + hunk_new_fragment + context_after
                preview_old_start = max(1, target_idx - len(context_before) + 1)
                preview_new_start = max(1, new_target_idx - len(context_before) + 1)
                preview_lines.extend(
                    agent._format_side_by_side_change_preview(
                        preview_old,
                        preview_new,
                        old_start_line=preview_old_start,
                        new_start_line=preview_new_start,
                    )
                )
        result_lines.extend(old_lines[src_idx:])
        new_text = newline.join(result_lines)
        if had_trailing_newline and len(result_lines) > 0:
            new_text += newline
        if preview_lines:
            print("变更预览（旧 ││ 新）：")
            print("   标记: '=' 未改动, '-' 删除, '+' 新增")
            for ln in preview_lines:
                print(ln)
        if need_confirm:
            ok = agent._prompt_confirm_yes_no_maybe_always(
                f"⚠️ 确认对文本文件应用 patch: {abs_path} ?",
                offer_always=False,
                kind="text_file",
            )
            if not ok:
                return {"success": False, "error": "用户取消了操作"}
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
            "message": f"已成功应用 patch 到 '{resolved.name}'",
        }
    except Exception as e:
        return {"success": False, "error": f"apply_patch 执行失败: {str(e)}"}


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
            return {"success": False, "error": f"图片文件 '{file_path}' 不存在"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' 不是一个文件"}
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"]
        if abs_path.suffix.lower() not in image_exts:
            return {"success": False, "error": f"不支持的文件格式: {abs_path.suffix}"}
        image_task_context = f"图片文件路径: {str(abs_path)}"
        image_user_prompt = prompt if prompt else "请先读取这张图片内容，再继续完成当前任务。"
        analysis = agent.call_ai(image_user_prompt, context=image_task_context, image_path=str(abs_path))
        return {"success": True, "analysis": analysis, "file": str(abs_path)}
    except Exception as e:
        return {"success": False, "error": f"图片读取失败: {str(e)}"}


def action_diff(agent: Any, file1: str, file2: str, options: Optional[str] = None) -> Dict[str, Any]:
    try:
        file1_path = Path(file1)
        file2_path = Path(file2)
        if not file1_path.exists():
            return {"success": False, "error": f"文件不存在: {file1}"}
        if not file2_path.exists():
            return {"success": False, "error": f"文件不存在: {file2}"}
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
                    "message": "文件比较完成" + ("，发现差异" if return_code == 1 else "，文件相同"),
                }
            return {
                "success": False,
                "command": full_command,
                "command_type": command_type,
                "error": stderr.strip() if stderr else f"fc命令执行失败，退出码: {return_code}",
                "output": stdout.strip() if stdout else "",
            }
        if return_code in [0, 1]:
            return {
                "success": True,
                "command": full_command,
                "command_type": command_type,
                "output": stdout.strip() if stdout else "",
                "has_differences": return_code == 1,
                "message": "文件比较完成" + ("，发现差异" if return_code == 1 else "，文件相同"),
            }
        return {
            "success": False,
            "command": full_command,
            "command_type": command_type,
            "error": stderr.strip() if stderr else f"{command_type}命令执行失败，退出码: {return_code}",
            "output": stdout.strip() if stdout else "",
        }
    except Exception as e:
        return {"success": False, "error": f"文件比较命令执行异常: {str(e)}"}
