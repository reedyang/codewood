import os
import sys
import json
import re
import shlex
import threading
import importlib
import warnings
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
from .integrations.mcp import McpManager, McpError
from .core.change_preview_formatter import ChangePreviewFormatter
from .ai.ai_provider_clients import AICallContext
from .services.session_memory_service import SessionMemoryService
from .policy.path_policy import PathPolicy
from .core.console_utils import (
    _ansi_blue,
    _ansi_gray,
    _ansi_red,
    _ansi_yellow,
    _ansi_green,
    _ansi_white,
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

# 根据操作系统选择合适的输入处理器
import platform

if platform.system() == "Windows":
    try:
        from .completion.windows_input import create_windows_input_handler
        TAB_COMPLETION_AVAILABLE = True
        INPUT_HANDLER_TYPE = "windows"
    except ImportError:
        TAB_COMPLETION_AVAILABLE = False
        INPUT_HANDLER_TYPE = "none"
else:
    try:
        from .completion.tab_completer import create_tab_completer
        TAB_COMPLETION_AVAILABLE = True
        INPUT_HANDLER_TYPE = "readline"
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

        bootstrap.setup_prompt_and_mcp(self)
        bootstrap.setup_skills(self, builtin_skills_dir=builtin_skills_dir)
        bootstrap.setup_input_handler(
            self,
            tab_completion_available=TAB_COMPLETION_AVAILABLE,
            input_handler_type=INPUT_HANDLER_TYPE,
            create_windows_input_handler=globals().get("create_windows_input_handler"),
            create_tab_completer=globals().get("create_tab_completer"),
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

        def _run() -> None:
            try:
                index.bind_workspace(target_root, storage_dir=target_storage)
                index.refresh_index(
                    force=bool(force),
                    timeout_ms=(None if force else 2000),
                )
            except Exception:
                pass
            finally:
                with gate:
                    self._project_context_refresh_inflight = False

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"smartshell-project-context-refresh:{reason or 'background'}",
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

    def _print_chat_history(self) -> None:
        title = f"===== Chat: [{self.active_chat_name}] ====="
        print(f"{_ansi_gray(title)}\n")
        if not self.conversation_history:
            print("(当前 Chat 暂无历史消息)")
            return
        for msg in self.conversation_history:
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "")
            if role == "user":
                print(f"{_ansi_gray('你:')} {content}")
            elif role == "assistant":
                display_response = self._normalize_display_text(
                    self._strip_tool_json_blocks_for_display(content)
                )
                if display_response:
                    print(f"{_ansi_gray('助手:')} {display_response}")
                tool_plan = self._find_tool_plan_anywhere(content)
                if tool_plan:
                    tool_name, args = tool_plan
                    if tool_name != "done":
                        print(f"{_ansi_gray('🔧 执行工具:')} {_ansi_blue(self._tool_call_summary(tool_name, args))}")
            else:
                print(content)

    def _rewrite_previous_prompt_as_user(self, user_text: str) -> None:
        """
        Best-effort: clear previous prompt line, then rewrite as gray '你:' line.
        """
        txt = str(user_text or "")
        if not txt:
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            # Current cursor is on line after Enter:
            #   <cwd>...
            #   <cursor here>
            # Move up once and clear the prompt line.
            sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.write(f"{_ansi_gray('你:')} {txt}\n")
            sys.stdout.flush()
        except Exception:
            pass

    def _clear_last_thinking_line(self) -> None:
        """
        Best-effort clear of the previously printed '🤖 AI正在思考...' line.
        """
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass

    def _append_chat_message(self, role: str, content: str) -> None:
        return self.session_memory_service.append_chat_message(role, content)

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
            for m in msgs:
                if str(m.get("role") or "").strip().lower() == "user":
                    first_user = str(m.get("content") or "").strip()
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
                return "新会话"
            return t

        try:
            prompt = (
                "你是聊天标题生成器。仅输出标题文本，不要解释。\n"
                "任务：根据用户第一条消息生成一个简短标题。\n"
                "要求：4-18个字符；不要标点结尾；不要出现“Chat/会话/标题/第一条消息”等词。\n"
                "如果消息非常短，可提炼为简短意图词。\n\n"
                f"<user_first_message>\n{first_user}\n</user_first_message>"
            )
            title = self.call_ai(prompt, context="", stream=False, session_summary_mode=True)
            t = title if isinstance(title, str) else ""
            t = t.strip().replace("\n", " ")
            t = re.sub(r"\s+", " ", t).strip(" \"'`[](){}")
            if any(bad in t for bad in ("第一条消息", "标题", "会话", "Chat", "chat")):
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
                    get_logger().exception("经验记忆 MemoryService 初始化失败")
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
                                svc.shutdown(wait=True)
                            except Exception:
                                pass
                    except Exception:
                        try:
                            get_logger().exception("知识库 KnowledgeService 构造失败")
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
            print(f"⚠️ Skill 热更新失败，继续使用当前已加载版本: {e}")

    def _load_mcp_config(self) -> Dict[str, Any]:
        """Load MCP configuration from <config_dir>/mcp.json."""
        mcp_path = self.config_dir / "mcp.json"
        if not mcp_path.is_file():
            return {"mcpServers": {}}
        try:
            with open(mcp_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print("⚠️ mcp.json 格式无效：根对象必须为 JSON object")
                return {"mcpServers": {}}
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                print("⚠️ mcp.json 格式无效：mcpServers 必须为 object")
                return {"mcpServers": {}}
            return {"mcpServers": servers}
        except Exception as e:
            print(f"⚠️ 读取 mcp.json 失败: {e}")
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
                return False, {}, "mcp.json 根对象必须为 JSON object"
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                return False, {}, "mcp.json 的 mcpServers 必须为 object"
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
            return {"success": False, "changed": False, "error": f"mcp.json 解析失败: {err}"}
        self._mcp_config_last_failed_file_sig = None
        new_struct_sig = self._calc_mcp_config_sig(new_cfg)
        if new_struct_sig == self._mcp_config_struct_sig:
            self._mcp_config_file_sig = cur_sig
            return {"success": True, "changed": False, "message": "MCP 配置未变化"}
        summary = self.mcp_manager.apply_config_changes(new_cfg, timeout_s=12.0)
        self.mcp_config = new_cfg
        self._mcp_config_struct_sig = new_struct_sig
        self._mcp_config_file_sig = cur_sig
        self.system_prompt = self._compose_system_prompt_snapshot(include_tools=False)
        return {
            "success": True,
            "changed": True,
            "summary": summary,
            "message": "MCP 配置重载完成",
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
        cmds: List[str] = []
        seen: Set[str] = set()
        for s in self.skills or []:
            sid = str(getattr(s, "skill_id", "")).strip()
            if sid:
                c = f"/{sid}"
                if c.lower() not in seen:
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
        return build_mcp_server_commands(self.mcp_manager.mcp_config)

    def _get_slash_dynamic_rules(self) -> List[Dict[str, Any]]:
        return build_slash_dynamic_rules(
            workspaces_state=self._workspaces_state,
            mcp_config=self.mcp_manager.mcp_config,
            mcp_scoped_groups_provider=self._get_slash_mcp_scoped_groups,
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
        Find one or more '/skill-id' tokens and match loaded skills by skill_id or name.
        Returns {"skills":[{"skill_id","name"}...], "rest"} when matched.
        """
        raw = (user_text or "").strip()
        if not raw:
            return None
        # token boundary: start or whitespace before '/', then read token until whitespace
        matches = list(re.finditer(r"(?<!\S)/([^\s/]+)", raw))
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
                pieces.append(raw[cursor:m.start()])
                cursor = m.end()
        if not selected:
            return None
        pieces.append(raw[cursor:])
        cleaned = "".join(pieces).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return {"skills": selected, "rest": cleaned}

    def _extract_forced_mcp_reference(self, user_text: str) -> Optional[Dict[str, Any]]:
        """
        Find one or more '/<server>/<tool_or_prompt>' tokens.
        Returns {"entries":[{server,name,kind}], "rest"} when matched.
        """
        raw = (user_text or "").strip()
        if not raw:
            return None
        matches = list(re.finditer(r"(?<!\S)/([^\s/]+)/([^\s]+)", raw))
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
            "【强制 MCP 引用】本轮任务必须优先参考并使用以下已指定 MCP 目标（按用户输入顺序）：",
        ]
        for e in entries:
            srv = str(e.get("server", "")).strip()
            name = str(e.get("name", "")).strip()
            kind = str(e.get("kind", "")).strip() or "unknown"
            lines.append(f"- `{srv}/{name}` ({kind})")
            if kind == "prompt":
                try:
                    pobj = self.mcp_manager.get_prompt(srv, name, {}, timeout_s=20.0)
                    desc = str((pobj or {}).get("description", "")).strip() if isinstance(pobj, dict) else ""
                    if desc:
                        lines.append(f"  prompt.description: {desc}")
                except Exception:
                    pass
        lines.append("若与 AGENTS.md 或通用规则冲突，以这些显式指定 MCP 目标为准（安全/越权硬限制除外）。")
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
                "等待知识库初始化超时（600s），请稍后在 /knowledge sync 重试"
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
            print(f"⚠️ 保存执行策略到配置失败: {e}")
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
            print(f"⚠️ 保存 session_summary_llm 到配置失败: {e}")
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
            print(f"⚠️ 保存 memory_enabled 到配置失败: {e}")
            return False

    def _enable_freedom(self) -> Dict[str, Any]:
        """兼容命令：设置 execution_policy=moderate"""
        if self.execution_policy == "moderate":
            return {"success": True, "message": "execution_policy 已处于 moderate"}
        self.execution_policy = "moderate"
        saved = self._save_execution_policy_to_config()
        return {
            "success": True,
            "message": f"execution_policy 已设置为 moderate{'（已保存配置）' if saved else ''}",
        }

    def _disable_freedom(self) -> Dict[str, Any]:
        """兼容命令：设置 execution_policy=confirmation"""
        if self.execution_policy == "confirmation":
            return {"success": True, "message": "execution_policy 已处于 confirmation"}
        self.execution_policy = "confirmation"
        saved = self._save_execution_policy_to_config()
        return {"success": True, "message": f"execution_policy 已设置为 confirmation{'（已保存配置）' if saved else ''}"}

    def _set_execution_policy(self, policy: str) -> Dict[str, Any]:
        pol = str(policy or "").strip().lower()
        if pol not in ("unlimited", "moderate", "confirmation"):
            return {
                "success": False,
                "error": "无效 execution_policy。可选值: unlimited, moderate, confirmation",
            }
        if self.execution_policy == pol:
            return {"success": True, "message": f"execution_policy 已处于 {pol}", "policy": pol}
        self.execution_policy = pol
        saved = self._save_execution_policy_to_config()
        return {
            "success": True,
            "message": f"execution_policy 已设置为 {pol}{'（已保存配置）' if saved else ''}",
            "policy": pol,
        }

    def _print_execution_policy_details(self) -> None:
        _pm = "`/execution-policy moderate`"
        _pc = "`/execution-policy confirmation`"
        _pu = "`/execution-policy unlimited`"
        pol = str(getattr(self, "execution_policy", "confirmation")).lower()
        print(f"执行策略 execution_policy：{pol}")
        if pol == "unlimited":
            print(
                _ansi_red(
                    "  所有操作直接执行，不做可逆性检测与确认。"
                    f"输入 {_pm} 可切换到 moderate；输入 {_pc} 可切回 confirmation。"
                )
            )
            print(_ansi_red("  注意事项：高风险操作也会直接执行，仅建议在完全可控环境使用。"))
        elif pol == "moderate":
            print(
                _ansi_yellow(
                    "  可逆操作在执行前会由 AI 判定，可逆则自动跳过 y/n 确认。AI 可逆性判定可能会犯错，请谨慎使用。"
                    f"输入 {_pc} 可切回 confirmation。"
                )
            )
        else:
            print(
                "  需确认的操作将始终询问 y/n。"
                f"输入 {_pm} 可切换到 moderate；输入 {_pu} 可切换到 unlimited。"
            )

    def _print_knowledge_status_details(self) -> None:
        svc = getattr(self, "knowledge_manager", None)
        manager_ready = bool(
            svc is not None and getattr(svc, "is_available", lambda: False)()
        )
        dep_ready = bool(KNOWLEDGE_AVAILABLE)
        print("知识库状态详情：")
        print(f"  feature: 始终启用（依赖可用时加载）")
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
            print("  注意事项：模型仅在用户明确要求检索或参考知识库时调用 knowledge_search；结果可能过时，关键结论请复核原文件。")
        elif dep_ready and not manager_ready:
            if svc is not None and not svc.is_ready():
                print("  知识库正在后台建立索引，请稍候；详情见 smartshell.log。")
            elif svc is not None and svc.is_ready() and not svc.is_available():
                print("  知识库初始化失败，请查看 smartshell.log。")
            else:
                print("  当前依赖可用但运行时未就绪。请查看日志、sentence-transformers 与配置目录 workspace/knowledge/。")
        else:
            if sys.version_info >= (3, 14):
                print("  当前环境不满足知识库依赖（例如 Python 3.14 下 ChromaDB 限制）。请使用 Python 3.12/3.13 并安装依赖。")
            else:
                print("  知识库依赖未安装或加载失败。请安装 requirements 中的知识库相关包。")

    def _print_memory_status_details(self) -> None:
        enabled = bool(getattr(self, "memory_enabled", True))
        dep = bool(MEMORY_AVAILABLE)
        ready = bool(self._ensure_memory_service())
        print("经验记忆状态详情（与知识库分离：内化教训/偏好，非文档库）：")
        print(f"  feature_enabled: {'yes' if enabled else 'no'}")
        print(f"  dependency_ready: {'yes' if dep else 'no'}")
        print(f"  runtime_ready: {'yes' if ready else 'no'}")
        if not enabled:
            print("  经验记忆功能已关闭。可使用 /memory enable 重新开启。")
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
                "  说明：每轮自然语言任务正常结束后会尝试后台自动反思（与上次触发间隔约 45 秒以上）；"
                "模型若认为有可复用教训才会写入（可能为 0 条）。"
                "也可手动 memory_search / memory_add 或 /memory remember；勿与 knowledge_search 混淆。"
            )
        elif dep and not ready:
            print("  记忆模块正在初始化或失败，请查看 smartshell.log 与配置目录 workspace/memory/。")
        else:
            print("  经验记忆不可用（初始化失败）；主程序可继续运行。")

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
        self._validate_single_model(self.provider, self.model_name, "模型")

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
                print(f"⚠️ 警告: {model_type} '{model_name}' 不在可用模型列表中")
                print(f"📋 可用模型: {available_models}")
                if available_models:
                    print(f"💡 建议使用: {available_models[0]}")
                print("💡 请检查 config.json 中的 model 配置")
        except ImportError:
            print(f"❌ 错误: 未安装 ollama 包，无法验证 {model_type}。请运行: pip install ollama")
        except Exception as e:
            print(f"⚠️ 验证{model_type}时出错: {e}")
            print(f"💡 请确保 Ollama 服务正在运行")

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
            image_path=image_path,
            history_user_input=history_user_input,
            history_skip_user=history_skip_user,
        )
        self.ai_orchestrator.context.provider = self.provider
        self.ai_orchestrator.context.model_name = self.model_name
        self.ai_orchestrator.context.openai_conf = self.openai_conf
        self.ai_orchestrator.context.openwebui_conf = self.openwebui_conf
        self.ai_orchestrator.context.work_directory = str(self.work_directory)
        return self.ai_orchestrator.call(call_ctx=call_ctx)

    def action_list_directory(self, path: Optional[str] = None, file_filter: Optional[str] = None) -> Dict[str, Any]:
        """列出目录内容"""
        return filesystem_actions.action_list_directory(self, path=path, file_filter=file_filter)

    def action_intelligent_filter(self, file_list_result: Dict[str, Any], filter_condition: str) -> Dict[str, Any]:
        """使用AI智能过滤文件列表"""
        return filesystem_actions.action_intelligent_filter(self, file_list_result=file_list_result, filter_condition=filter_condition)

    def action_change_directory(self, path: str) -> Dict[str, Any]:
        """切换工作目录"""
        return filesystem_actions.action_change_directory(self, path=path)

    def action_rename_file(self, old_name: str, new_name: str) -> Dict[str, Any]:
        """重命名文件或文件夹"""
        return filesystem_actions.action_rename_file(self, old_name=old_name, new_name=new_name)

    def action_move_file(self, source: str, destination: str, confirmed: bool = False) -> Dict[str, Any]:
        """移动文件或文件夹，支持通配符批量移动"""
        return filesystem_actions.action_move_file(self, source=source, destination=destination, confirmed=confirmed)

    def action_delete_file(self, file_name: str, confirmed: bool = False) -> Dict[str, Any]:
        """删除文件或文件夹，支持通配符批量删除"""
        return filesystem_actions.action_delete_file(self, file_name=file_name, confirmed=confirmed)

    def action_create_directory(self, dir_name: str) -> Dict[str, Any]:
        """创建新文件夹"""
        return filesystem_actions.action_create_directory(self, dir_name=dir_name)

    def action_get_file_info(self, file_name: str) -> Dict[str, Any]:
        """获取文件信息"""
        return filesystem_actions.action_get_file_info(self, file_name=file_name)

    def action_ffmpeg(self, source: str, target: str, options: Optional[str] = None) -> Dict[str, Any]:
        """调用ffmpeg处理媒体文件"""
        return filesystem_actions.action_ffmpeg(self, source=source, target=target, options=options)
    
    def action_summarize_file(self, file_path: str, max_lines: int = 50) -> dict:
        """总结文本文件内容"""
        return filesystem_actions.action_summarize_file(self, file_path=file_path, max_lines=max_lines)

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
        text_parts = [
            str(result.get("error") or ""),
            str(result.get("message") or ""),
            str(result.get("output") or ""),
            str(result.get("stderr") or ""),
        ]
        text = "\n".join(text_parts).lower()
        needles = [
            "用户取消",
            "取消了操作",
            "已由用户取消",
            "aborted by user",
            "installation aborted",
            "confirm installation yes(y)/no(n): n",
        ]
        return any(n.lower() in text for n in needles)

    def _reload_skills_if_workspace_skill_changed(self, paths: List[Path]) -> None:
        try:
            if any(self._is_workspace_skill_path(p) for p in paths):
                self._reload_skills()
                print("🔄 检测到 workspace/skills 变更，已自动重新加载 skills。")
        except Exception as e:
            print(f"⚠️ 自动重载 skills 失败: {e}")

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
        interactive: bool = True,
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

    def action_create_text_file(
        self, filename: str, content: str, confirmed: bool = False, overwrite: bool = False
    ) -> dict:
        """Create a user-requested file; supports relative paths."""
        return filesystem_actions.action_create_text_file(
            self, filename=filename, content=content, confirmed=confirmed, overwrite=overwrite
        )

    def action_read_file(
        self,
        file_path: str,
        max_lines: Optional[int] = None,
        start_line: Optional[int] = None,
        line_count: Optional[int] = None,
    ) -> dict:
        """读取文本文件内容（带行号），支持按行读取片段。"""
        return filesystem_actions.action_read_file(
            self,
            file_path=file_path,
            max_lines=max_lines,
            start_line=start_line,
            line_count=line_count,
        )

    def action_edit_text_file(
        self,
        file_path: str,
        start_line: int,
        line_span: int,
        operation: str,
        content: Optional[str] = None,
        confirmed: bool = False,
    ) -> dict:
        """按起始行与跨度对文本文件进行插入/删除/替换。"""
        return filesystem_actions.action_edit_text_file(
            self,
            file_path=file_path,
            start_line=start_line,
            line_span=line_span,
            operation=operation,
            content=content,
            confirmed=confirmed,
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

    def action_grep(self, params: Dict[str, Any]) -> dict:
        """Recursive regex grep over text files; results written to caller-specified file."""
        return command_actions.action_grep(self, params=params)

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
            "【首轮 Evidence Block（自动注入）】",
            "以下候选文件来自 project_context_search，请优先基于这些证据展开 read/grep/shell，避免盲目全局扫描：",
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
        """Hide tool-call JSON blocks from AI natural-language display."""
        if not isinstance(text, str) or not text:
            return ""

        def _replace_fence(match: re.Match) -> str:
            body = (match.group(1) or "").strip()
            if body.startswith("`") and body.endswith("`") and len(body) >= 2:
                body = body[1:-1].strip()
            try:
                obj = json.loads(body)
            except Exception:
                return match.group(0)
            if isinstance(obj, dict) and isinstance(
                (obj.get("tool") or obj.get("action")), str
            ):
                return ""
            return match.group(0)

        out = re.sub(
            r"```(?:json)?\s*(.*?)\s*```",
            _replace_fence,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        stripped = out.strip()
        if not stripped:
            return ""

        # Fallback for malformed responses that leave an unclosed markdown fence:
        # narrative + "```json\n{...tool...}" (no closing ```).
        unclosed = re.search(r"```(?:json)?\s*(.*)\Z", stripped, flags=re.IGNORECASE | re.DOTALL)
        if unclosed:
            body = (unclosed.group(1) or "").strip()
            if body.startswith("`") and body.endswith("`") and len(body) >= 2:
                body = body[1:-1].strip()
            try:
                obj = json.loads(body)
            except Exception:
                obj = None
            if isinstance(obj, dict) and isinstance((obj.get("tool") or obj.get("action")), str):
                return stripped[: unclosed.start()].strip()

        # Fallback for non-fenced trailing tool JSON.
        def _find_trailing_tool_json_span(s: str) -> Optional[Tuple[int, int]]:
            s = s.rstrip()
            if not s:
                return None
            n = len(s)
            for m_obj in re.finditer(r"\{", s):
                start = m_obj.start()
                depth = 0
                in_str = False
                esc = False
                end = -1
                i = start
                while i < n:
                    ch = s[i]
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
                            end = i + 1
                            break
                    i += 1
                if end == -1 or end != n:
                    continue
                chunk = s[start:end].strip()
                try:
                    obj = json.loads(chunk)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance((obj.get("tool") or obj.get("action")), str):
                    return (start, end)
            return None

        trailing_span = _find_trailing_tool_json_span(stripped)
        if trailing_span:
            start, _ = trailing_span
            prefix = stripped[:start]
            prefix = re.sub(r"```(?:json)?\s*$", "", prefix, flags=re.IGNORECASE)
            return prefix.strip()
        return stripped

    def _normalize_display_text(self, text: str) -> str:
        """
        Normalize assistant display text:
        - trim edges
        - collapse excessive blank lines to at most one empty line
        """
        if not isinstance(text, str) or not text:
            return ""
        s = text.replace("\r\n", "\n").replace("\r", "\n")
        if not s.strip():
            return ""
        lines = s.split("\n")
        out: List[str] = []
        prev_blank = False
        for ln in lines:
            blank = (ln.strip() == "")
            if blank:
                if prev_blank:
                    continue
                out.append("")
                prev_blank = True
            else:
                out.append(ln.rstrip())
                prev_blank = False
        while out and out[0] == "":
            out.pop(0)
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    def _tool_call_summary(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Generate one-line tool execution summary."""
        a = args if isinstance(args, dict) else {}
        if str(tool_name).strip().lower() == "read" and a:
            def _fmt_val(v: Any) -> str:
                vv = str(v).strip().replace("\n", " ")
                return (vv[:120] + "...") if len(vv) > 120 else vv

            preferred = ("path", "file_path", "filename", "file")
            parts: List[str] = []
            seen: set = set()
            for k in preferred:
                if k in a and a.get(k) not in (None, ""):
                    parts.append(f"{k}={_fmt_val(a.get(k))}")
                    seen.add(k)
                    break
            for k in sorted(a.keys()):
                if k in seen:
                    continue
                v = a.get(k)
                if v is None or v == "":
                    continue
                parts.append(f"{k}={_fmt_val(v)}")
            if parts:
                return f"{tool_name} ({', '.join(parts)})"

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
                if len(vv) > 120:
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
        print("  /cls, /clear screen")
        print("  /clear history")
        print("  /clear context")
        print("  /help")
        print("\nChat commands:")
        print("  /chat list | current | new [name] | switch <selector> | rename <selector> <new> | delete <selector> | delete all")
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
            print("  Use /<skill-id> <task> to force a skill in current turn.")
            skill_cmds = self._get_slash_skill_commands()
            if skill_cmds:
                print("  Skill shortcuts:")
                print("    " + ", ".join(skill_cmds))
        print("=" * 80)

    def run(self):
        from .runtime.runtime_loop import run_agent_loop
        return run_agent_loop(self)

    def _build_step_progress_context(self) -> str:
        """Build concise step progress summary from executed operations."""
        if not self.operation_results:
            return "【步骤进度】暂无已执行步骤。"

        lines = ["【步骤进度（按执行顺序）】"]
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
        Especially important for read/grep outputs in long investigative tasks.
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
        Detect repeated read/grep loops with near-identical arguments.
        """
        if tool_name not in ("read", "grep"):
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
        merge_marker = "【附加输出（shell merge file）】"
        if merge_marker not in output_text and len(output_text) < 4000:
            return ""
        return (
            "你刚得到的是原始信息输出。下一条回复必须先做“面向用户的结果提炼”，"
            "再决定是否 done：\n"
            "- 先给出 1-2 句最终结论（直接回答用户问题）；\n"
            "- 再给出不超过 3 条关键依据（优先最新来源，并标注时间）；\n"
            "- 给出时效性/不确定性提示；\n"
            "- 禁止原样粘贴大段网页正文。\n"
            "若上述提炼未完成，禁止直接输出 done。"
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
            # 1. 检查当前目录
            current_path = self.work_directory / command
            if current_path.is_file() and os.access(current_path, os.X_OK):
                return True
                
            # 2. 检查当前目录下的常见可执行文件扩展名
            for ext in ['.exe', '.bat', '.cmd', '.com', '.py', '.ps1']:
                current_path_with_ext = self.work_directory / (command + ext)
                if current_path_with_ext.is_file():
                    return True
                    
            # 3. 检查PATH环境变量
            if shutil.which(command):
                return True
                
        return False

    def _get_user_input_with_history(self) -> str:
        """
        获取用户输入，支持历史记录导航
        Returns:
            用户输入的字符串
        """
        import platform

        workspace_prompt_line = f"[Workspace: {self.workspace_name}][Chat: {self.active_chat_name}]"
        status_bar_fragments = [
            ("fg:ansiyellow", str(self.model_name)),
            ("", " "),
            ("fg:ansigreen", str(self.workspace_name)),
            ("", " "),
            ("fg:ansiwhite", str(self.active_chat_name)),
        ]
        status_bar_plain = (
            f"{_ansi_yellow(str(self.model_name))} "
            f"{_ansi_green(str(self.workspace_name))} "
            f"{_ansi_white(str(self.active_chat_name))}"
        )
        prompt = f"{str(self.work_directory)}>"
        
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
                    )
                except TypeError:
                    # readline/tab_completer handlers may not support status bar kwargs.
                    user_input = self.input_handler.get_input_with_completion(prompt)
                # 这里不直接写入 HistoryManager，交由上层 run() 统一处理，避免重复
                return user_input
            except Exception as e:
                print(f"⚠️ 输入处理器出错，回退到平台特定输入方案: {e}")
        
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
                # Re-assert cursor blink after every redraw (Windows Terminal compat).
                # NOTE: Application.after_render is an Event — must add via add_handler.
                try:
                    _app = getattr(session, "app", None)
                    if _app is not None:
                        def _on_after_render(_a) -> None:
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
                
                user_input = session.prompt(f"{str(self.work_directory)}>").strip()
                
                # 保存到历史记录
                if user_input:
                    self.history_manager.add_entry(user_input)
                
                return user_input
                
            except ImportError:
                # 如果没有prompt_toolkit，回退到标准input
                print("⚠️ 提示：安装 prompt_toolkit 可获得更好的输入体验：pip install prompt_toolkit")
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
                print(f"⚠️ prompt_toolkit 出错，回退到标准输入: {e}")
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
            raise McpError(f"不支持的 elicitation mode: {mode}")

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

        print(f"\n📩 MCP elicitation 请求来自 server={server}")
        if message:
            print(f"说明: {message}")

        if mode == "url":
            target_url = str(p.get("url", "") or "").strip()
            print(f"URL: {target_url}")
            consent = input("是否同意继续该 URL 流程？(y=accept / n=decline / Enter=cancel): ").strip().lower()
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
            consent = input("未提供 requestedSchema，是否接受本次请求？(y/n): ").strip().lower()
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
                raw = input(f"请输入 {label}{hint}: ")
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
                    print("输入格式无效，请重试。")

        submit = input("提交本次 elicitation 数据？(y=accept / n=decline / Enter=cancel): ").strip().lower()
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
                print("❌ 执行文件失败: 空命令")
                return False

            try:
                parts = shlex.split(raw, posix=os.name != "nt")
            except ValueError:
                parts = raw.split()
            if not parts:
                print("❌ 执行文件失败: 空命令")
                return False

            first = parts[0].strip().strip('"').strip("'")
            # Bare .py invocation (e.g. "!hello.py arg") should route through python.
            if first.lower().endswith(".py"):
                py_exe = shutil.which("python") or "python"
                cmd = subprocess.list2cmdline([py_exe] + parts)
            else:
                # Keep original command line as-is so "!python xxx.py" works correctly.
                cmd = raw
            
            # 使用Popen启动进程，让进程继承当前终端，支持交互
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdin=sys.stdin,      # 继承当前终端的输入
                stdout=sys.stdout,    # 继承当前终端的输出
                stderr=sys.stderr,    # 继承当前终端的错误输出
                cwd=str(self.work_directory)
            )
            
            # 等待进程结束
            return_code = process.wait()
            
            if return_code == 0:
                return True
            else:
                print(f"⚠️ 进程退出码: {return_code}")
                return False
                
        except Exception as e:
            print(f"❌ 执行文件失败: {e}")
            return False

