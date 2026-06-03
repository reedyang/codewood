import json
import os
from pathlib import Path
from typing import List, Optional
from datetime import datetime
from ...config.app_info import get_app_config_dirname, get_app_name
from ..localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text

class HistoryManager:
    def __init__(
        self,
        config_dir: str = get_app_config_dirname(),
        max_entries: int = 50,
        language: str = DEFAULT_DISPLAY_LANGUAGE,
    ):
        """
        Initialize the history manager.
        Args:
            config_dir: Directory that stores history.json (typically workspace/ under the {get_app_name()} config directory).
            max_entries: Maximum number of entries.
        """
        self.config_dir = Path(config_dir)
        self.history_file = self.config_dir / "history.json"
        self.max_entries = max_entries
        self.language = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
        self.history: List[str] = []
        self.current_index = -1  # Current position in the history list
        
        # Ensure the config directory exists.
        self.config_dir.mkdir(exist_ok=True)
        
        # Load history.
        self.load_history()

    def load_history(self):
        """Load history from disk."""
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
                    # Ensure we do not exceed the maximum number of entries.
                    if len(self.history) > self.max_entries:
                        self.history = self.history[-self.max_entries:]
            else:
                self.history = []
        except Exception as e:
            print(text("⚠️ Failed to load history: {error}", "⚠️ 加载历史记录失败：{error}", self.language).format(error=e))
            self.history = []
    
    def save_history(self):
        """Save history to disk."""
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
            print(text("⚠️ Failed to save history: {error}", "⚠️ 保存历史记录失败：{error}", self.language).format(error=e))
    
    def add_entry(self, command: str):
        """
        Add a new history entry.
        Args:
            command: The command entered by the user.
        """
        cleaned_command = command.strip()
        if not cleaned_command:
            return

        # Remove older entries with the same content so only the latest copy remains.
        self.history = [entry for entry in self.history if entry != cleaned_command]

        # Add the new entry.
        self.history.append(cleaned_command)
        
        # Enforce the maximum entry count.
        if len(self.history) > self.max_entries:
            self.history = self.history[-self.max_entries:]

        self.save_history()

        # Reset the current index.
        self.current_index = -1
    
    def get_previous(self) -> Optional[str]:
        """
        Get the previous history entry.
        Returns:
            The previous history entry, or None if there is none.
        """
        if not self.history:
            return None
        
        if self.current_index == -1:
            # First Up key press jumps to the last entry.
            self.current_index = len(self.history) - 1
        elif self.current_index > 0:
            # Move up through history.
            self.current_index -= 1
        else:
            # Already at the first entry.
            return None
        
        return self.history[self.current_index]
    
    def get_next(self) -> Optional[str]:
        """
        Get the next history entry.
        Returns:
            The next history entry, or None if there is none.
        """
        if not self.history or self.current_index == -1:
            return None
        
        if self.current_index < len(self.history) - 1:
            # Move down through history.
            self.current_index += 1
            return self.history[self.current_index]
        else:
            # Already at the last entry; reset the index.
            self.current_index = -1
            return None
    
    def reset_index(self):
        """Reset the current index."""
        self.current_index = -1
    
    def get_all_history(self) -> List[str]:
        """Get all history entries."""
        return self.history.copy()
    
    def clear_history(self):
        """Clear the history."""
        self.history = []
        self.current_index = -1
        self.save_history()
