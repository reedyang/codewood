#!/usr/bin/env python3
"""
Windows兼容的输入处理模块
使用prompt_toolkit库实现稳定的Tab补全功能和中文输入支持
"""

import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_WIN_DRIVE_BANG = re.compile(r"^([A-Za-z]:)(/.*)?$")

from .builtin_slash_commands import windows_slash_builtin_completions

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False


class FileCompleter(Completer):
    """文件补全器"""
    
    def __init__(self, work_directory: Path, slash_skill_commands: Optional[List[str]] = None):
        self.work_directory = work_directory
        self.slash_skill_commands = slash_skill_commands or []

    @staticmethod
    def _slash_fragment_for_completion(text: str) -> Tuple[int, str]:
        """
        Return the slash-command fragment being edited at cursor tail.
        Matches either:
        - line starts with '/...' (supports spaces in slash command)
        - or last token after whitespace is '/...'
        Returns (index_of_slash, fragment_from_slash_to_cursor), or (-1, '').
        """
        # Full-line slash built-in command (e.g. "/mcp ", "/knowledge search q")
        stripped = text.lstrip()
        if stripped.startswith("/"):
            slash_idx = len(text) - len(stripped)
            return slash_idx, text[slash_idx:]

        # Fallback token-style slash completion for non built-in contexts.
        m = re.search(r"(^|\s)(/[^\s]*)$", text)
        if not m:
            return -1, ""
        frag = m.group(2) or ""
        if not frag.startswith("/"):
            return -1, ""
        slash_idx = len(text) - len(frag)
        return slash_idx, frag

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

        # Windows: "/" -> built-in + skill slash commands only (no path-as-/foo)
        if os.name == "nt":
            idx, slash_part = self._slash_fragment_for_completion(text)
            if idx >= 0 and slash_part:
                builtin_matches = windows_slash_builtin_completions(
                    slash_part, dynamic_commands=self.slash_skill_commands
                )
                if builtin_matches:
                    spos = -len(slash_part)
                    seen = set()
                    for mc in builtin_matches:
                        if mc in seen:
                            continue
                        seen.add(mc)
                        yield Completion(mc, start_position=spos)
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
            # Windows-only input handler: always use current drive root.
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
                    
                    # 构建Windows风格路径
                    path = f"\\{item.name}"
                    
                    matches.append(path)
                    
            except PermissionError:
                # 如果没有权限访问根目录，返回空列表
                return []
            
            return sorted(matches)
        except Exception:
            return []

    @staticmethod
    def _windows_bang_drive_base(rel: str) -> Optional[Tuple[Path, str]]:
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
        Example: '!src/win' -> '!src\\windows_input.py'
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
                drive_pair = self._windows_bang_drive_base(rel)
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
            # Windows-only input handler: normalize to Windows separator.
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
                # Absolute path on Windows:
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
                    # 构建Windows风格路径
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


class WindowsInputHandler:
    """Windows输入处理器，使用prompt_toolkit实现Tab补全和中文输入支持"""
    
    def __init__(
        self,
        work_directory: Path,
        initial_history: Optional[List[str]] = None,
        slash_skill_commands: Optional[List[str]] = None,
    ):
        """
        初始化输入处理器
        Args:
            work_directory: 当前工作目录
            initial_history: 预置的历史命令列表（通常来自持久化的HistoryManager）
        """
        self.work_directory = work_directory
        self.history = []
        
        if PROMPT_TOOLKIT_AVAILABLE:
            # 使用prompt_toolkit，并将历史记录注入到会话中
            self.completer = FileCompleter(work_directory, slash_skill_commands)
            self._key_bindings = self._create_key_bindings()
            self._pt_history = InMemoryHistory()
            if initial_history:
                for entry in initial_history:
                    try:
                        self._pt_history.append_string(entry)
                    except Exception:
                        pass
            self.session = PromptSession(
                completer=self.completer,
                history=self._pt_history,
                key_bindings=self._key_bindings,
                enable_system_prompt=True,
                enable_suspend=True,
                complete_in_thread=True,
                complete_while_typing=True,
            )
        else:
            # 回退到标准input
            self.session = None
    
    def get_input_with_completion(self, prompt: str) -> str:
        """
        获取带自动补全的用户输入
        Args:
            prompt: 输入提示
        Returns:
            用户输入的文本
        """
        try:
            if self.session:
                # 使用prompt_toolkit
                user_input = self.session.prompt(prompt).strip()
            else:
                # 回退到标准input
                user_input = input(prompt).strip()
            
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

        return kb
    
    def update_work_directory(self, new_directory: Path):
        """更新工作目录"""
        self.work_directory = new_directory
        if self.session and hasattr(self, 'completer'):
            self.completer.work_directory = new_directory

    def set_slash_skill_commands(self, slash_skill_commands: Optional[List[str]] = None) -> None:
        if hasattr(self, "completer"):
            self.completer.slash_skill_commands = slash_skill_commands or []

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
                self._pt_history.append_string(entry)
            except Exception:
                pass
        self.session = PromptSession(
            completer=self.completer,
            history=self._pt_history,
            key_bindings=self._key_bindings,
            enable_system_prompt=True,
            enable_suspend=True,
            complete_in_thread=True,
            complete_while_typing=True,
        )

def create_windows_input_handler(
    work_directory: Path,
    initial_history: Optional[List[str]] = None,
    slash_skill_commands: Optional[List[str]] = None,
) -> WindowsInputHandler:
    """创建Windows输入处理器"""
    return WindowsInputHandler(work_directory, initial_history, slash_skill_commands)
