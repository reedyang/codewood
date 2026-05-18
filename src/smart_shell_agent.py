import os
import sys
import json
import hashlib
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

def _decode_subprocess_output(data: Optional[bytes]) -> str:
    """
    Decode shell stdout/stderr: prefer UTF-8, else system locale.
    Fixes mojibake when a UTF-8 child is decoded as cp936 on Chinese Windows.
    """
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    for dec in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(dec, errors="strict")
        except UnicodeDecodeError:
            continue
    import locale

    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _safe_console_write(text: str, stream: Any = None, append_newline: bool = True) -> None:
    """
    Write text to console safely on Windows terminals with legacy encodings (e.g. GBK).
    Falls back to replacement encoding instead of raising UnicodeEncodeError.
    """
    if text is None:
        return
    s = stream or sys.stdout
    try:
        s.write(text)
        if append_newline and not text.endswith("\n"):
            s.write("\n")
        s.flush()
        return
    except UnicodeEncodeError:
        pass

    enc = getattr(s, "encoding", None) or "utf-8"
    payload = text if (text.endswith("\n") or not append_newline) else (text + "\n")
    try:
        if hasattr(s, "buffer"):
            s.buffer.write(payload.encode(enc, errors="replace"))
            s.flush()
        else:
            s.write(payload.encode(enc, errors="replace").decode(enc, errors="replace"))
            s.flush()
    except Exception:
        # Last-resort fallback; avoid crashing the agent on terminal encoding issues.
        try:
            print(payload.encode("ascii", errors="replace").decode("ascii"), end="")
        except Exception:
            pass


# 导入历史记录管理器
from .app_logging import get_logger, setup_app_logging
from .history_manager import HistoryManager
from .skills_loader import (
    build_skills_routing_prefix,
    calc_skills_dirs_fingerprint,
    load_skills_merged,
    _list_bundled_script_paths,
)
from .mcp_manager import McpManager, McpError
from .tools.project_context_index import ProjectContextIndex
from .change_preview_formatter import ChangePreviewFormatter
from .ai_provider_clients import AICallContext
from .ai_orchestrator import AIOrchestrator, AgentAIContext
from .session_memory_service import SessionMemoryService
from .policy.path_policy import PathPolicy
from .tool_dispatcher import ToolDispatcher
from .builtin_command_router import dispatch_builtin_command
from .completion.slash_dynamic_completions import (
    build_mcp_scoped_commands,
    build_mcp_scoped_groups,
    build_mcp_server_commands,
    build_mcp_server_target_commands,
    build_slash_dynamic_rules,
    build_workspace_action_commands,
)
from . import filesystem_actions
from . import command_actions
from . import command_security

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


def _enable_windows_console_vt() -> None:
    """Enable ANSI escape sequences on Windows 10+ console when stdout is a TTY."""
    if sys.platform != "win32":
        return
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    except Exception:
        pass


def _stdout_color_enabled() -> bool:
    """
    Whether ANSI colors should be emitted. Unix/macOS terminals typically support SGR
    sequences on a TTY; Windows needs VT processing (see _enable_windows_console_vt).
    Honors NO_COLOR (https://no-color.org/), TERM=dumb, and optional FORCE_COLOR.
    """
    # NO_COLOR: any presence disables color (spec: regardless of value)
    if "NO_COLOR" in os.environ:
        return False
    force = (os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE") or "").strip().lower()
    if force in ("1", "true", "yes", "always"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    return True


def _ansi_red(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[31m{text}\033[0m"


def _ansi_yellow(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[33m{text}\033[0m"


def _ansi_gray(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[90m{text}\033[0m"

def _ansi_blue(text: str) -> str:
    if not _stdout_color_enabled():
        return text
    if sys.platform == "win32":
        _enable_windows_console_vt()
    return f"\033[34m{text}\033[0m"


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
        self.work_directory = startup_work_directory
        # Runtime guard: prevent AI from modifying smart-shell itself.
        self._self_repo_root = Path(__file__).resolve().parent.parent
        self.conversation_history = []
        self._chat_state: Dict[str, Any] = {}
        self.active_chat_id: str = ""
        self.active_chat_name: str = "New Chat"
        self._chat_state_lock = threading.RLock()
        self._queued_user_input: Optional[str] = None
        # 会话摘要（经验记忆检索 query 前缀）：滚动摘录始终更新；LLM 摘要按轮次节流更新。
        self._session_summary_llm: str = ""
        self._session_summary_rolling: str = ""
        self._last_llm_summary_pair_count: int = 0
        self.operation_results = []
        # Session-local paths created by action "script"; may be auto-removed after shell runs them
        self._ephemeral_script_paths: Set[str] = set()
        # All path keys for files AI created this session (scripts + outputs detected from shell), for freedom auto-confirm
        self._ai_created_path_keys: Set[str] = set()
        # Basename of last ephemeral script auto-removed after shell (avoid redundant delete + freedom prompt)
        self._last_auto_removed_ephemeral: Optional[str] = None
        # MCP auth-gate: avoid repeated token-prompt shell loops.
        self._mcp_pending_user_input: Dict[str, Dict[str, Any]] = {}
        
        if config_dir:
            self.config_dir = Path(config_dir)
        else:
            # 自动查找配置文件目录
            current_config_dir = Path(".smartshell")
            user_config_dir = Path.home() / ".smartshell"

            # 如果用户目录下有配置文件，使用用户目录
            if (user_config_dir / "config.json").exists():
                config_dir = user_config_dir
            elif (current_config_dir / "config.json").exists():
                config_dir = current_config_dir
            else:
                # 默认使用用户目录
                config_dir = user_config_dir

            self.config_dir = Path(config_dir)

        self.workspace_registry_path = self.config_dir / WORKSPACE_STATE_FILE
        self._workspaces_state = self._load_workspace_state()
        active_workspace_id = str(
            self._workspaces_state.get("active") or DEFAULT_WORKSPACE_ID
        )
        workspaces = self._workspaces_state.get("workspaces", {})
        active_workspace = workspaces.get(active_workspace_id) if isinstance(workspaces, dict) else None
        if not isinstance(active_workspace, dict):
            active_workspace = self._default_workspace_entry()
            self._workspaces_state["active"] = DEFAULT_WORKSPACE_ID
        self._apply_workspace_entry(active_workspace, startup_work_directory)

        self.history_manager = HistoryManager(str(self.ai_workspace_dir))
        self._load_chat_state()

        setup_app_logging(self.config_dir)

        # 知识库：不在主线程 import knowledge_manager（否则会同步加载 chromadb、transformers、torch 等，冷启动可达数秒）。
        # 实际加载见 _schedule_knowledge_service_background；单测可在构造前将本模块 KNOWLEDGE_AVAILABLE=False 以跳过。
        self.knowledge_manager = None

        # 加载配置（执行策略默认 confirmation）；知识库在依赖可用时始终启用，不再提供开关
        self.execution_policy = "confirmation"
        self.memory_enabled: bool = True
        self.session_summary_llm_enabled: bool = True
        self.memory_fallback_expansion_enabled: bool = True
        self.project_context_first_round_evidence_enabled: bool = True
        self.max_tool_rounds: int = 20
        try:
            cfg_path = self.config_dir / "config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                pol = str(cfg_data.get("execution_policy", "confirmation")).strip().lower()
                if pol not in ("unlimited", "moderate", "confirmation"):
                    pol = "confirmation"
                self.execution_policy = pol
                _sslm = cfg_data.get("session_summary_llm", True)
                if isinstance(_sslm, bool):
                    self.session_summary_llm_enabled = _sslm
                else:
                    self.session_summary_llm_enabled = str(_sslm).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                _mfe = cfg_data.get("memory_fallback_expansion", True)
                if isinstance(_mfe, bool):
                    self.memory_fallback_expansion_enabled = _mfe
                else:
                    self.memory_fallback_expansion_enabled = str(_mfe).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                _me = cfg_data.get("memory_enabled", True)
                if isinstance(_me, bool):
                    self.memory_enabled = _me
                else:
                    self.memory_enabled = str(_me).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                _pcfr = cfg_data.get("project_context_first_round_evidence", True)
                if isinstance(_pcfr, bool):
                    self.project_context_first_round_evidence_enabled = _pcfr
                else:
                    self.project_context_first_round_evidence_enabled = str(_pcfr).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                _mtr = cfg_data.get("max_tool_rounds", 20)
                try:
                    self.max_tool_rounds = int(_mtr)
                except Exception:
                    self.max_tool_rounds = 20
                if self.max_tool_rounds < 1:
                    self.max_tool_rounds = 1
                if self.max_tool_rounds > 200:
                    self.max_tool_rounds = 200
        except Exception as e:
            print(f"⚠️ 读取 config.json 失败（执行策略 / session_summary_llm 等使用默认值）: {e}")

        # Per-target allowlist for y/n confirmations (see confirm_allowlist.json)
        self._allowlist_shell_paths: Dict[str, str] = {}
        self._allowlist_shell_exes: Set[str] = set()
        self._allowlist_script: Set[str] = set()
        self._confirm_allowlist_salt: str = ""
        self._load_confirm_allowlist()
        # Cached combined script review for non-session scripts (path + content + command hash)
        self._freedom_script_review_entries: Dict[str, Dict[str, Any]] = {}
        self._load_freedom_script_review_cache()

        # 单模型配置（model_config 优先）
        if model_config and isinstance(model_config, dict):
            self.provider = str(model_config.get("provider", provider) or provider).strip()
            self.params = model_config.get("params", {}) or {}
            self.model_name = str(self.params.get("model", model_name) or model_name).strip()
        else:
            self.model_name = model_name
            self.provider = provider
            self.params = params or {}
        self.openai_conf = self.params if self.provider == "openai" else openai_conf
        self.openwebui_conf = self.params if self.provider == "openwebui" else openwebui_conf
        self.path_policy = PathPolicy(self)
        self.session_memory_service = SessionMemoryService(self)
        self.ai_orchestrator = AIOrchestrator(
            AgentAIContext(
                provider=self.provider,
                model_name=self.model_name,
                openai_conf=self.openai_conf,
                openwebui_conf=self.openwebui_conf,
                work_directory=str(self.work_directory),
                history_writer=self._append_chat_message,
                regular_message_builder=self._build_regular_task_messages,
                ollama_importer=_import_ollama_client,
            )
        )

        # 模型可用性校验（ollama.list）可能阻塞网络；见 _schedule_model_validation_background，在后台执行

        # 系统提示词
        prompt_path = os.path.join(os.path.dirname(__file__), 'system_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            self._base_system_prompt = f.read()
        self.mcp_config = self._load_mcp_config()
        self.mcp_manager = McpManager(
            self.config_dir,
            self.mcp_config,
            self.ai_workspace_dir,
            tool_policy_parent=self.ai_workspace_dir,
        )
        self.mcp_manager.register_client_method_handler("elicitation/create", self._handle_mcp_elicitation_create)
        # Async preload MCP tools cache on startup (non-blocking).
        self.mcp_manager.preload_all_async(timeout_s=12.0, force=False)
        self._mcp_config_path = self.config_dir / "mcp.json"
        self._mcp_config_file_sig = self._get_mcp_config_file_sig()
        self._mcp_config_struct_sig = self._calc_mcp_config_sig(self.mcp_config)
        self._mcp_config_last_failed_file_sig: Optional[Tuple[bool, int, int]] = None
        self.system_prompt = self._compose_system_prompt_snapshot(include_tools=False)
        self.tool_specs = self._load_tools_spec_from_jsonc()
        self.tools_prompt_template = self._load_tools_prompt_template()

        self._builtin_skills_root = (
            Path(builtin_skills_dir).expanduser().resolve()
            if builtin_skills_dir
            else Path(__file__).resolve().parent.parent / "skills"
        )
        self.skills = load_skills_merged(
            self.config_dir,
            self._builtin_skills_root,
            self.ai_workspace_dir,
        )
        self._skills_dirs_fingerprint = calc_skills_dirs_fingerprint(
            self.config_dir,
            self._builtin_skills_root,
            self.ai_workspace_dir,
        )
        self._skills_routing_prefix = build_skills_routing_prefix(self.skills)
        self._active_skill_full_prompt: str = ""
        self._active_skill_id: Optional[str] = None
        self._active_skill_source: Optional[str] = None  # local | mcp
        self._active_skill_section: int = 0
        self._active_skill_total_sections: int = 0
        self._active_skill_chunked: bool = False

        # 初始化输入处理器，确保属性存在
        self.input_handler = None
        if TAB_COMPLETION_AVAILABLE:
            try:
                if INPUT_HANDLER_TYPE == "windows":
                    try:
                        initial_history = self.history_manager.get_all_history()
                    except Exception:
                        initial_history = []
                    self.input_handler = create_windows_input_handler(
                        work_directory=self.work_directory,
                        initial_history=initial_history,
                        slash_skill_commands=self._get_slash_skill_commands(),
                        slash_mcp_commands=self._get_slash_mcp_server_commands(),
                        slash_dynamic_rules=self._get_slash_dynamic_rules(),
                    )
                elif INPUT_HANDLER_TYPE == "readline":
                    self.input_handler = create_tab_completer(self.work_directory)
                else:
                    print("⚠️ 未知的输入处理器类型")
            except Exception as e:
                print(f"⚠️ 输入处理器初始化失败: {e}")
        else:
            print("⚠️ Tab补全功能不可用")

        self._workspace_runtime_generation = 0
        self._project_context_index = ProjectContextIndex(
            workspace_root=self.work_directory,
            storage_dir=(self.ai_workspace_dir / "knowledge_db"),
        )
        self._schedule_model_validation_background()
        self._schedule_knowledge_service_background()
        self.memory_service = None
        self._last_memory_reflect_at = 0.0
        self._schedule_memory_service_background()
        self.tool_dispatcher = ToolDispatcher(self, self._execute_tool_call_legacy)

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
            self._project_context_index.bind_workspace(self.work_directory)
        except Exception:
            pass

    def _path_identity_key(self, path: Path) -> str:
        value = str(self._resolve_path_lenient(path))
        return value.casefold() if os.name == "nt" else value

    def _workspace_id_for_path(self, path: Path) -> str:
        digest = hashlib.sha1(self._path_identity_key(path).encode("utf-8")).hexdigest()
        return f"ws_{digest[:12]}"

    def _default_workspace_entry(self) -> Dict[str, Any]:
        root = self._resolve_path_lenient(self.config_dir / "workspace")
        return {
            "id": DEFAULT_WORKSPACE_ID,
            "name": DEFAULT_WORKSPACE_NAME,
            "kind": "default",
            "root": str(root),
            "storage": str(root),
        }

    def _workspace_root_path(self, entry: Dict[str, Any]) -> Path:
        if str(entry.get("id") or "") == DEFAULT_WORKSPACE_ID or str(entry.get("kind") or "").lower() == "default":
            return self._resolve_path_lenient(self.config_dir / "workspace")
        raw = entry.get("root") or entry.get("path") or entry.get("storage") or ""
        root = self._resolve_path_lenient(Path(str(raw)).expanduser())
        if root.name.casefold() == ".smartshell":
            return root.parent
        return root

    def _workspace_storage_path(self, entry: Dict[str, Any]) -> Path:
        if str(entry.get("id") or "") == DEFAULT_WORKSPACE_ID or str(entry.get("kind") or "").lower() == "default":
            return self._resolve_path_lenient(self.config_dir / "workspace")
        storage = entry.get("storage")
        if storage:
            return self._resolve_path_lenient(Path(str(storage)).expanduser())
        return self._workspace_root_path(entry) / ".smartshell"

    def _workspace_current_dir_path(self, entry: Dict[str, Any]) -> Optional[Path]:
        raw = entry.get("current_dir")
        if not raw:
            return None
        return self._resolve_path_lenient(Path(str(raw)).expanduser())

    def _load_workspace_state(self) -> Dict[str, Any]:
        raw_state: Dict[str, Any] = {}
        if self.workspace_registry_path.exists():
            try:
                with open(self.workspace_registry_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw_state = loaded
            except Exception as e:
                print(f"⚠️ 读取 workspace registry 失败，使用默认 workspace: {e}")

        raw_workspaces = raw_state.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}

        default_entry = self._default_workspace_entry()
        old_default = raw_workspaces.get(DEFAULT_WORKSPACE_ID)
        if isinstance(old_default, dict) and old_default.get("current_dir"):
            default_entry["current_dir"] = str(
                self._resolve_path_lenient(Path(str(old_default.get("current_dir"))))
            )

        workspaces: Dict[str, Dict[str, Any]] = {DEFAULT_WORKSPACE_ID: default_entry}
        for key, raw_entry in raw_workspaces.items():
            if key == DEFAULT_WORKSPACE_ID or not isinstance(raw_entry, dict):
                continue
            root_raw = raw_entry.get("root") or raw_entry.get("path")
            if not root_raw and raw_entry.get("storage"):
                storage_path = self._resolve_path_lenient(Path(str(raw_entry.get("storage"))))
                root_path = storage_path.parent if storage_path.name.casefold() == ".smartshell" else storage_path
            elif root_raw:
                root_path = self._resolve_path_lenient(Path(str(root_raw)))
            else:
                continue

            workspace_id = str(raw_entry.get("id") or key or self._workspace_id_for_path(root_path)).strip()
            if not workspace_id or workspace_id == DEFAULT_WORKSPACE_ID:
                workspace_id = self._workspace_id_for_path(root_path)
            name = str(raw_entry.get("name") or root_path.name or str(root_path)).strip()
            entry: Dict[str, Any] = {
                "id": workspace_id,
                "name": name,
                "kind": "custom",
                "root": str(root_path),
                "storage": str(root_path / ".smartshell"),
            }
            if raw_entry.get("current_dir"):
                entry["current_dir"] = str(
                    self._resolve_path_lenient(Path(str(raw_entry.get("current_dir"))))
                )
            workspaces[workspace_id] = entry

        active = str(raw_state.get("active") or DEFAULT_WORKSPACE_ID)
        if active not in workspaces:
            active = DEFAULT_WORKSPACE_ID
        return {"version": 1, "active": active, "workspaces": workspaces}

    def _save_workspace_state(self) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self.workspace_registry_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._workspaces_state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self.workspace_registry_path)
        except Exception as e:
            print(f"⚠️ 保存 workspace registry 失败: {e}")

    def _ensure_workspace_dirs(self) -> None:
        try:
            self.ai_workspace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⚠️ 无法创建 AI workspace 目录 {self.ai_workspace_dir}: {e}")
        self.ai_workspace_temp_dir = self.ai_workspace_dir / "temp"
        try:
            self.ai_workspace_temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⚠️ 无法创建 workspace temp 目录 {self.ai_workspace_temp_dir}: {e}")

    def _apply_workspace_entry(self, entry: Dict[str, Any], fallback_dir: Path) -> None:
        workspace_id = str(entry.get("id") or DEFAULT_WORKSPACE_ID)
        if workspace_id == DEFAULT_WORKSPACE_ID:
            entry.update(self._default_workspace_entry())
        root = self._workspace_root_path(entry)
        storage = self._workspace_storage_path(entry)
        self.workspace_id = workspace_id
        self.workspace_name = str(entry.get("name") or (DEFAULT_WORKSPACE_NAME if workspace_id == DEFAULT_WORKSPACE_ID else root.name)).strip()
        self.workspace_kind = str(entry.get("kind") or ("default" if workspace_id == DEFAULT_WORKSPACE_ID else "custom")).lower()
        self.workspace_root = root
        self.ai_workspace_dir = storage
        self._ensure_workspace_dirs()

        current_dir = self._workspace_current_dir_path(entry)
        if current_dir is not None and current_dir.exists() and current_dir.is_dir():
            self.work_directory = current_dir
        elif self.workspace_kind != "default" and root.exists() and root.is_dir():
            self.work_directory = root
        else:
            self.work_directory = self._resolve_path_lenient(fallback_dir)

        self._workspaces_state["active"] = self.workspace_id
        workspaces = self._workspaces_state.setdefault("workspaces", {})
        if isinstance(workspaces, dict):
            workspaces[self.workspace_id] = {
                "id": self.workspace_id,
                "name": self.workspace_name,
                "kind": self.workspace_kind,
                "root": str(self.workspace_root),
                "storage": str(self.ai_workspace_dir),
                **({"current_dir": str(self.work_directory)} if entry.get("current_dir") else {}),
            }

    def _save_current_workspace_position(self) -> None:
        self._sync_active_chat_messages()
        if not hasattr(self, "_workspaces_state"):
            return
        workspaces = self._workspaces_state.setdefault("workspaces", {})
        if not isinstance(workspaces, dict):
            return
        entry = workspaces.get(getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID))
        if not isinstance(entry, dict):
            workspace_id = getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID)
            if workspace_id == DEFAULT_WORKSPACE_ID:
                entry = self._default_workspace_entry()
            else:
                entry = {
                    "id": workspace_id,
                    "name": getattr(self, "workspace_name", str(workspace_id)),
                    "kind": getattr(self, "workspace_kind", "custom"),
                    "root": str(getattr(self, "workspace_root", self.work_directory)),
                    "storage": str(getattr(self, "ai_workspace_dir", self.work_directory / ".smartshell")),
                }
            workspaces[workspace_id] = entry
        entry["current_dir"] = str(self._resolve_path_lenient(self.work_directory))
        self._workspaces_state["active"] = getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID)
        self._save_workspace_state()

    def _workspace_path_from_arg(self, raw: str) -> Path:
        text = str(raw or "").strip().strip('"').strip("'")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self.work_directory / path
        return self._resolve_path_lenient(path)

    def _workspace_entry_by_root(self, root: Path, ignore_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        key = self._path_identity_key(root)
        workspaces = self._workspaces_state.get("workspaces", {})
        if not isinstance(workspaces, dict):
            return None
        for workspace_id, entry in workspaces.items():
            if ignore_id and workspace_id == ignore_id:
                continue
            if isinstance(entry, dict) and self._path_identity_key(self._workspace_root_path(entry)) == key:
                return entry
        return None

    def _workspace_name_exists(self, name: str, ignore_id: Optional[str] = None) -> bool:
        wanted = str(name or "").strip().casefold()
        workspaces = self._workspaces_state.get("workspaces", {})
        if not wanted or not isinstance(workspaces, dict):
            return False
        for workspace_id, entry in workspaces.items():
            if ignore_id and workspace_id == ignore_id:
                continue
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == wanted:
                return True
        return False

    def _workspace_entry_by_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        text = str(selector or "").strip().strip('"').strip("'")
        if not text:
            return None
        workspaces = self._workspaces_state.get("workspaces", {})
        if not isinstance(workspaces, dict):
            return None
        if text in workspaces and isinstance(workspaces[text], dict):
            return workspaces[text]
        folded = text.casefold()
        for entry in workspaces.values():
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == folded:
                return entry
        try:
            root = self._workspace_path_from_arg(text)
            return self._workspace_entry_by_root(root)
        except Exception:
            return None

    def _split_workspace_args(self, text: str) -> Tuple[List[str], Optional[str]]:
        try:
            parts = shlex.split(text or "", posix=False)
        except ValueError as e:
            return [], f"参数解析失败: {e}"
        return [p.strip().strip('"').strip("'") for p in parts if p.strip()], None

    def _parse_workspace_command_args(
        self,
        text: str,
        value_flags: Set[str],
        bool_flags: Set[str],
    ) -> Tuple[List[str], Dict[str, Any], Optional[str]]:
        parts, err = self._split_workspace_args(text)
        if err:
            return [], {}, err
        positionals: List[str] = []
        options: Dict[str, Any] = {}
        i = 0
        while i < len(parts):
            token = parts[i]
            matched_value_flag = None
            for flag in value_flags:
                if token == flag or token.startswith(f"{flag}="):
                    matched_value_flag = flag
                    break
            if matched_value_flag:
                key = matched_value_flag[2:].replace("-", "_")
                if token.startswith(f"{matched_value_flag}="):
                    value = token.split("=", 1)[1].strip()
                else:
                    i += 1
                    if i >= len(parts):
                        return [], {}, f"{matched_value_flag} 需要一个值"
                    value = parts[i]
                options[key] = value
            elif token in bool_flags:
                options[token[2:].replace("-", "_")] = True
            elif token.startswith("--"):
                return [], {}, f"未知参数: {token}"
            else:
                positionals.append(token)
            i += 1
        return positionals, options, None

    def _workspace_usage(self) -> str:
        return (
            "用法:\n"
            "  /workspace list\n"
            "  /workspace current\n"
            "  /workspace create <path> [--name <name>]\n"
            "  /workspace switch <name|id|path>\n"
            "  /workspace update <name|id|path> [--name <name>] [--path <path>]\n"
            "  /workspace rename <name|id|path> <new name>\n"
            "  /workspace delete <name|id|path> [--remove-files]\n"
            "    --remove-files: 删除该自定义 workspace 根目录下的 .smartshell/，"
            "包括 history、temp、skills、knowledge、knowledge_db 等 Smart Shell 数据；"
            "不会删除 workspace 根目录或其它项目文件。"
        )

    def _workspace_subcommand_usage(self, subcommand: str) -> str:
        usages = {
            "help": "/workspace help",
            "current": "/workspace current",
            "list": "/workspace list",
            "create": "/workspace create <path> [--name <name>]",
            "switch": "/workspace switch <name|id|path>",
            "update": "/workspace update <name|id|path> [--name <name>] [--path <path>]",
            "rename": "/workspace rename <name|id|path> <new name>",
            "delete": "/workspace delete <name|id|path> [--remove-files]",
        }
        usage = usages.get(str(subcommand or "").strip().lower())
        if usage:
            detail = ""
            if str(subcommand or "").strip().lower() == "delete":
                detail = (
                    "\n说明: --remove-files 会删除该自定义 workspace 根目录下的 .smartshell/ "
                    "及其所有文件和子目录；不会删除 workspace 根目录或其它项目文件。"
                )
            return f"用法: {usage}{detail}"
        return self._workspace_usage()

    def _handle_workspace_builtin_command(self, builtin_line: str) -> bool:
        raw = (builtin_line or "").strip()
        if not raw.lower().startswith("workspace"):
            return False
        parts, err = self._split_workspace_args(raw)
        if err:
            print(f"❌ {err}\n{self._workspace_usage()}")
            return True
        if not parts or parts[0].lower() != "workspace":
            return False
        if len(parts) == 1:
            self._print_workspace_help()
            return True

        sub = parts[1].lower()
        match = re.match(r"(?is)^workspace\s+\S+(?:\s+(.*))?$", raw)
        arg_text = (match.group(1) if match else "") or ""

        if sub == "help":
            if arg_text.strip():
                print(f"❌ {self._workspace_subcommand_usage('help')}")
            else:
                self._print_workspace_help()
            return True
        if sub == "current":
            if arg_text.strip():
                print(f"❌ {self._workspace_subcommand_usage('current')}")
            else:
                self._print_workspace_current()
            return True
        if sub == "list":
            if arg_text.strip():
                print(f"❌ {self._workspace_subcommand_usage('list')}")
            else:
                self._print_workspace_list()
            return True
        if sub == "create":
            print(self._workspace_create_command(arg_text.strip()))
            return True
        if sub == "switch":
            if not arg_text.strip():
                print(f"❌ {self._workspace_subcommand_usage('switch')}")
            else:
                print(self._workspace_switch_command(arg_text.strip()))
            return True
        if sub == "update":
            print(self._workspace_update_command(arg_text.strip()))
            return True
        if sub == "rename":
            print(self._workspace_rename_command(arg_text.strip()))
            return True
        if sub == "delete":
            print(self._workspace_delete_command(arg_text.strip()))
            return True

        print(f"❌ 无效 workspace 子命令: {parts[1]}\n{self._workspace_usage()}")
        return True

    def _print_workspace_help(self) -> None:
        print(self._workspace_usage())
        print("说明:")
        print("  - 默认 workspace 固定名为 Default，数据目录仍为 config.json 同级的 workspace/")
        print("  - 自定义 workspace 的 Smart Shell 数据保存在该目录下的 .smartshell/")
        print("  - /workspace delete 默认只移除登记；带 --remove-files 时会删除该自定义 workspace 的 .smartshell/ 及其全部内容，不会删除 workspace 根目录或其它项目文件。")
        print("  - 路径或名称包含空格时请使用引号")

    def _print_workspace_current(self) -> None:
        print(f"当前 workspace: {self.workspace_name} ({self.workspace_id})")
        print(f"  root: {self.workspace_root}")
        print(f"  storage: {self.ai_workspace_dir}")
        print(f"  current directory: {self.work_directory}")

    def _print_workspace_list(self) -> None:
        workspaces = self._workspaces_state.get("workspaces", {})
        if not isinstance(workspaces, dict):
            print("未找到 workspace 配置")
            return
        print("Workspaces:")
        ordered = sorted(
            workspaces.values(),
            key=lambda e: (0 if isinstance(e, dict) and e.get("id") == DEFAULT_WORKSPACE_ID else 1, str(e.get("name") if isinstance(e, dict) else "")),
        )
        for entry in ordered:
            if not isinstance(entry, dict):
                continue
            marker = "*" if str(entry.get("id")) == getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID) else " "
            print(f"{marker} {entry.get('name')} ({entry.get('id')})")
            print(f"    root: {self._workspace_root_path(entry)}")
            print(f"    storage: {self._workspace_storage_path(entry)}")
            if entry.get("current_dir"):
                print(f"    current: {entry.get('current_dir')}")

    def _workspace_create_command(self, arg_text: str) -> str:
        positionals, options, err = self._parse_workspace_command_args(arg_text, {"--name"}, set())
        if err:
            return f"❌ {err}\n{self._workspace_subcommand_usage('create')}"
        if len(positionals) != 1:
            return f"用法: /workspace create <path> [--name <name>]"
        root = self._workspace_path_from_arg(positionals[0])
        name = str(options.get("name") or root.name or str(root)).strip()
        if not name:
            return "❌ workspace 名称不能为空"
        if self._workspace_name_exists(name):
            return f"❌ workspace 名称已存在: {name}"
        existing = self._workspace_entry_by_root(root)
        if existing:
            return f"❌ 该目录已经是 workspace: {existing.get('name')} ({existing.get('id')})"
        try:
            root.mkdir(parents=True, exist_ok=True)
            storage = root / ".smartshell"
            storage.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"❌ 创建 workspace 目录失败: {e}"
        workspace_id = self._workspace_id_for_path(root)
        base_id = workspace_id
        counter = 2
        workspaces = self._workspaces_state.setdefault("workspaces", {})
        while workspace_id in workspaces:
            workspace_id = f"{base_id}_{counter}"
            counter += 1
        workspaces[workspace_id] = {
            "id": workspace_id,
            "name": name,
            "kind": "custom",
            "root": str(root),
            "storage": str(storage),
            "current_dir": str(root),
        }
        self._save_workspace_state()
        self._refresh_input_handler_skill_completions()
        return f"✅ 已创建 workspace: {name} ({workspace_id})\n  root: {root}\n  storage: {storage}"

    def _workspace_switch_command(self, selector: str) -> str:
        entry = self._workspace_entry_by_selector(selector)
        if not entry:
            return f"❌ 未找到 workspace: {selector}"
        if str(entry.get("id")) == getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID):
            return f"ℹ️ 已经在 workspace: {self.workspace_name}"
        self._save_current_workspace_position()
        self._apply_workspace_entry(entry, self.work_directory)
        # Do NOT save chat state here: _apply_workspace_entry already points
        # ai_workspace_dir to target workspace, while conversation/chat state is
        # still from previous workspace before _refresh_workspace_runtime().
        # Saving now would copy old chats into the new workspace.
        self._refresh_workspace_runtime()
        return f"✅ 已切换到 workspace: {self.workspace_name}\n  current directory: {self.work_directory}"

    def _workspace_update_command(self, arg_text: str) -> str:
        positionals, options, err = self._parse_workspace_command_args(arg_text, {"--name", "--path"}, set())
        if err:
            return f"❌ {err}\n{self._workspace_subcommand_usage('update')}"
        if len(positionals) != 1 or not options:
            return "用法: /workspace update <name|id|path> [--name <name>] [--path <path>]"
        entry = self._workspace_entry_by_selector(positionals[0])
        if not entry:
            return f"❌ 未找到 workspace: {positionals[0]}"
        workspace_id = str(entry.get("id") or "")
        if workspace_id == DEFAULT_WORKSPACE_ID:
            return "❌ 默认 workspace 的名称和目录固定，不能修改"
        active_workspace = workspace_id == getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID)
        if active_workspace:
            self._save_current_workspace_position()

        old_root = self._workspace_root_path(entry)
        old_storage = self._workspace_storage_path(entry)
        messages: List[str] = []
        if "name" in options:
            new_name = str(options.get("name") or "").strip()
            if not new_name:
                return "❌ workspace 名称不能为空"
            if self._workspace_name_exists(new_name, ignore_id=workspace_id):
                return f"❌ workspace 名称已存在: {new_name}"
            entry["name"] = new_name
            messages.append(f"name={new_name}")

        if "path" in options:
            new_root = self._workspace_path_from_arg(str(options.get("path") or ""))
            duplicate = self._workspace_entry_by_root(new_root, ignore_id=workspace_id)
            if duplicate:
                return f"❌ 目标目录已经是 workspace: {duplicate.get('name')} ({duplicate.get('id')})"
            new_storage = new_root / ".smartshell"
            if active_workspace:
                self._shutdown_mcp_runtime()
                self._shutdown_workspace_services(wait=True)
            try:
                new_root.mkdir(parents=True, exist_ok=True)
                if old_storage.exists() and self._path_identity_key(old_storage) != self._path_identity_key(new_storage) and not new_storage.exists():
                    new_storage.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_storage), str(new_storage))
                    messages.append("storage=moved")
                else:
                    new_storage.mkdir(parents=True, exist_ok=True)
                    if old_storage.exists() and self._path_identity_key(old_storage) != self._path_identity_key(new_storage):
                        messages.append("storage=kept-existing-new-location")
            except Exception as e:
                return f"❌ 修改 workspace 目录失败: {e}"
            current_dir = self._workspace_current_dir_path(entry)
            try:
                rel = current_dir.relative_to(old_root) if current_dir is not None else None
            except Exception:
                rel = None
            if rel is not None:
                candidate = new_root / rel
                entry["current_dir"] = str(candidate if candidate.exists() else new_root)
            else:
                entry["current_dir"] = str(new_root)
            entry["root"] = str(new_root)
            entry["storage"] = str(new_storage)
            messages.append(f"path={new_root}")

        self._save_workspace_state()
        self._refresh_input_handler_skill_completions()
        if active_workspace:
            self._apply_workspace_entry(entry, self.work_directory)
            self._refresh_workspace_runtime()
        return f"✅ 已修改 workspace: {entry.get('name')} ({workspace_id})\n  " + ", ".join(messages)

    def _workspace_rename_command(self, arg_text: str) -> str:
        positionals, options, err = self._parse_workspace_command_args(arg_text, set(), set())
        if err:
            return f"❌ {err}\n{self._workspace_subcommand_usage('rename')}"
        if len(positionals) < 2:
            return "用法: /workspace rename <name|id|path> <new name>"
        selector = positionals[0]
        new_name = " ".join(positionals[1:]).strip()
        return self._workspace_update_command(f'"{selector}" --name "{new_name}"')

    def _workspace_delete_command(self, arg_text: str) -> str:
        positionals, options, err = self._parse_workspace_command_args(arg_text, set(), {"--remove-files"})
        if err:
            return f"❌ {err}\n{self._workspace_subcommand_usage('delete')}"
        if len(positionals) != 1:
            return f"❌ {self._workspace_subcommand_usage('delete')}"
        entry = self._workspace_entry_by_selector(positionals[0])
        if not entry:
            return f"❌ 未找到 workspace: {positionals[0]}"
        workspace_id = str(entry.get("id") or "")
        if workspace_id == DEFAULT_WORKSPACE_ID:
            return "❌ 默认 workspace 不能删除"

        storage = self._workspace_storage_path(entry)
        remove_files = bool(options.get("remove_files"))
        if remove_files and storage.exists():
            confirm = input(f"确认删除 workspace 数据目录 '{storage}'？只会删除 .smartshell，不会删除 workspace 根目录。(y/n): ").strip().lower()
            if confirm != "y":
                return "已取消删除 workspace 数据目录"

        active_deleted = workspace_id == getattr(self, "workspace_id", DEFAULT_WORKSPACE_ID)
        if active_deleted:
            self._save_current_workspace_position()
        workspaces = self._workspaces_state.get("workspaces", {})
        if isinstance(workspaces, dict):
            workspaces.pop(workspace_id, None)
        if active_deleted:
            default_entry = (
                workspaces.get(DEFAULT_WORKSPACE_ID)
                if isinstance(workspaces, dict) and isinstance(workspaces.get(DEFAULT_WORKSPACE_ID), dict)
                else self._default_workspace_entry()
            )
            if isinstance(workspaces, dict):
                workspaces[DEFAULT_WORKSPACE_ID] = default_entry
            self._apply_workspace_entry(default_entry, self.work_directory)
            self._save_current_workspace_position()
            self._refresh_workspace_runtime()
        else:
            self._save_workspace_state()
        self._refresh_input_handler_skill_completions()

        removed_data = False
        if remove_files and storage.exists():
            try:
                shutil.rmtree(storage)
                removed_data = True
            except OSError as e:
                return f"⚠️ workspace 已从列表删除，但删除数据目录失败: {e}"
        suffix = f"\n  已删除数据目录: {storage}" if removed_data else ""
        return f"✅ 已删除 workspace: {entry.get('name')} ({workspace_id}){suffix}"

    def _chat_state_path(self) -> Path:
        return self.ai_workspace_dir / CHAT_STATE_FILE

    def _new_chat_entry(self, chat_id: str, name: str = "New Chat") -> Dict[str, Any]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id": chat_id,
            "name": name,
            "name_source": "default",
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    def _sanitize_persisted_chat_message(self, role: str, content: str) -> Optional[str]:
        r = str(role or "").strip().lower()
        c = str(content or "")
        if r != "user":
            return c
        marker = "\n\n【首轮回复硬性要求（必须遵守）】"
        if marker in c:
            c = c.split(marker, 1)[0]
        if c.startswith("【用户原始需求】\n"):
            return None
        c = c.strip()
        if not c:
            return None
        return c

    def _compact_redundant_user_turns(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Collapse repeated identical user turns caused by multi-round internal tool orchestration.
        Heuristic: if same user content reappears and all assistant messages in-between look like
        tool-planning/tool-json messages, keep only the first user turn.
        """
        compact: List[Dict[str, str]] = []
        last_user_content: Optional[str] = None
        assistant_since_last_user: List[str] = []
        for m in messages:
            role = str(m.get("role") or "").strip().lower()
            content = str(m.get("content") or "")
            if role == "assistant":
                assistant_since_last_user.append(content)
                compact.append(m)
                continue
            if role != "user":
                compact.append(m)
                continue
            same_user = last_user_content is not None and content == last_user_content
            if same_user and assistant_since_last_user:
                looks_internal = True
                for a in assistant_since_last_user:
                    s = str(a or "")
                    if ("```json" not in s) and ('{"tool"' not in s) and ("Step " not in s):
                        looks_internal = False
                        break
                if looks_internal:
                    assistant_since_last_user = []
                    continue
            compact.append(m)
            last_user_content = content
            assistant_since_last_user = []
        return compact

    def _default_chat_state(self) -> Dict[str, Any]:
        default_chat = self._new_chat_entry("chat-1")
        return {"version": 1, "active": "chat-1", "chats": [default_chat]}

    def _save_chat_state(self) -> None:
        try:
            p = self._chat_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._chat_state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存 chat 状态失败: {e}")

    def _chat_entries(self) -> List[Dict[str, Any]]:
        chats = self._chat_state.get("chats", [])
        if not isinstance(chats, list):
            chats = []
            self._chat_state["chats"] = chats
        return chats

    def _find_chat_by_id(self, chat_id: str) -> Optional[Dict[str, Any]]:
        for c in self._chat_entries():
            if not isinstance(c, dict):
                continue
            if str(c.get("id") or "") == chat_id:
                return c
        return None

    def _resolve_chat_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        text = str(selector or "").strip()
        if not text:
            return None
        chats = self._chat_entries()
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(chats):
                return chats[idx - 1]
        low = text.casefold()
        for c in chats:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "")
            name = str(c.get("name") or "")
            if text == cid or low == name.casefold():
                return c
        return None

    def _next_chat_id(self) -> str:
        existing = {str(c.get("id") or "") for c in self._chat_entries() if isinstance(c, dict)}
        i = 1
        while True:
            cid = f"chat-{i}"
            if cid not in existing:
                return cid
            i += 1

    def _load_chat_state(self) -> None:
        raw: Dict[str, Any] = {}
        p = self._chat_state_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw = loaded
            except Exception as e:
                print(f"⚠️ 读取 chat 状态失败，使用默认会话: {e}")
        chats_raw = raw.get("chats", [])
        chats: List[Dict[str, Any]] = []
        if isinstance(chats_raw, list):
            for c in chats_raw:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "").strip()
                if not cid:
                    cid = self._next_chat_id()
                name = str(c.get("name") or "New Chat").strip() or "New Chat"
                source = str(c.get("name_source") or "default").strip().lower()
                if source not in ("default", "auto", "manual"):
                    source = "default"
                messages = c.get("messages", [])
                if not isinstance(messages, list):
                    messages = []
                msgs = []
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    role = str(m.get("role") or "").strip().lower()
                    content = str(m.get("content") or "")
                    if role in ("user", "assistant"):
                        clean = self._sanitize_persisted_chat_message(role, content)
                        if clean is None:
                            continue
                        msgs.append({"role": role, "content": clean})
                chats.append(
                    {
                        "id": cid,
                        "name": name,
                        "name_source": source,
                        "created_at": str(c.get("created_at") or ""),
                        "updated_at": str(c.get("updated_at") or ""),
                        "messages": self._compact_redundant_user_turns(msgs),
                    }
                )
        if not chats:
            self._chat_state = self._default_chat_state()
            chats = self._chat_entries()
        active = str(raw.get("active") or self._chat_state.get("active") or "").strip()
        if not active or not any(str(c.get("id")) == active for c in chats):
            active = str(chats[0].get("id") or "chat-1")
        self._chat_state = {"version": 1, "active": active, "chats": chats}
        self._save_chat_state()
        self._activate_chat(active, announce=False, clear_screen=False, print_history=False)

    def _sync_active_chat_messages(self) -> None:
        with self._chat_state_lock:
            chat = self._find_chat_by_id(self.active_chat_id)
            if not chat:
                return
            chat["messages"] = list(self.conversation_history)
            chat["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_chat_state()

    def _activate_chat(
        self,
        chat_id: str,
        announce: bool = True,
        clear_screen: bool = False,
        print_history: bool = False,
    ) -> str:
        with self._chat_state_lock:
            chat = self._find_chat_by_id(chat_id)
            if not chat:
                return f"❌ 未找到 chat: {chat_id}"
            self._chat_state["active"] = chat_id
            self.active_chat_id = chat_id
            self.active_chat_name = str(chat.get("name") or "New Chat")
            self.conversation_history = list(chat.get("messages") or [])
            self.operation_results = []
            self._session_summary_llm = ""
            self._session_summary_rolling = ""
            self._last_llm_summary_pair_count = 0
            self._save_chat_state()
        if clear_screen:
            os.system("cls" if os.name == "nt" else "clear")
        if print_history:
            self._print_chat_history()
        if announce:
            return f"✅ 已切换到 Chat: [{self.active_chat_name}]"
        return ""

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
        Best-effort: clear workspace+prompt lines, then rewrite as gray '你:' line.
        """
        txt = str(user_text or "")
        if not txt:
            return
        if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
            return
        try:
            # Current cursor is on line after Enter:
            #   [Workspace: ...]
            #   <cwd>...
            #   <cursor here>
            # Move up twice and clear both prompt lines.
            sys.stdout.write("\x1b[1A\r\x1b[2K")
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

    def _chat_usage(self) -> str:
        return (
            "用法:\n"
            "  /chat list\n"
            "  /chat current\n"
            "  /chat new [name]\n"
            "  /chat switch <index|id|name>\n"
            "  /chat rename <index|id|name> <new name>\n"
            "  /chat delete <index|id|name>\n"
            "  /chat delete all\n"
        )

    def _print_chat_list(self) -> None:
        chats = self._chat_entries()
        if not chats:
            print("当前 workspace 下没有 chat")
            return
        print(f"Chats (workspace={self.workspace_name}):")
        for i, c in enumerate(chats, start=1):
            marker = "*" if str(c.get("id") or "") == self.active_chat_id else " "
            name = str(c.get("name") or "New Chat")
            cnt = len(c.get("messages") or [])
            print(f"{marker} [{i}] {name} - {cnt} msgs")

    def _handle_chat_builtin_command(self, builtin_line: str) -> bool:
        raw = str(builtin_line or "").strip()
        if not raw.lower().startswith("chat"):
            return False
        parts = shlex.split(raw)
        if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
            print(self._chat_usage())
            return True
        sub = parts[1].lower()
        if sub == "list":
            self._print_chat_list()
            return True
        if sub == "current":
            print(f"当前 Chat: [{self.active_chat_name}] ({self.active_chat_id})")
            return True
        if sub == "new":
            name = " ".join(parts[2:]).strip() if len(parts) > 2 else "New Chat"
            with self._chat_state_lock:
                cid = self._next_chat_id()
                self._chat_entries().append(self._new_chat_entry(cid, name=name))
                self._save_chat_state()
            self._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
            print(f"✅ 已创建并切换到 Chat: [{self.active_chat_name}] ({self.active_chat_id})")
            return True
        if sub == "switch":
            if len(parts) < 3:
                print("❌ 用法: /chat switch <index|id|name>")
                return True
            selector = " ".join(parts[2:]).strip()
            with self._chat_state_lock:
                target = self._resolve_chat_selector(selector)
                if not target:
                    print(f"❌ 未找到 chat: {selector}")
                    return True
                cid = str(target.get("id") or "")
            print(self._activate_chat(cid, announce=True, clear_screen=False, print_history=True))
            return True
        if sub == "rename":
            if len(parts) < 4:
                print("❌ 用法: /chat rename <index|id|name> <new name>")
                return True
            selector = parts[2]
            new_name = " ".join(parts[3:]).strip()
            if not new_name:
                print("❌ Chat 名称不能为空")
                return True
            with self._chat_state_lock:
                target = self._resolve_chat_selector(selector)
                if not target:
                    print(f"❌ 未找到 chat: {selector}")
                    return True
                target["name"] = new_name
                target["name_source"] = "manual"
                target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if str(target.get("id") or "") == self.active_chat_id:
                    self.active_chat_name = new_name
                self._save_chat_state()
            print(f"✅ 已重命名 Chat: {new_name}")
            return True
        if sub == "delete":
            if len(parts) < 3:
                print("❌ 用法: /chat delete <index|id|name>")
                return True
            selector = " ".join(parts[2:]).strip()
            if selector.lower() == "all":
                with self._chat_state_lock:
                    cid = self._next_chat_id()
                    self._chat_state["chats"] = [self._new_chat_entry(cid, name="New Chat")]
                    self._chat_state["active"] = cid
                    self._save_chat_state()
                self._activate_chat(cid, announce=False, clear_screen=False, print_history=True)
                print("✅ 已删除所有 Chat，并自动创建新的 Chat: [New Chat]")
                return True
            with self._chat_state_lock:
                target = self._resolve_chat_selector(selector)
                if not target:
                    print(f"❌ 未找到 chat: {selector}")
                    return True
                chats = self._chat_entries()
                if len(chats) <= 1:
                    print("❌ 至少保留一个 chat，不能删除最后一个")
                    return True
                tid = str(target.get("id") or "")
                chats[:] = [c for c in chats if str(c.get("id") or "") != tid]
                next_id = self.active_chat_id
                if tid == self.active_chat_id:
                    next_id = str(chats[0].get("id") or "")
                self._chat_state["chats"] = chats
                self._save_chat_state()
            print(f"✅ 已删除 Chat: {target.get('name')} ({target.get('id')})")
            if tid == self.active_chat_id and next_id:
                self._activate_chat(next_id, announce=False, clear_screen=False, print_history=True)
                print(f"✅ 已切换到 Chat: [{self.active_chat_name}]")
            return True
        print(f"❌ 未识别的 chat 子命令: {sub}\n{self._chat_usage()}")
        return True

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

    def _schedule_memory_service_background(self) -> None:
        """后台初始化经验记忆：在本线程内 import memory_manager，再构造 MemoryService（Markdown 后端，无重型依赖）。"""
        _mod = sys.modules[__name__]
        workspace_dir = str(self.ai_workspace_dir)
        generation = getattr(self, "_workspace_runtime_generation", 0)

        def _run() -> None:
            try:
                from . import memory_manager as _mm
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

    def _update_session_summary_rolling(self) -> None:
        return self.session_memory_service.update_session_summary_rolling()

    def _session_summary_for_retrieval(self) -> str:
        return self.session_memory_service.session_summary_for_retrieval()

    def _maybe_refresh_session_summary_llm(self) -> None:
        return self.session_memory_service.maybe_refresh_session_summary_llm()

    def _build_memory_retrieval_query(self, user_input: str) -> str:
        return self.session_memory_service.build_memory_retrieval_query(user_input)

    @staticmethod
    def _memory_row_sort_key(r: Dict[str, Any]) -> Tuple[int, float, int]:
        return SessionMemoryService.memory_row_sort_key(r)

    @staticmethod
    def _user_input_emphasizes_memory_or_identity(user_input: str) -> bool:
        return SessionMemoryService.user_input_emphasizes_memory_or_identity(user_input)

    def _memory_dialogue_excerpt_for_expansion(self) -> str:
        return self.session_memory_service.memory_dialogue_excerpt_for_expansion()

    def _memory_expansion_reference_block(self) -> str:
        return self.session_memory_service.memory_expansion_reference_block()

    @staticmethod
    def _parse_memory_expansion_json(text: str) -> Optional[Dict[str, Any]]:
        return SessionMemoryService.parse_memory_expansion_json(text)

    def _memory_expansion_keywords_query_string(self, expansion: Dict[str, Any]) -> str:
        return self.session_memory_service.memory_expansion_keywords_query_string(expansion)

    def _should_run_memory_query_expansion(
        self,
        rows_sem: List[Dict[str, Any]],
        rows_boost: List[Dict[str, Any]],
        identity_mode: bool,
    ) -> bool:
        return self.session_memory_service.should_run_memory_query_expansion(
            rows_sem, rows_boost, identity_mode
        )

    def _run_memory_expansion_llm(self, user_input: str) -> Optional[Dict[str, Any]]:
        return self.session_memory_service.run_memory_expansion_llm(user_input)

    def _memory_rows_for_prompt(self, user_input: str) -> List[Dict[str, Any]]:
        return self.session_memory_service.memory_rows_for_prompt(user_input)

    def _memory_context_for_prompt(self, user_input: str, max_chars: int = 2400) -> str:
        return self.session_memory_service.memory_context_for_prompt(user_input, max_chars)

    def _schedule_auto_memory_reflect(self) -> None:
        return self.session_memory_service.schedule_auto_memory_reflect()

    def _run_memory_reflection_body(self) -> None:
        return self.session_memory_service.run_memory_reflection_body()

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
                    from .knowledge_manager import KnowledgeService as _KS, KNOWLEDGE_AVAILABLE as _KAV
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

    def _build_mcp_system_append(self) -> str:
        """Build MCP section appended to system prompt (with redacted env values)."""
        servers = (self.mcp_config or {}).get("mcpServers", {})
        if not isinstance(servers, dict) or not servers:
            return "\n\n## MCP 配置\n未检测到可用 MCP server（config 目录下无 mcp.json 或配置为空）。"
        status_servers: Dict[str, Any] = {}
        try:
            status_servers = (self.mcp_manager.get_status().get("servers", {}) or {}) if self.mcp_manager else {}
        except Exception:
            status_servers = {}
        loaded: List[str] = []
        not_loaded: List[str] = []
        lines: List[str] = [
            "",
            "",
            "## MCP 配置",
            "已从 config 目录下的 mcp.json 加载 MCP servers。调用前请优先选择最匹配的 server。",
            "仅可引用“已加载”server 的工具能力；未加载 server 禁止在自然语言中当作可用能力引用。",
            "决策约束：当“已加载 + 已缓存 tools”中存在可覆盖用户意图的工具时，必须优先走 mcp_call_tool，"
            "不得先创建临时脚本或调用 shell 模拟实现（除非工具调用已明确失败且无等价 MCP 工具）。",
            "可用 servers（敏感 env 已脱敏，仅显示键名）：",
        ]
        for name, conf in servers.items():
            if not isinstance(conf, dict):
                lines.append(f"- {name}: 配置无效（应为 object）")
                continue
            st = status_servers.get(name, {})
            state_raw = str(st.get("state", "pending") or "pending").lower()
            state = "loaded" if state_raw == "success" else state_raw
            if state == "loaded":
                loaded.append(str(name))
            else:
                not_loaded.append(str(name))
            if "url" in conf:
                lines.append(f"- {name}: state={state}, type=remote, url={conf.get('url')}")
            else:
                cmd = str(conf.get("command", "")).strip() or "<missing>"
                args = conf.get("args", [])
                arg_preview = " ".join(str(x) for x in args[:3]) if isinstance(args, list) else ""
                if len(arg_preview) > 120:
                    arg_preview = arg_preview[:117] + "..."
                lines.append(f"- {name}: state={state}, type=stdio, command={cmd}, args={arg_preview}")
            env = conf.get("env")
            if isinstance(env, dict) and env:
                env_keys = ", ".join(str(k) for k in sorted(env.keys()))
                lines.append(f"  env_keys: {env_keys}")
        lines.append(f"已加载 servers: {', '.join(loaded) if loaded else '无'}")
        lines.append(f"未加载 servers: {', '.join(not_loaded) if not_loaded else '无'}")
        lines.append("已缓存 tools（调用 mcp_list_tools 后更新）：")
        try:
            lines.append(self.mcp_manager.cached_tools_for_prompt())
        except Exception:
            lines.append("尚无已缓存的 MCP tools。")
        lines.append("已缓存 resources（调用 mcp_list_resources 后更新）：")
        try:
            lines.append(self.mcp_manager.cached_resources_for_prompt())
        except Exception:
            lines.append("尚无已缓存的 MCP resources。")
        lines.append("已缓存 prompts（调用 mcp_list_prompts 后更新）：")
        try:
            lines.append(self.mcp_manager.cached_prompts_for_prompt())
        except Exception:
            lines.append("尚无已缓存的 MCP prompts。")
        return "\n".join(lines)

    @staticmethod
    def _strip_jsonc_comments(text: str) -> str:
        """Remove // and /* */ comments from JSONC while preserving string literals."""
        out: List[str] = []
        i = 0
        in_str = False
        esc = False
        n = len(text)
        while i < n:
            c = text[i]
            if in_str:
                out.append(c)
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
                continue
            if c == "/" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "/":
                    i += 2
                    while i < n and text[i] not in ("\n", "\r"):
                        i += 1
                    continue
                if nxt == "*":
                    i += 2
                    while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                        i += 1
                    i = i + 2 if i + 1 < n else n
                    continue
            out.append(c)
            i += 1
        return "".join(out)

    def _load_tools_spec_from_jsonc(self) -> List[Dict[str, Any]]:
        """Load tool specs from tools.jsonc with comment stripping."""
        path = Path(__file__).resolve().parent / "tools.jsonc"
        try:
            raw = path.read_text(encoding="utf-8")
            clean = self._strip_jsonc_comments(raw)
            parsed = json.loads(clean)
            if not isinstance(parsed, list):
                raise ValueError("tools.jsonc root must be array")
            return [x for x in parsed if isinstance(x, dict)]
        except Exception as e:
            print(f"⚠️ tools.jsonc 加载失败: {e}")
            return []

    def _build_user_preferences_system_append(self) -> str:
        """持久化用户偏好文件，固定注入 system（在 MCP/tools 之前）。"""
        try:
            from . import user_preferences_manager as _upm

            return _upm.build_system_append(Path(self.config_dir))
        except Exception:
            return ""

    def _build_agents_md_system_append(self) -> str:
        """Inject AGENTS.md content from config/workspace-related locations."""
        candidates: List[Tuple[str, Path]] = []
        try:
            candidates.append(("config", Path(self.config_dir) / "AGENTS.md"))
        except Exception:
            pass
        try:
            candidates.append(("workspace", Path(self.ai_workspace_dir) / "AGENTS.md"))
        except Exception:
            pass
        try:
            candidates.append(("workspace/.smartshell", Path(self.ai_workspace_dir) / ".smartshell" / "AGENTS.md"))
        except Exception:
            pass

        sections: List[str] = []
        seen_keys: set = set()
        for scope, file_path in candidates:
            try:
                resolved = file_path.expanduser().resolve()
            except Exception:
                resolved = file_path
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if not resolved.is_file():
                continue
            try:
                content = resolved.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if not content:
                continue
            sections.append(
                "\n".join(
                    [
                        f"### {scope} AGENTS.md",
                        f"Source: `{resolved}`",
                        content,
                    ]
                )
            )

        if not sections:
            return ""
        header = (
            "\n\n## User Custom Prompts (AGENTS.md)\n\n"
            "优先级说明：本节用于注入用户自定义提示；当用户在当前请求中**显式指定 skill**"
            "（例如 `/skill-id` 或已触发 `request_skill_prompt`）且与本节冲突时，"
            "以显式指定的 skill 正文为准。\n\n"
        )
        return header + "\n\n".join(sections) + "\n"

    def _compose_system_prompt_snapshot(self, include_tools: bool) -> str:
        """组装当前可见 system 快照：base + 用户偏好 + MCP [+ 工具目录]。"""
        core = (
            self._base_system_prompt
            + self._build_agents_md_system_append()
            + self._build_user_preferences_system_append()
            + self._build_mcp_system_append()
            + self._build_runtime_cache_prompt_append()
        )
        if include_tools:
            return core + "\n" + self._build_tools_prompt_append()
        return core

    def _build_runtime_cache_prompt_append(self) -> str:
        """Provide generic runtime cache-dir hints for all skills/scripts."""
        ws_root = Path(getattr(self, "workspace_root", self.work_directory))
        ws_id = str(getattr(self, "workspace_id", "") or "").strip().lower()
        if ws_id == DEFAULT_WORKSPACE_ID:
            cache_root = (ws_root / ".cache").resolve()
        else:
            cache_root = (ws_root / ".smartshell" / ".cache").resolve()
        return (
            "\n\n## Runtime Cache Directory Hint\n"
            "- 通用缓存根目录（workspace 级）: "
            f"`{cache_root}`\n"
            "- 若某个脚本支持 `--cache-dir` 参数或其它传递 cache 路径的的参数，则传入此目录。"
            "- 若脚本未声明或不支持 cache 参数，不要强行传参。"
        )

    def _build_tools_prompt_append(self) -> str:
        """Build tool catalog text injected into system prompt from external md template."""
        lines: List[str] = [self.tools_prompt_template.strip(), "", "Available tools:"]
        lines.insert(
            1,
            "当且仅当当前会话尚未注入目标 skill 正文时，先输出："
            "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\"}}；"
            "若该 skill 已注入（例如通过 `/skill-id` 显式启用），默认禁止重复调用 request_skill_prompt，直接继续业务步骤；"
            "但当技能正文为分段注入时，可按需调用 "
            "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\",\"section\":<n>}} "
            "加载第 n 段，或用 "
            "{\"tool\":\"request_skill_prompt\",\"args\":{\"skill_id\":\"<skill_id>\",\"full\":true}} "
            "加载完整正文。",
        )
        for t in (self.tool_specs or []):
            fn = (t or {}).get("function", {})
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            if name == "project_context_search" and not self._project_context_tool_allowed():
                continue
            desc = str(fn.get("description") or "").strip()
            params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
            props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
            arg_keys = ", ".join(sorted(str(k) for k in props.keys())) if props else "-"
            lines.append(f"- {name}: {desc} | args: {arg_keys}")
        return "\n".join(lines)

    def _load_tools_prompt_template(self) -> str:
        """Load tools-related prompt template from external markdown file."""
        path = Path(__file__).resolve().parent / "tools_prompt.md"
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"⚠️ tools_prompt.md 加载失败: {e}")
            return "## Tool Catalog (prompt-injected)"

    def _build_local_skill_context_pack(self, target: Any) -> str:
        """
        Build a compact, structured context pack for one local skill bundle.
        This keeps the model focused on high-signal files before reading long body text.
        """
        try:
            bundle_root = Path(str(getattr(target, "bundle_root", "") or "")).resolve()
        except Exception:
            bundle_root = Path(str(getattr(target, "bundle_root", "") or ""))
        skill_md = bundle_root / "SKILL.md"
        scripts = _list_bundled_script_paths(str(bundle_root), max_files=12)
        refs: List[str] = []
        try:
            refs_dir = bundle_root / "references"
            if refs_dir.is_dir():
                refs = [str(p.resolve()) for p in sorted(refs_dir.glob("*.md"), key=lambda p: p.name.lower())[:8]]
        except Exception:
            refs = []

        body = str(getattr(target, "body", "") or "")
        headings: List[str] = []
        for line in body.splitlines():
            s = str(line).strip()
            if s.startswith("#"):
                headings.append(s)
                if len(headings) >= 10:
                    break

        lines: List[str] = [
            "#### Skill Context Pack (compact)",
            f"- skill_id: `{getattr(target, 'skill_id', '')}`",
            f"- bundle_root: `{bundle_root}`",
            f"- skill_md: `{skill_md}`",
            f"- scripts_count: {len(scripts)}",
            f"- references_count: {len(refs)}",
        ]
        if scripts:
            lines.append("- scripts (absolute paths):")
            for p in scripts:
                lines.append(f"  - `{p}`")
        if refs:
            lines.append("- references (absolute paths):")
            for p in refs:
                lines.append(f"  - `{p}`")
        if headings:
            lines.append("- key headings:")
            for h in headings:
                lines.append(f"  - {h}")
        lines.append("- usage_hint: 优先基于上述路径做定点读取/执行，避免无界搜索。")
        return "\n".join(lines)

    def _default_skill_cache_dir(self, skill_id: str) -> Path:
        sid = str(skill_id or "").strip().lower() or "skill"
        ws_root = Path(getattr(self, "workspace_root", self.work_directory))
        ws_id = str(getattr(self, "workspace_id", "") or "").strip().lower()
        if ws_id == DEFAULT_WORKSPACE_ID:
            base = ws_root / ".cache"
        else:
            base = ws_root / ".smartshell" / ".cache"
        return (base / sid).resolve()

    def _build_mcp_skill_context_pack(self, server: str, skill_id: str, rendered_parts: List[str]) -> str:
        """
        Build a compact context pack for MCP prompt-backed skills.
        """
        char_count = sum(len(str(p or "")) for p in (rendered_parts or []))
        lines = [
            "#### Skill Context Pack (compact)",
            f"- source: `mcp`",
            f"- server: `{server}`",
            f"- skill_id: `{skill_id}`",
            f"- prompt_messages: {len(rendered_parts or [])}",
            f"- rendered_chars: {char_count}",
            "- usage_hint: 先按消息顺序执行首个可落地步骤，再根据结果迭代。",
        ]
        return "\n".join(lines)

    def _split_skill_body_sections(self, text: str) -> List[str]:
        """
        Split long SKILL body into semantic sections using markdown headings first.
        Falls back to character windows when headings are not enough.
        """
        body = str(text or "").strip()
        if not body:
            return []
        lines = body.splitlines()
        blocks: List[str] = []
        cur: List[str] = []
        for ln in lines:
            s = str(ln).lstrip()
            if s.startswith("#") and cur:
                blocks.append("\n".join(cur).strip())
                cur = [ln]
            else:
                cur.append(ln)
        if cur:
            blocks.append("\n".join(cur).strip())
        blocks = [b for b in blocks if b.strip()]
        if len(blocks) <= 1:
            chunks: List[str] = []
            start = 0
            while start < len(body):
                end = min(len(body), start + SKILL_PROMPT_MAX_SECTION_CHARS)
                chunks.append(body[start:end].strip())
                start = end
            return [c for c in chunks if c]

        merged: List[str] = []
        acc = ""
        for b in blocks:
            if not acc:
                acc = b
                continue
            if len(acc) + 2 + len(b) <= SKILL_PROMPT_MAX_SECTION_CHARS:
                acc = f"{acc}\n\n{b}"
            else:
                merged.append(acc.strip())
                acc = b
        if acc.strip():
            merged.append(acc.strip())
        return [m for m in merged if m]

    def _render_skill_section_payload(
        self,
        sections: List[str],
        requested_section: Optional[int],
        full: bool,
    ) -> Tuple[str, Dict[str, Any]]:
        total = len(sections)
        if total <= 0:
            return "", {"chunked": False, "section": 0, "total": 0, "full": True}
        if full or total <= SKILL_PROMPT_INITIAL_SECTIONS:
            payload = "\n\n".join(sections)
            return payload, {"chunked": False, "section": 1, "total": total, "full": True}

        idx = int(requested_section or 1)
        idx = 1 if idx < 1 else idx
        idx = total if idx > total else idx
        payload = sections[idx - 1]
        hint_lines = [
            "",
            f"[Skill 分段注入] 当前仅注入第 {idx}/{total} 段，以控制 prompt 体积。",
        ]
        if idx < total:
            hint_lines.append(
                "如需下一段，请调用 "
                f'{{"tool":"request_skill_prompt","args":{{"skill_id":"...","section":{idx + 1}}}}}。'
            )
        hint_lines.append(
            '如需完整正文，可调用 {"tool":"request_skill_prompt","args":{"skill_id":"...","full":true}}。'
        )
        return payload + "\n" + "\n".join(hint_lines), {
            "chunked": True,
            "section": idx,
            "total": total,
            "full": False,
        }

    def _build_single_skill_prompt(
        self,
        skill_id: str,
        requested_section: Optional[int] = None,
        full: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Build full prompt appendix for one selected skill.

        Resolution order:
        1) Local loaded Agent Skills (`self.skills`)
        2) MCP prompts fallback
        """
        sid = (skill_id or "").strip().lower()
        if not sid:
            return None, {"chunked": False, "section": 0, "total": 0, "full": True}
        target = None
        for s in self.skills or []:
            if str(getattr(s, "skill_id", "")).strip().lower() == sid:
                target = s
                break
        if target is None:
            # Fallback: treat `skill/...` as MCP prompt id.
            sid_raw = (skill_id or "").strip()
            if not sid_raw:
                return None, {"chunked": False, "section": 0, "total": 0, "full": True}
            mcp = getattr(self, "mcp_manager", None)
            if mcp is None:
                return None, {"chunked": False, "section": 0, "total": 0, "full": True}
            server_candidates: List[str] = []
            cfg_servers = {}
            try:
                cfg_servers = (mcp.mcp_config or {}).get("mcpServers", {}) if isinstance(mcp.mcp_config, dict) else {}
            except Exception:
                cfg_servers = {}
            if isinstance(cfg_servers, dict):
                for name in cfg_servers.keys():
                    n = str(name).strip()
                    if not n:
                        continue
                    server_candidates.append(n)

            for server in server_candidates:
                srv = str(server).strip()
                if not srv:
                    continue
                try:
                    # Ensure prompt cache is refreshed at least once for this server.
                    mcp.list_prompts(srv, timeout_s=12.0, use_cache=False)
                    prompt_obj = mcp.get_prompt(srv, sid_raw, {}, timeout_s=25.0)
                    desc = str(prompt_obj.get("description", "") or "").strip() if isinstance(prompt_obj, dict) else ""
                    messages = prompt_obj.get("messages", []) if isinstance(prompt_obj, dict) else []
                    rendered_parts: List[str] = []
                    if isinstance(messages, list):
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            role = str(msg.get("role", "") or "").strip() or "user"
                            content = msg.get("content")
                            text = ""
                            if isinstance(content, dict):
                                text = str(content.get("text", "") or "").strip()
                            elif isinstance(content, list):
                                chunks: List[str] = []
                                for c in content:
                                    if isinstance(c, dict):
                                        t = str(c.get("text", "") or "").strip()
                                        if t:
                                            chunks.append(t)
                                text = "\n\n".join(chunks).strip()
                            elif isinstance(content, str):
                                text = content.strip()
                            if not text:
                                continue
                            rendered_parts.append(f"#### MCP Prompt Message ({role})\n{text}")
                    if not rendered_parts and desc:
                        rendered_parts.append(desc)
                    if not rendered_parts:
                        continue
                    payload_text, meta = self._render_skill_section_payload(
                        sections=rendered_parts,
                        requested_section=requested_section,
                        full=full,
                    )
                    lines = [
                        "",
                        "## Agent Skill（按需加载）",
                        f"### MCP Skill Prompt: `{sid_raw}` · server `{srv}`",
                        f"**Description:** {desc or '(no description)'}",
                        "",
                        self._build_mcp_skill_context_pack(srv, sid_raw, rendered_parts),
                        "",
                        "【优先级】当前请求已显式指定该 skill：若与 AGENTS.md 或通用系统说明冲突，"
                        "按本 skill 正文执行（安全/越权/破坏性硬限制除外）。",
                        "",
                        "以下正文来自 MCP `prompts/get` 返回，请严格按其步骤执行：",
                        "",
                        payload_text,
                        "",
                    ]
                    return "\n".join(lines), meta
                except Exception:
                    continue
            return None, {"chunked": False, "section": 0, "total": 0, "full": True}
        _br = Path(target.bundle_root)
        body = str(getattr(target, "body", "") or "")
        if full or len(body) < SKILL_PROMPT_LONG_BODY_THRESHOLD:
            sections = [body]
        else:
            sections = self._split_skill_body_sections(body)
        payload_text, meta = self._render_skill_section_payload(
            sections=sections,
            requested_section=requested_section,
            full=full,
        )
        lines = [
            "",
            "## Agent Skill（按需加载）",
            f"### Skill: `{target.name}` · 目录 `{target.skill_id}`",
            f"**Description:** {target.description}",
            "",
            self._build_local_skill_context_pack(target),
            "",
            "【优先级】当前请求已显式指定该 skill：若与 AGENTS.md 或通用系统说明冲突，"
            "按本 skill 正文执行（安全/越权/破坏性硬限制除外）。",
            "",
            f"**Skill bundle root (absolute path on this machine):** `{target.bundle_root}`",
            f"**SKILL.md path (same bundle):** `{_br / 'SKILL.md'}`",
            "技能正文中的 `<skill_root>` 即指上文的 **Skill bundle root**。",
            "",
            payload_text,
            "",
        ]
        return "\n".join(lines), meta

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

    def _get_slash_workspace_switch_commands(self) -> List[str]:
        return build_workspace_action_commands(self._workspaces_state, "switch")

    def _get_slash_workspace_delete_commands(self) -> List[str]:
        return build_workspace_action_commands(self._workspaces_state, "delete")

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

    def _get_slash_mcp_commands(self) -> List[str]:
        return build_mcp_scoped_commands(self.mcp_manager)

    def _get_slash_mcp_server_commands(self) -> List[str]:
        return build_mcp_server_commands(self.mcp_manager.mcp_config)

    def _get_slash_mcp_server_info_commands(self) -> List[str]:
        """
        Dynamic completions for:
        - /mcp server-info <server>
        """
        return self._get_slash_mcp_server_target_commands("server-info")

    def _get_slash_mcp_reconnect_commands(self) -> List[str]:
        """
        Dynamic completions for:
        - /mcp reconnect <server>
        """
        return self._get_slash_mcp_server_target_commands("reconnect")

    def _get_slash_mcp_list_tools_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands("list-tools")

    def _get_slash_mcp_list_resources_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands("list-resources")

    def _get_slash_mcp_list_resource_templates_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands("list-resource-templates")

    def _get_slash_mcp_list_prompts_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands("list-prompts")

    def _get_slash_mcp_disable_tools_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands(
            "disable-tools", with_trailing_space=True
        )

    def _get_slash_mcp_enable_tools_commands(self) -> List[str]:
        return self._get_slash_mcp_server_target_commands(
            "enable-tools", with_trailing_space=True
        )

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

    def _confirm_allowlist_path(self) -> Path:
        return command_security.confirm_allowlist_path(self)

    def _freedom_script_review_cache_path(self) -> Path:
        return self.ai_workspace_dir / "freedom_script_review_cache.json"

    def _load_freedom_script_review_cache(self) -> None:
        self._freedom_script_review_entries = {}
        p = self._freedom_script_review_cache_path()
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            ent = data.get("entries")
            if isinstance(ent, dict):
                self._freedom_script_review_entries = {
                    str(k): v for k, v in ent.items() if isinstance(v, dict)
                }
        except Exception as e:
            print(f"⚠️ 读取 freedom_script_review_cache.json 失败: {e}")

    def _save_freedom_script_review_cache(self) -> bool:
        try:
            p = self._freedom_script_review_cache_path()
            payload = {
                "version": 1,
                "entries": dict(sorted(self._freedom_script_review_entries.items())),
            }
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            print(f"⚠️ 写入 freedom_script_review_cache.json 失败: {e}")
            return False

    @staticmethod
    def _sha256_utf8(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _freedom_script_eligible_for_combined_review(self, sp: Path) -> bool:
        """Local script types that get combined AI review in freedom mode (not python -c)."""
        if not sp.is_file():
            return False
        suf = sp.suffix.lower()
        if suf not in (".py", ".ps1", ".bat", ".cmd"):
            return False
        return True

    def _freedom_try_cached_user_script_review(
        self, path_key: str, script_body: str, command: Dict[str, Any]
    ) -> Optional[Tuple[bool, str]]:
        """If cache matches path + script hash + command JSON hash, return (skip, reason)."""
        cmd_json = json.dumps(command, ensure_ascii=False, sort_keys=True)
        h_body = self._sha256_utf8(script_body)
        h_cmd = self._sha256_utf8(cmd_json)
        rec = self._freedom_script_review_entries.get(path_key)
        if not isinstance(rec, dict):
            return None
        if rec.get("script_sha256") != h_body or rec.get("command_sha256") != h_cmd:
            return None
        skip = bool(rec.get("skip_confirm"))
        reason = rec.get("reason") if isinstance(rec.get("reason"), str) else ""
        if not reason:
            reason = "（缓存无说明）"
        return (skip, reason)

    def _freedom_save_user_script_review_cache(
        self,
        path_key: str,
        script_body: str,
        command: Dict[str, Any],
        skip: bool,
        reason: str,
    ) -> None:
        cmd_json = json.dumps(command, ensure_ascii=False, sort_keys=True)
        self._freedom_script_review_entries[path_key] = {
            "script_sha256": self._sha256_utf8(script_body),
            "command_sha256": self._sha256_utf8(cmd_json),
            "skip_confirm": skip,
            "reason": (reason or "")[:800],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_freedom_script_review_cache()

    def _normalize_path_allowlist_key(self, p: Path) -> str:
        return command_security.normalize_path_allowlist_key(p)

    def _shell_script_allowlist_key(self, command: str) -> Optional[str]:
        """Resolved script file path key; ignores arguments. None if no script file (e.g. python -c)."""
        return command_security.shell_script_allowlist_key(self, command)

    def _salted_sha256(self, text: str, salt: str) -> str:
        return command_security.salted_sha256(text, salt)

    def _shell_script_hash(self, script_path: Path) -> Optional[str]:
        """
        Compute salted hash for an allowlisted script file.
        Returns None if file cannot be read or salt is unavailable.
        """
        return command_security.shell_script_hash(self, script_path)

    def _shell_executable_allowlist_key(self, command: str) -> str:
        """
        Stable key for invocations without a script path: same executable / bare name
        regardless of trailing arguments (e.g. git, dir, or full path to an .exe).
        """
        return command_security.shell_executable_allowlist_key(self, command)

    def _load_confirm_allowlist(self) -> None:
        """Load shell targets that skip confirm with path+salted-hash verification."""
        return command_security.load_confirm_allowlist(self)

    def _save_confirm_allowlist(self) -> bool:
        return command_security.save_confirm_allowlist(self)

    def _shell_command_in_allowlist(self, command: str) -> bool:
        return command_security.shell_command_in_allowlist(self, command)

    def _shell_confirm_should_offer_always(self, command: str) -> bool:
        """
        Do not offer 'a' when shell runs a session-ephemeral AI script (created via script action
        this session, tracked in _ephemeral_script_paths).
        """
        return command_security.shell_confirm_should_offer_always(self, command)

    def _script_basename_in_allowlist(self, safe_name: str) -> bool:
        return command_security.script_basename_in_allowlist(self, safe_name)

    def _add_shell_command_allowlist(self, command: str) -> None:
        return command_security.add_shell_command_allowlist(self, command)

    def _add_script_basename_allowlist(self, safe_name: str) -> None:
        return command_security.add_script_basename_allowlist(self, safe_name)

    def _reset_always_confirm_skip(self) -> Dict[str, Any]:
        """Clear allowlist and restore y/n prompts."""
        return command_security.reset_always_confirm_skip(self)

    def _prompt_confirm_yes_no_maybe_always(
        self,
        prompt_core: str,
        *,
        offer_always: bool,
        kind: str,
        shell_command: Optional[str] = None,
        script_basename: Optional[str] = None,
    ) -> bool:
        """
        kind: 'shell' | 'script' | 'text_file'. Returns True if user proceeds.
        The **a / always** option is only used for **shell** (execute command / run script via OS)
        when offer_always is True; it records shell script path+hash or shell exe token.
        Workspace `script` and `text_file` are file writes: y/n only (no a).
        """
        if kind == "shell" and shell_command is not None and self._shell_command_in_allowlist(
            shell_command
        ):
            return True
        if offer_always:
            line = f"{prompt_core} (y/n/a，a=将本条加入免确认列表): "
        else:
            line = f"{prompt_core} (y/n): "
        raw = input(line).strip().lower()
        if offer_always and raw in ("a", "always"):
            if kind == "shell" and shell_command is not None:
                self._add_shell_command_allowlist(shell_command)
            print(
                f"ℹ️ 已写入 {self._confirm_allowlist_path()}。"
                "可使用 /always_confirm-reset 清空列表。"
            )
            return True
        return raw in ("y", "yes")

    def _parse_reversibility_response(self, text: str) -> Tuple[bool, str]:
        """Parse model JSON; on failure treat as irreversible (still require confirm)."""
        if not text or not isinstance(text, str):
            return False, "空响应"
        s = text.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
        if fence:
            s = fence.group(1)
        for i, ch in enumerate(s):
            if ch != "{":
                continue
            depth = 0
            for j in range(i, len(s)):
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[i : j + 1]
                        try:
                            obj = json.loads(chunk)
                            if "reversible" in obj:
                                r = obj["reversible"]
                                if isinstance(r, str):
                                    r = r.strip().lower() in ("true", "1", "yes", "是")
                                reason = str(obj.get("reason", "")).strip()[:200]
                                ok = bool(r)
                                return ok, (reason or ("可逆" if ok else "不可逆"))
                        except json.JSONDecodeError:
                            pass
                        break
        return False, "无法解析可逆性判定"

    def _parse_combined_freedom_response(
        self, text: str
    ) -> Tuple[bool, bool, Optional[bool], str]:
        """Parse one-shot freedom JSON: safe_auto, reversible, manipulation (optional), reason."""
        if not text or not isinstance(text, str):
            return False, False, True, "空响应"
        s = text.strip()
        if s.startswith("❌"):
            return False, False, True, s[:120]
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
        if fence:
            s = fence.group(1)
        for i, ch in enumerate(s):
            if ch != "{":
                continue
            depth = 0
            for j in range(i, len(s)):
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[i : j + 1]
                        try:
                            obj = json.loads(chunk)
                            if "safe_auto" in obj and "reversible" in obj:
                                sa = obj["safe_auto"]
                                rev = obj["reversible"]
                                if isinstance(sa, str):
                                    sa = sa.strip().lower() in ("true", "1", "yes", "是")
                                if isinstance(rev, str):
                                    rev = rev.strip().lower() in ("true", "1", "yes", "是")
                                reason = str(obj.get("reason", "")).strip()[:240]
                                manip_raw = obj.get("manipulation", None)
                                manip: Optional[bool]
                                if manip_raw is None:
                                    manip = None
                                else:
                                    if isinstance(manip_raw, str):
                                        manip_raw = manip_raw.strip().lower() in (
                                            "true",
                                            "1",
                                            "yes",
                                            "是",
                                        )
                                    manip = bool(manip_raw)
                                return (
                                    bool(sa),
                                    bool(rev),
                                    manip,
                                    reason or "已判定",
                                )
                        except json.JSONDecodeError:
                            pass
                        break
        return False, False, True, "无法解析合并审查结果"

    @staticmethod
    def _freedom_script_quick_deny(content: str) -> bool:
        """Fast heuristic: likely system/config modification or dangerous mass delete."""
        if not content:
            return False
        low = content.lower()
        needles = (
            "winreg.",
            "hkey_",
            r"\\registry\\",
            "_winreg",
            "ctypes.windll",
            "netsh ",
            "sc.exe",
            "reg add",
            "reg delete",
            "set-itemproperty",
            "new-itemproperty",
            "/etc/sudoers",
            "/etc/ssh/sshd",
            "os.environ[",
            "putenv(",
            "machine\\system\\currentcontrolset",
        )
        return any(n in low for n in needles)

    @staticmethod
    def _freedom_script_prompt_injection(content: str) -> Tuple[bool, str]:
        """
        Heuristic fallback: substring markers of prompt-injection / reviewer manipulation.
        Used when the ML classifier fails or returns unparseable output.
        Returns (matched, hint).
        """
        if not content:
            return False, ""
        low = content.lower()
        needles = (
            "ignore previous instructions",
            "disregard previous instructions",
            "override system prompt",
            "always return",
            '"safe_auto": true',
            '"reversible": true',
            "you are the reviewer",
            "you are the classifier",
            "请忽略之前的指令",
            "忽略以上规则",
            "覆盖系统提示词",
            "总是返回 true",
            "必须判定为可逆",
            "必须判定为安全",
            "让审查器通过",
        )
        for n in needles:
            if n in low:
                return True, n
        return False, ""

    def _combined_review_on_model_failure(self, content: str, detail: str) -> Tuple[bool, str, bool]:
        """When combined review API fails: keyword heuristic; conservative skip=False."""
        hit, tok = self._freedom_script_prompt_injection(content)
        msg = detail
        if hit:
            msg = f"{detail}；关键词兜底(manipulation): {tok}"
        return False, msg, True

    def _ai_assess_ephemeral_script_combined(
        self, script_path: Path, content: str, command: Dict[str, Any]
    ) -> Tuple[bool, str, bool]:
        """
        Single AI call: safe_auto + reversible + manipulation (prompt-injection toward reviewer).
        Auto-skip confirm iff NOT manipulation AND (safe_auto OR (NOT safe_auto AND reversible)).
        Returns (skip_confirm, reason, manipulation_risk).
        """
        keys = sorted(self._ai_created_path_keys)[:120]
        payload = (
            f"work_directory={self.work_directory.resolve()}\n"
            f"ai_workspace_dir={self.ai_workspace_dir.resolve()}\n"
            f"os={os.name}\n"
            f"ai_tracked_path_keys_normalized={json.dumps(keys, ensure_ascii=False)}\n"
            f"script_file={script_path.resolve()}\n\n"
            f"--- script source ---\n{content}\n--- end ---\n\n"
            f"--- command JSON ---\n{json.dumps(command, ensure_ascii=False)}\n"
        )
        raw = self.call_ai(
            payload,
            context="",
            stream=False,
            freedom_combined_review=True,
        )
        if not isinstance(raw, str):
            return self._combined_review_on_model_failure(content, "模型返回类型异常")
        if raw.strip().startswith("❌"):
            return self._combined_review_on_model_failure(content, raw.strip()[:120])
        safe_auto, reversible, manip, reason = self._parse_combined_freedom_response(raw)
        if "无法解析" in reason:
            return self._combined_review_on_model_failure(content, reason)
        if manip is None:
            hit, tok = self._freedom_script_prompt_injection(content)
            manip = hit
            if hit:
                reason = f"{reason}；关键词兜底(manipulation): {tok}"
        skip = (not manip) and (safe_auto or ((not safe_auto) and reversible))
        return skip, reason, bool(manip)

    def _ai_assess_reversible(self, command: Dict[str, Any]) -> Tuple[bool, str]:
        payload = json.dumps(command, ensure_ascii=False)
        raw = self.call_ai(
            payload, context="", stream=False, minimal_classifier=True
        )
        if not isinstance(raw, str):
            return False, "模型返回类型异常"
        if raw.strip().startswith("❌"):
            return False, raw.strip()[:120]
        return self._parse_reversibility_response(raw)

    def _freedom_auto_confirm(self, command: Dict[str, Any]) -> bool:
        """Return True to skip interactive confirmation (move/delete/shell/text_file/git write)."""
        policy = str(getattr(self, "execution_policy", "confirmation")).lower()
        if policy == "confirmation":
            return False
        if policy == "unlimited":
            return True
        action = command.get("tool") or command.get("action")
        params = command.get("args")
        if not isinstance(params, dict):
            params = command.get("params") or {}

        if action == "delete":
            p = params.get("path") or params.get("file_name") or params.get("name")
            if p and self._is_ai_created_path(str(p)):
                print("🦅 自由模式：删除目标为本会话 AI 创建或产出的文件，跳过确认。")
                return True

        if action == "move":
            src = params.get("source")
            if src and self._is_ai_created_path(str(src)):
                print("🦅 自由模式：移动源为本会话 AI 创建或产出的文件，跳过确认。")
                return True

        if action == "shell":
            cmd = params.get("command") or ""
            s = (cmd or "").strip()

            # Inline Python (-c): no script file on disk to review here
            if re.search(
                r"(?i)(?:^|[\s;&|])(?:py(?:thon)?(?:\d(?:\.\d)?)?|pythonw)\s+-\s*c\s+", s
            ):
                print("🦅 自由模式：工作目录内联 Python（-c），跳过确认。")
                return True

            sp = self._parse_shell_invoked_script_path(s)
            if sp is not None:
                # Reload allowlist so manual edits to confirm_allowlist.json can take effect immediately.
                self._load_confirm_allowlist()
                sk = self._normalize_path_allowlist_key(sp)
                expected = self._allowlist_shell_paths.get(sk)
                if expected:
                    actual = self._shell_script_hash(sp)
                    if actual and actual == expected:
                        print("🦅 自由模式：命中免确认脚本哈希校验，跳过 AI 审核并直接执行。")
                        return True

                k = self._ephemeral_path_key(sp)
                session_ephemeral = k in self._ephemeral_script_paths
                combined_eligible = sp.is_file() and (
                    session_ephemeral or self._freedom_script_eligible_for_combined_review(sp)
                )
                # Script file on disk: combined review for session AI scripts or workspace scripts
                if combined_eligible:
                    try:
                        body = sp.read_text(encoding="utf-8", errors="replace")
                    except OSError as e:
                        print(f"⚠️ 无法读取待审查脚本: {e}")
                        body = ""
                    max_len = 200_000
                    if len(body) > max_len:
                        body = body[:max_len] + "\n# ... [truncated for review] ..."
                    if self._freedom_script_quick_deny(body):
                        print(
                            "🦅 自由模式：脚本内容命中高风险启发规则（如注册表/系统配置相关），"
                            "改由操作级可逆判定。"
                        )
                        reversible, reason = self._ai_assess_reversible(command)
                        if reversible:
                            print(f"🦅 判定为可逆，自动跳过确认 — {reason}")
                        else:
                            print(f"🦅 判定为不可逆或不确定，仍需手动确认 — {reason}")
                        return reversible
                    # Persist/cache only for scripts not created via this session's "script" action
                    use_cache = not session_ephemeral
                    if use_cache:
                        cached = self._freedom_try_cached_user_script_review(k, body, command)
                        if cached is not None:
                            skip_c, reason_c = cached
                            tag = "可自动跳过确认" if skip_c else "需手动确认"
                            print(
                                f"🦅 自由模式：已使用配置文件中的脚本审核缓存（脚本与命令哈希一致），{tag} — {reason_c}"
                            )
                            return skip_c
                    print(
                        "🦅 自由模式：正在审查脚本安全、诱导内容与操作可逆性（单次 AI）…"
                    )
                    skip, reason, inj_risk = self._ai_assess_ephemeral_script_combined(
                        sp, body, command
                    )
                    if use_cache:
                        self._freedom_save_user_script_review_cache(k, body, command, skip, reason)
                    if inj_risk:
                        print(
                            _ansi_red(
                                "🚫 自由模式：合并审查判定脚本存在审查诱导/提示词注入风险 — "
                                f"{reason}"
                            )
                        )
                        print(
                            _ansi_red(
                                "🚫 建议不要执行该脚本；如必须执行，请先人工审查并手动确认。"
                            )
                        )
                        return False
                    if skip:
                        print(f"🦅 判定为可自动跳过确认 — {reason}")
                    else:
                        print(f"🦅 判定为需手动确认 — {reason}")
                    return skip

                if k in self._ai_created_path_keys:
                    print("🦅 自由模式：命令作用于本会话已跟踪的 AI 产出路径，跳过确认。")
                    return True

            print("🦅 自由模式：正在请 AI 判定操作是否可逆…")
            reversible, reason = self._ai_assess_reversible(command)
            if reversible:
                print(f"🦅 判定为可逆，自动跳过确认 — {reason}")
            else:
                print(f"🦅 判定为不可逆或不确定，仍需手动确认 — {reason}")
            return reversible

        print("🦅 自由模式：正在请 AI 判定操作是否可逆…")
        reversible, reason = self._ai_assess_reversible(command)
        if reversible:
            print(f"🦅 判定为可逆，自动跳过确认 — {reason}")
        else:
            print(f"🦅 判定为不可逆或不确定，仍需手动确认 — {reason}")
        return reversible

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

    def _safe_script_basename(self, filename: str) -> str:
        """Only the last path segment; prevents traversal out of ai_workspace_dir."""
        return Path(filename or "").name.strip()

    def _workspace_relative_script_triple(self, rel: Path) -> Tuple[Path, Path, Path]:
        """相对路径在 shell 解析时的三个候选根：当前工作目录、workspace/temp、workspace 根（兼容旧路径）。"""
        p_wd = (self.work_directory / rel).resolve()
        p_temp = (self.ai_workspace_temp_dir / rel).resolve()
        p_ws = (self.ai_workspace_dir / rel).resolve()
        return p_wd, p_temp, p_ws

    def _register_ephemeral_script(self, script_path: Path) -> None:
        key = self._ephemeral_path_key(script_path)
        self._ephemeral_script_paths.add(key)
        self._ai_created_path_keys.add(key)

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

    def _register_outputs_from_shell_command(self, command: str) -> None:
        """Heuristic: pandas/openpyxl output paths in -c one-liners → session AI outputs."""
        return command_actions.register_outputs_from_shell_command(self, command)

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

    def _rewrite_shell_command_script_arg_to_abs(self, command: str, resolved: Path) -> str:
        """Replace the script token with resolved absolute path (for python/py/... invocations)."""
        return command_actions.rewrite_shell_command_script_arg_to_abs(self, command, resolved)

    def _ensure_absolute_script_for_shell_cwd(self, command: str) -> str:
        """If the invoked script file lives only under ai_workspace_dir, expand it to an absolute path."""
        return command_actions.ensure_absolute_script_for_shell_cwd(self, command)

    def _tune_7z_output_for_piped_terminal(self, command: str) -> str:
        """Improve 7z visibility under piped/non-tty execution by adding stable output switches."""
        return command_actions.tune_7z_output_for_piped_terminal(command)

    def _parse_shell_invoked_executable(self, command: str) -> Optional[Path]:
        """Best-effort: path to the primary script/exe the user asked to run (first token)."""
        return command_actions.parse_shell_invoked_executable(self, command)

    def _get_path_policy(self) -> PathPolicy:
        pol = getattr(self, "path_policy", None)
        if pol is None:
            pol = PathPolicy(self)
            self.path_policy = pol
        return pol

    def _is_path_under(self, child: Path, root: Path) -> bool:
        return self._get_path_policy().is_path_under(child, root)

    def _is_smart_shell_protected_path(self, path: Path) -> bool:
        return self._get_path_policy().is_smart_shell_protected_path(path)

    def _reject_ai_workspace_root_level_write(self, path: Path) -> Optional[str]:
        return self._get_path_policy().reject_ai_workspace_root_level_write(path)

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

    def _is_dependency_install_command(self, command: str) -> bool:
        return command_actions.is_dependency_install_command(command)

    def _is_ai_workspace_script_command(self, command: str) -> bool:
        return command_actions.is_ai_workspace_script_command(self, command)

    def _blocked_by_self_protection(self, action: str) -> Dict[str, Any]:
        return self._get_path_policy().blocked_by_self_protection(action)

    def _try_remove_ephemeral_script_after_shell(self, command: str) -> Optional[str]:
        """Returns basename if an ephemeral script was removed, else None."""
        return command_actions.try_remove_ephemeral_script_after_shell(self, command)

    def _resolve_model_context_file_env(self, command: str) -> Optional[str]:
        """Resolve skill-provided merge env var for shell command."""
        return command_actions.resolve_model_context_file_env(self, command)

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

    def _grep_read_path_allowed(self, path: Path) -> bool:
        """Paths that may be read by grep (workspace + AI workspace)."""
        return command_actions.grep_read_path_allowed(self, path)

    def _grep_output_path_allowed(self, path: Path) -> bool:
        """Output file may be under workspace, AI workspace, or system temp."""
        return command_actions.grep_output_path_allowed(self, path)

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

    def _build_first_round_evidence_block(self, user_task: str) -> str:
        """
        Build one-shot evidence block for the first task round.
        Disabled in default workspace by hard policy.
        """
        if not self._project_context_feature_enabled():
            return ""
        q = str(user_task or "").strip()
        if not q:
            return ""
        try:
            res = self.action_project_context_search(
                {"query": q, "max_files": 8, "refresh": True}
            )
        except Exception:
            return ""
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
        return out.strip()

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
        from .tool_execution_engine import execute_tool_call_legacy
        return execute_tool_call_legacy(self, tool_name, arguments)

    def _parse_mcp_shortcut_command(self, builtin_line: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
        """
        Parse '/mcp ...' shortcuts into tool calls.
        Rules:
        - Only required parameters are accepted.
        - Optional parameters are not supported in shortcuts.
        - For 'mcp_list_disabled_tools', server is optional.
        """
        raw = (builtin_line or "").strip()
        if not raw:
            return None, {}, "命令为空"
        parts = raw.split()
        low = [p.lower() for p in parts]
        if not low:
            return None, {}, None
        if low[0] != "mcp":
            return None, {}, None
        if len(parts) < 2:
            return None, {}, "用法: /mcp <subcommand> [args]"
        cmd = low[1]

        if cmd == "reload-config" and len(parts) == 2:
            return "mcp_reload_config", {}, None
        if cmd == "reload-config" and len(parts) != 2:
            return None, {}, "用法: /mcp reload-config"
        if cmd == "status" and len(parts) == 2:
            return "mcp_status", {}, None
        if cmd == "status" and len(parts) != 2:
            return None, {}, "用法: /mcp status"
        if cmd == "status-refresh" and len(parts) == 2:
            return "mcp_status_refresh", {}, None
        if cmd == "status-refresh" and len(parts) != 2:
            return None, {}, "用法: /mcp status-refresh"
        if cmd == "reconnect" and len(parts) == 3:
            return "mcp_reconnect", {"server": parts[2]}, None
        if cmd == "reconnect":
            return None, {}, "用法: /mcp reconnect <server>"
        if cmd == "server-info" and len(parts) == 3:
            return "mcp_server_info", {"server": parts[2]}, None
        if cmd == "server-info":
            return None, {}, "用法: /mcp server-info <server>"
        if cmd == "list-tools" and len(parts) == 3:
            return "mcp_list_tools", {"server": parts[2]}, None
        if cmd == "list-tools":
            return None, {}, "用法: /mcp list-tools <server>"
        if cmd == "list-resources" and len(parts) == 3:
            return "mcp_list_resources", {"server": parts[2]}, None
        if cmd == "list-resources":
            return None, {}, "用法: /mcp list-resources <server>"
        if cmd == "list-resource-templates" and len(parts) == 3:
            return "mcp_list_resource_templates", {"server": parts[2]}, None
        if cmd == "list-resource-templates":
            return None, {}, "用法: /mcp list-resource-templates <server>"
        if cmd == "list-prompts" and len(parts) == 3:
            return "mcp_list_prompts", {"server": parts[2]}, None
        if cmd == "list-prompts":
            return None, {}, "用法: /mcp list-prompts <server>"
        if cmd == "list-disabled-tools":
            if len(parts) == 2:
                return "mcp_list_disabled_tools", {}, None
            if len(parts) == 3:
                return "mcp_list_disabled_tools", {"server": parts[2]}, None
            return None, {}, "用法: /mcp list-disabled-tools [server]"

        if cmd == "disable-tools" and len(parts) >= 4:
            server = parts[2]
            tools_csv = " ".join(parts[3:]).strip()
            tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
            if not tools:
                return None, {}, "缺少 tools 参数，请使用逗号分隔，例如: /mcp disable-tools playwright browser_click,browser_type"
            return "mcp_disable_tools", {"server": server, "tools": tools}, None
        if cmd == "disable-tools":
            return None, {}, "用法: /mcp disable-tools <server> <tool1,tool2>"

        if cmd == "enable-tools" and len(parts) >= 4:
            server = parts[2]
            tools_csv = " ".join(parts[3:]).strip()
            tools = [x.strip() for x in tools_csv.split(",") if x.strip()]
            if not tools:
                return None, {}, "缺少 tools 参数，请使用逗号分隔，例如: /mcp enable-tools playwright browser_click,browser_type"
            return "mcp_enable_tools", {"server": server, "tools": tools}, None
        if cmd == "enable-tools":
            return None, {}, "用法: /mcp enable-tools <server> <tool1,tool2>"

        return None, {}, (
            "无效 MCP 快捷命令。可用示例："
            "/mcp status, /mcp status-refresh, /mcp reload-config, "
            "/mcp reconnect <server>, /mcp server-info <server>, "
            "/mcp list-tools <server>, /mcp list-resources <server>, "
            "/mcp list-resource-templates <server>, /mcp list-prompts <server>, "
            "/mcp list-disabled-tools [server], "
            "/mcp disable-tools <server> <tool1,tool2>, /mcp enable-tools <server> <tool1,tool2>"
        )

    @staticmethod
    def _mcp_item_label(item: Any) -> str:
        if not isinstance(item, dict):
            return str(item)
        for k in ("display_name", "name", "uri", "id", "title"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return str(item)

    def _print_mcp_shortcut_result(self, tool_name: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
        print("\n=== MCP Command Result ===")
        print(f"Command: {tool_name}")
        if not result.get("success", False):
            print(f"Status : FAILED")
            print(f"Error  : {result.get('error', '未知错误')}")
            print("==========================\n")
            return

        print("Status : OK")
        if tool_name == "mcp_reload_config":
            print(f"Changed: {bool(result.get('changed', False))}")
            summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
            added = ", ".join(summary.get("added", [])) or "None"
            changed = ", ".join(summary.get("changed", [])) or "None"
            removed = ", ".join(summary.get("removed", [])) or "None"
            print(f"Added  : {added}")
            print(f"Updated: {changed}")
            print(f"Removed: {removed}")
        elif tool_name in ("mcp_status", "mcp_status_refresh"):
            status = result.get("status", {}) if isinstance(result.get("status"), dict) else {}
            print(f"Total  : {status.get('total', 0)}")
            print(f"Success: {status.get('success', 0)}")
            print(f"Failed : {status.get('failed', 0)}")
            print(f"Loading: {status.get('loading_count', 0)}")
            print(f"Loaded : {status.get('all_loaded', False)}")
            servers = status.get("servers", {}) if isinstance(status.get("servers"), dict) else {}
            if servers:
                print("Servers:")
                for s, st in servers.items():
                    if not isinstance(st, dict):
                        continue
                    print(f"- {s}: state={st.get('state','')}, tools={st.get('tool_count',0)}, source={st.get('source','')}")
        elif tool_name == "mcp_reconnect":
            print(f"Server : {result.get('server', args.get('server', ''))}")
            print(f"Source : {result.get('source', '')}")
            print(f"Tools  : {result.get('count', 0)}")
        elif tool_name == "mcp_server_info":
            info = result.get("info", {}) if isinstance(result.get("info"), dict) else {}
            status = info.get("status", {}) if isinstance(info.get("status"), dict) else {}
            print(f"Server : {result.get('server', args.get('server', ''))}")
            print(f"State  : {status.get('state', '')}")
            print(f"Source : {status.get('source', '')}")
            sections = info.get("sections", {}) if isinstance(info.get("sections"), dict) else {}
            for sec_key, title in (
                ("tools", "Tools"),
                ("resources", "Resources"),
                ("resource_templates", "ResourceTemplates"),
                ("prompts", "Prompts"),
            ):
                sec = sections.get(sec_key, {}) if isinstance(sections.get(sec_key), dict) else {}
                count = sec.get("count", 0)
                print(f"{title:<16}: {count}")
                items = sec.get("items", []) if isinstance(sec.get("items"), list) else []
                if items and sec_key in ("tools", "resources", "prompts"):
                    labels = [self._mcp_item_label(x) for x in items]
                    print(f"  - {', '.join(labels)}")
        elif tool_name in ("mcp_disable_tools", "mcp_enable_tools"):
            print(f"Server : {result.get('server', args.get('server', ''))}")
            disabled = result.get("disabled_tools", [])
            if not isinstance(disabled, list):
                disabled = []
            print(f"Disabled tools ({len(disabled)}): {', '.join(disabled) if disabled else 'None'}")
        elif tool_name == "mcp_list_disabled_tools":
            data = result.get("disabled_tools", {})
            if isinstance(data, dict):
                for s, arr in data.items():
                    tools = arr if isinstance(arr, list) else []
                    print(f"- {s}: {', '.join(tools) if tools else 'None'}")
            else:
                print("Disabled tools: None")
        elif tool_name in (
            "mcp_list_tools",
            "mcp_list_resources",
            "mcp_list_resource_templates",
            "mcp_list_prompts",
        ):
            server = result.get("server", args.get("server", ""))
            count = result.get("count", 0)
            print(f"Server : {server}")
            print(f"Count  : {count}")
            key = {
                "mcp_list_tools": "tools",
                "mcp_list_resources": "resources",
                "mcp_list_resource_templates": "templates",
                "mcp_list_prompts": "prompts",
            }.get(tool_name, "")
            items = result.get(key, []) if isinstance(result.get(key), list) else []
            if items:
                labels = [self._mcp_item_label(x) for x in items]
                print(f"Items  : {', '.join(labels)}")
        else:
            msg = result.get("message", "")
            if msg:
                print(f"Message: {msg}")
        print("==========================\n")

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
        from .runtime_loop import run_agent_loop
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

    def _parse_git_clone_command(self, command: str) -> Optional[Tuple[str, Optional[str]]]:
        s = str(command or "").strip()
        if not s:
            return None
        try:
            parts = shlex.split(s, posix=os.name != "nt")
        except ValueError:
            parts = s.split()
        if len(parts) < 3:
            return None
        if parts[0].lower() != "git" or parts[1].lower() != "clone":
            return None
        repo_url = ""
        target_dir: Optional[str] = None
        positional: List[str] = []
        i = 2
        while i < len(parts):
            tok = str(parts[i])
            if tok.startswith("-"):
                # Skip option value when present for common two-token options.
                if tok in ("-b", "--branch", "-o", "--origin", "--depth", "-c", "--config") and i + 1 < len(parts):
                    i += 2
                    continue
                i += 1
                continue
            positional.append(tok)
            i += 1
        if positional:
            repo_url = positional[0]
        if len(positional) >= 2:
            target_dir = positional[1]
        repo_url = str(repo_url or "").strip()
        if not repo_url:
            return None
        return repo_url, (str(target_dir).strip() if target_dir else None)

    @staticmethod
    def _repo_name_from_url(repo_url: str) -> str:
        raw = str(repo_url or "").strip().rstrip("/")
        if not raw:
            return ""
        if raw.endswith(".git"):
            raw = raw[:-4]
        return raw.split("/")[-1].strip().lower()

    def _detect_git_remote_origin(self, path: Path) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                capture_output=True,
                text=True,
                timeout=2.5,
            )
            if proc.returncode == 0:
                return (proc.stdout or "").strip()
        except Exception:
            pass
        return ""

    def _is_git_repo_dir(self, path: Path) -> bool:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            return False
        if (p / ".git").exists():
            return True
        try:
            proc = subprocess.run(
                ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=2.5,
            )
            return proc.returncode == 0 and "true" in (proc.stdout or "").strip().lower()
        except Exception:
            return False

    def _guard_git_clone_precheck(self, shell_cmd: str, shell_force: bool) -> Optional[Dict[str, Any]]:
        parsed = self._parse_git_clone_command(shell_cmd)
        if not parsed:
            return None
        repo_url, _target = parsed
        repo_name = self._repo_name_from_url(repo_url)
        wd = self.work_directory.resolve()

        wd_is_repo = self._is_git_repo_dir(wd)
        wd_remote = self._detect_git_remote_origin(wd) if wd_is_repo else ""
        wd_name_match = bool(repo_name and wd.name.strip().lower() == repo_name)
        wd_remote_match = bool(wd_remote and wd_remote.strip().lower() == repo_url.strip().lower())
        if wd_is_repo and (wd_name_match or wd_remote_match):
            return None

        first_level_dirs: List[Path] = []
        try:
            first_level_dirs = sorted([p for p in wd.iterdir() if p.is_dir()], key=lambda x: x.name.lower())
        except Exception:
            first_level_dirs = []

        candidates: List[str] = []
        for d in first_level_dirs:
            if not self._is_git_repo_dir(d):
                continue
            d_remote = self._detect_git_remote_origin(d)
            d_name_match = bool(repo_name and d.name.strip().lower() == repo_name)
            d_remote_match = bool(d_remote and d_remote.strip().lower() == repo_url.strip().lower())
            if d_name_match or d_remote_match:
                mark = "remote-match" if d_remote_match else "name-match"
                candidates.append(f"{d.name} ({mark})")

        # Hard stop: matching repo candidate already exists under current dir.
        if candidates and not shell_force:
            return {
                "success": False,
                "retryable": False,
                "blocked_by_guard": True,
                "needs_user_input": True,
                "input_type": "supplement",
                "question": (
                    "检测到当前目录一级子目录里已有疑似目标 repo。"
                    "请先确认并切换到现有 repo，再继续任务。"
                ),
                "error": (
                    f"已阻止直接 clone `{repo_url}`。当前目录 `{wd}` 的一级子目录匹配到: "
                    + ", ".join(candidates)
                    + "。请优先复用现有 repo；仅在你确认不存在可用副本时，才用 force=true 再次执行 clone。"
                ),
            }

        # If current directory is not target repo, require explicit confirmation to clone.
        if (not wd_is_repo or not (wd_name_match or wd_remote_match)) and not shell_force:
            top_dirs_preview = ", ".join(p.name for p in first_level_dirs[:30]) if first_level_dirs else "(none)"
            return {
                "success": False,
                "retryable": False,
                "blocked_by_guard": True,
                "needs_user_input": True,
                "input_type": "supplement",
                "question": "请先确认当前目录一级子目录中是否已有目标 repo；确认后再决定是否 clone。",
                "error": (
                    f"已阻止未确认的 git clone（repo={repo_url}）。"
                    f"当前目录 `{wd}` 不是目标 repo；已检查一级子目录: {top_dirs_preview}。"
                    "如需继续 clone，请在明确确认后使用 force=true 重新执行。"
                ),
            }
        return None

    def _handle_prefixed_command_inline(self, stripped_in: str, system_cmd_re: Any, os_name: str) -> bool:
        """
        Execute `/...` and `!...` immediately in wait-states (e.g. ask_more_info supplement).
        Returns True when consumed so caller should keep waiting for real supplement text.
        """
        s = str(stripped_in or "").strip()
        if not s:
            return False
        if s.startswith("/"):
            # In wait-state, '/skill-id ...' or '/server/tool ...' should be routed by
            # the main loop task parser, not treated as builtin slash command.
            try:
                if self._extract_forced_skill_reference(s) or self._extract_forced_mcp_reference(s):
                    return False
            except Exception:
                pass
            builtin_line = s[1:].lstrip()
            if not builtin_line:
                print("ℹ️ 单独输入 / 无效。")
                return True
            handled, should_exit = dispatch_builtin_command(
                self,
                builtin_line,
                os_name=os_name,
                wait_for_supplement=True,
                consume_unknown=False,
            )
            if handled:
                if should_exit:
                    raise SystemExit(0)
                return True
            bl = builtin_line.lower()
            mcp_tool, mcp_args, mcp_err = self._parse_mcp_shortcut_command(builtin_line)
            if mcp_tool:
                mcp_res = self.execute_tool_call(mcp_tool, mcp_args)
                self._print_mcp_shortcut_result(mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {})
                return True
            if bl == "mcp" or bl.startswith("mcp "):
                print(f"❌ {mcp_err}")
                return True
            if bl in ("exit", "quit"):
                self._save_current_workspace_position()
                print("👋 已退出 Smart Shell，再见！")
                raise SystemExit(0)
            if bl in ("cls", "clear screen"):
                os.system("cls" if os_name == "nt" else "clear")
                return True
            if bl == "clear":
                print("用法: /clear <screen|history|context>")
                return True
            if bl == "clear history":
                self.history_manager.clear_history()
                if self.input_handler is not None and hasattr(self.input_handler, "reset_command_history"):
                    self.input_handler.reset_command_history(self.history_manager.get_all_history())
                print("✅ 历史记录已清除")
                return True
            if bl == "clear context":
                self.conversation_history.clear()
                self._sync_active_chat_messages()
                self.operation_results.clear()
                self._last_auto_removed_ephemeral = None
                self._session_summary_llm = ""
                self._session_summary_rolling = ""
                self._last_llm_summary_pair_count = 0
                print("✅ 已清空 AI 上下文（对话历史与近期操作结果缓存，不影响命令行输入历史）")
                return True
            if self._handle_chat_builtin_command(builtin_line):
                return True

            if self._handle_workspace_builtin_command(builtin_line):
                return True
            if bl.startswith("execution-policy "):
                policy = bl.split(" ", 1)[1].strip().lower()
                if policy == "show":
                    self._print_execution_policy_details()
                elif policy:
                    self.execute_tool_call("execution_policy_set", {"policy": policy})
                else:
                    print("用法: /execution-policy <show|unlimited|moderate|confirmation>")
                return True
            if bl == "execution-policy":
                print("用法: /execution-policy <show|unlimited|moderate|confirmation>")
                return True
            if bl == "always_confirm-reset":
                self.execute_tool_call("always_confirm_reset", {})
                return True
            if bl == "knowledge status":
                self._print_knowledge_status_details()
                return True
            if bl == "memory status":
                self._print_memory_status_details()
                return True
            if bl == "memory enable":
                self.memory_enabled = True
                ok = self._save_memory_enabled_to_config()
                print(
                    "✅ 经验记忆功能已开启"
                    + ("；已写入 config.json" if ok else "（配置保存失败，仅本次进程生效）")
                )
                return True
            if bl == "memory disable":
                self.memory_enabled = False
                ok = self._save_memory_enabled_to_config()
                print(
                    "✅ 经验记忆功能已关闭"
                    + ("；已写入 config.json" if ok else "（配置保存失败，仅本次进程生效）")
                )
                return True
            if bl == "help":
                print("ℹ️ /help 可用；当前仍在等待补充信息，输入非命令文本将恢复原任务。")
                return True
            print("❌ 未识别的内置命令。请使用 /help 查看列表。")
            return True

        if s.startswith("!"):
            ui = s[1:].lstrip()
            if not ui:
                print("ℹ️ 单独输入 ! 无效。")
                return True
            if self._is_executable_file(ui):
                self._execute_file_directly(ui)
                return True
            user_input_cmd = ui
            if system_cmd_re.match(ui):
                if user_input_cmd.lower().startswith("ls") and os_name == "nt":
                    user_input_cmd = "dir " + user_input_cmd[2:].strip()
                elif user_input_cmd.lower().startswith("list") and os_name == "nt":
                    user_input_cmd = "dir " + user_input_cmd[4:].strip()
                elif user_input_cmd.lower().startswith("dir") and os_name != "nt":
                    user_input_cmd = "ls " + user_input_cmd[3:].strip()
                if user_input_cmd.lower().startswith("cd "):
                    path = user_input_cmd[3:].strip()
                    result = self.action_change_directory(path)
                    if not result["success"]:
                        print(f"❌ {result['error']}")
                    return True
            try:
                process = subprocess.Popen(
                    user_input_cmd if system_cmd_re.match(ui) else ui,
                    shell=True,
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    cwd=str(self.work_directory),
                )
                process.wait()
            except Exception as e:
                print(f"❌ 命令执行异常: {e}")
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
        prompt = f"{workspace_prompt_line}\n{str(self.work_directory)}>"
        
        # 重置历史记录索引
        self.history_manager.reset_index()

        # 优先使用已初始化的输入处理器（例如 Windows 下的 prompt_toolkit 补全）
        if self.input_handler is not None:
            try:
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
                from prompt_toolkit.formatted_text import FormattedText
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
                
                # 获取用户输入
                prompt_obj = FormattedText(
                    [
                        ("fg:ansibrightblack", workspace_prompt_line),
                        ("", "\n"),
                        ("", f"{str(self.work_directory)}>"),
                    ]
                )
                user_input = session.prompt(prompt_obj).strip()
                
                # 保存到历史记录
                if user_input:
                    self.history_manager.add_entry(user_input)
                
                return user_input
                
            except ImportError:
                # 如果没有prompt_toolkit，回退到标准input
                print("⚠️ 提示：安装 prompt_toolkit 可获得更好的输入体验：pip install prompt_toolkit")
                try:
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
                    user_input = input(prompt).strip()
                    if user_input:
                        self.history_manager.add_entry(user_input)
                    return user_input
                except KeyboardInterrupt:
                    raise KeyboardInterrupt
        else:
            # 非Windows系统使用简单的input
            try:
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
