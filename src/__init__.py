"""
Smart Shell 包

这个包包含基于本地Ollama的Smart Shell AI Agent，具有以下功能：
- 文件和目录管理
- 智能目录切换
- 操作结果反馈
- 自然语言交互
"""

import importlib
import sys

from .config.app_info import get_app_version
from .smart_shell_agent import SmartShellAgent

_LEGACY_MODULE_ALIASES = {
    "ai_provider_clients": ".ai.ai_provider_clients",
    "ai_orchestrator": ".ai.ai_orchestrator",
    "ai_special_mode_prompts": ".ai.ai_special_mode_prompts",
    "builtin_command_router": ".controllers.builtin_command_router",
    "chat_command_controller": ".controllers.chat_command_controller",
    "mcp_shortcut_controller": ".controllers.mcp_shortcut_controller",
    "workspace_command_controller": ".controllers.workspace_command_controller",
    "command_actions": ".actions.command_actions",
    "filesystem_actions": ".actions.filesystem_actions",
    "bootstrap": ".runtime.bootstrap",
    "prompt_composer": ".runtime.prompt_composer",
    "runtime_loop": ".runtime.runtime_loop",
    "execution_policy_service": ".services.execution_policy_service",
    "session_memory_service": ".services.session_memory_service",
}

for _legacy_name, _new_module in _LEGACY_MODULE_ALIASES.items():
    _legacy_qualified = f"{__name__}.{_legacy_name}"
    if _legacy_qualified not in sys.modules:
        sys.modules[_legacy_qualified] = importlib.import_module(_new_module, __name__)

__version__ = get_app_version()
__author__ = "AI Assistant"
__all__ = ["SmartShellAgent"] 
