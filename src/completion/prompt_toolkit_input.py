#!/usr/bin/env python3
"""
基于 prompt_toolkit 的跨平台输入处理模块
提供稳定的 Tab 补全、状态栏渲染和中文输入支持
"""

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_WIN_DRIVE_BANG = re.compile(r"^([A-Za-z]:)(/.*)?$")

from .builtin_slash_commands import slash_builtin_completions

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import (
        Completer,
        Completion,
        CompleteEvent,
        get_common_complete_suffix,
    )
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.styles import Style
    try:
        from prompt_toolkit.cursor_shapes import CursorShape
        from prompt_toolkit.cursor_shapes import SimpleCursorShapeConfig
    except Exception:
        CursorShape = None  # type: ignore[assignment]
        SimpleCursorShapeConfig = None  # type: ignore[assignment]
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False


def _is_vscode_terminal() -> bool:
    return str(os.environ.get("TERM_PROGRAM", "") or "").strip().lower() == "vscode"


def _attach_blink_after_render_hook(
    session,
    status_provider: Optional[Callable[[], str]] = None,
) -> None:
    """
    `Application.after_render` is an `Event` object — handlers must be added via
    `+=`/`add_handler` rather than reassignment, otherwise `Event.fire()` breaks.
    """
    if session is None:
        return
    app = getattr(session, "app", None)
    if app is None:
        return

    # `desired`: 当前渲染周期希望显示的状态栏文本（空串表示隐藏）
    state = {"visible": False, "text": "", "desired": ""}

    def _draw_overlay_line(output, text: str) -> None:
        # 使用保存/恢复光标，仅在提示符下方两行写入状态栏。
        output.write_raw("\x1b7")
        output.write_raw("\x1b[2B\r")
        output.write_raw("\x1b[2K")
        if text:
            output.write_raw(text)
        output.write_raw("\x1b8")

    def _on_before_render(_app) -> None:
        try:
            desired = str(status_provider() or "") if callable(status_provider) else ""
            state["desired"] = desired
            output = getattr(_app, "output", None)
            if (
                desired == ""
                and state["visible"]
                and output is not None
                and hasattr(output, "write_raw")
                and hasattr(output, "flush")
            ):
                _draw_overlay_line(output, "")
                output.flush()
                state["visible"] = False
                state["text"] = ""
        except Exception:
            pass

    def _on_after_render(_app) -> None:
        try:
            output = getattr(_app, "output", None)
            if output is not None and hasattr(output, "write_raw") and hasattr(output, "flush"):
                try:
                    desired = str(state.get("desired") or "")
                    # Always redraw when desired text exists: external prints (e.g.
                    # /chat switch history) may scroll previous overlay away, while
                    # state still says "visible".
                    if desired:
                        _draw_overlay_line(output, desired)
                        state["visible"] = True
                        state["text"] = desired
                except Exception:
                    pass
                if not _is_vscode_terminal():
                    output.write_raw("\x1b[?12h\x1b[?25h")
                output.flush()
                return
        except Exception:
            pass
        try:
            if not _is_vscode_terminal():
                sys.stdout.write("\x1b[?12h\x1b[?25h")
                sys.stdout.flush()
        except Exception:
            pass

    try:
        evt_before = getattr(app, "before_render", None)
        if evt_before is not None and hasattr(evt_before, "add_handler"):
            evt_before.add_handler(_on_before_render)
        evt = getattr(app, "after_render", None)
        if evt is not None and hasattr(evt, "add_handler"):
            evt.add_handler(_on_after_render)
    except Exception:
        pass


def _get_output_columns(session: Any, default: int = 80) -> int:
    """
    获取 prompt_toolkit 当前输出窗口列数（随窗口变化）。
    """
    try:
        output = getattr(session, "output", None)
        if output is None:
            app = getattr(session, "app", None)
            output = getattr(app, "output", None) if app is not None else None
        if output is not None and hasattr(output, "get_size"):
            size = output.get_size()
            cols = int(getattr(size, "columns", 0) or 0)
            if cols > 0:
                return cols
    except Exception:
        pass
    return int(default or 80)


def _sanitize_prompt_pollution(text: str, work_directory: Optional[Path] = None) -> str:
    """
    Best-effort cleanup for rare cases where prompt fragments leak into input.
    Example: 'D:\\tmp\\builds>>>>/exit' -> '/exit'
    """
    s = str(text or "")
    if not s:
        return s
    cleaned = s.replace("\r", "").strip()
    if not cleaned:
        return ""

    wd = str(work_directory or "").strip()
    if wd:
        prompt_prefix = f"{wd}>"
        while cleaned.startswith(prompt_prefix):
            cleaned = cleaned[len(prompt_prefix):].lstrip()

    if re.match(r"^>{2,}\s*\S", cleaned):
        cleaned = re.sub(r"^>+\s*", "", cleaned, count=1)

    return cleaned


class FileCompleter(Completer):
    """文件补全器"""
    
    def __init__(
        self,
        work_directory: Path,
        slash_skill_commands: Optional[List[str]] = None,
        slash_mcp_commands: Optional[List[str]] = None,
        slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
    ):
        self.work_directory = work_directory
        self.slash_skill_commands = slash_skill_commands or []
        self.slash_mcp_commands = slash_mcp_commands or []
        self.slash_dynamic_rules = slash_dynamic_rules or []

    def _resolve_dynamic_groups(self) -> List[Tuple[str, List[str]]]:
        """
        Unified delayed-dynamic completion groups.
        Declarative source: `slash_dynamic_rules`.
        """
        out: List[Tuple[str, List[str]]] = []

        def _to_groups(raw: Any) -> None:
            if not isinstance(raw, list):
                return
            for item in raw:
                if not (isinstance(item, tuple) and len(item) == 2):
                    continue
                trigger, cands = item
                trig = str(trigger or "")
                if not trig:
                    continue
                if not isinstance(cands, list):
                    continue
                normalized = [str(x) for x in cands if isinstance(x, str)]
                out.append((trig, normalized))

        rules = self.slash_dynamic_rules or []
        if isinstance(rules, list) and rules:
            for rule in rules:
                if not isinstance(rule, dict):
                    continue

                groups_provider = rule.get("groups_provider")
                if callable(groups_provider):
                    try:
                        _to_groups(groups_provider() or [])
                    except Exception:
                        pass
                    continue

                groups_raw = rule.get("groups")
                if isinstance(groups_raw, list):
                    _to_groups(groups_raw)
                    continue

                trigger = str(rule.get("trigger") or "")
                if not trigger:
                    continue
                candidates: List[str] = []
                cands_provider = rule.get("candidates_provider")
                if callable(cands_provider):
                    try:
                        raw = cands_provider() or []
                        if isinstance(raw, list):
                            candidates = [str(x) for x in raw if isinstance(x, str)]
                    except Exception:
                        candidates = []
                else:
                    raw = rule.get("candidates", [])
                    if isinstance(raw, list):
                        candidates = [str(x) for x in raw if isinstance(x, str)]
                out.append((trigger, candidates))
            return out
        return []

    @staticmethod
    def _slash_fragment_for_completion(text: str) -> Tuple[int, str]:
        """
        Return the slash-command fragment being edited at cursor tail.
        Matches either:
        - line starts with '/...' (supports spaces in slash command)
        - or last token after whitespace is '/...'
        Returns (index_of_slash, fragment_from_slash_to_cursor), or (-1, '').
        """
        # Prefer the trailing slash token near cursor.
        # Token may include nested slashes, e.g. "/mcp/windbg/" or "/mcp/windbg/list_xxx".
        # Strict boundary: if there is a previous character, it must be whitespace.
        m = re.search(r"/[^\s]*$", text)
        if m:
            frag = m.group(0) or ""
            if frag.startswith("/"):
                slash_idx = m.start()
                if slash_idx > 0:
                    prev = text[slash_idx - 1]
                    is_boundary = prev.isspace()
                    if is_boundary:
                        return slash_idx, frag
                else:
                    return slash_idx, frag

        # Full-line slash built-in command (e.g. "/mcp ", "/knowledge search q")
        stripped = text.lstrip()
        if stripped.startswith("/"):
            slash_idx = len(text) - len(stripped)
            return slash_idx, text[slash_idx:]
        return -1, ""

    @staticmethod
    def _bang_fragment_for_completion(text: str) -> Tuple[int, str]:
        """
        Return the '!...' path fragment at cursor tail (workspace-relative path after '!').
        Matches either:
        - line starts with '!...'
        - or last token after whitespace is '!...'
        Returns (index_of_bang, fragment_from_bang_to_cursor), or (-1, '').
        """
        stripped = text.lstrip()
        if stripped.startswith("!"):
            bang_idx = len(text) - len(stripped)
            return bang_idx, text[bang_idx:]
        m = re.search(r"(^|\s)(![^\s]*)$", text)
        if not m:
            return -1, ""
        frag = m.group(2) or ""
        if not frag.startswith("!"):
            return -1, ""
        bang_idx = len(text) - len(frag)
        return bang_idx, frag

    @staticmethod
    def _dynamic_completion_display(
        slash_part: str,
        candidate: str,
        delayed_dynamic_groups: List[Tuple[str, List[str]]],
    ) -> str:
        """
        For delayed dynamic slash completions, display only the incremental part
        (e.g. "/workspace switch code-review" -> "code-review" when user already
        typed "/workspace switch ").
        """
        sp = str(slash_part or "")
        c = str(candidate or "")
        if not sp or not c:
            return c
        sp_l = sp.lower()
        c_l = c.lower()
        for trigger_prefix, _ in delayed_dynamic_groups or []:
            trig = str(trigger_prefix or "")
            if not trig:
                continue
            trig_l = trig.lower()
            if not sp_l.startswith(trig_l):
                continue
            if not c_l.startswith(trig_l):
                continue
            rest = c[len(trig):]
            # For '/mcp/' second-layer completion, display only server name.
            if trig_l == "/mcp/" and rest.endswith("/"):
                rest = rest[:-1]
            # If no visible incremental part, keep full display text.
            return rest if rest else c
        return c

    def get_completions(self, document, complete_event):
        """获取补全选项"""
        text = document.text_before_cursor

        # Token-based path completion from the last whitespace boundary.
        # This enables completion while typing commands like: "open src/win"
        # where only the trailing token should be replaced.
        token_start, token = self._extract_last_token(text)

        # Windows: '!' + workspace-relative path -> path completion only
        if os.name == "nt":
            bidx, bang_part = self._bang_fragment_for_completion(text)
            if bidx >= 0 and bang_part:
                path_matches = self._get_workspace_path_completions_for_bang(bang_part)
                if path_matches:
                    spos = -len(bang_part)
                    seen = set()
                    for mc in path_matches:
                        if mc in seen:
                            continue
                        seen.add(mc)
                        yield Completion(
                            mc,
                            start_position=spos,
                            display=self._path_leaf_name(mc),
                        )
                    return

        # Slash built-ins: always provide command completion when applicable.
        # Special-case lone "/" on non-Windows to avoid enumerating root files.
        idx, slash_part = self._slash_fragment_for_completion(text)
        if idx >= 0 and slash_part:
            delayed_groups = self._resolve_dynamic_groups()
            builtin_matches = slash_builtin_completions(
                slash_part,
                dynamic_commands=(
                    self.slash_skill_commands
                    + self.slash_mcp_commands
                ),
                delayed_dynamic_groups=delayed_groups,
            )
            if builtin_matches:
                # When delayed dynamic completion is active, hide the trigger
                # command itself from menu (e.g. "/workspace switch ").
                active_trigger_norms = {
                    str(trigger or "").rstrip().lower()
                    for trigger, _ in delayed_groups
                    if str(trigger or "").strip()
                    and slash_part.lower().startswith(str(trigger or "").lower())
                }
                if active_trigger_norms:
                    builtin_matches = [
                        mc
                        for mc in builtin_matches
                        if str(mc or "").rstrip().lower() not in active_trigger_norms
                    ]
                if builtin_matches:
                    spos = -len(slash_part)
                    seen = set()
                    all_delayed_groups = delayed_groups
                    for mc in builtin_matches:
                        if mc in seen:
                            continue
                        seen.add(mc)
                        display_text = self._dynamic_completion_display(
                            slash_part, mc, all_delayed_groups
                        )
                        yield Completion(
                            mc,
                            start_position=spos,
                            display=display_text,
                        )
                    return
            if slash_part == "/":
                return
            # Keep Windows behavior: slash prefix is command namespace, not file path.
            if os.name == "nt":
                return

        # Generic token-based file/path completion (from last whitespace boundary)
        if token:
            if "/" in token or "\\" in token:
                token_matches = self._get_path_completions(token)
            else:
                token_matches = self._get_local_completions(token)

            if token_matches:
                seen = set()
                for mc in token_matches:
                    if mc in seen:
                        continue
                    seen.add(mc)
                    if "/" in token or "\\" in token:
                        yield Completion(
                            mc,
                            start_position=-len(token),
                            display=self._path_leaf_name(mc),
                        )
                    else:
                        yield Completion(mc, start_position=-len(token))
                return
        
        # If input becomes empty, hide completion menu.
        if not text or text.strip() == "":
            return
        
        # 智能检测文件名部分
        file_part, prefix, suffix = self._extract_file_part(text)
        
        # 获取文件补全选项
        if '/' in file_part or '\\' in file_part:
            # 路径补全
            completions = self._get_path_completions(file_part)
        else:
            # 当前目录下的文件/文件夹补全
            completions = self._get_local_completions(file_part)
        
        # 确保每个补全选项只出现一次
        seen = set()
        for completion in completions:
            if completion not in seen:
                seen.add(completion)
                # 构建完整的补全结果
                full_completion = prefix + completion + suffix
                yield Completion(full_completion, start_position=-len(text))
    
    def _extract_file_part(self, text: str) -> tuple:
        """
        智能提取输入文本中的文件名部分
        Args:
            text: 输入文本
        Returns:
            (file_part, prefix, suffix) - 文件名部分、前缀、后缀
        """
        # Path completion: extract path part so "cd C:\Users\re" is completed as path "C:\Users\re"
        if '/' in text or '\\' in text:
            stripped = text.strip()
            if stripped.lower().startswith("cd ") or stripped.lower().startswith("cd\t"):
                idx = text.lower().find("cd ")
                if idx < 0:
                    idx = text.lower().find("cd\t")
                if idx >= 0:
                    prefix = text[: idx + 3]
                    path_part = text[idx + 3 :].strip()
                    return path_part, prefix, ""
            return text, "", ""
        
        # 获取当前目录的所有文件名
        try:
            current_files = [item.name for item in self.work_directory.iterdir() if not item.name.startswith('.')]
        except Exception:
            current_files = []
        
        # 智能检测：查找可能匹配当前目录文件名的部分
        words = text.split()
        if not words:
            return "", "", ""
        
        # 策略1：检查最后一个词是否匹配文件名开头
        last_word = words[-1]
        for filename in current_files:
            if filename.lower().startswith(last_word.lower()):
                prefix = " ".join(words[:-1])
                if prefix:
                    prefix += " "
                return last_word, prefix, ""
        
        # 策略2：检查最后几个词组合是否匹配文件名
        for i in range(len(words), 0, -1):
            candidate = " ".join(words[i-1:])
            for filename in current_files:
                if filename.lower().startswith(candidate.lower()):
                    prefix = " ".join(words[:i-1])
                    if prefix:
                        prefix += " "
                    return candidate, prefix, ""
        
        # 策略3：检查是否包含完整的文件名（带扩展名）
        for filename in current_files:
            if filename.lower() in text.lower():
                # 找到文件名在文本中的位置
                filename_lower = filename.lower()
                text_lower = text.lower()
                start_pos = text_lower.find(filename_lower)
                if start_pos != -1:
                    prefix = text[:start_pos]
                    suffix = text[start_pos + len(filename):]
                    return filename, prefix, suffix
        
        # 策略4：如果没有找到匹配，使用最后一个词作为候选
        prefix = " ".join(words[:-1])
        if prefix:
            prefix += " "
        return last_word, prefix, ""
    
    def _get_directory_contents(self) -> List[str]:
        """获取当前目录的内容"""
        try:
            items = []
            for item in self.work_directory.iterdir():
                # 只显示可见文件（不以.开头）
                if not item.name.startswith('.'):
                    items.append(item.name)
            return sorted(items)
        except Exception:
            return []
    
    def _get_local_completions(self, text: str) -> List[str]:
        """获取当前目录下的本地补全"""
        try:
            # Avoid noisy completion when fragment is exactly "."
            if text == ".":
                return []
            matches = []
            for item in self.work_directory.iterdir():
                if item.name.lower().startswith(text.lower()):
                    matches.append(item.name)
            
            # 如果没有找到匹配项，尝试智能补全
            if not matches and text:
                matches = self._smart_local_completion(text)
            
            # 如果只有一个匹配项，直接返回
            if len(matches) == 1:
                return matches
            
            # 如果有多个匹配项，返回所有匹配项供用户选择
            return sorted(matches)
        except Exception:
            return []
    
    def _smart_local_completion(self, text: str) -> List[str]:
        """
        智能本地补全，包括自动添加常见文件扩展名
        Args:
            text: 要补全的文本
        Returns:
            智能补全的文件/文件夹名列表
        """
        matches = []

        # Avoid fuzzy matching all dot-containing filenames for a single dot fragment.
        if text == ".":
            return matches
        
        # 常见文件扩展名
        common_extensions = ['.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md', '.log', '.ini', '.cfg', '.conf']
        
        # 1. 尝试直接匹配（不区分大小写）
        for item in self.work_directory.iterdir():
            if item.name.lower().startswith(text.lower()):
                matches.append(item.name)
        
        # 2. 如果没有直接匹配，尝试添加常见扩展名
        if not matches:
            for ext in common_extensions:
                potential_file = self.work_directory / (text + ext)
                if potential_file.exists() and potential_file.is_file():
                    matches.append(text + ext)
        
        # 3. 如果还是没有，尝试模糊匹配（包含子字符串）
        if not matches:
            for item in self.work_directory.iterdir():
                if text.lower() in item.name.lower():
                    matches.append(item.name)
        
        # 4. 如果文件名部分看起来像是不完整的扩展名，尝试补全
        if not matches and '.' in text:
            # 例如：输入"test.t"时，尝试匹配"test.txt"
            base_name, partial_ext = text.rsplit('.', 1)
            for ext in common_extensions:
                if ext.startswith('.' + partial_ext):
                    potential_file = self.work_directory / (base_name + ext)
                    if potential_file.exists() and potential_file.is_file():
                        matches.append(base_name + ext)
        
        return matches
    
    def _get_root_directory_completions(self, separator: str, file_part: str = "") -> List[str]:
        """
        获取根目录补全
        Args:
            separator: 路径分隔符
            file_part: 文件名部分（可选）
        Returns:
            根目录下的文件/文件夹列表
        """
        try:
            # Drive-root completion: use current drive root.
            current_drive = Path.cwd().anchor  # 例如 'C:\\'
            root_dir = Path(current_drive)
            
            if not root_dir.exists() or not root_dir.is_dir():
                return []
            
            matches = []
            try:
                for item in root_dir.iterdir():
                    # 跳过隐藏文件和系统文件
                    if item.name.startswith('.'):
                        continue
                    
                    # 如果指定了file_part，只返回匹配的文件
                    if file_part and not item.name.lower().startswith(file_part.lower()):
                        continue
                    
                    # 构建反斜杠风格路径
                    path = f"\\{item.name}"
                    
                    matches.append(path)
                    
            except PermissionError:
                # 如果没有权限访问根目录，返回空列表
                return []
            
            return sorted(matches)
        except Exception:
            return []

    @staticmethod
    def _drive_letter_bang_base(rel: str) -> Optional[Tuple[Path, str]]:
        """
        If rel is a Windows path starting with X: (absolute drive), return (directory to
        list, filename prefix). pathlib's (workdir / 'd:') is not drive root when cwd is
        on the same letter — use Path('d:\\') instead.
        """
        norm = rel.replace("\\", "/")
        m = _WIN_DRIVE_BANG.match(norm)
        if not m:
            return None
        drive = m.group(1)
        rest_raw = m.group(2)
        tail = rest_raw.lstrip("/") if rest_raw else ""
        root = Path(drive + "\\")
        if not tail:
            return root, ""
        if "/" in tail:
            dir_rel, file_part = tail.rsplit("/", 1)
            base_dir = root / dir_rel.replace("/", "\\")
        else:
            base_dir = root
            file_part = tail
        return base_dir, file_part

    def _get_workspace_path_completions_for_bang(self, bang_part: str) -> List[str]:
        """
        Build workspace-relative path completions for leading '!...'.
        Example: '!src/p' -> '!src\\completion\\prompt_toolkit_input.py'
        On Windows, '!d:\\' lists D:\\ root (not the workspace when it is on D:).
        """
        try:
            if not bang_part.startswith("!"):
                return []
            rel = bang_part[1:]
            # '!\\...' or '!//' style: not workspace-relative
            if rel.startswith("\\") or rel.startswith("/"):
                return []
            # Lone "!" only: no completions (avoid popping up the full workspace list)
            if not rel:
                return []

            if os.name == "nt":
                nd = rel.replace("\\", "/")
                # Bare "x:" only — do not list drive root until user types !x:\ or !x:/
                if re.fullmatch(r"[A-Za-z]:", nd):
                    return []
                drive_pair = self._drive_letter_bang_base(rel)
                if drive_pair is not None:
                    base_dir, file_part = drive_pair
                    if not base_dir.exists() or not base_dir.is_dir():
                        return []
                    matches = []
                    for item in base_dir.iterdir():
                        if item.name.startswith("."):
                            continue
                        if file_part and not item.name.lower().startswith(file_part.lower()):
                            continue
                        candidate = "!" + os.path.normpath(str((base_dir / item.name)))
                        matches.append(candidate)
                    return sorted(matches)

            normalized = rel.replace("\\", "/")
            if "/" in normalized:
                dir_part, file_part = normalized.rsplit("/", 1)
                base_dir = (self.work_directory / dir_part).resolve()
            else:
                dir_part, file_part = "", normalized
                base_dir = self.work_directory

            if not base_dir.exists() or not base_dir.is_dir():
                return []

            matches = []
            for item in base_dir.iterdir():
                if item.name.startswith("."):
                    continue
                if file_part and not item.name.lower().startswith(file_part.lower()):
                    continue
                if dir_part:
                    win_dir = dir_part.replace("/", "\\")
                    candidate = f"!{win_dir}\\{item.name}"
                else:
                    candidate = f"!{item.name}"
                matches.append(candidate)
            return sorted(matches)
        except Exception:
            return []

    @staticmethod
    def _extract_last_token(text: str) -> Tuple[int, str]:
        """
        Extract the trailing token after the last whitespace before cursor.
        Returns (start_index, token), or (-1, "") when unavailable.
        """
        if not text:
            return -1, ""
        m = re.search(r"(^|\s)([^\s]+)$", text)
        if not m:
            return -1, ""
        token = m.group(2) or ""
        if not token:
            return -1, ""
        start = len(text) - len(token)
        return start, token

    @staticmethod
    def _path_leaf_name(candidate: str) -> str:
        """Display only leaf name for path-like completion candidates."""
        cleaned = candidate.rstrip("\\/")
        if not cleaned:
            return candidate
        return cleaned.replace("/", "\\").split("\\")[-1]
    
    def _get_path_completions(self, text: str) -> List[str]:
        """获取路径补全"""
        try:
            # Normalize to backslash separator for workspace-style suggestions.
            text = text.replace("/", "\\")
            separator = '\\'
            
            # Root trigger: one or more pure separators should list root entries.
            if text and set(text) == {"\\"}:
                return self._get_root_directory_completions(separator)
            
            parts = text.split(separator)
            if len(parts) == 1:
                return self._get_local_completions(text)
            
            # 构建目录路径
            dir_part = separator.join(parts[:-1])
            file_part = parts[-1]
            
            # 特殊处理：如果dir_part为空，表示根目录
            if dir_part == '':
                return self._get_root_directory_completions(separator, file_part)
            
            # 解析目录路径
            if dir_part.startswith("\\") or (len(dir_part) > 1 and dir_part[1] == ':'):
                # Absolute path with drive letter:
                # - leading "\" is drive-root relative (map to current drive root)
                # - "d:" should be normalized to "d:\\" for drive root
                if dir_part.startswith("\\"):
                    current_drive = Path.cwd().anchor  # e.g. "D:\\"
                    base_dir = (Path(current_drive) / dir_part.lstrip("\\")).resolve()
                elif len(dir_part) == 2 and dir_part[0].isalpha() and dir_part[1] == ':':
                    base_dir = Path(dir_part + "\\")
                else:
                    base_dir = Path(dir_part)
            else:
                # 相对路径
                base_dir = self.work_directory / dir_part
            
            if not base_dir.exists() or not base_dir.is_dir():
                return []
            
            # 在指定目录下查找匹配的文件/文件夹
            matches = []
            for item in base_dir.iterdir():
                if item.name.lower().startswith(file_part.lower()):
                    # 构建反斜杠风格路径
                    relative_path = f"{dir_part}\\{item.name}"
                    
                    # Only append separator for directories when input ends with separator
                    if text.endswith(separator) and item.is_dir():
                        matches.append(relative_path + separator)
                    else:
                        matches.append(relative_path)
            
            # 如果没有找到匹配项，尝试智能补全
            if not matches and file_part:
                smart_matches = self._smart_path_completion(base_dir, file_part, separator, dir_part)
                matches.extend(smart_matches)
            
            # 如果只有一个匹配项，直接返回
            if len(matches) == 1:
                return matches
            
            # 如果有多个匹配项，返回所有匹配项供用户选择
            return sorted(matches)
        except Exception:
            return []
    
    def _smart_path_completion(self, base_dir: Path, file_part: str, separator: str, dir_part: str) -> List[str]:
        """
        智能路径补全，包括自动添加常见文件扩展名
        Args:
            base_dir: 基础目录
            file_part: 文件名部分
            separator: 路径分隔符
            dir_part: 当前目录路径部分
        Returns:
            智能补全的路径列表
        """
        matches = []
        
        # 常见文件扩展名
        common_extensions = ['.txt', '.py', '.js', '.html', '.css', '.json', '.xml', '.md', '.log', '.ini', '.cfg', '.conf']
        
        # 1. 尝试直接匹配（不区分大小写）
        for item in base_dir.iterdir():
            if item.name.lower().startswith(file_part.lower()):
                relative_path = f"{dir_part}\\{item.name}"
                matches.append(relative_path)
        
        # 2. 如果没有直接匹配，尝试添加常见扩展名
        if not matches:
            for ext in common_extensions:
                potential_file = base_dir / (file_part + ext)
                if potential_file.exists() and potential_file.is_file():
                    relative_path = f"{dir_part}\\{file_part}{ext}"
                    matches.append(relative_path)
        
        # 3. 如果还是没有，尝试模糊匹配（包含子字符串）
        if not matches:
            for item in base_dir.iterdir():
                if file_part.lower() in item.name.lower():
                    relative_path = f"{dir_part}\\{item.name}"
                    matches.append(relative_path)
        
        # 4. 如果文件名部分看起来像是不完整的扩展名，尝试补全
        if not matches and '.' in file_part:
            # 例如：输入"test.t"时，尝试匹配"test.txt"
            base_name, partial_ext = file_part.rsplit('.', 1)
            for ext in common_extensions:
                if ext.startswith('.' + partial_ext):
                    potential_file = base_dir / (base_name + ext)
                    if potential_file.exists() and potential_file.is_file():
                        relative_path = f"{dir_part}\\{base_name}{ext}"
                        matches.append(relative_path)
        
        return matches
    
    def _find_common_prefix(self, strings: List[str]) -> str:
        """找到字符串列表的共同前缀"""
        if not strings:
            return ""
        
        # 找到最短字符串的长度
        min_len = min(len(s) for s in strings)
        
        # 逐字符比较
        for i in range(min_len):
            char = strings[0][i]
            for s in strings[1:]:
                if s[i] != char:
                    return strings[0][:i]
        
        return strings[0][:min_len]


class PromptToolkitInputHandler:
    """prompt_toolkit 输入处理器（跨平台）"""
    
    def __init__(
        self,
        work_directory: Path,
        initial_history: Optional[List[str]] = None,
        slash_skill_commands: Optional[List[str]] = None,
        slash_mcp_commands: Optional[List[str]] = None,
        slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        初始化输入处理器
        Args:
            work_directory: 当前工作目录
            initial_history: 预置的历史命令列表（通常来自持久化的HistoryManager）
        """
        self.work_directory = work_directory
        self.history = []
        self._status_bar_text = ""
        self._status_bar_fragments = []
        self._status_bar_enabled = True
        self.renders_prompt_separator_inline = False
        self._pt_style = None
        self._pt_cursor_shape = None
        if (
            PROMPT_TOOLKIT_AVAILABLE
            and CursorShape is not None
            and SimpleCursorShapeConfig is not None
        ):
            try:
                self._pt_cursor_shape = SimpleCursorShapeConfig(CursorShape.BLINKING_BEAM)
            except Exception:
                self._pt_cursor_shape = None
        
        if PROMPT_TOOLKIT_AVAILABLE:
            try:
                self._pt_style = Style.from_dict(
                    {
                        # Keep toolbar with no background color.
                        "bottom-toolbar": "",
                    }
                )
            except Exception:
                self._pt_style = None
            # 使用prompt_toolkit，并将历史记录注入到会话中
            self.completer = FileCompleter(
                work_directory,
                slash_skill_commands,
                slash_mcp_commands,
                slash_dynamic_rules,
            )
            self._key_bindings = self._create_key_bindings()
            self._pt_history = InMemoryHistory()
            if initial_history:
                for entry in initial_history:
                    try:
                        cleaned = _sanitize_prompt_pollution(entry, work_directory)
                        if cleaned:
                            self._pt_history.append_string(cleaned)
                    except Exception:
                        pass
            session_kwargs = dict(
                completer=self.completer,
                history=self._pt_history,
                key_bindings=self._key_bindings,
                enable_system_prompt=True,
                enable_suspend=True,
                complete_in_thread=True,
                complete_while_typing=True,
            )
            if self._pt_style is not None:
                session_kwargs["style"] = self._pt_style
            if self._pt_cursor_shape is not None:
                session_kwargs["cursor"] = self._pt_cursor_shape
            self.session = PromptSession(**session_kwargs)
            _attach_blink_after_render_hook(
                self.session,
                status_provider=self._status_line_for_overlay,
            )
        else:
            # 回退到标准input
            self.session = None

    def _render_bottom_toolbar(self):
        try:
            if not self._status_bar_enabled:
                return ""
            text = str(self._status_bar_text or "")
            frags = self._status_bar_fragments if isinstance(self._status_bar_fragments, list) else []
            if (not text.strip()) and (not frags):
                return ""
            if self.session is None:
                return ""
            # Keep one blank line between prompt and status line.
            if frags:
                return [("", "\n")] + frags
            return [("", "\n" + text)]
        except Exception:
            return ""

    def _status_line_for_overlay(self) -> str:
        try:
            if not self._status_bar_enabled:
                return ""
            if self.session is not None:
                app = getattr(self.session, "app", None)
                if app is not None:
                    buf = getattr(app, "current_buffer", None)
                    if buf is not None and str(getattr(buf, "text", "") or ""):
                        # 输入中隐藏状态栏，清空输入后再显示。
                        return ""
            return str(self._status_bar_text or "")
        except Exception:
            return ""

    def get_terminal_columns(self, default: int = 80) -> int:
        if self.session is not None:
            return _get_output_columns(self.session, default=default)
        return int(default or 80)

    def get_input_with_completion(
        self,
        prompt: str,
        status_bar_text: str = "",
        status_bar_fragments: Optional[List[Tuple[str, str]]] = None,
        show_status_bar: bool = True,
        show_separator: bool = True,
    ) -> str:
        """
        获取带自动补全的用户输入
        Args:
            prompt: 输入提示
            status_bar_text: 状态栏文本（显示在控制台最底部）
            status_bar_fragments: 状态栏分段样式文本（prompt_toolkit formatted text）
            show_status_bar: 是否显示状态栏
        Returns:
            用户输入的文本
        """
        self._status_bar_text = str(status_bar_text or "")
        self._status_bar_fragments = (
            list(status_bar_fragments)
            if isinstance(status_bar_fragments, list)
            else []
        )
        self._status_bar_enabled = bool(show_status_bar)
        try:
            if self.session:
                # Avoid bottom_toolbar (it is pinned to terminal bottom). Render a
                # status line 2 lines below prompt while keeping prompt_toolkit in
                # charge of prompt/cursor layout to avoid corruption.
                if "\n" in prompt:
                    first, rest = prompt.split("\n", 1)
                    print(f"\x1b[90m{first}\x1b[0m")
                    prompt_line = rest
                else:
                    prompt_line = prompt

                user_input = self.session.prompt(prompt_line).strip()
            else:
                # 回退到标准input
                user_input = input(prompt).strip()

            user_input = _sanitize_prompt_pollution(user_input, self.work_directory)
            
            # 保存到历史记录
            if user_input:
                self.history.append(user_input)

            return user_input
            
        except KeyboardInterrupt:
            print("^C")
            raise
        except EOFError:
            print()
            raise KeyboardInterrupt
        except Exception as e:
            print(f"\n输入错误: {e}")
            return ""

    def _create_key_bindings(self):
        """
        Keep completion menu updated while deleting characters.
        """
        kb = KeyBindings()

        @kb.add("backspace")
        def _on_backspace(event):
            buf = event.current_buffer
            buf.delete_before_cursor(count=1)
            # Recompute completions immediately after deletion.
            buf.start_completion(select_first=False)

        @kb.add("delete")
        def _on_delete(event):
            buf = event.current_buffer
            buf.delete(count=1)
            # Recompute completions immediately after deletion.
            buf.start_completion(select_first=False)

        @kb.add("tab")
        def _on_tab(event):
            """
            Trigger completion and, for '/mcp/<mcp-server-name>/' roots, immediately
            trigger a second completion so tools/prompts show without extra typing.
            """
            buf = event.current_buffer
            try:
                if not hasattr(self, "completer"):
                    buf.start_completion(select_first=False)
                    return

                def _prepare_delayed_trigger(text_before_cursor: str) -> bool:
                    _, slash_part = FileCompleter._slash_fragment_for_completion(
                        text_before_cursor
                    )
                    if not slash_part:
                        return False
                    sl = slash_part.lower()

                    delayed_groups = []
                    try:
                        delayed_groups = self.completer._resolve_dynamic_groups()
                    except Exception:
                        delayed_groups = []
                    triggers = [
                        str(trig or "")
                        for trig, _ in delayed_groups
                        if str(trig or "").strip()
                    ]
                    for trig in triggers:
                        if sl == trig.lower():
                            return True
                    # If a completion inserted the command without trailing space,
                    # add one so delayed dynamic candidates can be shown immediately.
                    for trig in triggers:
                        if sl == trig.rstrip().lower():
                            try:
                                buf.insert_text(" ")
                            except Exception:
                                return False
                            return True

                    return False

                # Always compute candidates from current document first; if there is
                # exactly one candidate, force-apply it even when a completion menu
                # was previously open.
                complete_event = CompleteEvent(completion_requested=True)
                candidates = list(
                    self.completer.get_completions(buf.document, complete_event)
                )
                if len(candidates) == 1:
                    try:
                        buf.apply_completion(candidates[0])
                    except Exception:
                        buf.start_completion(select_first=False)
                    if _prepare_delayed_trigger(buf.document.text_before_cursor):
                        try:
                            if hasattr(buf, "cancel_completion"):
                                buf.cancel_completion()
                        except Exception:
                            pass
                        buf.start_completion(select_first=False)
                    return

                # Multiple candidates: insert common suffix first (e.g. 'ski' -> 'skill/').
                if len(candidates) > 1:
                    common_suffix = ""
                    try:
                        common_suffix = get_common_complete_suffix(
                            buf.document, candidates
                        )
                    except Exception:
                        common_suffix = ""
                    if common_suffix:
                        try:
                            buf.insert_text(common_suffix)
                        except Exception:
                            pass
                        if _prepare_delayed_trigger(buf.document.text_before_cursor):
                            try:
                                if hasattr(buf, "cancel_completion"):
                                    buf.cancel_completion()
                            except Exception:
                                pass
                        buf.start_completion(select_first=False)
                        return

                    # Slash command mode: when there are multiple candidates but
                    # no common suffix, Tab should still commit the first
                    # candidate (historical behavior users expect), instead of
                    # only moving menu selection.
                    _, slash_part = FileCompleter._slash_fragment_for_completion(
                        buf.document.text_before_cursor
                    )
                    if slash_part:
                        try:
                            buf.apply_completion(candidates[0])
                        except Exception:
                            pass
                        if _prepare_delayed_trigger(buf.document.text_before_cursor):
                            try:
                                if hasattr(buf, "cancel_completion"):
                                    buf.cancel_completion()
                            except Exception:
                                pass
                            buf.start_completion(select_first=False)
                        return

                # Multiple/no candidates without common suffix: usual menu behavior.
                if buf.complete_state:
                    before_text = buf.document.text_before_cursor
                    buf.complete_next()
                    # Some prompt_toolkit states only move selection without
                    # inserting text immediately. Force-apply current completion
                    # so delayed trigger detection can work in the same Tab press.
                    try:
                        after_text = buf.document.text_before_cursor
                        if after_text == before_text:
                            # Preferred: apply the currently selected completion
                            # from completion state when available.
                            if (
                                getattr(buf, "complete_state", None) is not None
                                and getattr(
                                    buf.complete_state, "current_completion", None
                                )
                                is not None
                            ):
                                buf.apply_completion(buf.complete_state.current_completion)
                            # Fallback: if still unchanged, apply first candidate
                            # from the completion snapshot we computed for this Tab.
                            if (
                                buf.document.text_before_cursor == before_text
                                and len(candidates) > 0
                            ):
                                buf.apply_completion(candidates[0])
                    except Exception:
                        pass
                    if _prepare_delayed_trigger(buf.document.text_before_cursor):
                        try:
                            if hasattr(buf, "cancel_completion"):
                                buf.cancel_completion()
                        except Exception:
                            pass
                        buf.start_completion(select_first=False)
                else:
                    buf.start_completion(select_first=False)
            except Exception:
                buf.start_completion(select_first=False)
                return

        return kb
    
    def update_work_directory(self, new_directory: Path):
        """更新工作目录"""
        self.work_directory = new_directory
        if self.session and hasattr(self, 'completer'):
            self.completer.work_directory = new_directory

    def set_slash_skill_commands(self, slash_skill_commands: Optional[List[str]] = None) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_skill_commands = slash_skill_commands or []

    def set_slash_mcp_commands(self, slash_mcp_commands: Optional[List[str]] = None) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_mcp_commands = slash_mcp_commands or []

    def set_slash_dynamic_rules(
        self, slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None
    ) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_dynamic_rules = slash_dynamic_rules or []

    def reset_command_history(self, entries: Optional[List[str]] = None) -> None:
        """
        Rebuild prompt_toolkit InMemoryHistory from entries (e.g. after HistoryManager.clear_history()).
        Must be called when disk history is cleared; otherwise arrow-key history stays stale in RAM.
        """
        self.history = []
        if not PROMPT_TOOLKIT_AVAILABLE or not getattr(self, "session", None):
            return
        entries = entries if entries is not None else []
        self._pt_history = InMemoryHistory()
        for entry in entries:
            try:
                cleaned = _sanitize_prompt_pollution(entry, self.work_directory)
                if cleaned:
                    self._pt_history.append_string(cleaned)
            except Exception:
                pass
        session_kwargs = dict(
            completer=self.completer,
            history=self._pt_history,
            key_bindings=self._key_bindings,
            enable_system_prompt=True,
            enable_suspend=True,
            complete_in_thread=True,
            complete_while_typing=True,
        )
        if getattr(self, "_pt_style", None) is not None:
            session_kwargs["style"] = self._pt_style
        if getattr(self, "_pt_cursor_shape", None) is not None:
            session_kwargs["cursor"] = self._pt_cursor_shape
        self.session = PromptSession(**session_kwargs)
        _attach_blink_after_render_hook(
            self.session,
            status_provider=self._status_line_for_overlay,
        )


def create_prompt_toolkit_input_handler(
    work_directory: Path,
    initial_history: Optional[List[str]] = None,
    slash_skill_commands: Optional[List[str]] = None,
    slash_mcp_commands: Optional[List[str]] = None,
    slash_dynamic_rules: Optional[List[Dict[str, Any]]] = None,
) -> PromptToolkitInputHandler:
    """创建 prompt_toolkit 输入处理器"""
    return PromptToolkitInputHandler(
        work_directory,
        initial_history,
        slash_skill_commands,
        slash_mcp_commands,
        slash_dynamic_rules,
    )
