import os
import sys
import json
import re
import shlex
import time
import threading
import importlib
import warnings
import unicodedata
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Set
import shutil
import subprocess
from datetime import datetime

# call_ai 对 OpenAI/OpenWebUI 使用 verify=False 时 urllib3 会对每条请求发出 InsecureRequestWarning；
# 进程启动时关闭该类告警，避免打断终端输出（企业内网自签证书场景常见）。
try:
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    from urllib3.exceptions import InsecureRequestWarning

    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
except Exception:
    pass

# 导入历史记录管理器
from .core.logging.app_logging import get_logger
from .core.state.history_manager import HistoryManager
from .core.config.skills_loader import (
    build_skills_routing_prefix,
    calc_skills_dirs_fingerprint,
    load_skills_merged,
)
from .core.config.config_env import resolve_string_values_in_data
from .core.config.model_providers import parse_configured_models
from .core.assistant_output_highlighter import (
    format_assistant_display_response,
    highlight_assistant_display_line,
    normalize_display_text,
    strip_tool_json_blocks_for_display,
)
from .core.status_bar import (
    build_status_bar_render_data,
    clamp_status_token_usage_percent,
    refresh_status_context_usage_snapshot as refresh_status_context_usage_snapshot_fn,
)
from .integrations.mcp import McpManager, McpError
from .core.change_preview_formatter import ChangePreviewFormatter
from .ai.ai_provider_clients import AICallContext
from .services.session_memory_service import SessionMemoryService
from .policy.path_policy import PathPolicy
from .core.console_utils import (
    _WorkingStatusTicker,
    _ansi_blue,
    _ansi_gray,
    _ansi_red,
    _ansi_yellow,
    _ansi_green,
    _ansi_rgb,
)
from .controllers.builtin_command_router import dispatch_builtin_command
from .controllers.workspace_command_controller import (
    handle_workspace_builtin_command,
    parse_workspace_command_args,
    print_workspace_current,
    print_workspace_help,
    print_workspace_list,
    split_workspace_args,
    workspace_create_command,
    workspace_delete_command,
    workspace_rename_command,
    workspace_subcommand_usage,
    workspace_switch_command,
    workspace_update_command,
    workspace_usage,
)
from .controllers.chat_command_controller import (
    chat_usage,
    handle_chat_builtin_command,
    print_chat_list,
)
from .controllers.model_command_controller import (
    handle_model_builtin_command,
)
from .controllers.mcp_shortcut_controller import (
    mcp_item_label,
    parse_mcp_shortcut_command,
    print_mcp_shortcut_result,
)
from .completion.slash_dynamic_completions import (
    build_mcp_scoped_commands,
    build_mcp_scoped_groups,
    build_mcp_server_commands,
    build_mcp_server_target_commands,
    build_slash_dynamic_rules,
    build_workspace_action_commands,
)
from .actions import filesystem_actions
from .actions import command_actions
from .runtime import bootstrap
from .services import execution_policy_service
from .runtime import prompt_composer
from .managers import WorkspaceStateManager, ChatStateManager

# memory_manager 在后台线程中导入（见 _schedule_memory_service_background），避免阻塞主线程初始化。
MEMORY_AVAILABLE = False  # type: ignore[misc, assignment]
MemoryService = None  # type: ignore[misc, assignment]

# knowledge_manager 在后台线程中导入（见 _schedule_knowledge_service_background），避免主线程拉取 Chroma/torch 等。
# KNOWLEDGE_AVAILABLE / KnowledgeService 由该线程赋到模块上；单测可在构造前设置 KNOWLEDGE_AVAILABLE=False 以跳过。
KnowledgeService = None  # type: ignore
KNOWLEDGE_AVAILABLE = True  # 构造前为「未探测」；单测设为 False 可跳过 knowledge 包加载

DIRECT_SHELL_USER_HISTORY_PREFIX = "[DIRECT_SHELL_USER_COMMAND]"
DIRECT_SHELL_RESULT_HISTORY_PREFIX = "[DIRECT_SHELL_RESULT]"
MODEL_TOOL_RESULT_HISTORY_PREFIX = "[MODEL_TOOL_RESULT]"
CONVERSATION_INTERRUPTED_HISTORY_PREFIX = "[CONVERSATION_INTERRUPTED]"
INTERNAL_SLASH_USER_HISTORY_PREFIX = "[INTERNAL_SLASH_USER_COMMAND]"
INTERNAL_SLASH_RESULT_HISTORY_PREFIX = "[INTERNAL_SLASH_RESULT]"
TASK_WORKED_SUMMARY_HISTORY_PREFIX = "[TASK_WORKED_SUMMARY]"

# 根据操作系统选择合适的输入处理器
import platform

if platform.system() == "Windows":
    try:
        from .completion.prompt_toolkit_input import create_prompt_toolkit_input_handler
        TAB_COMPLETION_AVAILABLE = True
        INPUT_HANDLER_TYPE = "prompt_toolkit"
    except ImportError:
        TAB_COMPLETION_AVAILABLE = False
        INPUT_HANDLER_TYPE = "none"
else:
    try:
        from .completion.prompt_toolkit_input import create_prompt_toolkit_input_handler
        TAB_COMPLETION_AVAILABLE = True
        INPUT_HANDLER_TYPE = "prompt_toolkit"
    except ImportError:
        TAB_COMPLETION_AVAILABLE = False
        INPUT_HANDLER_TYPE = "none"


def _import_ollama_client():
    """
    惰性加载 ollama Python 包；仅在调用方已确认使用 ollama provider 时使用。
    避免仅配置 openai/openwebui 时启动阶段执行 import ollama。
    """
    return importlib.import_module("ollama")


# 经验记忆主检索 query：仅本轮用户输入（上限见 MEMORY_RETRIEVAL_QUERY_MAX_CHARS）。
# 以下两项仍用于查询扩展 LLM 的「近期对话摘录」参考块，不用于关键词检索 query。
MEMORY_RETRIEVAL_ROUNDS = 3
MEMORY_RETRIEVAL_MSG_MAX_CHARS = 400
MEMORY_RETRIEVAL_QUERY_MAX_CHARS = 2000
# 主检索（关键词打分）偏弱时触发 LLM 查询扩展；raw_score 为 memory_manager 未归一化得分
MEMORY_FALLBACK_MIN_RAW_SCORE = 4.0
MEMORY_EXPANSION_MAX_KEYWORD_CHARS = 600
# 与身份/称呼相关的 memory_type，排序时与 preference 同簇，便于新写入的更正与旧 durable 公平竞争
MEMORY_IDENTITY_CLUSTER_TYPES = frozenset(
    {
        "preference",
        "assistant_name",
        "nickname",
        "identity",
        "user_name",
        "display_name",
    }
)

# 会话级摘要：cheap 滚动摘录 + 可选周期性 LLM 压缩，并入经验记忆检索 query。
SESSION_SUMMARY_ROLLING_MAX_CHARS = 600
SESSION_SUMMARY_MSG_SNIPPET = 120
SESSION_SUMMARY_LLM_INTERVAL_PAIRS = 6
SESSION_SUMMARY_LLM_MAX_CHARS = 1200
SESSION_SUMMARY_LLM_HISTORY_MSGS = 16
CHAT_RECENT_MESSAGES = 10
SKILL_PROMPT_LONG_BODY_THRESHOLD = 7000
SKILL_PROMPT_INITIAL_SECTIONS = 3
SKILL_PROMPT_MAX_SECTION_CHARS = 2600

DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default"
WORKSPACE_STATE_FILE = "workspaces.json"
CHAT_STATE_FILE = "chats.json"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1b\][^\a\x1b]*(?:\a|\x1b\\)")
DIRECT_SHELL_WORKING_STATUS_MARQUEE_FPS = 10.0
CONVERSATION_INTERRUPT_BANNER_RECENT_WINDOW_SECONDS = 5.0
INPUT_PROMPT = "› "
TASK_DOMAIN_VALUES = frozenset(
    {
        "software_development",
        "documentation_writing",
        "visual_design",
        "data_analysis",
        "finance",
        "lifestyle",
        "project_coordination",
        "general_other",
    }
)
DOMAIN_PROMPT_FILE_MAP: Dict[str, str] = {
    "software_development": "domain_software_development.md",
    "documentation_writing": "domain_documentation_writing.md",
    "visual_design": "domain_visual_design.md",
    "data_analysis": "domain_data_analysis.md",
    "finance": "domain_finance.md",
    "lifestyle": "domain_lifestyle.md",
    "project_coordination": "domain_project_coordination.md",
}


class SmartShellAgent:
    def __init__(self, model_name: str = "gemma3:4b", work_directory: Optional[str] = None, provider: str = "ollama", openai_conf: Optional[dict] = None, openwebui_conf: Optional[dict] = None, params: Optional[dict] = None, model_config: Optional[dict] = None, config_dir: Optional[str] = None, builtin_skills_dir: Optional[str] = None):
        """
        初始化Smart Shell
        Args:
            model_name: 模型名称（兼容旧格式）
            work_directory: 工作目录
            provider: 模型服务提供方（兼容旧格式）
            openai_conf: openai参数（兼容旧格式）
            openwebui_conf: openwebui参数（兼容旧格式）
            params: 通用参数（兼容调用）
            model_config: 模型配置（provider + params）
            config_dir: 配置文件目录（可选）；持久化状态位于该目录下的 workspace/
            builtin_skills_dir: 内建 Agent Skills 根目录；未传则使用项目根目录下的 skills/
        """
        startup_work_directory = Path(work_directory) if work_directory else Path.cwd()

        bootstrap.setup_core_state(
            self,
            startup_work_directory=startup_work_directory,
            self_repo_root=Path(__file__).resolve().parent.parent,
        )

        self.config_dir = bootstrap.resolve_config_dir(config_dir)
        self._workspace_state_manager = WorkspaceStateManager(
            self,
            default_workspace_id=DEFAULT_WORKSPACE_ID,
            default_workspace_name=DEFAULT_WORKSPACE_NAME,
        )
        self._chat_state_manager = ChatStateManager(self, chat_state_file=CHAT_STATE_FILE)

        bootstrap.setup_workspace_and_history(
            self,
            startup_work_directory=startup_work_directory,
            workspace_state_file=WORKSPACE_STATE_FILE,
            default_workspace_id=DEFAULT_WORKSPACE_ID,
        )
        bootstrap.setup_runtime_preferences(self)
        bootstrap.setup_policy_caches(self)

        bootstrap.setup_model_ai_stack(
            self,
            model_name=model_name,
            provider=provider,
            openai_conf=openai_conf,
            openwebui_conf=openwebui_conf,
            params=params,
            model_config=model_config,
            ollama_importer=_import_ollama_client,
        )
        self._restore_active_chat_model()

        bootstrap.setup_prompt_and_mcp(self)
        bootstrap.setup_skills(self, builtin_skills_dir=builtin_skills_dir)
        bootstrap.setup_input_handler(
            self,
            tab_completion_available=TAB_COMPLETION_AVAILABLE,
            input_handler_type=INPUT_HANDLER_TYPE,
            create_prompt_toolkit_input_handler=globals().get("create_prompt_toolkit_input_handler"),
        )
        bootstrap.setup_runtime_services(self)

    def _resolve_path_lenient(self, path: Path) -> Path:
        try:
            return Path(path).expanduser().resolve()
        except Exception:
            return Path(path).expanduser().absolute()

    def _is_default_workspace(self) -> bool:
        return (
            str(getattr(self, "workspace_id", "")).strip() == DEFAULT_WORKSPACE_ID
            or str(getattr(self, "workspace_kind", "")).strip().lower() == "default"
        )

    def _project_context_feature_enabled(self) -> bool:
        # Hard policy: default workspace is tool-calling only, no project-management aids.
        if self._is_default_workspace():
            return False
        return bool(getattr(self, "project_context_first_round_evidence_enabled", True))

    def _project_context_tool_allowed(self) -> bool:
        # Tool visibility/execution follows the same hard policy.
        return not self._is_default_workspace()

    def _bind_project_index_workspace(self) -> None:
        try:
            self._project_context_index.bind_workspace(
                self.work_directory,
                storage_dir=(self.ai_workspace_dir / "knowledge_db"),
            )
        except Exception:
            pass

    def _schedule_project_context_refresh_background(self, force: bool = False, reason: str = "") -> bool:
        if not self._project_context_tool_allowed():
            return False
        index = getattr(self, "_project_context_index", None)
        if index is None:
            return False
        gate = getattr(self, "_project_context_refresh_gate", None)
        if gate is None:
            gate = threading.Lock()
            self._project_context_refresh_gate = gate
        with gate:
            if bool(getattr(self, "_project_context_refresh_inflight", False)):
                return False
            self._project_context_refresh_inflight = True
        target_root = Path(self.work_directory)
        target_storage = Path(self.ai_workspace_dir) / "knowledge_db"
        reason_text = str(reason or "background")
        pc_logger = get_logger("smartshell.project_context")
        started_at = datetime.now()
        try:
            pc_logger.info(
                "项目上下文后台刷新已调度: reason=%s force=%s workspace=%s storage=%s",
                reason_text,
                bool(force),
                str(target_root),
                str(target_storage),
            )
        except Exception:
            pass

        def _run() -> None:
            refresh_result: Dict[str, Any] = {}
            try:
                index.bind_workspace(target_root, storage_dir=target_storage)
                refresh_result = index.refresh_index(
                    force=bool(force),
                    timeout_ms=(None if force else 2000),
                )
            except Exception:
                pass
            finally:
                with gate:
                    self._project_context_refresh_inflight = False
                try:
                    elapsed_ms = int((datetime.now() - started_at).total_seconds() * 1000)
                    pc_logger.info(
                        "项目上下文后台刷新完成: reason=%s force=%s elapsed_ms=%s timed_out=%s files_total=%s scanned=%s processed=%s",
                        reason_text,
                        bool(force),
                        elapsed_ms,
                        bool(refresh_result.get("timed_out", False)),
                        int(refresh_result.get("files_total", 0) or 0),
                        int(refresh_result.get("scanned", 0) or 0),
                        int(refresh_result.get("processed", 0) or 0),
                    )
                except Exception:
                    pass

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"smartshell-project-context-refresh:{reason_text}",
        ).start()
        return True

    def _path_identity_key(self, path: Path) -> str:
        return self._workspace_state_manager.path_identity_key(path)

    def _workspace_id_for_path(self, path: Path) -> str:
        return self._workspace_state_manager.workspace_id_for_path(path)

    def _default_workspace_entry(self) -> Dict[str, Any]:
        return self._workspace_state_manager.default_workspace_entry()

    def _workspace_root_path(self, entry: Dict[str, Any]) -> Path:
        return self._workspace_state_manager.workspace_root_path(entry)

    def _workspace_storage_path(self, entry: Dict[str, Any]) -> Path:
        return self._workspace_state_manager.workspace_storage_path(entry)

    def _workspace_current_dir_path(self, entry: Dict[str, Any]) -> Optional[Path]:
        return self._workspace_state_manager.workspace_current_dir_path(entry)

    def _load_workspace_state(self) -> Dict[str, Any]:
        return self._workspace_state_manager.load_workspace_state()

    def _save_workspace_state(self) -> None:
        self._workspace_state_manager.save_workspace_state()

    def _ensure_workspace_dirs(self) -> None:
        self._workspace_state_manager.ensure_workspace_dirs()

    def _apply_workspace_entry(self, entry: Dict[str, Any], fallback_dir: Path) -> None:
        self._workspace_state_manager.apply_workspace_entry(entry, fallback_dir)

    def _save_current_workspace_position(self) -> None:
        self._workspace_state_manager.save_current_workspace_position()

    def _shell_execution_cwd(self) -> Path:
        """Return the cwd to use when executing shell commands/scripts."""
        try:
            root = self._resolve_path_lenient(Path(self.workspace_root))
        except Exception:
            return self.work_directory
        if root.exists() and root.is_dir():
            return root
        return self.work_directory

    def _reset_work_directory_to_startup_initial(self) -> None:
        """Restore current directory to startup initial directory and persist state."""
        target = None
        try:
            startup_dir = getattr(self, "startup_initial_directory", None)
            if startup_dir is not None:
                target = self._resolve_path_lenient(Path(startup_dir))
        except Exception:
            target = None
        if target is None:
            target = self.work_directory
        if not target.exists() or not target.is_dir():
            return
        self.work_directory = target
        try:
            if hasattr(self, "input_handler") and self.input_handler:
                if hasattr(self.input_handler, "update_work_directory"):
                    self.input_handler.update_work_directory(target)
        except Exception:
            pass
        try:
            self._save_current_workspace_position()
        except Exception:
            pass

    def _workspace_path_from_arg(self, raw: str) -> Path:
        return self._workspace_state_manager.workspace_path_from_arg(raw)

    def _workspace_entry_by_root(self, root: Path, ignore_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._workspace_state_manager.workspace_entry_by_root(root, ignore_id=ignore_id)

    def _workspace_name_exists(self, name: str, ignore_id: Optional[str] = None) -> bool:
        return self._workspace_state_manager.workspace_name_exists(name, ignore_id=ignore_id)

    def _workspace_entry_by_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        return self._workspace_state_manager.workspace_entry_by_selector(selector)

    def _handle_workspace_builtin_command(self, builtin_line: str) -> bool:
        return handle_workspace_builtin_command(self, builtin_line)

    def _load_runtime_config_data(self) -> Dict[str, Any]:
        cached = getattr(self, "_resolved_config_data", None)
        if isinstance(cached, dict) and cached:
            return cached
        cfg_data: Dict[str, Any] = {}
        try:
            cfg_path = self.config_dir / "config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    loaded = resolve_string_values_in_data(json.load(f))
                if isinstance(loaded, dict):
                    cfg_data = loaded
        except Exception:
            cfg_data = {}
        self._resolved_config_data = dict(cfg_data)
        return cfg_data

    def _get_configured_model_catalog(self) -> List[Dict[str, Any]]:
        cfg_data = self._load_runtime_config_data()
        providers = cfg_data.get("model_providers")
        if not isinstance(providers, list):
            return []
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for item in providers:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            params_raw = item.get("params", {})
            if not provider or not isinstance(params_raw, dict):
                continue
            parsed_models = parse_configured_models(params_raw)
            if not parsed_models:
                continue
            base_params = dict(params_raw)
            base_params["models"] = [str(item.get("name") or "").strip() for item in parsed_models]
            for model_item in parsed_models:
                model_name = str(model_item.get("name") or "").strip()
                context_window = int(model_item.get("context_window") or 0)
                selector = f"{provider}:{model_name}"
                key = selector.lower()
                if key in seen:
                    continue
                seen.add(key)
                params = dict(base_params)
                params["model"] = model_name
                params["context_window"] = context_window
                out.append(
                    {
                        "provider": provider,
                        "name": model_name,
                        "selector": selector,
                        "params": params,
                    }
                )
        return out

    def _get_configured_model_selectors(self) -> List[str]:
        return [str(item.get("selector") or "") for item in self._get_configured_model_catalog()]

    def _current_model_selector(self) -> str:
        provider = str(getattr(self, "provider", "") or "").strip()
        model_name = str(getattr(self, "model_name", "") or "").strip()
        if not provider or not model_name:
            return ""
        return f"{provider}:{model_name}"

    def _find_configured_model_choice(self, selector: str) -> Optional[Dict[str, Any]]:
        needle = str(selector or "").strip().lower()
        if not needle:
            return None
        for item in self._get_configured_model_catalog():
            if str(item.get("selector") or "").strip().lower() == needle:
                return item
        return None

    def _apply_runtime_model_choice(self, choice: Dict[str, Any], validate: bool = False) -> None:
        provider = str(choice.get("provider") or "").strip()
        model_name = str(choice.get("name") or "").strip()
        params = dict(choice.get("params") or {})
        if model_name:
            params["model"] = model_name
        self.provider = provider
        self.model_name = model_name
        self.params = params
        self.openai_conf = self.params if self.provider == "openai" else None
        self.openwebui_conf = self.params if self.provider == "openwebui" else None
        try:
            ctx = self.ai_orchestrator.context
            ctx.provider = self.provider
            ctx.model_name = self.model_name
            ctx.openai_conf = self.openai_conf
            ctx.openwebui_conf = self.openwebui_conf
        except Exception:
            pass
        if validate and self.provider == "ollama":
            try:
                self._validate_single_model(self.provider, self.model_name, "model")
            except Exception:
                pass

    def _set_active_chat_model(
        self,
        provider: str,
        model_name: str,
        save_state: bool = True,
    ) -> None:
        with self._chat_state_lock:
            chat = self._find_chat_by_id(self.active_chat_id)
            if not chat:
                return
            chat["model_provider"] = str(provider or "").strip()
            chat["model_name"] = str(model_name or "").strip()
            chat["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if save_state:
                self._save_chat_state()

    def _apply_chat_model_from_entry(
        self, chat: Dict[str, Any], persist_if_missing: bool = False
    ) -> bool:
        provider = str((chat or {}).get("model_provider") or "").strip()
        model_name = str((chat or {}).get("model_name") or "").strip()
        if not provider or not model_name:
            if persist_if_missing:
                chat["model_provider"] = str(getattr(self, "provider", "") or "").strip()
                chat["model_name"] = str(getattr(self, "model_name", "") or "").strip()
            return False

        current = self._current_model_selector().lower()
        selector = f"{provider}:{model_name}".lower()
        if selector == current:
            return False

        choice = self._find_configured_model_choice(f"{provider}:{model_name}")
        if choice:
            self._apply_runtime_model_choice(choice, validate=False)
        else:
            fallback_params = dict(getattr(self, "params", {}) or {})
            if str(getattr(self, "provider", "") or "").strip().lower() != provider.lower():
                fallback_params = {}
            fallback_params["model"] = model_name
            self._apply_runtime_model_choice(
                {
                    "provider": provider,
                    "name": model_name,
                    "params": fallback_params,
                    "selector": f"{provider}:{model_name}",
                },
                validate=False,
            )
        return True

    def _restore_active_chat_model(self) -> None:
        with self._chat_state_lock:
            chat = self._find_chat_by_id(self.active_chat_id)
            if not chat:
                return
            self._apply_chat_model_from_entry(chat, persist_if_missing=True)
            self._save_chat_state()

    def _switch_model_by_selector(self, selector: str) -> str:
        choice = self._find_configured_model_choice(selector)
        if not choice:
            selectors = self._get_configured_model_selectors()
            if selectors:
                return (
                    f"❌ Model not found: {selector}\n"
                    "Available models:\n  - " + "\n  - ".join(selectors)
                )
            return "❌ Model configuration not found. Please check model_providers in config.json."

        target = str(choice.get("selector") or "").strip()
        if target.lower() == self._current_model_selector().lower():
            self._set_active_chat_model(
                str(choice.get("provider") or ""),
                str(choice.get("name") or ""),
                save_state=True,
            )
            self._refresh_status_context_usage_snapshot()
            return f"ℹ️ Current model already in use: {target}"

        self._apply_runtime_model_choice(choice, validate=True)
        self._set_active_chat_model(self.provider, self.model_name, save_state=True)
        self._refresh_status_context_usage_snapshot()
        return f"✅ Switched model: {target}"

    def _handle_model_builtin_command(self, builtin_line: str) -> bool:
        return handle_model_builtin_command(self, builtin_line)

    def _new_chat_entry(self, chat_id: str, name: str = "New Chat") -> Dict[str, Any]:
        return self._chat_state_manager.new_chat_entry(chat_id, name=name)

    def _save_chat_state(self) -> None:
        self._chat_state_manager.save_chat_state()

    def _chat_entries(self) -> List[Dict[str, Any]]:
        return self._chat_state_manager.chat_entries()

    def _find_chat_by_id(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return self._chat_state_manager.find_chat_by_id(chat_id)

    def _resolve_chat_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        return self._chat_state_manager.resolve_chat_selector(selector)

    def _next_chat_id(self) -> str:
        return self._chat_state_manager.next_chat_id()

    def _load_chat_state(self) -> None:
        self._chat_state_manager.load_chat_state()

    def _sync_active_chat_messages(self) -> None:
        self._chat_state_manager.sync_active_chat_messages()

    def _persist_active_chat_usage_snapshot(self) -> None:
        self._chat_state_manager.persist_active_chat_usage_snapshot()

    def _clear_active_chat_context_and_tasks(self) -> None:
        self.conversation_history.clear()
        self.operation_results.clear()
        self._last_auto_removed_ephemeral = None
        self._session_summary_llm = ""
        self._session_summary_rolling = ""
        self._last_llm_summary_pair_count = 0
        self._active_runtime_task_id = ""
        self._active_runtime_task_domains = []
        self._last_context_usage_percent = 0
        self._last_context_input_tokens = 0
        self._chat_state_manager.clear_chat_context_and_tasks(self.active_chat_id)
        try:
            self._persist_active_chat_usage_snapshot()
        except Exception:
            pass

    def _start_chat_task(
        self,
        root_user_input: str,
        domains: List[str],
        classifier: Optional[Dict[str, Any]] = None,
        switched_from_task_id: str = "",
    ) -> str:
        tid = self._chat_state_manager.start_task(
            self.active_chat_id,
            root_user_input=root_user_input,
            domains=domains,
            classifier=classifier,
            switched_from_task_id=switched_from_task_id,
        )
        task_id = str(tid or "").strip()
        if task_id:
            self._active_runtime_task_id = task_id
        dvals = [str(x).strip() for x in (domains or []) if str(x).strip()]
        self._active_runtime_task_domains = dvals if dvals else ["general_other"]
        try:
            self._refresh_status_context_usage_snapshot(
                user_input_hint=str(root_user_input or ""),
                context_hint="task started",
            )
        except Exception:
            pass
        try:
            svc = getattr(self, "session_memory_service", None)
            schedule_refresh = getattr(svc, "schedule_context_usage_refresh_async", None)
            if callable(schedule_refresh):
                schedule_refresh(
                    user_input_hint=str(root_user_input or ""),
                    context_hint="task started",
                )
        except Exception:
            pass
        return task_id

    def _close_chat_task(self, task_id: str, status: str) -> bool:
        tid = str(task_id or "").strip()
        if not tid:
            return False
        return bool(self._chat_state_manager.close_task(self.active_chat_id, tid, status))

    @staticmethod
    def _parse_domain_classifier_json(text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw or raw.startswith("❌") or raw.startswith("Error calling LLM"):
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```\s*$", "", raw)
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            start = raw.find("{")
            if start >= 0:
                depth = 0
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        depth += 1
                    elif raw[i] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                data = json.loads(raw[start : i + 1])
                            except Exception:
                                data = None
                            break
        if not isinstance(data, dict):
            return None
        return data

    def _classify_task_domains(self, user_input: str) -> Dict[str, Any]:
        default = {
            "primary_domain": "general_other",
            "secondary_domains": [],
            "confidence": 0.0,
            "reason": "fallback",
            "domains": ["general_other"],
        }
        try:
            raw = self.call_ai(
                str(user_input or ""),
                context="",
                stream=False,
                domain_classifier_mode=True,
                history_skip_user=True,
            )
        except Exception:
            return default
        if not isinstance(raw, str):
            return default
        parsed = self._parse_domain_classifier_json(raw)
        if not isinstance(parsed, dict):
            return default
        primary = str(parsed.get("primary_domain") or "").strip()
        if primary not in TASK_DOMAIN_VALUES:
            primary = "general_other"
        secondary_raw = parsed.get("secondary_domains")
        secondary: List[str] = []
        if isinstance(secondary_raw, list):
            for item in secondary_raw:
                dom = str(item or "").strip()
                if dom and dom in TASK_DOMAIN_VALUES and dom != primary and dom not in secondary:
                    secondary.append(dom)
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = str(parsed.get("reason") or "").strip()
        domains = [primary] + [d for d in secondary if d != primary]
        return {
            "primary_domain": primary,
            "secondary_domains": secondary,
            "confidence": confidence,
            "reason": reason,
            "domains": domains or ["general_other"],
        }

    def _domain_specific_system_prompt_append(self) -> str:
        domains = list(getattr(self, "_active_runtime_task_domains", None) or [])
        if not domains:
            return ""
        cache = getattr(self, "_domain_prompt_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._domain_prompt_cache = cache
        blocks: List[str] = []
        seen: Set[str] = set()
        for d in domains:
            dom = str(d or "").strip()
            if not dom or dom in seen or dom == "general_other":
                continue
            seen.add(dom)
            text = ""
            path_name = DOMAIN_PROMPT_FILE_MAP.get(dom, "")
            if path_name:
                if path_name in cache:
                    text = str(cache.get(path_name) or "")
                else:
                    p = Path(__file__).resolve().parent / "prompts" / path_name
                    try:
                        text = p.read_text(encoding="utf-8").strip()
                    except Exception:
                        text = ""
                    cache[path_name] = text
            if text:
                blocks.append(text.strip())
        if not blocks:
            return ""
        return "\n\n" + "\n\n".join(blocks) + "\n"

    def _activate_chat(
        self,
        chat_id: str,
        announce: bool = True,
        clear_screen: bool = False,
        print_history: bool = False,
    ) -> str:
        return self._chat_state_manager.activate_chat(
            chat_id,
            announce=announce,
            clear_screen=clear_screen,
            print_history=print_history,
        )

    def _active_chat_history_anchor_key(self) -> str:
        workspace_id = str(getattr(self, "workspace_id", "") or "").strip()
        chat_id = str(getattr(self, "active_chat_id", "") or "").strip()
        if not chat_id:
            return ""
        if workspace_id:
            return f"{workspace_id}::{chat_id}"
        return chat_id

    def _remember_active_chat_history_first_visible_index(self, index: int) -> None:
        key = self._active_chat_history_anchor_key()
        if not key:
            return
        try:
            total = len(list(self.conversation_history or []))
        except Exception:
            total = 0
        idx = max(0, min(int(index or 0), total))
        anchors = getattr(self, "_chat_history_first_visible_index_map", None)
        if not isinstance(anchors, dict):
            anchors = {}
            self._chat_history_first_visible_index_map = anchors
        anchors[key] = idx

    def _get_active_chat_history_first_visible_index(self) -> int:
        key = self._active_chat_history_anchor_key()
        if not key:
            return 0
        anchors = getattr(self, "_chat_history_first_visible_index_map", None)
        if not isinstance(anchors, dict):
            return 0
        try:
            total = len(list(self.conversation_history or []))
        except Exception:
            total = 0
        try:
            idx = int(anchors.get(key, 0) or 0)
        except Exception:
            idx = 0
        return max(0, min(idx, total))

    def _reload_chat_history_from_anchor_on_resize(self) -> None:
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            pass
        try:
            from .runtime.runtime_loop import _print_startup_overview

            _print_startup_overview(self)
        except Exception:
            pass
        self._print_chat_history(start_index=self._get_active_chat_history_first_visible_index())

    def _handle_terminal_columns_changed_during_input(self, previous_cols: int, new_cols: int) -> bool:
        try:
            prev = int(previous_cols or 0)
            now = int(new_cols or 0)
        except Exception:
            return False
        if prev <= 0 or now <= 0 or prev == now:
            return False
        self._chat_history_reload_last_terminal_width = now
        self._force_reload_chat_history_from_anchor_once = True
        return True

    def _maybe_reload_chat_history_on_terminal_resize(self) -> None:
        try:
            width = max(1, int(self._terminal_columns_for_prompt_separator(default=80)))
        except Exception:
            return
        prev = int(getattr(self, "_chat_history_reload_last_terminal_width", 0) or 0)
        self._chat_history_reload_last_terminal_width = width
        if prev <= 0 or prev == width:
            return
        self._reload_chat_history_from_anchor_on_resize()

    @staticmethod
    def _tool_call_history_match_key(tool_name: str, args: Dict[str, Any]) -> str:
        tname = str(tool_name or "").strip()
        a = args if isinstance(args, dict) else {}
        try:
            payload = {"tool": tname, "args": a}
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            return tname

    def _consume_tool_call_failed_state_from_operation_results(
        self,
        operation_results: List[Dict[str, Any]],
        cursor: int,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Tuple[bool, int]:
        result, idx = self._consume_tool_call_result_from_operation_results(
            operation_results,
            cursor,
            tool_name,
            args,
        )
        return (not bool(result.get("success", True))), idx

    def _consume_tool_call_result_from_operation_results(
        self,
        operation_results: List[Dict[str, Any]],
        cursor: int,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], int]:
        items = operation_results if isinstance(operation_results, list) else []
        idx = max(0, int(cursor or 0))
        target_tool = str(tool_name or "").strip()
        target_key = self._tool_call_history_match_key(target_tool, args)
        while idx < len(items):
            item = items[idx]
            idx += 1
            if not isinstance(item, dict):
                continue
            cmd = item.get("command")
            if not isinstance(cmd, dict):
                continue
            op_tool = str(cmd.get("tool") or cmd.get("action") or "").strip()
            if not op_tool or op_tool != target_tool:
                continue
            op_args = cmd.get("args")
            if not isinstance(op_args, dict):
                op_args = {}
            op_key = self._tool_call_history_match_key(op_tool, op_args)
            if op_key != target_key:
                continue
            res = item.get("result")
            if not isinstance(res, dict):
                res = {}
            return res, idx
        return {}, cursor

    def _extract_model_shell_replay_output(self, shell_result: Any) -> Tuple[str, str]:
        payload = shell_result if isinstance(shell_result, dict) else {}
        out_text = str(payload.get("display_output") or "")
        err_text = str(payload.get("display_stderr") or "")
        if not out_text and not err_text:
            out_text = str(payload.get("output") or "")
            err_text = str(payload.get("stderr") or "")
        return out_text, err_text

    def _model_tool_result_matches_plan(
        self,
        tool_name: str,
        args: Dict[str, Any],
        payload: Any,
    ) -> bool:
        if not isinstance(payload, dict):
            return False
        p_tool = str(payload.get("tool") or "").strip()
        if not p_tool:
            return False
        p_args = payload.get("args")
        if not isinstance(p_args, dict):
            p_args = {}
        target_key = self._tool_call_history_match_key(str(tool_name or "").strip(), args if isinstance(args, dict) else {})
        payload_key = self._tool_call_history_match_key(p_tool, p_args)
        return target_key == payload_key

    def _refresh_chat_history_after_tool_output(self) -> None:
        start = self._get_active_chat_history_first_visible_index()
        self._remember_active_chat_history_first_visible_index(start)
        try:
            self._sync_active_chat_messages()
        except Exception:
            pass
        self._reload_chat_history_from_anchor_on_resize()

    def _print_chat_history(self, start_index: int = 0) -> None:
        all_hist = list(self.conversation_history or [])
        start = max(0, min(int(start_index or 0), len(all_hist)))
        self._remember_active_chat_history_first_visible_index(start)
        hist = all_hist[start:]
        operation_results = list(getattr(self, "operation_results", None) or [])
        tool_result_cursor = 0
        if not hist:
            self._show_separator_next_prompt = False
            return
        for idx, msg in enumerate(hist):
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            if role == "user":
                direct_cmd = self._parse_direct_shell_user_history_content(content)
                if direct_cmd:
                    failed = False
                    if idx + 1 < len(hist):
                        nxt = hist[idx + 1]
                        nxt_role = str(nxt.get("role") or "").strip().lower()
                        if nxt_role == "assistant":
                            nxt_payload = self._parse_direct_shell_result_history_content(
                                str(nxt.get("content") or "")
                            )
                            if isinstance(nxt_payload, dict):
                                try:
                                    failed = int(nxt_payload.get("return_code") or 0) != 0
                                except Exception:
                                    failed = False
                    self._print_direct_shell_command_feedback(
                        direct_cmd,
                        failed=failed,
                        erase_previous=False,
                    )
                    continue
                slash_cmd = self._parse_internal_slash_user_history_content(content)
                if slash_cmd:
                    print(self._format_user_chat_display_message(slash_cmd))
                    continue
                print(self._format_user_chat_display_message(content))
            elif role == "assistant":
                interrupted_event = self._parse_conversation_interrupted_history_content(content)
                if interrupted_event is not None:
                    self._print_conversation_interrupted_banner()
                    continue
                direct_result = self._parse_direct_shell_result_history_content(content)
                if direct_result is not None:
                    aborted_result = self._is_direct_shell_result_aborted(direct_result)
                    out_text = str(direct_result.get("stdout") or "")
                    err_text = str(direct_result.get("stderr") or "")
                    if aborted_result:
                        merged = out_text + err_text
                        out_text = self._normalize_aborted_direct_shell_stdout_for_history(merged)
                        err_text = ""
                    force_continuation = False
                    if aborted_result:
                        head = out_text.lstrip("\r\n").lower()
                        force_continuation = head.startswith("command aborted by user")
                    self._print_direct_shell_history_output(
                        out_text,
                        err_text,
                        force_first_line_continuation=force_continuation,
                    )
                    if aborted_result:
                        self._print_conversation_interrupted_banner()
                    else:
                        self._print_direct_shell_history_separator()
                    continue
                slash_result = self._parse_internal_slash_result_history_content(content)
                if slash_result is not None:
                    self._print_internal_slash_history_output(
                        str(slash_result.get("output") or "")
                    )
                    continue
                worked_summary = self._parse_task_worked_summary_history_content(content)
                if worked_summary is not None:
                    try:
                        elapsed_seconds = int(worked_summary.get("elapsed_seconds") or 0)
                    except Exception:
                        elapsed_seconds = 0
                    self._print_task_worked_summary_line(elapsed_seconds)
                    continue
                model_tool_result = self._parse_model_tool_result_history_content(content)
                if model_tool_result is not None:
                    model_tool = str(model_tool_result.get("tool") or "").strip()
                    model_args = model_tool_result.get("args")
                    if not isinstance(model_args, dict):
                        model_args = {}
                    failed = not bool(model_tool_result.get("success", True))
                    if model_tool:
                        self._print_tool_call_feedback(model_tool, model_args, failed=failed)
                    if model_tool == "shell":
                        out_text, err_text = self._extract_model_shell_replay_output(model_tool_result)
                        if out_text or err_text:
                            self._print_direct_shell_history_output(out_text, err_text)
                    continue
                display_response = format_assistant_display_response(content)
                if display_response:
                    print(self._format_assistant_chat_display_message(display_response))
                tool_plan = self._find_tool_plan_anywhere(content)
                if tool_plan:
                    tool_name, args = tool_plan
                    if tool_name != "done":
                        if tool_name == "shell" and (idx + 1) < len(hist):
                            nxt = hist[idx + 1]
                            nxt_role = str(nxt.get("role") or "").strip().lower()
                            if nxt_role == "assistant":
                                nxt_payload = self._parse_model_tool_result_history_content(
                                    str(nxt.get("content") or "")
                                )
                                if self._model_tool_result_matches_plan(tool_name, args, nxt_payload):
                                    continue
                        tool_result, tool_result_cursor = self._consume_tool_call_result_from_operation_results(
                            operation_results,
                            tool_result_cursor,
                            tool_name,
                            args,
                        )
                        failed = not bool(tool_result.get("success", True))
                        self._print_tool_call_feedback(tool_name, args, failed=failed)
                        if tool_name == "shell":
                            out_text, err_text = self._extract_model_shell_replay_output(tool_result)
                            if out_text or err_text:
                                self._print_direct_shell_history_output(out_text, err_text)
            else:
                print(content)
        self._show_separator_next_prompt = False

    def _print_direct_shell_history_separator(self) -> None:
        width = max(1, int(self._terminal_columns_for_line_estimate()))
        print("")
        try:
            print(_ansi_gray("─" * width))
        except Exception:
            print(_ansi_gray("-" * width))
        print("")

    def _format_task_worked_summary_line(self, elapsed_seconds: int, terminal_width: Optional[int] = None) -> str:
        total = max(0, int(elapsed_seconds or 0))
        minutes, seconds = divmod(total, 60)
        if minutes <= 0:
            elapsed = f"{seconds}s"
        else:
            elapsed = f"{minutes}m {seconds}s"
        head = f"─ Worked for {elapsed} "
        width = max(20, int(terminal_width or self._terminal_columns_for_line_estimate()))
        if len(head) >= width:
            return head[:width]
        return head + ("─" * (width - len(head)))

    def _print_task_worked_summary_line(self, elapsed_seconds: int) -> None:
        line = self._format_task_worked_summary_line(elapsed_seconds)
        print("")
        print(_ansi_gray(line))
        print("")

    def _build_task_worked_summary_history_content(self, elapsed_seconds: int) -> str:
        payload = {
            "kind": "task_worked_summary",
            "elapsed_seconds": max(0, int(elapsed_seconds or 0)),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return f"{TASK_WORKED_SUMMARY_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_task_worked_summary_history_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(TASK_WORKED_SUMMARY_HISTORY_PREFIX):
            return None
        body = text[len(TASK_WORKED_SUMMARY_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "task_worked_summary":
            return None
        return payload

    def _record_task_worked_summary_history(self, elapsed_seconds: int) -> None:
        content = self._build_task_worked_summary_history_content(elapsed_seconds)
        self._append_chat_message("assistant", content)

    def _rewrite_previous_prompt_as_user(self, user_text: str) -> None:
        """
        Best-effort: clear previous prompt line, then rewrite as gray '›' line.
        """
        txt = str(user_text or "")
        if not txt:
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        rendered = self._format_user_chat_display_message(txt)
        display_probe = rendered
        line_count = max(1, int(self._estimate_rendered_line_count(display_probe)))
        try:
            # Current cursor is on line after Enter. Clear all prompt input rows
            # (multi-line input may have consumed multiple rows), then redraw once.
            for _ in range(line_count):
                sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.write(f"{rendered}\n")
            sys.stdout.flush()
        except Exception:
            pass

    def _clear_last_thinking_line(self) -> None:
        """
        Best-effort clear of the current transient working-status line.
        """
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass

    def _terminal_columns_for_line_estimate(self) -> int:
        width = 80
        # Keep wrap behavior aligned with prompt_toolkit's live output viewport
        # only when the prompt session is active. When session is inactive,
        # some input handlers return fallback width (often 80), which causes
        # premature wraps and large right-side whitespace.
        ih = getattr(self, "input_handler", None)
        try:
            if ih is not None and hasattr(ih, "get_terminal_columns"):
                has_session_attr = hasattr(ih, "session")
                session_active = bool(getattr(ih, "session", None)) if has_session_attr else True
                if session_active:
                    cols0 = int(ih.get_terminal_columns(default=0) or 0)
                    if cols0 > 0:
                        return cols0
        except Exception:
            pass
        try:
            cols1 = int(os.get_terminal_size(sys.__stdout__.fileno()).columns or 0)
            if cols1 > 0:
                return cols1
        except Exception:
            pass
        try:
            cols2 = int(os.get_terminal_size(sys.stdout.fileno()).columns or 0)
            if cols2 > 0:
                return cols2
        except Exception:
            pass
        try:
            cols3 = int(shutil.get_terminal_size(fallback=(80, 24)).columns or 80)
            if cols3 > 0:
                width = cols3
        except Exception:
            pass
        return max(1, int(width))

    def _estimate_rendered_line_count(self, text: str) -> int:
        raw = str(text or "")
        if not raw:
            return 0
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        parts = normalized.split("\n")
        if parts and parts[-1] == "":
            parts = parts[:-1]
        if not parts:
            return 0
        width = self._terminal_columns_for_line_estimate()
        total = 0
        for part in parts:
            clean = ANSI_ESCAPE_RE.sub("", part).expandtabs(4)
            plen = 0
            for ch in clean:
                if unicodedata.combining(ch):
                    continue
                cat = unicodedata.category(ch)
                if cat in ("Cc", "Cf"):
                    continue
                plen += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            total += max(1, (plen + width - 1) // width)
        return total

    def _register_shell_output_for_auto_hide(self, stdout_text: str, stderr_text: str = "") -> None:
        total = self._estimate_rendered_line_count(stdout_text) + self._estimate_rendered_line_count(stderr_text)
        prev = int(getattr(self, "_last_shell_output_visible_lines", 0) or 0)
        self._last_shell_output_visible_lines = max(0, prev + int(total))

    def _hide_previous_shell_output_if_needed(self, safety_buffer_lines: int = 0) -> None:
        lines = int(getattr(self, "_last_shell_output_visible_lines", 0) or 0)
        if lines <= 0:
            return
        extra = max(0, int(safety_buffer_lines or 0))
        lines = lines + extra
        self._last_shell_output_visible_lines = 0
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            for _ in range(min(lines, 2000)):
                sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass

    def _print_prompt_separator(self) -> None:
        """
        在命令提示符前输出一行分隔符，宽度随终端窗口实时变化。
        """
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        width = self._terminal_columns_for_prompt_separator(default=80)
        width = max(1, int(width))
        try:
            print("")
            print(_ansi_gray("─" * width))
            print("")
            self._prompt_separator_rendered = True
        except Exception:
            try:
                print("")
                print(_ansi_gray("-" * width))
                print("")
                self._prompt_separator_rendered = True
            except Exception:
                self._prompt_separator_rendered = False

    def _terminal_columns_for_prompt_separator(self, default: int = 80) -> int:
        width = max(1, int(default or 80))
        width_from_input_handler = False
        ih = getattr(self, "input_handler", None)
        try:
            if ih is not None and hasattr(ih, "get_terminal_columns"):
                cols = int(ih.get_terminal_columns(default=width) or 0)
                if cols > 0:
                    width = cols
                    width_from_input_handler = True
        except Exception:
            pass
        if not width_from_input_handler:
            try:
                width1 = int(os.get_terminal_size(sys.__stdout__.fileno()).columns or 0)
                if width1 > 0:
                    width = width1
            except Exception:
                pass
            if width <= 0:
                width = 80
            try:
                width2 = int(os.get_terminal_size(sys.stdout.fileno()).columns or 0)
                if width2 > 0:
                    width = width2
            except Exception:
                pass
            if width <= 0:
                try:
                    width = int(shutil.get_terminal_size(fallback=(width, 24)).columns or width)
                except Exception:
                    width = max(1, int(default or 80))
        return max(1, int(width))

    def _print_conversation_interrupted_banner(self) -> int:
        msg = "■ Conversation interrupted - tell the model what to do differently. Something went wrong?"
        print("")
        try:
            print(_ansi_rgb(msg, 197, 15, 31))
        except Exception:
            print(msg)
        print("")
        try:
            self._conversation_interrupt_banner_recent = True
            self._conversation_interrupt_banner_recent_at = float(time.monotonic())
        except Exception:
            pass
        return 3

    def _consume_conversation_interrupted_banner_recent(self) -> bool:
        try:
            recent = bool(getattr(self, "_conversation_interrupt_banner_recent", False))
            recent_at = float(getattr(self, "_conversation_interrupt_banner_recent_at", 0.0) or 0.0)
            fresh = False
            if recent:
                if recent_at <= 0.0:
                    fresh = True
                else:
                    fresh = (float(time.monotonic()) - recent_at) <= float(
                        CONVERSATION_INTERRUPT_BANNER_RECENT_WINDOW_SECONDS
                    )
            self._conversation_interrupt_banner_recent = False
            self._conversation_interrupt_banner_recent_at = 0.0
            return bool(fresh)
        except Exception:
            return False

    def _clear_prompt_separator(self) -> None:
        """
        清理上一轮输入前显示的分隔符（不清理提示符行）。
        该方法应在用户按回车后、开始输出新消息前调用。
        """
        if not bool(getattr(self, "_prompt_separator_rendered", False)):
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            self._prompt_separator_rendered = False
            return
        try:
            # Current cursor is on the line below prompt after Enter:
            #   separator
            #   prompt line
            #   cursor here
            # Move to separator, clear it, and return to current line.
            sys.stdout.write("\x1b[2A\r\x1b[2K\x1b[2B\r")
            sys.stdout.flush()
        except Exception:
            pass
        self._prompt_separator_rendered = False

    def _erase_last_user_input_line(self) -> None:
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            sys.stdout.write("\x1b[1A\r\x1b[2K\r")
            sys.stdout.flush()
        except Exception:
            pass

    def _print_direct_shell_command_feedback(
        self,
        command: str,
        failed: bool = False,
        erase_previous: bool = True,
    ) -> None:
        if erase_previous:
            self._erase_last_user_input_line()
        line = self._format_direct_shell_command_feedback_line(command, failed=failed)
        print(line)

    def _print_tool_call_feedback(
        self,
        tool_name: str,
        args: Dict[str, Any],
        failed: bool = False,
    ) -> None:
        line = self._format_tool_call_feedback_line(tool_name, args, failed=failed)
        print(line)

    def _format_tool_call_feedback_line(
        self,
        tool_name: str,
        args: Dict[str, Any],
        failed: bool = False,
    ) -> str:
        summary = self._tool_call_summary(tool_name, args)
        bullet = _ansi_rgb("•", 197, 15, 31) if bool(failed) else _ansi_rgb("•", 19, 161, 14)
        return self._format_wrapped_command_feedback_line(
            f"{bullet} Ran ",
            summary,
        )

    @staticmethod
    def _feedback_char_display_width(ch: str) -> int:
        if not ch:
            return 0
        if unicodedata.combining(ch):
            return 0
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf"):
            return 0
        return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

    @classmethod
    def _feedback_text_display_width(cls, text: str) -> int:
        total = 0
        clean = cls._strip_console_color_controls(str(text or ""))
        for ch in clean:
            total += cls._feedback_char_display_width(ch)
        return total

    @classmethod
    def _wrap_feedback_text_by_display_width(cls, text: str, max_width: int) -> List[str]:
        raw = str(text or "")
        if not raw:
            return [""]
        limit = max(1, int(max_width or 1))
        chunks: List[str] = []
        current: List[str] = []
        current_w = 0
        for ch in raw:
            ch_w = cls._feedback_char_display_width(ch)
            if current and (current_w + ch_w > limit):
                chunks.append("".join(current))
                current = [ch]
                current_w = ch_w
            else:
                current.append(ch)
                current_w += ch_w
        if current:
            chunks.append("".join(current))
        return chunks or [""]

    @classmethod
    def _wrap_ansi_text_by_display_width(cls, text: str, max_width: int) -> List[str]:
        raw = str(text or "")
        if not raw:
            return [""]
        limit = max(1, int(max_width or 1))
        chunks: List[str] = []
        current: List[str] = []
        current_w = 0
        active_sgr = ""
        i = 0
        n = len(raw)
        while i < n:
            if raw[i] == "\x1b":
                m = ANSI_ESCAPE_RE.match(raw, i)
                if not m:
                    m = ANSI_OSC_RE.match(raw, i)
                if m:
                    seq = m.group(0)
                    current.append(seq)
                    if seq.endswith("m") and seq.startswith("\x1b["):
                        codes = seq[2:-1]
                        if (not codes) or codes == "0":
                            active_sgr = ""
                        else:
                            active_sgr = seq
                    i = m.end()
                    continue
            ch = raw[i]
            ch_w = cls._feedback_char_display_width(ch)
            if current_w > 0 and (current_w + ch_w > limit):
                chunks.append("".join(current))
                current = [active_sgr] if active_sgr else []
                current_w = 0
                continue
            current.append(ch)
            current_w += ch_w
            i += 1
        if current:
            chunks.append("".join(current))
        return chunks or [""]

    def _format_chat_message_with_wrap(
        self,
        marker_symbol: str,
        message_text: str,
        *,
        colored_text: bool = False,
    ) -> str:
        marker = str(marker_symbol or "").strip() or "•"
        raw = str(message_text or "")
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
        logical_lines = normalized.split("\n") if normalized else [""]
        first_prefix_plain = f"{marker} "
        first_prefix_ansi = f"{_ansi_gray(marker)} "
        cont_prefix_plain = "  "
        cols = max(8, int(self._terminal_columns_for_line_estimate() or 80))
        first_line_width = max(1, cols - self._feedback_text_display_width(first_prefix_plain))
        cont_line_width = max(1, cols - self._feedback_text_display_width(cont_prefix_plain))

        rendered: List[str] = []
        for idx, logical_line in enumerate(logical_lines):
            is_first_logical_line = idx == 0
            line_prefix = first_prefix_ansi if is_first_logical_line else cont_prefix_plain
            max_width = first_line_width if is_first_logical_line else cont_line_width
            if colored_text:
                chunks = self._wrap_ansi_text_by_display_width(logical_line, max_width)
            else:
                chunks = self._wrap_feedback_text_by_display_width(logical_line, max_width)
            if not chunks:
                chunks = [""]
            rendered.append(f"{line_prefix}{chunks[0]}")
            if len(chunks) > 1:
                for seg in chunks[1:]:
                    rendered.append(f"{cont_prefix_plain}{seg}")
        return "\n".join(rendered)

    def _format_user_chat_display_message(self, user_text: str) -> str:
        return self._format_chat_message_with_wrap("›", user_text, colored_text=False)

    def _format_assistant_chat_display_message(self, assistant_text: str) -> str:
        return self._format_chat_message_with_wrap("•", assistant_text, colored_text=True)

    def _terminal_columns_for_command_feedback(self) -> int:
        width = 0
        try:
            cols1 = int(os.get_terminal_size(sys.__stdout__.fileno()).columns or 0)
            if cols1 > 0:
                width = cols1
        except Exception:
            pass
        try:
            cols2 = int(os.get_terminal_size(sys.stdout.fileno()).columns or 0)
            if cols2 > 0:
                width = cols2 if width <= 0 else max(width, cols2)
        except Exception:
            pass
        if width <= 0:
            try:
                cols3 = int(shutil.get_terminal_size(fallback=(80, 24)).columns or 80)
                if cols3 > 0:
                    width = cols3
            except Exception:
                pass
        if width <= 0:
            ih = getattr(self, "input_handler", None)
            try:
                if ih is not None and hasattr(ih, "get_terminal_columns"):
                    cols4 = int(ih.get_terminal_columns(default=80) or 0)
                    if cols4 > 0:
                        width = cols4
            except Exception:
                pass
        return max(1, int(width or 80))

    def _format_wrapped_command_feedback_line(self, lead_prefix: str, command_text: str) -> str:
        lead = str(lead_prefix or "")
        cmd = str(command_text or "").replace("\r", " ").replace("\n", " ").strip()
        cols = max(8, int(self._terminal_columns_for_command_feedback() or 80))
        cont_prefix = _ansi_gray("  │ ")
        first_line_width = max(1, cols - self._feedback_text_display_width(lead))
        cont_line_width = max(1, cols - self._feedback_text_display_width("  │ "))
        highlighted_cmd = highlight_assistant_display_line(cmd)
        first_chunks = self._wrap_ansi_text_by_display_width(highlighted_cmd, first_line_width)
        if not first_chunks:
            return lead
        rendered = [f"{lead}{first_chunks[0]}"]
        if len(first_chunks) > 1:
            # Re-wrap the full tail using continuation width. If we keep wrapping
            # tail chunks individually, they remain constrained by first-line width
            # and leave large right-side whitespace on continuation rows.
            tail = "".join(first_chunks[1:])
            wrapped = self._wrap_ansi_text_by_display_width(tail, cont_line_width)
            for seg in wrapped:
                rendered.append(f"{cont_prefix}{seg}")
        return "\n".join(rendered)

    def _repaint_tool_call_feedback_if_failed(
        self,
        tool_name: str,
        args: Dict[str, Any],
        failed: bool,
        up_lines: int = 1,
    ) -> None:
        if not bool(failed):
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            line = self._format_tool_call_feedback_line(tool_name, args, failed=True)
            line_rows = max(1, int(self._estimate_rendered_line_count(line) or 1))
            offset = max(1, int(up_lines or 1) + (line_rows - 1))
            # Best-effort: repaint previous "Ran ..." line in failure color.
            sys.stdout.write("\x1b7")
            sys.stdout.write(f"\x1b[{offset}A\r\x1b[2K{line}")
            sys.stdout.write("\x1b8")
            sys.stdout.flush()
        except Exception:
            # Avoid duplicate feedback lines when repaint fails.
            pass

    def _format_direct_shell_command_feedback_line(self, command: str, failed: bool = False) -> str:
        cmd = str(command or "").replace("\r", " ").replace("\n", " ").strip()
        bullet = _ansi_rgb("•", 197, 15, 31) if bool(failed) else _ansi_rgb("•", 19, 161, 14)
        return self._format_wrapped_command_feedback_line(
            f"{bullet} You ran ",
            cmd,
        )

    def _repaint_direct_shell_command_feedback_if_failed(
        self,
        command: str,
        rendered_output_lines: int,
        cursor_at_line_start: bool,
        failed: bool,
    ) -> None:
        if not bool(failed):
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            rendered = max(0, int(rendered_output_lines or 0))
            at_line_start = bool(cursor_at_line_start)
            line = self._format_direct_shell_command_feedback_line(command, failed=True)
            line_rows = max(1, int(self._estimate_rendered_line_count(line) or 1))
            offset = rendered + (1 if at_line_start else 0) + (line_rows - 1)
            if offset <= 0:
                offset = 1
            # Save current cursor, repaint command feedback line in red, restore cursor.
            sys.stdout.write("\x1b7")
            sys.stdout.write(f"\x1b[{offset}A\r\x1b[2K{line}")
            sys.stdout.write("\x1b8")
            sys.stdout.flush()
        except Exception:
            pass

    def _build_direct_shell_user_history_content(self, raw_user_command: str) -> str:
        cmd = str(raw_user_command or "").strip()
        return f"{DIRECT_SHELL_USER_HISTORY_PREFIX}{cmd}"

    def _parse_direct_shell_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_USER_HISTORY_PREFIX):
            return ""
        cmd = text[len(DIRECT_SHELL_USER_HISTORY_PREFIX):].strip()
        return cmd

    def _build_direct_shell_result_history_content(
        self,
        raw_user_command: str,
        executed_command: str,
        cwd: str,
        return_code: int,
        stdout_text: str,
        stderr_text: str,
        aborted_by_user: bool = False,
    ) -> str:
        payload = {
            "kind": "direct_shell_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "executed_command": str(executed_command or ""),
            "cwd": str(cwd or ""),
            "return_code": int(return_code),
            "stdout": str(stdout_text or ""),
            "stderr": str(stderr_text or ""),
            "aborted_by_user": bool(aborted_by_user),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return f"{DIRECT_SHELL_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_direct_shell_result_history_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(DIRECT_SHELL_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(DIRECT_SHELL_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "direct_shell_result":
            return None
        return payload

    def _build_model_tool_result_history_content(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
    ) -> str:
        t = str(tool_name or "").strip()
        a = args if isinstance(args, dict) else {}
        r = result if isinstance(result, dict) else {}
        payload = {
            "kind": "model_tool_result",
            "tool": t,
            "args": a,
            "success": bool(r.get("success", True)),
            "return_code": r.get("return_code"),
            "output": str(r.get("output") or ""),
            "stderr": str(r.get("stderr") or ""),
            "display_output": str(r.get("display_output") or ""),
            "display_stderr": str(r.get("display_stderr") or ""),
            "display_rendered_lines": int(r.get("display_rendered_lines") or 0),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return f"{MODEL_TOOL_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_model_tool_result_history_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(MODEL_TOOL_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(MODEL_TOOL_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "model_tool_result":
            return None
        return payload

    def _record_model_tool_execution_history(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        t = str(tool_name or "").strip().lower()
        if t != "shell":
            return
        content = self._build_model_tool_result_history_content(
            tool_name=str(tool_name or "").strip(),
            args=args if isinstance(args, dict) else {},
            result=result if isinstance(result, dict) else {},
        )
        self._append_chat_message("assistant", content)

    def _build_conversation_interrupted_history_content(
        self,
        interrupted_kind: str = "task",
        reason: str = "user_interrupt",
        detail: str = "",
    ) -> str:
        payload = {
            "kind": "conversation_interrupted",
            "interrupted_kind": str(interrupted_kind or "task"),
            "reason": str(reason or "user_interrupt"),
            "detail": str(detail or ""),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return f"{CONVERSATION_INTERRUPTED_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_conversation_interrupted_history_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):
            return None
        body = text[len(CONVERSATION_INTERRUPTED_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "conversation_interrupted":
            return None
        return payload

    def _record_conversation_interrupted_history(
        self,
        interrupted_kind: str = "task",
        reason: str = "user_interrupt",
        detail: str = "",
    ) -> None:
        assistant_content = self._build_conversation_interrupted_history_content(
            interrupted_kind=interrupted_kind,
            reason=reason,
            detail=detail,
        )
        self._append_chat_message("assistant", assistant_content)

    def _print_direct_shell_history_output(
        self,
        stdout_text: str,
        stderr_text: str,
        force_first_line_continuation: bool = False,
    ) -> int:
        shared_state: Dict[str, Any] = {"first_line_emitted": bool(force_first_line_continuation)}
        out_stream, err_stream = self._create_direct_shell_output_streams(shared_state)
        out = str(stdout_text or "")
        err = str(stderr_text or "")
        if out:
            out_stream.write(out)
            out_stream.flush()
        if err:
            err_stream.write(err)
            err_stream.flush()
        try:
            return max(0, int(shared_state.get("rendered_line_count", 0) or 0))
        except Exception:
            return 0

    def _record_direct_shell_execution_history(
        self,
        raw_user_command: str,
        executed_command: str,
        cwd: str,
        return_code: int,
        stdout_text: str,
        stderr_text: str,
        aborted_by_user: bool = False,
    ) -> None:
        raw_cmd = str(raw_user_command or "").strip()
        if not raw_cmd:
            return
        executed = str(executed_command or "").strip() or raw_cmd
        cwd_text = str(cwd or "").strip()
        user_content = self._build_direct_shell_user_history_content(raw_cmd)
        assistant_content = self._build_direct_shell_result_history_content(
            raw_user_command=raw_cmd,
            executed_command=executed,
            cwd=cwd_text,
            return_code=int(return_code),
            stdout_text=str(stdout_text or ""),
            stderr_text=str(stderr_text or ""),
            aborted_by_user=bool(aborted_by_user),
        )
        self._append_chat_message("user", user_content)
        self._append_chat_message("assistant", assistant_content)

    def _build_internal_slash_user_history_content(self, raw_user_command: str) -> str:
        cmd = str(raw_user_command or "").strip()
        return f"{INTERNAL_SLASH_USER_HISTORY_PREFIX}{cmd}"

    def _parse_internal_slash_user_history_content(self, content: str) -> str:
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_USER_HISTORY_PREFIX):
            return ""
        cmd = text[len(INTERNAL_SLASH_USER_HISTORY_PREFIX):].strip()
        return cmd

    def _build_internal_slash_result_history_content(
        self,
        raw_user_command: str,
        output_text: str,
    ) -> str:
        payload = {
            "kind": "internal_slash_result",
            "invoked_by": "user",
            "raw_user_command": str(raw_user_command or ""),
            "output": str(output_text or ""),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return f"{INTERNAL_SLASH_RESULT_HISTORY_PREFIX}{json.dumps(payload, ensure_ascii=False)}"

    def _parse_internal_slash_result_history_content(self, content: str) -> Optional[Dict[str, Any]]:
        text = str(content or "")
        if not text.startswith(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):
            return None
        body = text[len(INTERNAL_SLASH_RESULT_HISTORY_PREFIX):].strip()
        if not body:
            return None
        try:
            payload = json.loads(body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("kind") or "").strip() != "internal_slash_result":
            return None
        return payload

    def _print_internal_slash_history_output(self, output_text: str) -> None:
        out = str(output_text or "")
        if not out:
            return
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            print(out, end="")

    def _should_record_internal_slash_execution_history(self, raw_user_command: str) -> bool:
        cmd = str(raw_user_command or "").strip()
        if not cmd:
            return False
        normalized = cmd.lower()
        normalized = re.sub(r"\s+", " ", normalized)
        if normalized in {"/chat reload", "/clear context", "/clear screen"}:
            return False
        if normalized == "/chat switch" or normalized.startswith("/chat switch "):
            return False
        return True

    def _record_internal_slash_execution_history(
        self,
        raw_user_command: str,
        output_text: str,
    ) -> None:
        raw_cmd = str(raw_user_command or "").strip()
        if not self._should_record_internal_slash_execution_history(raw_cmd):
            return
        user_content = self._build_internal_slash_user_history_content(raw_cmd)
        assistant_content = self._build_internal_slash_result_history_content(
            raw_user_command=raw_cmd,
            output_text=str(output_text or ""),
        )
        self._append_chat_message("user", user_content)
        self._append_chat_message("assistant", assistant_content)

    def _is_direct_shell_result_aborted(self, result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if bool(result.get("aborted_by_user", False)):
            return True
        out_text = str(result.get("stdout") or "")
        return "command aborted by user" in out_text.lower()

    def _normalize_aborted_direct_shell_stdout_for_history(self, stdout_text: str) -> str:
        text = str(stdout_text or "")
        marker = "command aborted by user"
        if marker not in text.lower():
            return text
        lines = text.splitlines(keepends=True)
        kept: List[str] = []
        found = False
        for line in lines:
            low = line.lower()
            if marker not in low:
                kept.append(line)
                continue
            found = True
            idx = low.find(marker)
            rebuilt = line[:idx] + line[idx + len(marker):]
            if rebuilt.endswith("\n"):
                rebuilt = rebuilt.rstrip(" \t\r\n") + "\n"
            if rebuilt.strip():
                kept.append(rebuilt)
        merged = "".join(kept)
        if found:
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += "command aborted by user\n"
        return merged

    class _DirectShellOutputStream:
        def __init__(self, base_stream: Any, shared_state: Dict[str, Any]) -> None:
            self._base_stream = base_stream
            self._shared_state = shared_state
            self._line_start = True
            self._visual_col = 0

        @property
        def encoding(self) -> Optional[str]:
            try:
                return getattr(self._base_stream, "encoding", None)
            except Exception:
                return None

        def fileno(self) -> int:
            return self._base_stream.fileno()

        def isatty(self) -> bool:
            try:
                return bool(self._base_stream.isatty())
            except Exception:
                return False

        def flush(self) -> None:
            try:
                self._base_stream.flush()
            except Exception:
                pass

        def writable(self) -> bool:
            return True

        def _terminal_columns(self) -> int:
            try:
                if hasattr(self._base_stream, "fileno"):
                    cols = int(os.get_terminal_size(self._base_stream.fileno()).columns or 0)
                    if cols > 0:
                        return cols
            except Exception:
                pass
            try:
                cols = int(shutil.get_terminal_size(fallback=(80, 24)).columns or 80)
                if cols > 0:
                    return cols
            except Exception:
                pass
            return 80

        @staticmethod
        def _char_display_width(ch: str) -> int:
            if not ch:
                return 0
            if unicodedata.combining(ch):
                return 0
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf"):
                return 0
            return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

        def write(self, text: str) -> int:
            s = str(text or "")
            if not s:
                return 0
            s = SmartShellAgent._strip_console_color_controls(s)
            if not s:
                return 0
            if not bool(self._shared_state.get("_first_write_cleared_ticker_line", False)):
                self._shared_state["_first_write_cleared_ticker_line"] = True
                try:
                    self._base_stream.write("\r\x1b[2K")
                    self._base_stream.flush()
                except Exception:
                    pass
            if not bool(self._shared_state.get("_first_text_emitted_notified", False)):
                self._shared_state["_first_text_emitted_notified"] = True
                cb = self._shared_state.get("on_text_emitted")
                if callable(cb):
                    try:
                        cb()
                    except Exception:
                        pass
            term_cols = max(8, int(self._terminal_columns() or 80))
            out_parts: List[str] = []
            for ch in s:
                if self._line_start:
                    if not bool(self._shared_state.get("first_line_emitted", False)):
                        indent = "  └ "
                        self._shared_state["first_line_emitted"] = True
                    else:
                        indent = "    "
                    out_parts.append(indent)
                    self._visual_col = len(indent)
                    self._line_start = False
                    try:
                        self._shared_state["rendered_line_count"] = int(
                            self._shared_state.get("rendered_line_count", 0) or 0
                        ) + 1
                    except Exception:
                        self._shared_state["rendered_line_count"] = 1
                if ch == "\n":
                    out_parts.append("\n")
                    self._line_start = True
                    self._visual_col = 0
                    continue
                ch_w = self._char_display_width(ch)
                if ch_w > 0 and self._visual_col + ch_w > term_cols:
                    out_parts.append("\n    ")
                    self._line_start = False
                    self._visual_col = 4
                    try:
                        self._shared_state["rendered_line_count"] = int(
                            self._shared_state.get("rendered_line_count", 0) or 0
                        ) + 1
                    except Exception:
                        self._shared_state["rendered_line_count"] = 1
                out_parts.append(ch)
                self._visual_col += ch_w
            rendered = "".join(out_parts)
            self._base_stream.write(_ansi_gray(rendered))
            self._shared_state["cursor_at_line_start"] = bool(self._line_start)
            return len(s)

    @staticmethod
    def _strip_console_color_controls(text: str) -> str:
        raw = str(text or "")
        if not raw:
            return ""
        no_osc = ANSI_OSC_RE.sub("", raw)
        return ANSI_ESCAPE_RE.sub("", no_osc)

    def _build_direct_shell_output_stream(
        self, base_stream: Any, shared_state: Optional[Dict[str, Any]] = None
    ) -> Any:
        state = shared_state if isinstance(shared_state, dict) else {"first_line_emitted": False}
        return SmartShellAgent._DirectShellOutputStream(base_stream, state)

    def _create_direct_shell_output_streams(
        self, shared_state: Optional[Dict[str, Any]] = None
    ) -> Tuple[Any, Any]:
        if isinstance(shared_state, dict):
            state = shared_state
            state.setdefault("first_line_emitted", False)
            state.setdefault("rendered_line_count", 0)
            state.setdefault("cursor_at_line_start", True)
            state.setdefault("_first_write_cleared_ticker_line", False)
            state.setdefault("_first_text_emitted_notified", False)
        else:
            state = {
                "first_line_emitted": False,
                "rendered_line_count": 0,
                "cursor_at_line_start": True,
                "_first_write_cleared_ticker_line": False,
                "_first_text_emitted_notified": False,
            }
        out_stream = self._build_direct_shell_output_stream(sys.stdout, state)
        err_stream = self._build_direct_shell_output_stream(sys.stderr, state)
        return out_stream, err_stream

    def _register_interruptible_process(self, process: Any) -> None:
        if process is None:
            return
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        with lock:
            procs = getattr(self, "_interruptible_processes", None)
            if not isinstance(procs, dict):
                procs = {}
                self._interruptible_processes = procs
            procs[id(process)] = process

    def _unregister_interruptible_process(self, process: Any) -> None:
        if process is None:
            return
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        with lock:
            procs = getattr(self, "_interruptible_processes", None)
            if isinstance(procs, dict):
                try:
                    procs.pop(id(process), None)
                except Exception:
                    pass

    def _terminate_single_process_tree(self, process: Any) -> None:
        if process is None:
            return
        try:
            if hasattr(process, "poll") and process.poll() is not None:
                return
        except Exception:
            pass
        pid = None
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except Exception:
            pid = None
        if os.name == "nt" and pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            return
        try:
            if pid:
                import signal

                try:
                    os.killpg(pid, signal.SIGKILL)
                    return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass

    @staticmethod
    def _process_abort_key(process: Any) -> str:
        pid = None
        try:
            pid = int(getattr(process, "pid", 0) or 0)
        except Exception:
            pid = 0
        if pid and pid > 0:
            return f"pid:{pid}"
        return f"obj:{id(process)}"

    def _mark_process_aborted(self, process: Any) -> None:
        if process is None:
            return
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        key = self._process_abort_key(process)
        with lock:
            marks = getattr(self, "_aborted_process_keys", None)
            if not isinstance(marks, set):
                marks = set()
                self._aborted_process_keys = marks
            marks.add(key)

    def _consume_process_aborted(self, process: Any) -> bool:
        if process is None:
            return False
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return False
        key = self._process_abort_key(process)
        with lock:
            marks = getattr(self, "_aborted_process_keys", None)
            if not isinstance(marks, set):
                return False
            if key in marks:
                marks.discard(key)
                return True
            return False

    def _terminate_interruptible_processes(self) -> None:
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        with lock:
            cur = getattr(self, "_interruptible_processes", {})
            if isinstance(cur, dict):
                procs = list(cur.values())
            else:
                procs = []
        for p in procs:
            self._mark_process_aborted(p)
            self._terminate_single_process_tree(p)

    def _request_task_interrupt(self, source: str = "esc", cancel_task: bool = False) -> None:
        if cancel_task:
            lock = getattr(self, "_interrupt_state_lock", None)
            if lock is not None:
                with lock:
                    self._task_interrupt_requested = True
            else:
                self._task_interrupt_requested = True
        self._terminate_interruptible_processes()
        if cancel_task:
            try:
                import _thread

                _thread.interrupt_main()
            except Exception:
                pass

    def _consume_task_interrupt_requested(self) -> bool:
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            wanted = bool(getattr(self, "_task_interrupt_requested", False))
            self._task_interrupt_requested = False
            return wanted
        with lock:
            wanted = bool(getattr(self, "_task_interrupt_requested", False))
            self._task_interrupt_requested = False
            return wanted

    def _poll_windows_escape_pressed_async_fallback(self) -> bool:
        """Fallback ESC polling for terminals where msvcrt.kbhit() is unreliable."""
        if os.name != "nt":
            return False
        state = getattr(self, "_interrupt_async_escape_state", None)
        if not isinstance(state, dict):
            state = {
                "ready": False,
                "get_async_key_state": None,
                "get_foreground_window": None,
                "console_hwnd": 0,
                "esc_down": False,
            }
            try:
                import ctypes

                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                get_async_key_state = getattr(user32, "GetAsyncKeyState", None)
                get_foreground_window = getattr(user32, "GetForegroundWindow", None)
                get_console_window = getattr(kernel32, "GetConsoleWindow", None)
                console_hwnd = int(get_console_window() or 0) if callable(get_console_window) else 0
                if callable(get_async_key_state) and callable(get_foreground_window) and console_hwnd:
                    state.update(
                        {
                            "ready": True,
                            "get_async_key_state": get_async_key_state,
                            "get_foreground_window": get_foreground_window,
                            "console_hwnd": console_hwnd,
                        }
                    )
            except Exception:
                pass
            self._interrupt_async_escape_state = state
        if not bool(state.get("ready", False)):
            return False
        try:
            get_foreground_window = state.get("get_foreground_window")
            if callable(get_foreground_window):
                fg_hwnd = int(get_foreground_window() or 0)
                if fg_hwnd and fg_hwnd != int(state.get("console_hwnd") or 0):
                    state["esc_down"] = False
                    return False
            get_async_key_state = state.get("get_async_key_state")
            if not callable(get_async_key_state):
                return False
            # VK_ESCAPE=0x1B; high bit means currently down.
            cur = int(get_async_key_state(0x1B) or 0)
            is_down = bool(cur & 0x8000)
            was_down = bool(state.get("esc_down", False))
            state["esc_down"] = is_down
            return bool(is_down and not was_down)
        except Exception:
            return False

    def _poll_windows_escape_pressed(self) -> bool:
        """Primary + fallback ESC polling used by the background interrupt monitor."""
        if os.name != "nt":
            return False
        msvcrt = None
        try:
            import msvcrt as _msvcrt

            msvcrt = _msvcrt
        except Exception:
            msvcrt = None
        if msvcrt is not None:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b"\x00", b"\xe0"):
                        try:
                            if msvcrt.kbhit():
                                _ = msvcrt.getch()
                        except Exception:
                            pass
                        return False
                    if ch == b"\x1b":
                        return True
            except Exception:
                pass
        return self._poll_windows_escape_pressed_async_fallback()

    def _start_interrupt_monitor(self, cancel_task_on_interrupt: bool = False) -> None:
        if os.name != "nt":
            return
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        with lock:
            self._interrupt_monitor_refs = int(getattr(self, "_interrupt_monitor_refs", 0) or 0) + 1
            if cancel_task_on_interrupt:
                self._interrupt_monitor_cancel_task_refs = int(
                    getattr(self, "_interrupt_monitor_cancel_task_refs", 0) or 0
                ) + 1
            existing = getattr(self, "_interrupt_monitor_thread", None)
            if existing is not None and getattr(existing, "is_alive", lambda: False)():
                return
            stop_event = threading.Event()
            self._interrupt_monitor_stop_event = stop_event

            def _monitor() -> None:
                while not stop_event.wait(0.03):
                    try:
                        if not self._poll_windows_escape_pressed():
                            continue
                        with lock:
                            should_cancel_task = bool(
                                int(getattr(self, "_interrupt_monitor_cancel_task_refs", 0) or 0) > 0
                            )
                        self._request_task_interrupt(source="esc", cancel_task=should_cancel_task)
                    except Exception:
                        continue

            th = threading.Thread(
                target=_monitor,
                name="smartshell-esc-interrupt-monitor",
                daemon=True,
            )
            self._interrupt_monitor_thread = th
            th.start()

    def _stop_interrupt_monitor(self, cancel_task_on_interrupt: bool = False) -> None:
        lock = getattr(self, "_interrupt_state_lock", None)
        if lock is None:
            return
        stop_event = None
        thread_obj = None
        with lock:
            refs = int(getattr(self, "_interrupt_monitor_refs", 0) or 0)
            if refs > 0:
                refs -= 1
            self._interrupt_monitor_refs = refs
            if cancel_task_on_interrupt:
                c_refs = int(getattr(self, "_interrupt_monitor_cancel_task_refs", 0) or 0)
                if c_refs > 0:
                    c_refs -= 1
                self._interrupt_monitor_cancel_task_refs = c_refs
            if refs == 0:
                stop_event = getattr(self, "_interrupt_monitor_stop_event", None)
                thread_obj = getattr(self, "_interrupt_monitor_thread", None)
                self._interrupt_monitor_thread = None
                self._interrupt_monitor_cancel_task_refs = 0
        if stop_event is not None:
            try:
                stop_event.set()
            except Exception:
                pass
        if thread_obj is not None and getattr(thread_obj, "is_alive", lambda: False)():
            try:
                thread_obj.join(timeout=0.3)
            except Exception:
                pass

    def _stream_direct_shell_pipe_to_prefixed_output(
        self,
        pipe: Any,
        target_stream: Any,
        capture_chunks: Optional[List[str]] = None,
    ) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                text_chunk = decoder.decode(chunk, final=False)
                if text_chunk:
                    if isinstance(capture_chunks, list):
                        capture_chunks.append(text_chunk)
                    target_stream.write(text_chunk)
                    target_stream.flush()
            tail = decoder.decode(b"", final=True)
            if tail:
                if isinstance(capture_chunks, list):
                    capture_chunks.append(tail)
                target_stream.write(tail)
                target_stream.flush()
        except Exception:
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _run_direct_shell_with_prefixed_output(self, command: str, cwd: Path) -> int:
        import subprocess
        import threading

        status_ticker: Optional[_WorkingStatusTicker] = None
        ticker_lock = threading.Lock()

        def _stop_status_ticker() -> None:
            nonlocal status_ticker
            with ticker_lock:
                if status_ticker is None:
                    return
                try:
                    status_ticker.stop()
                finally:
                    status_ticker = None

        stream_state: Dict[str, Any] = {
            "first_line_emitted": False,
            "rendered_line_count": 0,
            "cursor_at_line_start": True,
            "_first_write_cleared_ticker_line": False,
            "_first_text_emitted_notified": False,
            "on_text_emitted": _stop_status_ticker,
        }
        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        out_stream, _ = self._create_direct_shell_output_streams(stream_state)
        status_ticker = _WorkingStatusTicker(
            sys.stdout,
            fps=DIRECT_SHELL_WORKING_STATUS_MARQUEE_FPS,
        )
        self._start_interrupt_monitor(cancel_task_on_interrupt=False)
        status_ticker.start()
        try:
            process = None
            process = subprocess.Popen(
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # Merge stderr into stdout so rendered output order follows
                # real arrival order and avoids cross-thread stream races.
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                text=False,
            )
            self._register_interruptible_process(process)
            t_out: Optional[threading.Thread] = None
            stdout_pipe = getattr(process, "stdout", None)
            if stdout_pipe is not None and hasattr(stdout_pipe, "read"):
                t_out = threading.Thread(
                    target=self._stream_direct_shell_pipe_to_prefixed_output,
                    args=(stdout_pipe, out_stream, stdout_chunks),
                    daemon=True,
                )
                t_out.start()
            return_code = process.wait()
            if t_out is not None:
                t_out.join()
            aborted_by_user = bool(self._consume_process_aborted(process))
            if aborted_by_user:
                need_leading_newline = not bool(stream_state.get("cursor_at_line_start", True))
                abort_notice = "command aborted by user\n"
                try:
                    if need_leading_newline:
                        out_stream.write("\n")
                    # Keep abort notice aligned with normal continuation output
                    # (four-space indent), not as a new first output line.
                    stream_state["first_line_emitted"] = True
                    out_stream.write(abort_notice)
                    out_stream.flush()
                except Exception:
                    pass
                stdout_chunks.append(("\n" if need_leading_newline else "") + abort_notice)
                banner_lines = 0
                try:
                    banner_lines = int(self._print_conversation_interrupted_banner() or 0)
                except Exception:
                    banner_lines = 0
                if banner_lines > 0:
                    try:
                        stream_state["rendered_line_count"] = int(
                            stream_state.get("rendered_line_count", 0) or 0
                        ) + int(banner_lines)
                    except Exception:
                        pass
                    stream_state["cursor_at_line_start"] = True
            try:
                self._last_direct_shell_execution = {
                    "executed_command": str(command or ""),
                    "cwd": str(cwd or ""),
                    "return_code": int(return_code),
                    "stdout": "".join(stdout_chunks),
                    "stderr": "".join(stderr_chunks),
                    "aborted_by_user": bool(aborted_by_user),
                    "rendered_output_lines": int(stream_state.get("rendered_line_count", 0) or 0),
                    "cursor_at_line_start": bool(stream_state.get("cursor_at_line_start", True)),
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            except Exception:
                pass
            return int(return_code)
        finally:
            try:
                self._unregister_interruptible_process(process)
            except Exception:
                pass
            _stop_status_ticker()
            self._stop_interrupt_monitor(cancel_task_on_interrupt=False)

    def _append_chat_message(self, role: str, content: str) -> None:
        self.session_memory_service.append_chat_message(role, content)
        if str(role or "").strip().lower() == "assistant":
            try:
                self.session_memory_service.schedule_context_usage_refresh_async()
            except Exception:
                pass
        return None

    def _maybe_schedule_auto_chat_name(self) -> None:
        with self._chat_state_lock:
            chat = self._find_chat_by_id(self.active_chat_id)
            if not chat:
                return
            if str(chat.get("name_source") or "") == "manual":
                return
            chat_id = str(chat.get("id") or "")
            msgs = list(chat.get("messages") or [])
            first_user = ""
            parse_slash_user = getattr(self, "_parse_internal_slash_user_history_content", None)
            for m in msgs:
                if str(m.get("role") or "").strip().lower() == "user":
                    raw_user = str(m.get("content") or "").strip()
                    if not raw_user:
                        continue
                    parsed_slash_cmd = ""
                    if callable(parse_slash_user):
                        try:
                            parsed_slash_cmd = str(parse_slash_user(raw_user) or "").strip()
                        except Exception:
                            parsed_slash_cmd = ""
                    candidate = parsed_slash_cmd if parsed_slash_cmd else raw_user
                    # Built-in slash commands and their internal history should not drive chat auto naming.
                    if candidate.startswith("/"):
                        continue
                    first_user = candidate
                    break
            if not first_user:
                return
            if str(chat.get("name_source") or "") == "auto":
                return

        def _fallback_title(text: str) -> str:
            t = re.sub(r"\s+", " ", str(text or "").strip())
            t = t.strip(" \"'`[](){}")
            if len(t) > 18:
                t = t[:18]
            if len(t) < 2:
                return "New Chat"
            return t

        try:
            prompt = (
                "You are a chat title generator. Output only the title text with no explanation.\n"
                "Task: Generate a short title from the user's first message.\n"
                "Requirements: 4-18 characters; no trailing punctuation; avoid words like 'Chat/session/title/first message'.\n"
                "If the message is very short, extract a concise intent phrase.\n\n"
                f"<user_first_message>\n{first_user}\n</user_first_message>"
            )
            title = self.call_ai(prompt, context="", stream=False, session_summary_mode=True)
            t = title if isinstance(title, str) else ""
            t = t.strip().replace("\n", " ")
            t = re.sub(r"\s+", " ", t).strip(" \"'`[](){}")
            if any(bad in t for bad in ("first message", "title", "session", "Chat", "chat")):
                t = ""
            if len(t) > 18:
                t = t[:18]
            if len(t) < 2:
                t = _fallback_title(first_user)
            with self._chat_state_lock:
                chat = self._find_chat_by_id(chat_id)
                if not chat:
                    return
                if str(chat.get("name_source") or "") == "manual":
                    return
                chat["name"] = t
                chat["name_source"] = "auto"
                chat["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if chat_id == self.active_chat_id:
                    self.active_chat_name = t
                self._save_chat_state()
        except Exception:
            with self._chat_state_lock:
                chat = self._find_chat_by_id(chat_id)
                if not chat:
                    return
                if str(chat.get("name_source") or "") == "manual":
                    return
                t = _fallback_title(first_user)
                chat["name"] = t
                chat["name_source"] = "auto"
                chat["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if chat_id == self.active_chat_id:
                    self.active_chat_name = t
                self._save_chat_state()

    def _handle_chat_builtin_command(self, builtin_line: str) -> bool:
        return handle_chat_builtin_command(self, builtin_line)

    def _shutdown_mcp_runtime(self) -> None:
        manager = getattr(self, "mcp_manager", None)
        clients = getattr(manager, "_clients", None)
        if not isinstance(clients, dict):
            return
        for client in list(clients.values()):
            try:
                client._shutdown_unlocked()
            except Exception:
                pass
        clients.clear()

    def _shutdown_workspace_services(self, wait: bool = True) -> None:
        self._workspace_runtime_generation = getattr(self, "_workspace_runtime_generation", 0) + 1
        if wait:
            knowledge_event = getattr(self, "_knowledge_import_done", None)
            if knowledge_event is not None:
                try:
                    knowledge_event.wait(timeout=120.0)
                except Exception:
                    pass
        for attr in ("knowledge_manager", "memory_service"):
            svc = getattr(self, attr, None)
            setattr(self, attr, None)
            if svc is None:
                continue
            shutdown = getattr(svc, "shutdown", None)
            if not callable(shutdown):
                continue
            try:
                shutdown(wait=wait)
            except TypeError:
                try:
                    shutdown()
                except Exception:
                    pass
            except Exception:
                pass

    def _refresh_workspace_runtime(self) -> None:
        self._shutdown_workspace_services(wait=True)
        self._ensure_workspace_dirs()
        self.history_manager = HistoryManager(str(self.ai_workspace_dir))
        self._load_chat_state()
        if self.input_handler is not None:
            try:
                if hasattr(self.input_handler, "update_workspace_directory"):
                    self.input_handler.update_workspace_directory(self.workspace_root)
                if hasattr(self.input_handler, "update_work_directory"):
                    self.input_handler.update_work_directory(self.work_directory)
                if hasattr(self.input_handler, "reset_command_history"):
                    self.input_handler.reset_command_history(self.history_manager.get_all_history())
            except Exception:
                pass

        self._allowlist_shell_paths = {}
        self._allowlist_shell_exes = set()
        self._allowlist_script = set()
        self._confirm_allowlist_salt = ""
        self._load_confirm_allowlist()
        self._freedom_script_review_entries = {}
        self._load_freedom_script_review_cache()

        self._shutdown_mcp_runtime()
        self.mcp_manager = McpManager(
            self.config_dir,
            self.mcp_config,
            self.ai_workspace_dir,
            tool_policy_parent=self.ai_workspace_dir,
        )
        self.mcp_manager.register_client_method_handler("elicitation/create", self._handle_mcp_elicitation_create)
        self.mcp_manager.preload_all_async(timeout_s=12.0, force=False)
        self.system_prompt = self._compose_system_prompt_snapshot(include_tools=False)
        self._reload_skills()
        self.knowledge_manager = None
        self._schedule_knowledge_service_background()
        self.memory_service = None
        self._last_memory_reflect_at = 0.0
        self._schedule_memory_service_background()
        self._schedule_project_context_refresh_background(force=False, reason="workspace-refresh")

    def _schedule_memory_service_background(self) -> None:
        """后台初始化经验记忆：在本线程内 import memory_manager，再构造 MemoryService（Markdown 后端，无重型依赖）。"""
        _mod = sys.modules[__name__]
        workspace_dir = str(self.ai_workspace_dir)
        generation = getattr(self, "_workspace_runtime_generation", 0)

        def _run() -> None:
            try:
                from .core.state import memory_manager as _mm
            except ImportError:
                _mod.MEMORY_AVAILABLE = False  # type: ignore[misc, assignment]
                _mod.MemoryService = None  # type: ignore[misc, assignment]
                return
            mav = bool(getattr(_mm, "MEMORY_AVAILABLE", False))
            MS = getattr(_mm, "MemoryService", None)
            _mod.MEMORY_AVAILABLE = mav  # type: ignore[misc, assignment]
            _mod.MemoryService = MS  # type: ignore[misc, assignment]
            if not mav or MS is None:
                return
            try:
                svc = MS(workspace_dir)
                if (
                    str(self.ai_workspace_dir) == workspace_dir
                    and getattr(self, "_workspace_runtime_generation", 0) == generation
                ):
                    self.memory_service = svc
                else:
                    try:
                        svc.shutdown(wait=False)
                    except Exception:
                        pass
            except Exception:
                try:
                    get_logger().exception("Experiential memory MemoryService initialization failed")
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True, name="smartshell-memory-init").start()

    def _ensure_memory_service(self) -> bool:
        if not bool(getattr(self, "memory_enabled", True)):
            return False
        if not MEMORY_AVAILABLE or self.memory_service is None:
            return False
        svc = self.memory_service
        if not svc.wait_ready(30.0):
            return False
        return svc.is_available()

    def _memory_scope_key(self) -> str:
        try:
            return str(self.work_directory.resolve())
        except Exception:
            return str(self.work_directory)

    def _schedule_auto_memory_reflect(self) -> None:
        return self.session_memory_service.schedule_auto_memory_reflect()

    def _schedule_knowledge_service_background(self) -> None:
        """
        在后台线程中执行 knowledge_manager 的 import 与 KnowledgeService 构造。
        import 链会加载 ChromaDB、sentence_transformers、PyTorch、langchain 等，若在主线程执行会明显拖慢到提示符的时间。
        """
        _mod = sys.modules[__name__]
        if getattr(_mod, "KNOWLEDGE_AVAILABLE", None) is False:
            return
        if os.environ.get("SMARTSHELL_SKIP_KNOWLEDGE", "").strip().lower() in ("1", "true", "yes"):
            return

        self._knowledge_import_done = threading.Event()
        workspace_dir = str(self.ai_workspace_dir)
        generation = getattr(self, "_workspace_runtime_generation", 0)

        def _run() -> None:
            try:
                try:
                    from .core.state.knowledge_manager import KnowledgeService as _KS, KNOWLEDGE_AVAILABLE as _KAV
                except ImportError:
                    _mod.KnowledgeService = None  # type: ignore
                    _mod.KNOWLEDGE_AVAILABLE = False
                    return
                _mod.KnowledgeService = _KS
                _mod.KNOWLEDGE_AVAILABLE = _KAV
                if _KAV and _KS is not None:
                    try:
                        svc = _KS(workspace_dir)
                        if (
                            str(self.ai_workspace_dir) == workspace_dir
                            and getattr(self, "_workspace_runtime_generation", 0) == generation
                        ):
                            self.knowledge_manager = svc
                        else:
                            try:
                                svc.shutdown(wait=False)
                            except Exception:
                                pass
                    except Exception:
                        try:
                            get_logger().exception("Knowledge base KnowledgeService construction failed")
                        except Exception:
                            pass
                        _mod.KnowledgeService = None  # type: ignore
                        _mod.KNOWLEDGE_AVAILABLE = False
            finally:
                self._knowledge_import_done.set()

        threading.Thread(
            target=_run,
            name="smartshell-kb-import",
            daemon=True,
        ).start()

    def _schedule_model_validation_background(self) -> None:
        """
        Ollama 模型列表探测可能阻塞；在后台线程执行，缩短 main 打印模型信息后到出现提示符的等待。
        非 ollama provider 不启动线程。
        """
        ollama_needed = getattr(self, "provider", "") == "ollama"
        if not ollama_needed:
            return

        def _run() -> None:
            try:
                self._validate_model()
            except Exception:
                pass

        threading.Thread(
            target=_run,
            name="smartshell-ollama-validate",
            daemon=True,
        ).start()

    def _reload_skills(self, force: bool = False) -> None:
        """Reload skills only when skill dirs fingerprint changed (or forced)."""
        try:
            latest_fp = calc_skills_dirs_fingerprint(
                self.config_dir,
                self._builtin_skills_root,
                self.ai_workspace_dir,
            )
            if not force and latest_fp == getattr(self, "_skills_dirs_fingerprint", ""):
                return
            self.skills = load_skills_merged(
                self.config_dir,
                self._builtin_skills_root,
                self.ai_workspace_dir,
            )
            self._skills_dirs_fingerprint = latest_fp
            self._skills_routing_prefix = build_skills_routing_prefix(self.skills)
            self._refresh_input_handler_skill_completions()
        except Exception as e:
            print(f"⚠️ Skill hot reload failed, continuing with the currently loaded version: {e}")

    def _load_mcp_config(self) -> Dict[str, Any]:
        """Load MCP configuration from <config_dir>/mcp.json."""
        mcp_path = self.config_dir / "mcp.json"
        if not mcp_path.is_file():
            return {"mcpServers": {}}
        try:
            with open(mcp_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print("⚠️ Invalid mcp.json format: root object must be a JSON object")
                return {"mcpServers": {}}
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                print("⚠️ Invalid mcp.json format: mcpServers must be an object")
                return {"mcpServers": {}}
            return {"mcpServers": servers}
        except Exception as e:
            print(f"⚠️ Failed to read mcp.json: {e}")
            return {"mcpServers": {}}

    def _get_mcp_config_file_sig(self) -> Tuple[bool, int, int]:
        p = self._mcp_config_path
        try:
            if not p.is_file():
                return (False, 0, 0)
            st = p.stat()
            return (True, int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), int(st.st_size))
        except Exception:
            return (False, 0, 0)

    @staticmethod
    def _calc_mcp_config_sig(cfg: Dict[str, Any]) -> str:
        try:
            servers = cfg.get("mcpServers", {}) if isinstance(cfg, dict) else {}
            if not isinstance(servers, dict):
                servers = {}
            return json.dumps({"mcpServers": servers}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            return ""

    def _load_mcp_config_strict(self) -> Tuple[bool, Dict[str, Any], str]:
        """
        Strict mcp config loader for hot-reload:
        - parse failure returns ok=False and keeps current runtime config unchanged.
        """
        p = self._mcp_config_path
        if not p.is_file():
            return True, {"mcpServers": {}}, ""
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False, {}, "mcp.json root object must be a JSON object"
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                return False, {}, "mcp.json mcpServers must be an object"
            return True, {"mcpServers": servers}, ""
        except Exception as e:
            return False, {}, str(e)

    def _reload_mcp_config_now(self) -> Dict[str, Any]:
        """
        Manual trigger for MCP config reload.
        Always parse current mcp.json and apply diff against in-memory config.
        """
        cur_sig = self._get_mcp_config_file_sig()
        ok, new_cfg, err = self._load_mcp_config_strict()
        if not ok:
            self._mcp_config_last_failed_file_sig = cur_sig
            return {"success": False, "changed": False, "error": f"Failed to parse mcp.json: {err}"}
        self._mcp_config_last_failed_file_sig = None
        new_struct_sig = self._calc_mcp_config_sig(new_cfg)
        if new_struct_sig == self._mcp_config_struct_sig:
            self._mcp_config_file_sig = cur_sig
            return {"success": True, "changed": False, "message": "MCP configuration unchanged"}
        summary = self.mcp_manager.apply_config_changes(new_cfg, timeout_s=12.0)
        self.mcp_config = new_cfg
        self._mcp_config_struct_sig = new_struct_sig
        self._mcp_config_file_sig = cur_sig
        self.system_prompt = self._compose_system_prompt_snapshot(include_tools=False)
        return {
            "success": True,
            "changed": True,
            "summary": summary,
            "message": "MCP configuration reloaded",
        }

    def _load_tools_spec_from_jsonc(self) -> List[Dict[str, Any]]:
        return prompt_composer.load_tools_spec_from_jsonc(self)

    def _compose_system_prompt_snapshot(self, include_tools: bool) -> str:
        return prompt_composer.compose_system_prompt_snapshot(self, include_tools=include_tools)

    def _load_tools_prompt_template(self) -> str:
        return prompt_composer.load_tools_prompt_template()

    def _build_single_skill_prompt(
        self,
        skill_id: str,
        requested_section: Optional[int] = None,
        full: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        return prompt_composer.build_single_skill_prompt(
            self,
            skill_id=skill_id,
            requested_section=requested_section,
            full=full,
            long_body_threshold=SKILL_PROMPT_LONG_BODY_THRESHOLD,
            initial_sections=SKILL_PROMPT_INITIAL_SECTIONS,
            max_section_chars=SKILL_PROMPT_MAX_SECTION_CHARS,
        )
    def _get_slash_skill_commands(self) -> List[str]:
        if self.skills:
            return ["/skills/"]
        return []

    def _get_slash_skill_target_commands(self) -> List[str]:
        cmds: List[str] = []
        seen: Set[str] = set()
        for s in self.skills or []:
            sid = str(getattr(s, "skill_id", "")).strip()
            if not sid:
                continue
            c = f"/skills/{sid}"
            if c.lower() in seen:
                continue
            seen.add(c.lower())
            cmds.append(c)
        return sorted(cmds, key=str.lower)

    def _refresh_input_handler_skill_completions(self) -> None:
        try:
            if self.input_handler is not None and hasattr(self.input_handler, "set_slash_skill_commands"):
                self.input_handler.set_slash_skill_commands(self._get_slash_skill_commands())
            if self.input_handler is not None and hasattr(self.input_handler, "set_slash_mcp_commands"):
                self.input_handler.set_slash_mcp_commands(
                    self._get_slash_mcp_server_commands()
                )
            if self.input_handler is not None and hasattr(
                self.input_handler, "set_slash_dynamic_rules"
            ):
                self.input_handler.set_slash_dynamic_rules(
                    self._get_slash_dynamic_rules()
                )
        except Exception:
            pass

    def _get_slash_mcp_server_commands(self) -> List[str]:
        # MCP server candidates are provided by delayed dynamic trigger '/mcp/'.
        # Keep first-layer '/' menu clean: do not inject '/mcp/<server>/' here.
        return []

    def _get_slash_connected_mcp_server_commands(self) -> List[str]:
        cmds: List[str] = []
        seen: Set[str] = set()
        for cmd in build_mcp_scoped_commands(self.mcp_manager):
            value = str(cmd or "").strip()
            if not re.match(r"^/mcp/[^/]+/$", value, flags=re.IGNORECASE):
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            cmds.append(value)
        return sorted(cmds, key=str.lower)

    def _get_slash_dynamic_rules(self) -> List[Dict[str, Any]]:
        return build_slash_dynamic_rules(
            workspaces_state=self._workspaces_state,
            mcp_config=self.mcp_manager.mcp_config,
            mcp_scoped_groups_provider=self._get_slash_mcp_scoped_groups,
            model_selectors_provider=self._get_configured_model_selectors,
            skill_targets_provider=self._get_slash_skill_target_commands,
            mcp_root_server_commands_provider=self._get_slash_connected_mcp_server_commands,
        )

    def _get_slash_mcp_server_target_commands(
        self, subcommand: str, with_trailing_space: bool = False
    ) -> List[str]:
        return build_mcp_server_target_commands(
            self.mcp_manager.mcp_config,
            subcommand=subcommand,
            with_trailing_space=with_trailing_space,
        )

    def _get_slash_mcp_scoped_groups(self) -> List[Tuple[str, List[str]]]:
        return build_mcp_scoped_groups(self.mcp_manager)

    def _extract_forced_skill_reference(self, user_text: str) -> Optional[Dict[str, Any]]:
        """
        Find one or more '/skills/<skill-name>' references and match loaded skills by
        skill_id or name.
        Returns {"skills":[{"skill_id","name"}...], "rest"} when matched.
        """
        raw = (user_text or "").strip()
        if not raw:
            return None
        matches = list(
            re.finditer(r"(?<!\S)/skills/([^\s/]+)", raw, flags=re.IGNORECASE)
        )
        if not matches:
            return None
        skill_by_token: Dict[str, Dict[str, str]] = {}
        for s in self.skills or []:
            sid = str(getattr(s, "skill_id", "")).strip()
            sname = str(getattr(s, "name", "")).strip()
            if not sid:
                continue
            skill_by_token[sid.lower()] = {"skill_id": sid, "name": sname or sid}
            if sname:
                skill_by_token[sname.lower()] = {"skill_id": sid, "name": sname or sid}

        selected: List[Dict[str, str]] = []
        selected_ids: Set[str] = set()
        pieces: List[str] = []
        cursor = 0
        for m in matches:
            token_l = (m.group(1) or "").strip().lower()
            matched = skill_by_token.get(token_l)
            if matched:
                sid_l = str(matched.get("skill_id", "")).strip().lower()
                if sid_l and sid_l not in selected_ids:
                    selected_ids.add(sid_l)
                    selected.append(matched)
        if not selected:
            return None
        for m in matches:
            start, end = m.start(), m.end()
            if start < cursor:
                continue
            token_l = (m.group(1) or "").strip().lower()
            if token_l not in skill_by_token:
                continue
            pieces.append(raw[cursor:start])
            cursor = end
        pieces.append(raw[cursor:])
        cleaned = "".join(pieces).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return {"skills": selected, "rest": cleaned}

    def _extract_forced_mcp_reference(self, user_text: str) -> Optional[Dict[str, Any]]:
        """
        Find one or more '/mcp/<mcp-server-name>/<tool-name|prompt-name>' tokens.
        Returns {"entries":[{server,name,kind}], "rest"} when matched.
        """
        raw = (user_text or "").strip()
        if not raw:
            return None
        matches = list(re.finditer(r"(?<!\S)/mcp/([^\s/]+)/([^\s]+)", raw, flags=re.IGNORECASE))
        if not matches:
            return None

        servers = {}
        try:
            servers = (self.mcp_manager.mcp_config or {}).get("mcpServers", {})
        except Exception:
            servers = {}
        if not isinstance(servers, dict):
            servers = {}
        server_names = {str(s).strip().lower(): str(s).strip() for s in servers.keys() if str(s).strip()}
        if not server_names:
            return None

        entries: List[Dict[str, str]] = []
        seen: Set[str] = set()
        pieces: List[str] = []
        cursor = 0
        for m in matches:
            server_l = (m.group(1) or "").strip().lower()
            target = str(m.group(2) or "").strip()
            if not server_l or not target:
                continue
            srv = server_names.get(server_l)
            if not srv:
                continue
            kind = ""
            try:
                tools, _ = self.mcp_manager.list_tools(srv, timeout_s=8.0, use_cache=False)
                if any(str((t or {}).get("name", "")).strip() == target for t in (tools or []) if isinstance(t, dict)):
                    kind = "tool"
            except Exception:
                pass
            if not kind:
                try:
                    prompts, _ = self.mcp_manager.list_prompts(srv, timeout_s=8.0, use_cache=False)
                    if any(str((p or {}).get("name", "")).strip() == target for p in (prompts or []) if isinstance(p, dict)):
                        kind = "prompt"
                except Exception:
                    pass
            if not kind:
                continue
            key = f"{srv.lower()}/{target.lower()}/{kind}"
            if key not in seen:
                seen.add(key)
                entries.append({"server": srv, "name": target, "kind": kind})
            pieces.append(raw[cursor:m.start()])
            cursor = m.end()

        if not entries:
            return None
        pieces.append(raw[cursor:])
        cleaned = "".join(pieces).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return {"entries": entries, "rest": cleaned}

    def _build_forced_mcp_prefix(self, entries: List[Dict[str, str]]) -> str:
        if not entries:
            return ""
        lines: List[str] = [
            "[Forced MCP references] For this task, prioritize referencing and using the following explicitly specified MCP targets (in the order provided by the user):",
        ]
        for e in entries:
            srv = str(e.get("server", "")).strip()
            name = str(e.get("name", "")).strip()
            kind = str(e.get("kind", "")).strip() or "unknown"
            lines.append(f"- `/mcp/{srv}/{name}` ({kind})")
            if kind == "prompt":
                try:
                    pobj = self.mcp_manager.get_prompt(srv, name, {}, timeout_s=20.0)
                    desc = str((pobj or {}).get("description", "")).strip() if isinstance(pobj, dict) else ""
                    if desc:
                        lines.append(f"  prompt.description: {desc}")
                except Exception:
                    pass
        lines.append("If AGENTS.md or general rules conflict, these explicitly specified MCP targets take precedence (except hard safety/privilege constraints).")
        return "\n".join(lines) + "\n\n"

    def _ensure_knowledge_manager(self) -> bool:
        """等待知识库服务就绪。依赖不可用或初始化失败时返回 False。"""
        if not KNOWLEDGE_AVAILABLE:
            return False
        # 后台线程可能仍在 import chromadb/torch；先等到赋值完成或确认失败
        if self.knowledge_manager is None:
            done = getattr(self, "_knowledge_import_done", None)
            if done is not None:
                done.wait(timeout=120.0)
        svc = self.knowledge_manager
        if svc is None:
            return False
        if not svc.wait_ready(600.0):
            get_logger("smartshell.knowledge").warning(
                "Timed out waiting for knowledge base initialization (600s). Please retry later with /knowledge sync."
            )
            return False
        return svc.is_available()

    def _save_execution_policy_to_config(self) -> bool:
        """将执行策略保存到 config.json"""
        try:
            cfg_path = self.config_dir / "config.json"
            cfg_data = {}
            if cfg_path.exists():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["execution_policy"] = str(self.execution_policy)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ Failed to save execution policy to config: {e}")
            return False

    def _save_session_summary_llm_to_config(self) -> bool:
        """将 session_summary_llm 开关写入 config.json（与 execution_policy 等并存）。"""
        try:
            cfg_path = self.config_dir / "config.json"
            cfg_data: Dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["session_summary_llm"] = bool(
                getattr(self, "session_summary_llm_enabled", True)
            )
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ Failed to save session_summary_llm to config: {e}")
            return False

    def _save_memory_enabled_to_config(self) -> bool:
        """将 memory_enabled 开关写入 config.json。"""
        try:
            cfg_path = self.config_dir / "config.json"
            cfg_data: Dict[str, Any] = {}
            if cfg_path.exists():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["memory_enabled"] = bool(getattr(self, "memory_enabled", True))
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ Failed to save memory_enabled to config: {e}")
            return False

    def _enable_freedom(self) -> Dict[str, Any]:
        """兼容命令：设置 execution_policy=moderate"""
        if self.execution_policy == "moderate":
            return {"success": True, "message": "execution_policy is already set to moderate"}
        self.execution_policy = "moderate"
        saved = self._save_execution_policy_to_config()
        return {
            "success": True,
            "message": f"execution_policy set to moderate{' (config saved)' if saved else ''}",
        }

    def _disable_freedom(self) -> Dict[str, Any]:
        """兼容命令：设置 execution_policy=confirmation"""
        if self.execution_policy == "confirmation":
            return {"success": True, "message": "execution_policy is already set to confirmation"}
        self.execution_policy = "confirmation"
        saved = self._save_execution_policy_to_config()
        return {"success": True, "message": f"execution_policy set to confirmation{' (config saved)' if saved else ''}"}

    def _set_execution_policy(self, policy: str) -> Dict[str, Any]:
        pol = str(policy or "").strip().lower()
        if pol not in ("unlimited", "moderate", "confirmation"):
            return {
                "success": False,
                "error": "Invalid execution_policy. Allowed values: unlimited, moderate, confirmation",
            }
        if self.execution_policy == pol:
            return {"success": True, "message": f"execution_policy is already set to {pol}", "policy": pol}
        self.execution_policy = pol
        saved = self._save_execution_policy_to_config()
        return {
            "success": True,
            "message": f"execution_policy set to {pol}{' (config saved)' if saved else ''}",
            "policy": pol,
        }

    def _print_execution_policy_details(self) -> None:
        _pm = "`/execution-policy moderate`"
        _pc = "`/execution-policy confirmation`"
        _pu = "`/execution-policy unlimited`"
        pol = str(getattr(self, "execution_policy", "confirmation")).lower()
        print(f"Execution policy: {pol}")
        if pol == "unlimited":
            print(
                _ansi_red(
                    "  All operations execute directly without safety checks or confirmations."
                    f"Type {_pm} to switch to moderate; type {_pc} to switch back to confirmation."
                )
            )
            print(_ansi_red("  Warning: high-risk operations will also execute directly. Use only in fully controlled environments."))
        elif pol == "moderate":
            print(
                _ansi_yellow(
                    "  Safe operations are evaluated by AI before execution; if judged safe, y/n confirmation is skipped automatically. AI safety judgment may be wrong, so use with caution."
                    f"Type {_pc} to switch back to confirmation."
                )
            )
        else:
            print(
                "  Operations requiring confirmation will always ask y/n."
                f"Type {_pm} to switch to moderate; type {_pu} to switch to unlimited."
            )

    def _print_knowledge_status_details(self) -> None:
        svc = getattr(self, "knowledge_manager", None)
        manager_ready = bool(
            svc is not None and getattr(svc, "is_available", lambda: False)()
        )
        dep_ready = bool(KNOWLEDGE_AVAILABLE)
        print("Knowledge base status details:")
        print(f"  feature: always enabled (loaded when dependencies are available)")
        print(f"  runtime_ready: {'yes' if manager_ready else 'no'}")
        print(f"  dependency_ready: {'yes' if dep_ready else 'no'}")
        if dep_ready and manager_ready:
            try:
                stats = self.knowledge_manager.get_knowledge_stats()  # type: ignore[union-attr]
                if isinstance(stats, dict):
                    docs = stats.get("total_documents", stats.get("documents_count", "-"))
                    chunks = stats.get("total_chunks", stats.get("chunks_count", "-"))
                    emb = stats.get("embedding_model", "-")
                    print(f"  documents_count: {docs}")
                    print(f"  chunks_count: {chunks}")
                    print(f"  embedding_model: {emb}")
            except Exception as e:
                print(f"  stats_error: {e}")
            print("  Note: the model only calls knowledge_search when the user explicitly asks to query/reference the knowledge base; results may be stale, so verify key conclusions against source files.")
        elif dep_ready and not manager_ready:
            if svc is not None and not svc.is_ready():
                print("  Knowledge base indexing is running in the background; please wait. See smartshell.log for details.")
            elif svc is not None and svc.is_ready() and not svc.is_available():
                print("  Knowledge base initialization failed. Please check smartshell.log.")
            else:
                print("  Dependencies are available but runtime is not ready. Check logs, sentence-transformers, and workspace/knowledge/ under the config directory.")
        else:
            if sys.version_info >= (3, 14):
                print("  Current environment does not satisfy knowledge base dependencies (for example, ChromaDB limitations on Python 3.14). Please use Python 3.12/3.13 and install dependencies.")
            else:
                print("  Knowledge base dependencies are not installed or failed to load. Install the knowledge-related packages from requirements.")

    def _print_memory_status_details(self) -> None:
        enabled = bool(getattr(self, "memory_enabled", True))
        dep = bool(MEMORY_AVAILABLE)
        ready = bool(self._ensure_memory_service())
        print("Experiential memory status details (separate from knowledge base: internalized lessons/preferences, not a document store):")
        print(f"  feature_enabled: {'yes' if enabled else 'no'}")
        print(f"  dependency_ready: {'yes' if dep else 'no'}")
        print(f"  runtime_ready: {'yes' if ready else 'no'}")
        if not enabled:
            print("  Experiential memory is disabled. Use /memory enable to turn it back on.")
            return
        if dep and ready:
            try:
                st = self.memory_service.stats()  # type: ignore[union-attr]
                if isinstance(st, dict):
                    print(f"  total_memories: {st.get('total_memories', '-')}")
                    print(f"  storage_backend: {st.get('storage_backend', '-')}")
                    print(f"  storage_dir: {st.get('storage_dir', '-')}")
            except Exception as e:
                print(f"  stats_error: {e}")
            print(
                "  Note: after each natural-language task completes normally, background auto-reflection may run (roughly 45+ seconds apart from the previous trigger); "
                "entries are written only when the model finds reusable lessons (possibly zero entries). "
                "You can also use memory_search / memory_add or /memory remember manually; do not confuse this with knowledge_search."
            )
        elif dep and not ready:
            print("  Memory module is initializing or failed. Check smartshell.log and workspace/memory/ under the config directory.")
        else:
            print("  Experiential memory is unavailable (initialization failed); the main program can continue running.")

    def _load_freedom_script_review_cache(self) -> None:
        return execution_policy_service.load_freedom_script_review_cache(self)

    def _load_confirm_allowlist(self) -> None:
        return execution_policy_service.load_confirm_allowlist(self)

    def _shell_command_in_allowlist(self, command: str) -> bool:
        return execution_policy_service.shell_command_in_allowlist(self, command)

    def _shell_confirm_should_offer_always(self, command: str) -> bool:
        return execution_policy_service.shell_confirm_should_offer_always(self, command)

    def _reset_always_confirm_skip(self) -> Dict[str, Any]:
        return execution_policy_service.reset_always_confirm_skip(self)

    def _prompt_confirm_yes_no_maybe_always(
        self,
        prompt_core: str,
        *,
        offer_always: bool,
        kind: str,
        shell_command: Optional[str] = None,
        script_basename: Optional[str] = None,
    ) -> bool:
        return execution_policy_service.prompt_confirm_yes_no_maybe_always(
            self,
            prompt_core,
            offer_always=offer_always,
            kind=kind,
            shell_command=shell_command,
            script_basename=script_basename,
        )

    def _freedom_auto_confirm(self, command: Dict[str, Any]) -> bool:
        return execution_policy_service.freedom_auto_confirm(self, command)
    def _validate_model(self) -> None:
        """验证模型是否可用（仅 ollama 模式）。"""
        self._validate_single_model(self.provider, self.model_name, "model")

    def _validate_single_model(self, provider: str, model_name: str, model_type: str):
        """验证单个模型是否可用"""
        if provider != "ollama":
            return
        try:
            ollama = _import_ollama_client()
            models = ollama.list()
            available_models = []
            for model in models.get('models', []):
                if hasattr(model, 'model'):
                    available_models.append(model.model)
                elif isinstance(model, dict):
                    available_models.append(model.get('name', model.get('model', 'unknown')))
                else:
                    available_models.append(str(model))
            if model_name not in available_models:
                print(f"⚠️ Warning: {model_type} '{model_name}' is not in the available model list")
                print(f"📋 Available models: {available_models}")
                if available_models:
                    print(f"💡 Suggested model: {available_models[0]}")
                print("💡 Please check model configuration in config.json")
        except ImportError:
            print(f"❌ Error: 'ollama' package is not installed, cannot validate {model_type}. Please run: pip install ollama")
        except Exception as e:
            print(f"⚠️ Error validating {model_type}: {e}")
            print(f"💡 Please ensure the Ollama service is running")

    def _build_regular_task_messages(self, user_input: str, context: str = "") -> Tuple[List[Dict[str, Any]], bool]:
        return self.session_memory_service.build_regular_task_messages(user_input, context)

    def call_ai(
        self,
        user_input: str,
        context: str = "",
        stream: bool = False,
        minimal_classifier: bool = False,
        freedom_combined_review: bool = False,
        return_message: bool = False,
        reflection_mode: bool = False,
        session_summary_mode: bool = False,
        memory_query_expansion_mode: bool = False,
        domain_classifier_mode: bool = False,
        image_path: Optional[str] = None,
        history_user_input: Optional[str] = None,
        history_skip_user: bool = False,
    ):
        """调用大模型 API 获取回复；支持流式输出。"""
        call_ctx = AICallContext(
            user_input=user_input,
            context=context,
            stream=stream,
            minimal_classifier=minimal_classifier,
            freedom_combined_review=freedom_combined_review,
            return_message=return_message,
            reflection_mode=reflection_mode,
            session_summary_mode=session_summary_mode,
            memory_query_expansion_mode=memory_query_expansion_mode,
            domain_classifier_mode=domain_classifier_mode,
            image_path=image_path,
            history_user_input=history_user_input,
            history_skip_user=history_skip_user,
        )
        self.ai_orchestrator.context.provider = self.provider
        self.ai_orchestrator.context.model_name = self.model_name
        self.ai_orchestrator.context.model_params = self.params
        self.ai_orchestrator.context.openai_conf = self.openai_conf
        self.ai_orchestrator.context.openwebui_conf = self.openwebui_conf
        self.ai_orchestrator.context.work_directory = str(self.work_directory)
        return self.ai_orchestrator.call(call_ctx=call_ctx)

    def action_ffmpeg(self, source: str, target: str, options: Optional[str] = None) -> Dict[str, Any]:
        """调用ffmpeg处理媒体文件"""
        return filesystem_actions.action_ffmpeg(self, source=source, target=target, options=options)

    def _ephemeral_path_key(self, path: Path) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        s = str(resolved)
        if os.name == "nt":
            s = os.path.normcase(s)
        return s

    def _workspace_relative_script_triple(self, rel: Path) -> Tuple[Path, Path, Path]:
        """相对路径在 shell 解析时的三个候选根：当前工作目录、workspace/temp、workspace 根（兼容旧路径）。"""
        p_wd = (self.work_directory / rel).resolve()
        p_temp = (self.ai_workspace_temp_dir / rel).resolve()
        p_ws = (self.ai_workspace_dir / rel).resolve()
        return p_wd, p_temp, p_ws

    def _try_register_ai_output_literal(self, raw: str) -> None:
        """Register a path string as AI-created if it resolves under work_directory or ai_workspace_dir."""
        raw = (raw or "").strip()
        if not raw or ".." in raw:
            return
        try:
            p = Path(raw)
            if not p.is_absolute():
                for base in (self.work_directory, self.ai_workspace_temp_dir, self.ai_workspace_dir):
                    try:
                        q = (base / p).resolve()
                        q.relative_to(base.resolve())
                        self._ai_created_path_keys.add(self._ephemeral_path_key(q))
                        return
                    except ValueError:
                        continue
            else:
                q = p.resolve()
                for base in (self.work_directory, self.ai_workspace_temp_dir, self.ai_workspace_dir):
                    try:
                        q.relative_to(base.resolve())
                        self._ai_created_path_keys.add(self._ephemeral_path_key(q))
                        return
                    except ValueError:
                        continue
        except OSError:
            pass

    def _is_ai_created_path(self, path_str: str) -> bool:
        if not path_str or not str(path_str).strip():
            return False
        try:
            p = Path(path_str.strip())
            if not p.is_absolute():
                for base in (self.work_directory, self.ai_workspace_temp_dir, self.ai_workspace_dir):
                    q = (base / p).resolve()
                    if self._ephemeral_path_key(q) in self._ai_created_path_keys:
                        return True
                return False
            p = p.resolve()
            return self._ephemeral_path_key(p) in self._ai_created_path_keys
        except OSError:
            return False

    def _parse_shell_invoked_script_path(self, command: str) -> Optional[Path]:
        return command_actions.parse_shell_invoked_script_path(self, command)

    def _get_path_policy(self) -> PathPolicy:
        pol = getattr(self, "path_policy", None)
        if pol is None:
            pol = PathPolicy(self)
            self.path_policy = pol
        return pol

    def _is_path_under(self, child: Path, root: Path) -> bool:
        return self._get_path_policy().is_path_under(child, root)

    def _workspace_skills_root(self) -> Path:
        return self._get_path_policy().workspace_skills_root()

    def _resolve_user_path(self, raw_path: str) -> Path:
        return self._get_path_policy().resolve_user_path(raw_path)

    def _is_workspace_skill_path(self, path: Path) -> bool:
        return self._get_path_policy().is_workspace_skill_path(path)

    def _skill_id_exists(self, skill_id: str) -> bool:
        sid = (skill_id or "").strip().lower()
        if not sid:
            return False
        for s in self.skills or []:
            cur = str(getattr(s, "skill_id", "")).strip().lower()
            if cur == sid:
                return True
        return False

    def _is_local_skill_id(self, skill_id: str) -> bool:
        sid = (skill_id or "").strip().lower()
        if not sid:
            return False
        for s in self.skills or []:
            if str(getattr(s, "skill_id", "")).strip().lower() == sid:
                return True
        return False

    def _canonical_skill_id(self, skill_id_or_name: str) -> str:
        """Resolve skill id/name to canonical skill_id (lowercased)."""
        key = str(skill_id_or_name or "").strip().lower()
        if not key:
            return ""
        for s in self.skills or []:
            sid = str(getattr(s, "skill_id", "")).strip()
            sname = str(getattr(s, "name", "")).strip()
            if not sid:
                continue
            if key == sid.lower() or (sname and key == sname.lower()):
                return sid.lower()
        return key

    def _result_indicates_user_cancelled(self, result: Dict[str, Any]) -> bool:
        """Best-effort detect user-cancelled operations across tools."""
        if not isinstance(result, dict):
            return False
        for k in ("cancelled", "cancelled_by_user", "user_cancelled"):
            if bool(result.get(k, False)):
                return True
        success_flag = result.get("success")
        if isinstance(success_flag, bool) and success_flag:
            # Successful tool calls may legitimately contain words like "用户取消" in
            # file content; do not treat normal output as cancellation.
            return False
        text_parts = [
            str(result.get("error") or ""),
            str(result.get("message") or ""),
        ]
        text = "\n".join(text_parts).lower()
        needles = [
            "user cancelled",
            "cancelled operation",
            "cancelled by user",
            "aborted by user",
            "installation aborted",
            "confirm installation yes(y)/no(n): n",
        ]
        return any(n.lower() in text for n in needles)

    def _reload_skills_if_workspace_skill_changed(self, paths: List[Path]) -> None:
        try:
            if any(self._is_workspace_skill_path(p) for p in paths):
                self._reload_skills()
                print("🔄 Detected changes in workspace/skills; skills have been auto-reloaded.")
        except Exception as e:
            print(f"⚠️ Failed to auto-reload skills: {e}")

    def _append_shell_merge_output_path(
        self,
        stdout_text: str,
        return_code: int,
        merge_path: Optional[str],
    ) -> str:
        return command_actions.append_shell_merge_output_path(stdout_text, return_code, merge_path)

    def action_shell_command(
        self,
        command: str,
        confirmed: bool = False,
        interactive: bool = False,
        input_data: Optional[str] = None,
    ) -> dict:
        """Run a shell command; capture stdout/stderr for AI context while echoing to the terminal."""
        return command_actions.action_shell_command(
            self,
            command=command,
            confirmed=confirmed,
            interactive=interactive,
            input_data=input_data,
        )

    def action_apply_unified_patch(
        self, file_path: str, patch: str, confirmed: bool = False
    ) -> dict:
        """对指定文本文件应用 unified patch。"""
        return filesystem_actions.action_apply_unified_patch(
            self, file_path=file_path, patch=patch, confirmed=confirmed
        )

    def action_read_image(self, file_path: str, prompt: str = "") -> dict:
        """读取图片内容，支持多种图片格式"""
        return filesystem_actions.action_read_image(self, file_path=file_path, prompt=prompt)

    def action_diff(self, file1: str, file2: str, options: Optional[str] = None) -> dict:
        """跨平台文件比较：Windows上优先使用diff.exe，否则使用fc命令；其他平台使用diff命令"""
        return filesystem_actions.action_diff(self, file1=file1, file2=file2, options=options)

    def action_project_context_search(self, params: Dict[str, Any]) -> dict:
        """
        M1 project context retrieval:
        - keep a lightweight incremental index
        - return ranked candidate files/symbols for the query
        """
        return command_actions.action_project_context_search(self, params=params)

    def _render_evidence_block_from_project_context_result(self, res: Dict[str, Any]) -> str:
        if not isinstance(res, dict) or not res.get("success", False):
            return ""
        cands = res.get("candidates") if isinstance(res.get("candidates"), list) else []
        if not cands:
            return ""
        lines: List[str] = [
            "[First-turn Evidence Block (auto-injected)]",
            "The following candidate files come from project_context_search. Prioritize shell search/reads based on this evidence to avoid blind global scanning:",
        ]
        for i, c in enumerate(cands[:8], start=1):
            if not isinstance(c, dict):
                continue
            p = str(c.get("path") or "").strip()
            score = c.get("score")
            reasons = c.get("reasons") if isinstance(c.get("reasons"), list) else []
            syms = c.get("symbols") if isinstance(c.get("symbols"), list) else []
            if not p:
                continue
            lines.append(
                f"{i}. `{p}` (score={score}; reasons={', '.join(str(x) for x in reasons[:3]) or '-'})"
            )
            if syms:
                lines.append(f"   symbols: {', '.join(str(x) for x in syms[:4])}")
        lines.append("")
        return "\n".join(lines)

    def _parse_tool_plan_from_response(self, text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Parse model response into tool plan under strict rule: exactly one tool JSON at reply end."""
        if not isinstance(text, str):
            return None
        text = text.strip()
        if not text:
            return None
        # Prefer fenced payloads first, supporting:
        # ```json\n{"tool":"...","args":{...}}\n```
        # ```\n`{"tool":"...","args":{...}}`\n```
        fence_payloads: List[str] = []
        for fm in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL):
            body = (fm.group(1) or "").strip()
            if body.startswith("`") and body.endswith("`") and len(body) >= 2:
                body = body[1:-1].strip()
            if body:
                fence_payloads.append(body)

        # Collect balanced JSON objects with their byte ranges.
        spans: List[Tuple[int, int, str]] = []
        for m_obj in re.finditer(r"\{", text):
            start = m_obj.start()
            depth = 0
            in_str = False
            esc = False
            end = -1
            i = start
            while i < len(text):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    i += 1
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
                i += 1
            if end != -1:
                chunk = text[start : end + 1].strip()
                spans.append((start, end + 1, chunk))

        valid: List[Tuple[int, int, Tuple[str, Dict[str, Any]]]] = []
        for payload in fence_payloads:
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            tool_name = obj.get("tool") or obj.get("action")
            args = obj.get("args") or obj.get("params") or {}
            if isinstance(tool_name, str) and tool_name.strip():
                if not isinstance(args, dict):
                    args = {}
                # Fenced payload has highest priority when valid.
                return (tool_name.strip(), args)

        for start, end, c in spans:
            try:
                obj = json.loads(c)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            tool_name = obj.get("tool") or obj.get("action")
            args = obj.get("args") or obj.get("params") or {}
            if isinstance(tool_name, str) and tool_name.strip():
                if not isinstance(args, dict):
                    args = {}
                valid.append((start, end, (tool_name.strip(), args)))

        if not valid:
            return None

        # Prefer the last valid JSON whose tail is only harmless closers.
        for start, end, plan in sorted(valid, key=lambda x: x[1], reverse=True):
            tail = text[end:].strip()
            if not tail or re.fullmatch(r"[\s`\-]*", tail):
                return plan
        return None

    def _find_tool_plan_anywhere(self, text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Find any valid tool plan JSON in response, regardless of position."""
        if not isinstance(text, str):
            return None
        for m_obj in re.finditer(r"\{", text):
            start = m_obj.start()
            depth = 0
            in_str = False
            esc = False
            end = -1
            i = start
            while i < len(text):
                ch = text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    i += 1
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
                i += 1
            if end == -1:
                continue
            chunk = text[start : end + 1].strip()
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            tool_name = obj.get("tool") or obj.get("action")
            args = obj.get("args") or obj.get("params") or {}
            if isinstance(tool_name, str) and tool_name.strip():
                if not isinstance(args, dict):
                    args = {}
                return (tool_name.strip(), args)
        return None

    def _strip_tool_json_blocks_for_display(self, text: str) -> str:
        return strip_tool_json_blocks_for_display(text)

    def _normalize_display_text(self, text: str) -> str:
        return normalize_display_text(text)

    def _format_assistant_display_response(self, text: str) -> str:
        return format_assistant_display_response(text)

    def _tool_call_summary(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Generate one-line tool execution summary."""
        a = args if isinstance(args, dict) else {}
        if str(tool_name).strip().lower() == "apply_patch":
            p = str(a.get("path") or "").strip() or "-"
            patch_v = a.get("patch")
            if isinstance(patch_v, str):
                patch_info = f"patch_chars={len(patch_v)}"
            elif patch_v is None:
                patch_info = "patch=missing"
            else:
                patch_info = f"patch_type={type(patch_v).__name__}"
            return f"apply_patch (path={p}, {patch_info})"
        if str(tool_name).strip().lower() == "shell":
            cmd = str(a.get("command") or "").strip()
            m = re.match(
                r"(?is)^(?:powershell(?:\.exe)?)\s+-ExecutionPolicy\s+Bypass\s+-Command\s+(?P<payload>.+)$",
                cmd,
            )
            if m:
                summary = m.group("payload").strip()
                if len(summary) >= 2 and summary[0] == summary[-1] and summary[0] in ("'", '"'):
                    quote = summary[0]
                    summary = summary[1:-1]
                    if quote == '"':
                        summary = summary.replace('`"', '"')
                    else:
                        summary = summary.replace("''", "'")
                if not summary:
                    summary = cmd
                return summary
        for k in (
            "skill_id",
            "mcp",
            "resource_id",
            "server",
            "tool",
            "url",
            "path",
            "filename",
            "file",
            "source",
            "target",
            "command",
            "query",
        ):
            v = a.get(k)
            if isinstance(v, str) and v.strip():
                vv = v.strip().replace("\n", " ")
                if k != "command" and len(vv) > 120:
                    vv = vv[:120] + "..."
                return f"{tool_name} ({k}={vv})"
        if a:
            keys = ",".join(sorted([str(k) for k in a.keys()])[:5])
            return f"{tool_name} (args: {keys})"
        return tool_name

    def _format_side_by_side_change_preview(
        self,
        old_lines: List[str],
        new_lines: List[str],
        old_start_line: int = 1,
        new_start_line: int = 1,
    ) -> List[str]:
        """Build compact side-by-side change preview with = / - / + markers."""
        return ChangePreviewFormatter.format_side_by_side(
            old_lines=old_lines,
            new_lines=new_lines,
            old_start_line=old_start_line,
            new_start_line=new_start_line,
        )

    def _format_side_by_side_change_preview_segments(
        self,
        segments: List[Dict[str, Any]],
    ) -> List[str]:
        """Build side-by-side preview for multiple hunks with omitted-line markers."""
        return ChangePreviewFormatter.format_side_by_side_segments(segments=segments)

    def execute_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        dispatcher = getattr(self, "tool_dispatcher", None)
        if dispatcher is not None:
            return dispatcher.dispatch_or_fallback(tool_name, arguments)
        return self._execute_tool_call_legacy(tool_name, arguments)

    def _execute_tool_call_legacy(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from .tooling.execution_engine import execute_tool_call_legacy
        return execute_tool_call_legacy(self, tool_name, arguments)

    def _parse_mcp_shortcut_command(self, builtin_line: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
        return parse_mcp_shortcut_command(builtin_line)

    def _print_mcp_shortcut_result(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        print_mcp_shortcut_result(tool_name, args, result)

    def _print_main_help(self) -> None:
        print("\nSmart Shell Help")
        print("=" * 80)
        print("\nBuilt-in commands:")
        print("  /exit, /quit")
        print("  /clear screen")
        print("  /clear input history")
        print("  /clear context")
        print("  /help")
        print("  /model [<model_provider>:<name>]")
        print("\nChat commands:")
        print("  /chat list | current | reload | new [name] | switch <selector> | rename <selector> <new> | delete <selector> | delete all")
        print("\nWorkspace commands:")
        print("  /workspace current | list | create <path> [--name <name>] | switch <selector>")
        print("  /workspace update <selector> [--name <name>] [--path <path>]")
        print("  /workspace delete <selector> [--remove-files]")
        print("\nMCP commands:")
        print("  /mcp status | status-refresh | reload-config")
        print("  /mcp reconnect <server> | server-info <server>")
        print("  /mcp list-tools <server> | list-resources <server>")
        print("  /mcp list-resource-templates <server> | list-prompts <server>")
        print("  /mcp list-disabled-tools [server]")
        print("  /mcp disable-tools <server> <tool1,tool2>")
        print("  /mcp enable-tools <server> <tool1,tool2>")
        print("\nKnowledge and memory:")
        print("  /knowledge status | sync | stats | search <query>")
        print("  /memory status | enable | disable | stats | list | search <query> | remember <text> | delete <id>")
        print("  /session-summary on|off|show")
        print("  /execution-policy show|unlimited|moderate|confirmation")
        print("  /always_confirm-reset")
        print("\nDirect shell (bypass AI):")
        print("  Use ! prefix, e.g. !ls, !dir, !git status, !python script.py")
        if self.skills:
            print(
                f"\nLoaded Agent Skills: {len(self.skills)} "
                f"(builtin: {self._builtin_skills_root}, external: {self.config_dir / 'skills'})"
            )
            print("  Use /skills/<skill-name> <task> to force a skill in current turn.")
            skill_cmds = self._get_slash_skill_commands()
            if skill_cmds:
                print("  Skill shortcuts:")
                print("    " + ", ".join(skill_cmds))
        print("=" * 80)

    def run(self):
        from .runtime.runtime_loop import run_agent_loop
        return run_agent_loop(self)

    def shutdown(self, wait: bool = False) -> None:
        """
        统一关闭运行期资源。默认非阻塞，避免退出流程被后台线程池/任务拖住。
        """
        try:
            self._shutdown_workspace_services(wait=wait)
        except Exception:
            pass
        try:
            self._shutdown_mcp_runtime()
        except Exception:
            pass

    def _build_step_progress_context(self) -> str:
        """Build concise step progress summary from executed operations."""
        if not self.operation_results:
            return "[Step Progress] No steps have been executed yet."

        lines = ["[Step Progress (in execution order)]"]
        for i, item in enumerate(self.operation_results, start=1):
            cmd = item.get("command") or {}
            res = item.get("result") or {}
            action = cmd.get("action") or cmd.get("tool") or "unknown"
            ok = bool(res.get("success", True))
            status = "completed" if ok else "failed"
            detail = str(res.get("message") or res.get("error") or "").replace("\n", " ").strip()
            if len(detail) > 160:
                detail = detail[:160] + "..."
            lines.append(
                f"- Step {i}: [{status}] tool={action}, detail={detail or '-'}"
            )
        return "\n".join(lines)

    def _compact_result_for_next_input(self, result: Dict[str, Any], max_chars: int = 3000) -> str:
        """
        Keep next-round context focused by compressing large tool payloads.
        Especially important for large shell outputs in long investigative tasks.
        """
        if not isinstance(result, dict):
            return ""
        compact = dict(result)
        for k in ("content", "output", "stderr", "analysis"):
            if k in compact and isinstance(compact.get(k), str):
                v = str(compact.get(k) or "")
                if len(v) > 800:
                    compact[k] = v[:800] + " ...[truncated]"
        s = json.dumps(compact, ensure_ascii=False)
        if len(s) > max_chars:
            s = s[:max_chars] + " ...[truncated]"
        return s

    def _is_repeated_tool_call_pattern(
        self,
        tool_name: str,
        args: Dict[str, Any],
        lookback: int = 6,
    ) -> bool:
        """
        Detect repeated shell loops with near-identical arguments.
        """
        if tool_name not in ("shell",):
            return False
        target = json.dumps({"tool": tool_name, "args": args or {}}, ensure_ascii=False, sort_keys=True)
        hits = 0
        for item in reversed(self.operation_results[-lookback:]):
            cmd = item.get("command") if isinstance(item, dict) else {}
            if not isinstance(cmd, dict):
                continue
            t = str(cmd.get("tool") or cmd.get("action") or "").strip()
            if t != tool_name:
                continue
            a = cmd.get("args")
            if not isinstance(a, dict):
                a = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
            cur = json.dumps({"tool": t, "args": a or {}}, ensure_ascii=False, sort_keys=True)
            if cur == target:
                hits += 1
            if hits >= 2:
                return True
        return False

    def _build_post_result_synthesis_rule(
        self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]
    ) -> str:
        """
        When shell output is very large or includes merged file content (see
        ``_append_shell_merge_output_path``), ask the model to synthesize for the user.
        """
        if tool_name != "shell":
            return ""
        output_text = str(result.get("output") or "")
        # Must match marker in _append_shell_merge_output_path (generic bridge output).
        merge_marker = "[Additional output (shell merge file)]"
        if merge_marker not in output_text and len(output_text) < 4000:
            return ""
        return (
            "You just received raw information output. In the next reply, you must first produce a user-facing distilled result, "
            "then decide whether to output done:\n"
            "- First provide 1-2 sentence final conclusion (directly answering the user);\n"
            "- Then provide no more than 3 key supporting points (prefer latest sources and include timestamps);\n"
            "- Provide timeliness/uncertainty notes;\n"
            "- Do not paste large chunks of webpage body verbatim.\n"
            "If the above distillation is incomplete, do not output done directly."
        )

    def _infer_selected_skill(self, command: Dict[str, Any], ai_response: str) -> Optional[Dict[str, str]]:
        """
        Infer selected skill from command metadata / script path / model text.
        Returns {"skill_id": "...", "name": "..."} or None.
        """
        if not self.skills:
            return None

        id_to_name = {str(s.skill_id): str(s.name) for s in self.skills}
        alias_to_id: Dict[str, str] = {}
        for s in self.skills:
            sid = str(s.skill_id).strip().lower()
            sname = str(s.name).strip().lower()
            if sid:
                alias_to_id[sid] = str(s.skill_id)
            if sname:
                alias_to_id[sname] = str(s.skill_id)

        for key in ("skill", "skill_name", "skill_id", "use_skill"):
            val = command.get(key)
            if isinstance(val, str) and val.strip():
                raw = val.strip()
                low = raw.lower()
                sid = alias_to_id.get(low) or alias_to_id.get(low.replace("_", "-"))
                if sid:
                    return {"skill_id": sid, "name": id_to_name.get(sid, sid)}

        blobs = [json.dumps(command, ensure_ascii=False), ai_response or ""]
        for blob in blobs:
            for m in re.finditer(r"[\\/](?:skills)[\\/](?P<sid>[a-zA-Z0-9._-]+)[\\/]", blob, flags=re.IGNORECASE):
                sid_raw = m.group("sid")
                sid = alias_to_id.get(sid_raw.lower()) or sid_raw
                if sid in id_to_name:
                    return {"skill_id": sid, "name": id_to_name.get(sid, sid)}

        # Conservative fallback: only trust explicit textual markers in AI output.
        # Avoid broad substring matching that may cause false-positive skill attribution.
        ai = (ai_response or "").lower()
        for alias, sid in alias_to_id.items():
            if not alias:
                continue
            if (
                f"skill: {alias}" in ai
                or f"skill=`{alias}`" in ai
                or f"skill_id={alias}" in ai
                or f"skill_id=`{alias}`" in ai
            ):
                return {"skill_id": sid, "name": id_to_name.get(sid, sid)}
        return None

    def _is_executable_file(self, user_input: str) -> bool:
        """
        检查输入是否为可执行文件
        Args:
            user_input: 用户输入
        Returns:
            True if executable, False otherwise
        """
        import shutil
        import os
        
        # 去除可能的参数
        command = user_input.split()[0] if user_input.strip() else ""
        if not command:
            return False
            
        # 检查是否为绝对路径或相对路径的可执行文件
        if os.path.isabs(command):
            # 绝对路径
            if os.path.isfile(command) and os.access(command, os.X_OK):
                return True
        else:
            # 相对路径或文件名
            execution_cwd = self._shell_execution_cwd()
            # 1. 检查当前目录
            current_path = execution_cwd / command
            if current_path.is_file() and os.access(current_path, os.X_OK):
                return True
                
            # 2. 检查当前目录下的常见可执行文件扩展名
            for ext in ['.exe', '.bat', '.cmd', '.com', '.py', '.ps1']:
                current_path_with_ext = execution_cwd / (command + ext)
                if current_path_with_ext.is_file():
                    return True
                    
            # 3. 检查PATH环境变量
            if shutil.which(command):
                return True
                
        return False

    def _status_token_usage_percent(self) -> int:
        return clamp_status_token_usage_percent(getattr(self, "_last_context_usage_percent", 0))

    def _refresh_status_context_usage_snapshot(self, user_input_hint: str = "", context_hint: str = "") -> None:
        refresh_status_context_usage_snapshot_fn(
            getattr(self, "session_memory_service", None),
            user_input_hint=user_input_hint,
            context_hint=context_hint,
        )

    def _status_bar_render_data(self) -> Tuple[List[Tuple[str, str]], str]:
        return build_status_bar_render_data(
            str(getattr(self, "model_name", "") or ""),
            str(getattr(self, "workspace_name", "") or ""),
            str(getattr(self, "active_chat_name", "") or ""),
            getattr(self, "_last_context_usage_percent", 0),
        )

    def _get_user_input_with_history(self) -> str:
        """
        获取用户输入，支持历史记录导航
        Returns:
            用户输入的字符串
        """
        import platform

        if bool(getattr(self, "_force_reload_chat_history_from_anchor_once", False)):
            self._force_reload_chat_history_from_anchor_once = False
            self._reload_chat_history_from_anchor_on_resize()
        else:
            self._maybe_reload_chat_history_on_terminal_resize()

        status_bar_fragments, status_bar_plain = self._status_bar_render_data()
        prompt = INPUT_PROMPT
        startup_prompt_pending = bool(getattr(self, "_startup_prompt_pending", True))
        if startup_prompt_pending:
            self._startup_prompt_pending = False
        suppress_separator_on_startup = startup_prompt_pending

        separator_requested = bool(getattr(self, "_show_separator_next_prompt", False))
        if separator_requested:
            self._show_separator_next_prompt = False

        suppress_separator_once = bool(getattr(self, "_suppress_next_separator", False))
        if suppress_separator_once:
            self._suppress_next_separator = False

        show_separator = (
            separator_requested
            and (not suppress_separator_once)
            and (not suppress_separator_on_startup)
        )
        if show_separator and not bool(
            getattr(self.input_handler, "renders_prompt_separator_inline", False)
        ):
            self._print_prompt_separator()
        
        # 重置历史记录索引
        self.history_manager.reset_index()

        # 优先使用已初始化的输入处理器（例如 Windows 下的 prompt_toolkit 补全）
        if self.input_handler is not None:
            try:
                try:
                    user_input = self.input_handler.get_input_with_completion(
                        prompt,
                        status_bar_text=status_bar_plain,
                        status_bar_fragments=status_bar_fragments,
                        show_status_bar=True,
                        show_separator=show_separator,
                    )
                    self._prompt_separator_rendered = bool(show_separator)
                except TypeError:
                    # Legacy handlers may not support status bar kwargs.
                    user_input = self.input_handler.get_input_with_completion(prompt)
                # 这里不直接写入 HistoryManager，交由上层 run() 统一处理，避免重复
                return user_input
            except Exception as e:
                print(f"⚠️ Input handler error; falling back to platform-specific input mode: {e}")
        
        # 在Windows系统上，优先使用prompt_toolkit以获得更好的中文输入支持
        if platform.system() == "Windows":
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.history import InMemoryHistory
                try:
                    from prompt_toolkit.cursor_shapes import CursorShape
                    from prompt_toolkit.cursor_shapes import SimpleCursorShapeConfig
                except Exception:
                    CursorShape = None  # type: ignore[assignment]
                    SimpleCursorShapeConfig = None  # type: ignore[assignment]
                
                # 创建历史记录
                history = InMemoryHistory()
                for entry in self.history_manager.get_all_history():
                    history.append_string(entry)
                
                # 创建会话
                session_kwargs = {"history": history}
                if CursorShape is not None and SimpleCursorShapeConfig is not None:
                    try:
                        session_kwargs["cursor"] = SimpleCursorShapeConfig(CursorShape.BLINKING_BEAM)
                    except Exception:
                        pass
                session = PromptSession(**session_kwargs)
                # 对 Windows Terminal 维持闪烁；Cursor 内置终端不强制发闪烁序列。
                try:
                    _app = getattr(session, "app", None)
                    if _app is not None:
                        _is_vscode_term = (
                            str(os.environ.get("TERM_PROGRAM", "") or "").strip().lower()
                            == "vscode"
                        )
                        def _on_after_render(_a) -> None:
                            if _is_vscode_term:
                                return
                            try:
                                _o = getattr(_a, "output", None)
                                if _o is not None and hasattr(_o, "write_raw") and hasattr(_o, "flush"):
                                    _o.write_raw("\x1b[?12h\x1b[?25h")
                                    _o.flush()
                                    return
                            except Exception:
                                pass
                            try:
                                sys.stdout.write("\x1b[?12h\x1b[?25h")
                                sys.stdout.flush()
                            except Exception:
                                pass
                        _evt = getattr(_app, "after_render", None)
                        if _evt is not None and hasattr(_evt, "add_handler"):
                            _evt.add_handler(_on_after_render)
                except Exception:
                    pass
                
                user_input = session.prompt(prompt).strip()
                
                # 保存到历史记录
                if user_input:
                    self.history_manager.add_entry(user_input)
                
                return user_input
                
            except ImportError:
                # 如果没有prompt_toolkit，回退到标准input
                print("⚠️ Tip: install prompt_toolkit for a better input experience: pip install prompt_toolkit")
                try:
                    print("")
                    print(status_bar_plain)
                    user_input = input(prompt).strip()
                    if user_input:
                        self.history_manager.add_entry(user_input)
                    return user_input
                except KeyboardInterrupt:
                    raise KeyboardInterrupt
            except Exception as e:
                # 如果prompt_toolkit出错，回退到标准input
                print(f"⚠️ prompt_toolkit error; falling back to standard input: {e}")
                try:
                    print("")
                    print(status_bar_plain)
                    user_input = input(prompt).strip()
                    if user_input:
                        self.history_manager.add_entry(user_input)
                    return user_input
                except KeyboardInterrupt:
                    raise KeyboardInterrupt
        else:
            # 非Windows系统使用简单的input
            try:
                print("")
                print(status_bar_plain)
                user_input = input(prompt).strip()
                if user_input:
                    self.history_manager.add_entry(user_input)
                return user_input
            except KeyboardInterrupt:
                raise KeyboardInterrupt

    def _normalize_elicitation_value(self, raw: str, schema: Dict[str, Any]) -> Any:
        t = str((schema or {}).get("type", "string")).strip().lower()
        if t == "integer":
            return int(raw.strip())
        if t == "number":
            return float(raw.strip())
        if t == "boolean":
            v = raw.strip().lower()
            if v in ("1", "true", "t", "yes", "y", "on"):
                return True
            if v in ("0", "false", "f", "no", "n", "off"):
                return False
            raise ValueError("boolean input expected")
        return raw

    def _handle_mcp_elicitation_create(self, server: str, params: Dict[str, Any]) -> Dict[str, Any]:
        p = params if isinstance(params, dict) else {}
        mode = str(p.get("mode", "form") or "form").strip().lower()
        message = str(p.get("message", "") or "").strip()
        if mode not in ("form", "url"):
            raise McpError(f"Unsupported elicitation mode: {mode}")

        # Non-interactive fallback for tests/piped execution.
        # SMART_SHELL_AUTO_ACCEPT_ELICITATION=1 can force auto-accept in test runners.
        auto_accept_elicitation = str(
            os.environ.get("SMART_SHELL_AUTO_ACCEPT_ELICITATION", "")
        ).strip().lower() in ("1", "true", "yes", "on")
        stdin_is_tty = False
        try:
            stdin_obj = getattr(sys, "stdin", None)
            stdin_is_tty = bool(stdin_obj is not None and stdin_obj.isatty())
        except Exception:
            # Some test runners replace stdin with objects that may not support isatty().
            stdin_is_tty = False
        if auto_accept_elicitation or (not stdin_is_tty):
            if mode == "url":
                return {"action": "accept"}
            requested = p.get("requestedSchema", {})
            props = requested.get("properties", {}) if isinstance(requested, dict) else {}
            content: Dict[str, Any] = {}
            if isinstance(props, dict):
                for key, meta in props.items():
                    k = str(key).strip()
                    if not k:
                        continue
                    default = meta.get("default") if isinstance(meta, dict) else None
                    content[k] = default if default is not None else ""
            return {"action": "accept", "content": content}

        print(f"\n📩 MCP elicitation request from server={server}")
        if message:
            print(f"Description: {message}")

        if mode == "url":
            target_url = str(p.get("url", "") or "").strip()
            print(f"URL: {target_url}")
            consent = input("Do you agree to continue this URL flow? (y=accept / n=decline / Enter=cancel): ").strip().lower()
            if consent == "y":
                return {"action": "accept"}
            if consent == "n":
                return {"action": "decline"}
            return {"action": "cancel"}

        requested = p.get("requestedSchema", {})
        if not isinstance(requested, dict):
            requested = {}
        props = requested.get("properties", {})
        required = requested.get("required", [])
        required_set = {str(x) for x in required} if isinstance(required, list) else set()
        content: Dict[str, Any] = {}
        if not isinstance(props, dict) or not props:
            consent = input("No requestedSchema was provided. Accept this request? (y/n): ").strip().lower()
            return {"action": "accept" if consent == "y" else "decline", "content": content}

        for key, meta in props.items():
            k = str(key).strip()
            if not k:
                continue
            s = meta if isinstance(meta, dict) else {}
            title = str(s.get("title", "") or "").strip()
            desc = str(s.get("description", "") or "").strip()
            default = s.get("default")
            label = title or k
            hint_parts = []
            if desc:
                hint_parts.append(desc)
            if k in required_set:
                hint_parts.append("required")
            if default is not None:
                hint_parts.append(f"default={default}")
            hint = f" ({', '.join(hint_parts)})" if hint_parts else ""
            while True:
                raw = input(f"Please input {label}{hint}: ")
                if raw == "" and default is not None:
                    content[k] = default
                    break
                if raw == "" and k not in required_set:
                    content[k] = ""
                    break
                try:
                    content[k] = self._normalize_elicitation_value(raw, s)
                    break
                except Exception:
                    print("Invalid input format, please try again.")

        submit = input("Submit elicitation data now? (y=accept / n=decline / Enter=cancel): ").strip().lower()
        if submit == "y":
            return {"action": "accept", "content": content}
        if submit == "n":
            return {"action": "decline"}
        return {"action": "cancel"}

    def _execute_file_directly(self, user_input: str) -> bool:
        """
        直接执行可执行文件，实时显示输出并支持交互输入
        Args:
            user_input: 用户输入
        Returns:
            True if executed successfully, False otherwise
        """
        import subprocess
        import os
        import sys
        import shlex
        import shutil
        
        try:
            raw = str(user_input or "").strip()
            if not raw:
                print("❌ Execution failed: empty command")
                return False

            try:
                parts = shlex.split(raw, posix=os.name != "nt")
            except ValueError:
                parts = raw.split()
            if not parts:
                print("❌ Execution failed: empty command")
                return False

            first = parts[0].strip().strip('"').strip("'")
            # Bare .py invocation (e.g. "!hello.py arg") should route through python.
            if first.lower().endswith(".py"):
                py_exe = shutil.which("python") or "python"
                cmd = subprocess.list2cmdline([py_exe] + parts)
            else:
                # Keep original command line as-is so "!python xxx.py" works correctly.
                cmd = raw
            
            try:
                # 通过父进程重放 stdout/stderr，确保输出前缀格式一致生效。
                return_code = self._run_direct_shell_with_prefixed_output(
                    cmd,
                    self._shell_execution_cwd(),
                )

                if return_code == 0:
                    return True
                else:
                    return False
            finally:
                self._reset_work_directory_to_startup_initial()
                
        except Exception as e:
            print(f"❌ Execution failed: {e}")
            return False

