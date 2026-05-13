import glob
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def action_list_directory(agent: Any, path: Optional[str] = None, file_filter: Optional[str] = None) -> Dict[str, Any]:
    target_path = agent._resolve_user_path(str(path)) if path else agent.work_directory
    if not target_path.exists():
        return {"success": False, "error": f"目录 '{target_path}' 不存在"}
    if not target_path.is_dir():
        return {"success": False, "error": f"'{target_path}' 不是一个目录"}

    items = []
    try:
        for item in target_path.iterdir():
            if file_filter:
                if item.is_file():
                    if not (
                        file_filter.lower() in item.name.lower()
                        or item.suffix.lower() == f".{file_filter.lower()}"
                        or item.name.lower().endswith(f".{file_filter.lower()}")
                    ):
                        continue
                else:
                    if file_filter.lower() not in item.name.lower():
                        continue
            item_info = {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
                "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
            items.append(item_info)
    except PermissionError:
        return {"success": False, "error": "权限不足，无法访问目录"}

    sorted_items = sorted(items, key=lambda x: (x["type"], x["name"]))
    filter_info = f" (过滤: {file_filter})" if file_filter else ""
    return {
        "success": True,
        "path": str(target_path),
        "items": sorted_items,
        "total_files": len([i for i in sorted_items if i["type"] == "file"]),
        "total_dirs": len([i for i in sorted_items if i["type"] == "directory"]),
        "filter": file_filter,
        "filter_info": filter_info,
    }


def action_intelligent_filter(agent: Any, file_list_result: Dict[str, Any], filter_condition: str) -> Dict[str, Any]:
    try:
        files_info = []
        for item in file_list_result.get("items", []):
            info = f"- {item['name']} | {item['type']} | {item['size']} bytes | 修改时间: {item['modified']}"
            files_info.append(info)
        files_text = "\n".join(files_info)
        ai_prompt = f"""
你现在是一个数据分析助手，不是文件管理命令生成器。

任务：从以下文件列表中筛选出符合条件的文件。

筛选条件：{filter_condition}

文件数据：
{files_text}

分析要求：
1. 仔细检查每个文件的信息（名称、大小、时间等）
2. 判断哪些文件符合筛选条件
3. 只返回符合条件的文件名，每行一个
4. 不要返回JSON、不要生成命令、不要添加解释

示例（假设要筛选大于500字节的文件）：
large_document.txt
big_image.jpg

现在开始分析："""

        ai_response = agent.call_ai(ai_prompt)
        if "无符合条件的文件" in ai_response:
            filtered_items = []
        else:
            lines = ai_response.strip().split("\n")
            valid_names = []
            original_items = {item["name"]: item for item in file_list_result.get("items", [])}
            for line in lines:
                line = line.strip()
                if (
                    line
                    and not line.startswith("请")
                    and not line.startswith("根据")
                    and not line.startswith("文件")
                    and not line.startswith("筛选")
                    and not line.startswith("可选")
                    and not line.startswith("示例")
                    and not line.startswith("{")
                    and not line.startswith("```")
                ):
                    clean_name = line.replace("- ", "").replace("* ", "").replace("+ ", "").strip()
                    if clean_name in original_items:
                        valid_names.append(clean_name)
            filtered_items = [original_items[name] for name in valid_names]

        return {
            "success": True,
            "path": file_list_result.get("path", ""),
            "items": filtered_items,
            "total_files": len([i for i in filtered_items if i["type"] == "file"]),
            "total_dirs": len([i for i in filtered_items if i["type"] == "directory"]),
            "filter": filter_condition,
            "filter_info": f" (智能过滤: {filter_condition})",
        }
    except Exception as e:
        return {"success": False, "error": f"智能过滤失败: {str(e)}", "original_result": file_list_result}


def action_change_directory(agent: Any, path: str) -> Dict[str, Any]:
    try:
        if path == "..":
            new_path = agent.work_directory.parent
        elif path == ".":
            new_path = agent.work_directory
        elif path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
            new_path = Path(path)
        else:
            new_path = agent.work_directory / path
        new_path = new_path.resolve()
        if not new_path.exists():
            return {"success": False, "error": f"目录 '{path}' 不存在"}
        if not new_path.is_dir():
            return {"success": False, "error": f"'{path}' 不是一个目录"}
        old_dir = agent.work_directory
        agent.work_directory = new_path
        if agent.input_handler:
            agent.input_handler.update_work_directory(new_path)
        agent._save_current_workspace_position()
        return {
            "success": True,
            "old_directory": str(old_dir),
            "new_directory": str(new_path),
            "message": f"已切换到目录: {new_path}",
        }
    except Exception as e:
        return {"success": False, "error": f"切换目录失败: {str(e)}"}


def action_rename_file(agent: Any, old_name: str, new_name: str) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        old_path = agent.work_directory / old_name
        new_path = agent.work_directory / new_name
        decision = policy.can_modify_path([old_path, new_path], "rename")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        if not old_path.exists():
            return {"success": False, "error": f"文件 '{old_name}' 不存在"}
        if new_path.exists():
            return {"success": False, "error": f"目标文件 '{new_name}' 已存在"}
        old_path.rename(new_path)
        agent._reload_skills_if_workspace_skill_changed([old_path, new_path])
        return {
            "success": True,
            "old_name": old_name,
            "new_name": new_name,
            "message": f"成功将 '{old_name}' 重命名为 '{new_name}'",
        }
    except Exception as e:
        return {"success": False, "error": f"重命名失败: {str(e)}"}


def action_move_file(agent: Any, source: str, destination: str, confirmed: bool = False) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        if "*" in source or "?" in source:
            pattern = str(agent._resolve_user_path(source))
            matched_files = [Path(p) for p in glob.glob(pattern) if Path(p).is_file()]
            if not matched_files:
                return {"success": False, "error": f"未找到匹配的文件: {source}"}
            dest_path = agent._resolve_user_path(destination)
            decision = policy.can_write_path(dest_path, "move")
            if not decision.get("allowed", False):
                return {"success": False, "error": decision.get("error", "")}
            decision = policy.can_modify_path(matched_files, "move")
            if not decision.get("allowed", False):
                return {"success": False, "error": decision.get("error", "")}
            dest_path.mkdir(parents=True, exist_ok=True)
            if not confirmed:
                confirmation = input(f"您确定要批量移动 {len(matched_files)} 个文件到 '{dest_path}' 吗？(y/n): ")
                if confirmation.lower() != "y":
                    return {"success": False, "error": "用户取消了批量移动操作", "confirmation_needed": False}
            moved = []
            for file_path in matched_files:
                target = dest_path / file_path.name
                shutil.move(str(file_path), str(target))
                moved.append(file_path.name)
            changed_paths = matched_files + [dest_path / p.name for p in matched_files]
            agent._reload_skills_if_workspace_skill_changed(changed_paths)
            return {
                "success": True,
                "source": source,
                "destination": str(dest_path),
                "moved_files": moved,
                "message": f"成功批量移动 {len(moved)} 个文件到 '{dest_path}'",
            }

        source_path = agent._resolve_user_path(source)
        dest_path = agent._resolve_user_path(destination)
        decision = policy.can_write_path(dest_path, "move")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        decision = policy.can_modify_path(source_path, "move")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        if not source_path.exists():
            return {"success": False, "error": f"源文件 '{source}' 不存在"}
        if not confirmed:
            confirmation = input(f"您确定要将 '{source}' 移动到 '{dest_path}' 吗？(y/n): ")
            if confirmation.lower() != "y":
                return {"success": False, "error": "用户取消了移动操作", "confirmation_needed": False}
        shutil.move(str(source_path), str(dest_path))
        agent._reload_skills_if_workspace_skill_changed([source_path, dest_path])
        return {
            "success": True,
            "source": source,
            "destination": str(dest_path),
            "message": f"成功将 '{source}' 移动到 '{dest_path}'",
        }
    except Exception as e:
        return {"success": False, "error": f"移动失败: {str(e)}"}


def action_delete_file(agent: Any, file_name: str, confirmed: bool = False) -> Dict[str, Any]:
    policy = agent._get_path_policy()
    if "*" in file_name or "?" in file_name:
        pattern = str((agent.work_directory / file_name).resolve())
        matched_files = [Path(p) for p in glob.glob(pattern)]
        decision = policy.can_modify_path(matched_files, "delete")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        if not matched_files:
            return {"success": False, "error": f"未找到匹配的文件: {file_name}"}
        if not confirmed:
            confirmation = input(f"您确定要批量删除 {len(matched_files)} 个文件/目录吗？(y/n): ")
            if confirmation.lower() != "y":
                return {"success": False, "warning": f"用户拒绝批量删除 '{file_name}', 请跳过这些文件/目录", "confirmation_needed": False}
        results = []
        for file_path in matched_files:
            try:
                if not file_path.exists():
                    results.append({"file": str(file_path), "success": False, "error": "不存在"})
                    continue
                if file_path.is_dir():
                    shutil.rmtree(file_path)
                    results.append({"file": str(file_path), "success": True, "type": "directory", "message": f"成功删除目录 '{file_path.name}'"})
                else:
                    file_path.unlink()
                    results.append({"file": str(file_path), "success": True, "type": "file", "message": f"成功删除文件 '{file_path.name}'"})
            except Exception as e:
                results.append({"file": str(file_path), "success": False, "error": f"删除失败: {str(e)}"})
        all_success = all(r.get("success", False) for r in results)
        if all_success:
            agent._reload_skills_if_workspace_skill_changed(matched_files)
        return {"success": all_success, "deleted": results, "count": len(results)}

    if not confirmed:
        confirmation = input(f"您确定要删除 '{file_name}' 吗？(y/n): ")
        if confirmation.lower() != "y":
            return {"success": False, "warning": f"用户拒绝删除 '{file_name}'，请跳过这个文件/目录", "confirmation_needed": False}
    try:
        file_path = agent.work_directory / file_name
        decision = policy.can_modify_path(file_path, "delete")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        if not file_path.exists():
            return {"success": False, "error": f"文件 '{file_name}' 不存在"}
        if file_path.is_dir():
            shutil.rmtree(file_path)
            agent._reload_skills_if_workspace_skill_changed([file_path])
            return {"success": True, "file_name": file_name, "type": "directory", "message": f"成功删除目录 '{file_name}'"}
        file_path.unlink()
        agent._reload_skills_if_workspace_skill_changed([file_path])
        return {"success": True, "file_name": file_name, "type": "file", "message": f"成功删除文件 '{file_name}'"}
    except Exception as e:
        return {"success": False, "error": f"删除失败: {str(e)}"}


def action_create_directory(agent: Any, dir_name: str) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        dir_path = agent._resolve_user_path(dir_name)
        decision = policy.can_write_path(dir_path, "mkdir")
        if not decision.get("allowed", False):
            return {"success": False, "error": decision.get("error", "")}
        if dir_path.parent.resolve() == agent._workspace_skills_root():
            skill_id = dir_path.name
            if agent._skill_id_exists(skill_id):
                return {"success": False, "error": f"技能 '{skill_id}' 已存在（不可与现有 skill 同名）"}
        if dir_path.exists():
            return {"success": False, "error": f"文件夹 '{dir_name}' 已存在"}
        dir_path.mkdir(parents=True)
        agent._reload_skills_if_workspace_skill_changed([dir_path])
        return {
            "success": True,
            "dir_name": dir_name,
            "full_path": str(dir_path),
            "message": f"成功创建文件夹 '{dir_name}'（路径: {dir_path}）",
        }
    except Exception as e:
        return {"success": False, "error": f"创建文件夹失败: {str(e)}"}


def action_get_file_info(agent: Any, file_name: str) -> Dict[str, Any]:
    try:
        file_path = agent.work_directory / file_name
        if not file_path.exists():
            return {"success": False, "error": f"文件 '{file_name}' 不存在"}
        stat = file_path.stat()
        return {
            "success": True,
            "name": file_name,
            "type": "directory" if file_path.is_dir() else "file",
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "permissions": oct(stat.st_mode)[-3:],
            "full_path": str(file_path),
        }
    except Exception as e:
        return {"success": False, "error": f"获取文件信息失败: {str(e)}"}


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


def action_summarize_file(agent: Any, file_path: str, max_lines: int = 50) -> Dict[str, Any]:
    try:
        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            abs_path = agent.work_directory / file_path
        if not abs_path.exists():
            return {"success": False, "error": f"文件 '{file_path}' 不存在"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' 不是一个文件"}
        stat = abs_path.stat()
        text_exts = [".txt", ".md", ".json", ".py", ".csv", ".log", ".ini", ".yaml", ".yml"]
        if abs_path.suffix.lower() not in text_exts and stat.st_size > 1024 * 1024:
            return {"success": False, "error": "仅支持文本文件或小于1MB的文件总结"}
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append("... (内容过长已截断)")
                        break
                    lines.append(line.rstrip("\n"))
                content = "\n".join(lines)
        except Exception as e:
            return {"success": False, "error": f"无法读取文件内容: {str(e)}"}
        prompt = f"请用中文简要总结以下文件内容（200字以内）：\n{content}"
        summary = agent.call_ai(prompt)
        return {"success": True, "summary": summary, "file": str(abs_path)}
    except Exception as e:
        return {"success": False, "error": f"总结文件失败: {str(e)}"}


def action_create_text_file(agent: Any, filename: str, content: str, confirmed: bool = False, overwrite: bool = False) -> Dict[str, Any]:
    if not filename or content is None:
        return {"success": False, "error": "缺少文件名或内容"}
    filename_s = str(filename).strip()
    if not filename_s:
        return {"success": False, "error": "无效的文件名"}
    file_path = agent._resolve_user_path(filename_s)
    policy = agent._get_path_policy()
    decision = policy.can_write_path(file_path, "text_file")
    if not decision.get("allowed", False):
        return {"success": False, "error": decision.get("error", "")}
    safe_name = file_path.name
    existed_before = file_path.exists()
    if existed_before and not overwrite:
        return {
            "success": False,
            "error": f"文件 '{safe_name}' 已存在。若需覆盖，请在 JSON 的 params 中设置 \"overwrite\": true。",
        }
    if existed_before and overwrite:
        return {
            "success": False,
            "error": (
                f"检测到目标文件 '{safe_name}' 已存在。为避免覆盖丢失，请不要使用 text_file 覆盖现有文本文件。"
                "请改用 edit_text（单段按行修改）或 apply_patch（多段修改）。"
            ),
        }
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"success": False, "error": f"创建父目录失败: {str(e)}"}
    print(f"请求创建文本文件: {safe_name} → {file_path}")
    print(f"内容:\n{content}")
    if agent._is_path_under(file_path, agent.ai_workspace_dir):
        confirmed = True
    if not confirmed:
        ok = agent._prompt_confirm_yes_no_maybe_always(
            f"⚠️ 确认创建文本文件: {file_path} ?",
            offer_always=False,
            kind="text_file",
        )
        if not ok:
            return {"success": False, "error": "用户取消了操作"}
    try:
        with open(file_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(content)
        resolved = file_path.resolve()
        agent._ai_created_path_keys.add(agent._ephemeral_path_key(resolved))
        agent._reload_skills_if_workspace_skill_changed([resolved])
        verb = "覆盖写入" if overwrite and existed_before else "创建"
        return {"success": True, "filename": safe_name, "full_path": str(resolved), "message": f"成功{verb}文本文件 '{safe_name}'（路径: {resolved}）"}
    except Exception as e:
        return {"success": False, "error": f"创建文本文件失败: {str(e)}"}


def action_read_file(
    agent: Any,
    file_path: str,
    max_lines: Optional[int] = None,
    start_line: Optional[int] = None,
    line_count: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        abs_path = agent._resolve_user_path(str(file_path))
        if not abs_path.exists():
            return {"success": False, "error": f"文件 '{file_path}' 不存在"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' 不是一个文件"}
        stat = abs_path.stat()
        text_exts = [".txt", ".md", ".json", ".py", ".csv", ".log", ".ini", ".yaml", ".yml"]
        if abs_path.suffix.lower() not in text_exts and stat.st_size > 1024 * 1024:
            return {"success": False, "error": "仅支持文本文件或小于1MB的文件读取"}
        encodings = ["utf-8", "gbk", "gb2312", "utf-16", "latin1"]
        content = None
        effective_start = int(start_line) if start_line is not None else 1
        if effective_start <= 0:
            effective_start = 1
        requested_count = line_count if line_count is not None else max_lines
        effective_count = int(requested_count) if requested_count is not None else 100
        if effective_count <= 0:
            effective_count = 100
        used_count = effective_count
        read_plan = [effective_count]
        if requested_count is None:
            for candidate in (300, 800):
                if candidate > read_plan[-1]:
                    read_plan.append(candidate)
        for enc in encodings:
            try:
                for plan_count in read_plan:
                    with open(abs_path, "r", encoding=enc, errors="replace") as f:
                        lines = []
                        truncated = False
                        end_line = effective_start + plan_count - 1
                        for i, line in enumerate(f, start=1):
                            if i < effective_start:
                                continue
                            if i > end_line:
                                truncated = True
                                lines.append("... (内容过长已截断)")
                                break
                            lines.append(f"{i}|{line.rstrip(chr(13)+chr(10))}")
                        content = "\n".join(lines)
                        used_count = plan_count
                    if requested_count is None and truncated and plan_count < 800:
                        continue
                    break
                break
            except Exception:
                continue
        if content is None:
            return {"success": False, "error": "无法读取文件内容，可能编码不受支持"}
        return {
            "success": True,
            "file": str(abs_path),
            "content": content,
            "start_line_used": effective_start,
            "line_count_used": used_count,
            "auto_expand_line_count": requested_count is None,
        }
    except Exception as e:
        return {"success": False, "error": f"读取文件失败: {str(e)}"}


def action_edit_text_file(
    agent: Any,
    file_path: str,
    start_line: int,
    line_span: int,
    operation: str,
    content: Optional[str] = None,
    confirmed: bool = False,
) -> Dict[str, Any]:
    try:
        policy = agent._get_path_policy()
        operation_s = str(operation or "").strip().lower()
        if operation_s not in ("insert", "delete", "replace"):
            return {"success": False, "error": "operation 仅支持 insert/delete/replace"}
        if operation_s in ("insert", "replace") and content is None:
            return {"success": False, "error": f"{operation_s} 操作需要 content 参数"}
        abs_path = agent._resolve_user_path(str(file_path))
        if not abs_path.exists():
            return {"success": False, "error": f"文件 '{file_path}' 不存在"}
        if not abs_path.is_file():
            return {"success": False, "error": f"'{file_path}' 不是一个文件"}
        decision = policy.can_write_path(abs_path, "edit_text")
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
        total = len(old_lines)
        s = int(start_line)
        if s <= 0:
            return {"success": False, "error": "start_line 必须 >= 1"}
        span = int(line_span)
        if span < 0:
            return {"success": False, "error": "line_span 必须 >= 0"}

        new_lines = list(old_lines)
        inserted_lines = [] if content is None else str(content).splitlines()
        if operation_s == "insert":
            if s > total + 1:
                return {"success": False, "error": f"insert 起始行超出范围，最大允许 {total + 1}"}
            idx = s - 1
            new_lines[idx:idx] = inserted_lines
        elif operation_s == "delete":
            if s > total:
                return {"success": False, "error": f"delete 起始行超出范围，当前总行数 {total}"}
            if span == 0:
                span = 1
            end = min(total, s - 1 + span)
            del new_lines[s - 1 : end]
        else:
            if s > total:
                return {"success": False, "error": f"replace 起始行超出范围，当前总行数 {total}"}
            if span == 0:
                span = 1
            end = min(total, s - 1 + span)
            new_lines[s - 1 : end] = inserted_lines

        new_text = newline.join(new_lines)
        if had_trailing_newline and len(new_lines) > 0:
            new_text += newline

        ctx_from = max(1, s - 1)
        ctx_to_old = min(total, s + max(span, 1))
        old_fragment = old_lines[ctx_from - 1 : ctx_to_old]
        affected_new = len(inserted_lines) if operation_s != "delete" else 0
        ctx_to_new = min(len(new_lines), s + max(affected_new, 1))
        new_fragment = new_lines[ctx_from - 1 : ctx_to_new]
        preview_lines = agent._format_side_by_side_change_preview(
            old_fragment, new_fragment, old_start_line=ctx_from, new_start_line=ctx_from
        )
        if preview_lines:
            print("变更预览（旧 ││ 新）：")
            print("   标记: '=' 未改动, '-' 删除, '+' 新增")
            for ln in preview_lines:
                print(ln)
        if need_confirm:
            ok = agent._prompt_confirm_yes_no_maybe_always(
                f"⚠️ 确认修改文本文件: {abs_path} ?",
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
            "operation": operation_s,
            "start_line": s,
            "line_span": span,
            "original_line_count": total,
            "updated_line_count": len(new_lines),
            "change_preview": preview_lines,
            "message": f"已对文件 '{resolved.name}' 执行 {operation_s} 操作",
        }
    except Exception as e:
        return {"success": False, "error": f"edit_text 执行失败: {str(e)}"}


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
            cur = target_idx
            hunk_old_fragment: List[str] = []
            hunk_new_fragment: List[str] = []
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
                    cur += 1
                elif prefix == "+":
                    result_lines.append(text)
                    hunk_new_fragment.append(text)
                else:
                    return {"success": False, "error": f"不支持的 hunk 行前缀: {prefix}"}
            src_idx = cur
            if hunk_old_fragment or hunk_new_fragment:
                preview_lines.extend(
                    agent._format_side_by_side_change_preview(
                        hunk_old_fragment,
                        hunk_new_fragment,
                        old_start_line=max(1, target_idx + 1),
                        new_start_line=max(1, target_idx + 1),
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
