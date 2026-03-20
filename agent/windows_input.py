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

from .builtin_slash_commands import windows_slash_builtin_completions

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.styles import Style
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False


class FileCompleter(Completer):
    """文件补全器"""
    
    def __init__(self, work_directory: Path):
        self.work_directory = work_directory

    @staticmethod
    def _leading_slash_builtin_fragment(text: str) -> Tuple[int, str]:
        """
        If the first non-whitespace character is '/', return (index of '/', fragment from '/' to end).
        Otherwise (-1, '').
        """
        m = re.match(r"^\s*", text)
        i = m.end() if m else 0
        if i < len(text) and text[i] == "/":
            return i, text[i:]
        return -1, ""

    def get_completions(self, document, complete_event):
        """获取补全选项"""
        text = document.text_before_cursor

        # Windows: first non-space char is "/" -> complete built-in slash commands only
        if os.name == "nt":
            idx, slash_part = self._leading_slash_builtin_fragment(text)
            if idx >= 0 and slash_part:
                matches = windows_slash_builtin_completions(slash_part)
                if matches:
                    spos = -len(slash_part)
                    for mc in matches:
                        yield Completion(mc, start_position=spos)
                return
        
        # 如果输入为空或只有空白字符，直接返回所有文件
        if not text or text.strip() == "":
            completions = self._get_directory_contents()
            seen = set()
            for completion in completions:
                if completion not in seen:
                    seen.add(completion)
                    yield Completion(completion, start_position=-len(text))
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
            import platform
            
            if platform.system() == "Windows":
                # Windows系统：获取当前驱动器的根目录
                current_drive = Path.cwd().anchor  # 例如 'C:\\'
                root_dir = Path(current_drive)
            else:
                # Unix系统：根目录是 '/'
                root_dir = Path('/')
            
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
                    
                    # 构建路径
                    if separator == '/':
                        path = f"/{item.name}"
                    else:
                        path = f"\\{item.name}"
                    
                    matches.append(path)
                    
            except PermissionError:
                # 如果没有权限访问根目录，返回空列表
                return []
            
            return sorted(matches)
        except Exception:
            return []
    
    def _get_path_completions(self, text: str) -> List[str]:
        """获取路径补全"""
        try:
            # 分离目录和文件名部分
            if '/' in text:
                separator = '/'
            else:
                separator = '\\'
            
            # 特殊处理：如果输入只是单个分隔符，显示根目录内容
            if text == '\\' or text == '/':
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
            if dir_part.startswith('/') or (len(dir_part) > 1 and dir_part[1] == ':'):
                # 绝对路径；Windows 下 "d:" 需规范为 "d:\\" 才表示该盘根目录
                if len(dir_part) == 2 and dir_part[0].isalpha() and dir_part[1] == ':':
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
                    # 构建相对路径，保持原始分隔符风格
                    if separator == '/':
                        # Unix风格路径
                        relative_path = f"{dir_part}/{item.name}"
                    else:
                        # Windows风格路径
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
                if separator == '/':
                    relative_path = f"{dir_part}/{item.name}"
                else:
                    relative_path = f"{dir_part}\\{item.name}"
                matches.append(relative_path)
        
        # 2. 如果没有直接匹配，尝试添加常见扩展名
        if not matches:
            for ext in common_extensions:
                potential_file = base_dir / (file_part + ext)
                if potential_file.exists() and potential_file.is_file():
                    if separator == '/':
                        relative_path = f"{dir_part}/{file_part}{ext}"
                    else:
                        relative_path = f"{dir_part}\\{file_part}{ext}"
                    matches.append(relative_path)
        
        # 3. 如果还是没有，尝试模糊匹配（包含子字符串）
        if not matches:
            for item in base_dir.iterdir():
                if file_part.lower() in item.name.lower():
                    if separator == '/':
                        relative_path = f"{dir_part}/{item.name}"
                    else:
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
                        if separator == '/':
                            relative_path = f"{dir_part}/{base_name}{ext}"
                        else:
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
    
    def __init__(self, work_directory: Path, initial_history: Optional[List[str]] = None):
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
            self.completer = FileCompleter(work_directory)
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
                enable_system_prompt=True,
                enable_suspend=True,
                complete_in_thread=True
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
    
    def update_work_directory(self, new_directory: Path):
        """更新工作目录"""
        self.work_directory = new_directory
        if self.session and hasattr(self, 'completer'):
            self.completer.work_directory = new_directory

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
            enable_system_prompt=True,
            enable_suspend=True,
            complete_in_thread=True,
        )

def create_windows_input_handler(work_directory: Path, initial_history: Optional[List[str]] = None) -> WindowsInputHandler:
    """创建Windows输入处理器"""
    return WindowsInputHandler(work_directory, initial_history)