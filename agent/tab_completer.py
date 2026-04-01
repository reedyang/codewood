#!/usr/bin/env python3
"""
Tab键自动补全模块
支持文件名和目录名的自动补全功能
"""

import os
import sys
import glob
from pathlib import Path
from typing import List, Optional, Tuple

# 尝试导入readline，在Windows上可能不可用
try:
    import readline
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False


class TabCompleter:
    """Tab键自动补全器"""
    
    def __init__(self, work_directory: Path):
        """
        初始化补全器
        Args:
            work_directory: 当前工作目录
        """
        self.work_directory = work_directory
        self._setup_readline()
    
    def _setup_readline(self):
        """设置readline以支持tab补全"""
        if not READLINE_AVAILABLE:
            print("⚠️ 警告: readline模块不可用，tab补全功能将不可用")
            return
        
        try:
            # 设置补全函数
            readline.set_completer(self._completer)
            # 设置tab键为补全触发键
            readline.parse_and_bind('tab: complete')
            # 设置补全分隔符
            readline.set_completer_delims(' \t\n`!@#$%^&*()=+[{]}\\|;:\'",<>?')
        except Exception as e:
            print(f"⚠️ 警告: 无法设置tab补全功能: {e}")
    
    def _completer(self, text: str, state: int) -> Optional[str]:
        """
        readline补全函数。使用整行内容提取路径，以支持 "cd C:\\path\\" 等绝对/相对路径补全。
        Args:
            text: 当前词（readline 按分隔符切出的词，可能只是路径最后一段）
            state: 补全状态（0表示开始，>0表示继续）
        Returns:
            补全的文本，如果state为0则返回None表示结束
        """
        if state == 0:
            # Use full line buffer so path completion works for "cd C:\Users\..." etc.
            path_fragment = text
            if READLINE_AVAILABLE:
                try:
                    line = readline.get_line_buffer()
                    begidx = readline.get_begidx()
                    endidx = readline.get_endidx()
                    # If line looks like "cd <path>", complete the path part only
                    if line.strip().lower().startswith("cd ") or line.strip().lower().startswith("cd\t"):
                        idx = line.lower().find("cd ")
                        if idx < 0:
                            idx = line.lower().find("cd\t")
                        if idx >= 0:
                            path_start = idx + 3
                            path_fragment = line[path_start:endidx].strip()
                    else:
                        path_fragment = line[begidx:endidx]
                except Exception:
                    pass
            self._path_fragment_for_suffix = path_fragment
            self._completions = self._get_completions(path_fragment)
            self._completion_index = 0

        if self._completion_index < len(self._completions):
            completion = self._completions[self._completion_index]
            self._completion_index += 1
            # Return only the suffix that replaces the current word (so path completion works)
            path_frag = getattr(self, "_path_fragment_for_suffix", text)
            if path_frag and ("/" in path_frag or "\\" in path_frag or (len(path_frag) > 1 and path_frag[1] == ":")):
                if len(completion) >= len(path_frag) and completion[: len(path_frag)].lower() == path_frag.lower():
                    return completion[len(path_frag) :]
            return completion
        return None
    
    def _get_completions(self, text: str) -> List[str]:
        """
        获取补全列表
        Args:
            text: 当前输入的文本
        Returns:
            补全选项列表
        """
        # Keep consistent with Windows input handler: no suggestions on empty input.
        if not text or text.strip() == "":
            return []
        # Avoid noisy suggestions when fragment is exactly "."
        if text == ".":
            return []
        
        # 检查是否包含路径分隔符
        if '/' in text or '\\' in text:
            return self._get_path_completions(text)
        else:
            # 当前目录下的文件/文件夹补全
            return self._get_local_completions(text)
    
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
        """
        获取当前目录下的本地补全
        Args:
            text: 要补全的文本
        Returns:
            匹配的文件/文件夹名列表
        """
        try:
            if text == ".":
                return []
            matches = []
            for item in self.work_directory.iterdir():
                if item.name.lower().startswith(text.lower()):
                    matches.append(item.name)
            
            # 如果没有找到匹配项，尝试智能补全
            if not matches and text:
                matches = self._smart_local_completion(text)
            
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
    
    def _find_common_prefix(self, strings: List[str]) -> str:
        """
        找到字符串列表的共同前缀
        Args:
            strings: 字符串列表
        Returns:
            共同前缀
        """
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
    
    def _get_path_completions(self, text: str) -> List[str]:
        """
        获取路径补全
        Args:
            text: 包含路径的文本
        Returns:
            匹配的路径列表
        """
        try:
            # 检测路径分隔符并统一处理
            if '/' in text and '\\' in text:
                # 混合路径，优先使用第一个出现的分隔符
                if text.find('/') < text.find('\\'):
                    separator = '/'
                else:
                    separator = '\\'
            elif '/' in text:
                separator = '/'
            elif '\\' in text:
                separator = '\\'
            else:
                return self._get_local_completions(text)
            
            # Root trigger: one or more pure separators should list root entries.
            if (separator == "\\" and text and set(text) == {"\\"}) or (
                separator == "/" and text and set(text) == {"/"}
            ):
                return self._get_root_directory_completions(separator)
            
            # 统一路径分隔符进行处理
            normalized_text = text.replace('\\', '/') if separator == '/' else text.replace('/', '\\')
            parts = normalized_text.split(separator)
            
            if len(parts) == 1:
                return self._get_local_completions(text)
            
            # 构建目录路径
            dir_part = separator.join(parts[:-1])
            file_part = parts[-1]
            
            # 特殊处理：如果dir_part为空，表示根目录
            if dir_part == '':
                return self._get_root_directory_completions(separator, file_part)
            
            # 解析目录路径
            base_dir = self._resolve_directory_path(dir_part)
            if not base_dir or not base_dir.exists() or not base_dir.is_dir():
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
                    
                    # 只有在目录项且原始文本以分隔符结尾时才添加分隔符
                    if text.endswith(separator) and item.is_dir():
                        matches.append(relative_path + separator)
                    else:
                        matches.append(relative_path)
            
            # 如果没有找到匹配项，尝试智能补全
            if not matches and file_part:
                matches = self._smart_path_completion(base_dir, file_part, separator, dir_part)
            
            return sorted(matches)
        except Exception:
            return []
    
    def _resolve_directory_path(self, dir_part: str) -> Optional[Path]:
        """
        解析目录路径
        Args:
            dir_part: 目录路径部分
        Returns:
            解析后的Path对象，如果无效则返回None
        """
        try:
            if dir_part.startswith('/'):
                # Unix绝对路径
                return Path(dir_part)
            elif len(dir_part) > 1 and dir_part[1] == ':':
                # Windows: "d:" must be "d:\\" to mean drive root (Path("d:") is current dir on that drive)
                if len(dir_part) == 2 and dir_part[0].isalpha():
                    return Path(dir_part + "\\")
                return Path(dir_part)
            elif dir_part.startswith('~'):
                # 用户主目录
                return Path.home() / dir_part[1:]
            else:
                # 相对路径
                return self.work_directory / dir_part
        except Exception:
            return None
    
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
                    
                    # 只有在目录项且file_part为空时才添加分隔符
                    if not file_part and item.is_dir():
                        path += separator
                    
                    matches.append(path)
                    
            except PermissionError:
                # 如果没有权限访问根目录，返回空列表
                return []
            
            return sorted(matches)
        except Exception:
            return []
    
    def update_work_directory(self, new_directory: Path):
        """更新工作目录"""
        self.work_directory = new_directory
    
    def get_input_with_completion(self, prompt: str) -> str:
        """
        获取带自动补全的用户输入
        Args:
            prompt: 输入提示
        Returns:
            用户输入的文本
        """
        try:
            if READLINE_AVAILABLE:
                return input(prompt)
            else:
                # 如果readline不可用，使用简单的输入
                return input(prompt)
        except (EOFError, KeyboardInterrupt):
            raise


def create_tab_completer(work_directory: Path) -> TabCompleter:
    """
    创建tab补全器
    Args:
        work_directory: 工作目录
    Returns:
        TabCompleter实例
    """
    return TabCompleter(work_directory) 