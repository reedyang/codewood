import json
import os
from pathlib import Path
from typing import List, Optional
from datetime import datetime
from ...config.app_info import get_app_config_dirname, get_app_name

class HistoryManager:
    def __init__(self, config_dir: str = get_app_config_dirname(), max_entries: int = 50):
        """
        初始化历史记录管理器
        Args:
            config_dir: 存放 history.json 的目录（通常为 {get_app_name()} 配置目录下的 workspace/）
            max_entries: 最大记录数
        """
        self.config_dir = Path(config_dir)
        self.history_file = self.config_dir / "history.json"
        self.max_entries = max_entries
        self.history: List[str] = []
        self.current_index = -1  # 当前在历史记录中的位置
        
        # 确保配置目录存在
        self.config_dir.mkdir(exist_ok=True)
        
        # 加载历史记录
        self.load_history()

    def load_history(self):
        """从文件加载历史记录"""
        try:
            if self.history_file.exists():
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    raw_history = data.get('history', [])
                    self.history = [
                        str(entry)
                        for entry in raw_history
                        if str(entry or "").strip()
                    ]
                    # 确保不超过最大记录数
                    if len(self.history) > self.max_entries:
                        self.history = self.history[-self.max_entries:]
            else:
                self.history = []
        except Exception as e:
            print(f"⚠️ 加载历史记录失败: {e}")
            self.history = []
    
    def save_history(self):
        """保存历史记录到文件"""
        try:
            data = {
                'history': [
                    entry
                    for entry in self.history
                    if str(entry or "").strip()
                ]
            }
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存历史记录失败: {e}")
    
    def add_entry(self, command: str):
        """
        添加新的历史记录
        Args:
            command: 用户输入的命令
        """
        cleaned_command = command.strip()
        if not cleaned_command:
            return

        # 追加前先清理历史中的同内容旧记录，确保同一命令仅保留最新一条。
        self.history = [entry for entry in self.history if entry != cleaned_command]

        # 添加新记录
        self.history.append(cleaned_command)
        
        # 维护最大记录数
        if len(self.history) > self.max_entries:
            self.history = self.history[-self.max_entries:]

        self.save_history()

        # 重置当前索引
        self.current_index = -1
    
    def get_previous(self) -> Optional[str]:
        """
        获取上一条历史记录
        Returns:
            上一条历史记录，如果没有则返回None
        """
        if not self.history:
            return None
        
        if self.current_index == -1:
            # 第一次按上键，跳到最后一条记录
            self.current_index = len(self.history) - 1
        elif self.current_index > 0:
            # 向上浏览历史记录
            self.current_index -= 1
        else:
            # 已经到第一条记录
            return None
        
        return self.history[self.current_index]
    
    def get_next(self) -> Optional[str]:
        """
        获取下一条历史记录
        Returns:
            下一条历史记录，如果没有则返回None
        """
        if not self.history or self.current_index == -1:
            return None
        
        if self.current_index < len(self.history) - 1:
            # 向下浏览历史记录
            self.current_index += 1
            return self.history[self.current_index]
        else:
            # 已经到最后一条记录，重置索引
            self.current_index = -1
            return None
    
    def reset_index(self):
        """重置当前索引"""
        self.current_index = -1
    
    def get_all_history(self) -> List[str]:
        """获取所有历史记录"""
        return self.history.copy()
    
    def clear_history(self):
        """清空历史记录"""
        self.history = []
        self.current_index = -1
        self.save_history()
