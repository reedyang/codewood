import threading
from pathlib import Path
from typing import Any, Optional

from ..ai.ai_orchestrator import AIOrchestrator, AgentAIContext
from ..config.app_info import get_app_config_dirname, get_app_name, get_app_slug_kebab
from ..core.logging.app_logging import setup_app_logging
from ..core.config.config_env import resolve_string_values_in_data
from ..core.config.config_jsonc import CONFIG_JSONC_FILENAME, load_config_jsonc
from ..core.state.history_manager import HistoryManager
from ..integrations.mcp import McpManager
from ..policy.path_policy import PathPolicy
from ..services.session_memory_service import SessionMemoryService
from ..core.config.skills_loader import (
    build_skills_routing_prefix,
    calc_skills_dirs_fingerprint,
    load_skills_merged,
)
from ..tooling.dispatcher import ToolDispatcher
from ..tools.project_context_index import ProjectContextIndex

DEFAULT_AUTO_COMPACT_TRIGGER_PERCENT = 60


def setup_core_state(agent: Any, startup_work_directory: Path, self_repo_root: Path) -> None:
    agent.work_directory = startup_work_directory
    try:
        agent.startup_initial_directory = Path(startup_work_directory).expanduser().resolve()
    except Exception:
        agent.startup_initial_directory = Path(startup_work_directory).expanduser()
    agent._self_repo_root = self_repo_root
    agent.conversation_history = []
    agent._chat_state = {}
    agent.active_chat_id = ""
    agent.active_chat_name = "New Chat"
    agent._chat_state_lock = threading.RLock()
    agent._queued_user_input = None

    agent._session_summary_llm = ""
    agent._session_summary_rolling = ""
    agent._last_llm_summary_pair_count = 0
    agent.operation_results = []
    agent._in_task_execution = False
    agent._last_shell_output_visible_lines = 0
    agent._tool_call_feedback_interstitial_lines = 0

    agent._ephemeral_script_paths = set()
    agent._ai_created_path_keys = set()
    agent._last_auto_removed_ephemeral = None
    agent._mcp_pending_user_input = {}
    agent._force_current_input_as_requirement_once = False
    agent._last_cancelled_task = ""
    agent._active_runtime_task_id = ""
    agent._startup_chat_state_warning = ""
    agent._chat_history_first_visible_index_map = {}
    agent._chat_history_reload_last_terminal_width = 0
    agent._force_reload_chat_history_from_anchor_once = False
    agent._suppress_next_prompt_chat_reload_once = False
    agent._task_interrupt_requested = False
    agent._interruptible_processes = {}
    agent._interrupt_state_lock = threading.RLock()
    agent._interrupt_monitor_stop_event = threading.Event()
    agent._interrupt_monitor_thread = None
    agent._interrupt_monitor_refs = 0
    agent._interrupt_monitor_cancel_task_refs = 0
    agent._aborted_process_keys = set()
    agent._process_interrupt_requested = False
    agent._conversation_interrupt_banner_recent = False
    agent._conversation_interrupt_banner_recent_at = 0.0


def resolve_config_dir(config_dir: Optional[str]) -> Path:
    if config_dir:
        return Path(config_dir)

    config_dirname = get_app_config_dirname()
    current_config_dir = Path(config_dirname)
    user_config_dir = Path.home() / config_dirname
    if (user_config_dir / CONFIG_JSONC_FILENAME).exists():
        return user_config_dir
    if (current_config_dir / CONFIG_JSONC_FILENAME).exists():
        return current_config_dir
    return user_config_dir


def setup_workspace_and_history(
    agent: Any,
    startup_work_directory: Path,
    workspace_state_file: str,
    default_workspace_id: str,
) -> None:
    agent.workspace_registry_path = agent.config_dir / workspace_state_file
    agent._workspaces_state = agent._load_workspace_state()
    active_workspace_id = str(agent._workspaces_state.get("active") or default_workspace_id)
    workspaces = agent._workspaces_state.get("workspaces", {})
    active_workspace = (
        workspaces.get(active_workspace_id) if isinstance(workspaces, dict) else None
    )
    if not isinstance(active_workspace, dict):
        active_workspace = agent._default_workspace_entry()
        agent._workspaces_state["active"] = default_workspace_id
    agent._apply_workspace_entry(active_workspace, startup_work_directory)

    agent.history_manager = HistoryManager(str(agent.workspace_config_dir), language=getattr(agent, "display_language", "en") or "en")
    agent._load_chat_state()
    setup_app_logging(agent.config_dir)


def setup_runtime_preferences(agent: Any) -> None:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language

    agent.execution_policy = "confirmation"
    agent.memory_enabled = True
    agent.memory_fallback_expansion_enabled = True
    agent.project_context_first_round_evidence_enabled = True
    agent.auto_compact_trigger_percent = DEFAULT_AUTO_COMPACT_TRIGGER_PERCENT
    agent.display_language = DEFAULT_DISPLAY_LANGUAGE
    # Tool gates: default disabled so only core built-in coding tools are available.
    agent.mcp_tools_enabled = False
    # None means unlimited auto-execution rounds for a single task.
    agent.max_tool_rounds = None
    agent._resolved_config_data = {}
    try:
        cfg_path = agent.config_dir / CONFIG_JSONC_FILENAME
        if cfg_path.exists():
            cfg_data = resolve_string_values_in_data(load_config_jsonc(cfg_path))
            if isinstance(cfg_data, dict):
                agent._resolved_config_data = dict(cfg_data)
            else:
                cfg_data = {}
            language_value = normalize_display_language(
                cfg_data.get("language")
            ) or DEFAULT_DISPLAY_LANGUAGE
            agent.display_language = language_value
            pol = str(cfg_data.get("execution_policy", "confirmation")).strip().lower()
            if pol not in ("unlimited", "moderate", "confirmation"):
                pol = "confirmation"
            agent.execution_policy = pol

            _mfe = cfg_data.get("memory_fallback_expansion", True)
            agent.memory_fallback_expansion_enabled = (
                _mfe
                if isinstance(_mfe, bool)
                else str(_mfe).strip().lower() in ("1", "true", "yes", "on")
            )

            _me = cfg_data.get("memory_enabled", True)
            agent.memory_enabled = (
                _me
                if isinstance(_me, bool)
                else str(_me).strip().lower() in ("1", "true", "yes", "on")
            )

            _pcfr = cfg_data.get("project_context_first_round_evidence", True)
            agent.project_context_first_round_evidence_enabled = (
                _pcfr
                if isinstance(_pcfr, bool)
                else str(_pcfr).strip().lower() in ("1", "true", "yes", "on")
            )

            _mcp_tools_enabled = cfg_data.get("mcp_tools_enabled", False)
            agent.mcp_tools_enabled = (
                _mcp_tools_enabled
                if isinstance(_mcp_tools_enabled, bool)
                else str(_mcp_tools_enabled).strip().lower() in ("1", "true", "yes", "on")
            )

            _compact_pct = cfg_data.get("auto_compact_trigger_percent", DEFAULT_AUTO_COMPACT_TRIGGER_PERCENT)
            try:
                parsed_compact_pct = int(_compact_pct)
            except Exception:
                parsed_compact_pct = None
            if parsed_compact_pct is None or parsed_compact_pct < 1 or parsed_compact_pct > 100:
                print(
                    f"⚠️ Invalid auto_compact_trigger_percent in {CONFIG_JSONC_FILENAME}: "
                    f"{_compact_pct!r}; using default {DEFAULT_AUTO_COMPACT_TRIGGER_PERCENT}%."
                )
                parsed_compact_pct = DEFAULT_AUTO_COMPACT_TRIGGER_PERCENT
            agent.auto_compact_trigger_percent = parsed_compact_pct

            _mtr = cfg_data.get("max_tool_rounds", None)
            if _mtr is None:
                agent.max_tool_rounds = None
            else:
                try:
                    parsed_rounds = int(_mtr)
                except Exception:
                    parsed_rounds = None
                # Keep backward compatibility for explicit positive values.
                agent.max_tool_rounds = parsed_rounds if parsed_rounds and parsed_rounds > 0 else None
    except Exception as e:
        print(f"⚠️ Failed to read {CONFIG_JSONC_FILENAME} (execution policy and related settings will use defaults): {e}")


def setup_policy_caches(agent: Any) -> None:
    agent._allowlist_shell_paths = {}
    agent._allowlist_shell_exes = set()
    agent._allowlist_script = set()
    agent._confirm_allowlist_salt = ""
    agent._load_confirm_allowlist()

    agent._freedom_script_review_entries = {}
    agent._load_freedom_script_review_cache()


def setup_model_ai_stack(
    agent: Any,
    *,
    model_name: str,
    provider: str,
    openai_conf: Optional[dict],
    params: Optional[dict],
    model_config: Optional[dict],
    ollama_importer: Any,
) -> None:
    if model_config and isinstance(model_config, dict):
        agent.provider = str(model_config.get("provider", provider) or provider).strip()
        agent.params = model_config.get("params", {}) or {}
        agent.model_name = str(agent.params.get("model", model_name) or model_name).strip()
    else:
        agent.model_name = model_name
        agent.provider = provider
        agent.params = params or {}

    agent.openai_conf = agent.params if agent.provider == "openai" else openai_conf

    agent.path_policy = PathPolicy(agent)
    agent.session_memory_service = SessionMemoryService(agent)
    agent.ai_orchestrator = AIOrchestrator(
        AgentAIContext(
            provider=agent.provider,
            model_name=agent.model_name,
            model_params=agent.params,
            openai_conf=agent.openai_conf,
            work_directory=str(agent.work_directory),
            history_writer=agent._append_chat_message,
            regular_message_builder=agent._build_regular_task_messages,
            ollama_importer=ollama_importer,
        )
    )


def setup_prompt_and_mcp(agent: Any) -> None:
    prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system_prompt.md"
    with open(prompt_path, "r", encoding="utf-8") as f:
        agent._base_system_prompt = (
            f.read()
            .replace("{{APP_NAME}}", get_app_name())
            .replace("{{APP_SLUG_KEBAB}}", get_app_slug_kebab())
        )

    agent.mcp_config = agent._load_mcp_config()
    agent.mcp_manager = McpManager(
        agent.config_dir,
        agent.mcp_config,
        agent.workspace_config_dir,
        tool_policy_parent=agent.workspace_config_dir,
        language=getattr(agent, "display_language", "en") or "en",
    )
    agent.mcp_manager.register_client_method_handler(
        "elicitation/create",
        agent._handle_mcp_elicitation_create,
    )
    agent.mcp_manager.preload_all_async(timeout_s=12.0, force=False)

    agent._mcp_config_path = agent.config_dir / "mcp.jsonc"
    agent._mcp_config_file_sig = agent._get_mcp_config_file_sig()
    agent._mcp_config_struct_sig = agent._calc_mcp_config_sig(agent.mcp_config)
    agent._mcp_config_last_failed_file_sig = None

    agent.system_prompt = agent._compose_system_prompt_snapshot(include_tools=False)
    agent.tool_specs = agent._load_tools_spec_from_jsonc()
    agent.tools_prompt_template = agent._load_tools_prompt_template()


def setup_skills(agent: Any, builtin_skills_dir: Optional[str]) -> None:
    agent._builtin_skills_root = (
        Path(builtin_skills_dir).expanduser().resolve()
        if builtin_skills_dir
        else Path(__file__).resolve().parent.parent / "skills"
    )
    agent.skills = load_skills_merged(
        agent.config_dir,
        agent._builtin_skills_root,
        agent.workspace_config_dir,
        language=getattr(agent, "display_language", "en") or "en",
    )
    agent._skills_dirs_fingerprint = calc_skills_dirs_fingerprint(
        agent.config_dir,
        agent._builtin_skills_root,
        agent.workspace_config_dir,
    )
    agent._skills_routing_prefix = build_skills_routing_prefix(agent.skills)
    agent._active_skill_full_prompt = ""
    agent._active_skill_id = None
    agent._active_skill_source = None
    agent._active_skill_section = 0
    agent._active_skill_total_sections = 0
    agent._active_skill_chunked = False


def setup_input_handler(
    agent: Any,
    *,
    tab_completion_available: bool,
    input_handler_type: str,
    create_prompt_toolkit_input_handler: Any = None,
) -> None:
    agent.input_handler = None
    if not tab_completion_available:
        print("⚠️ Tab completion is unavailable")
        return

    try:
        if input_handler_type == "prompt_toolkit":
            try:
                initial_history = agent.history_manager.get_all_history()
            except Exception:
                initial_history = []
            if create_prompt_toolkit_input_handler is None:
                raise RuntimeError("prompt_toolkit input handler is unavailable")
            agent.input_handler = create_prompt_toolkit_input_handler(
                work_directory=agent.work_directory,
                workspace_directory=agent.workspace_root,
                initial_history=initial_history,
                slash_skill_commands=agent._get_slash_skill_commands(),
                slash_mcp_commands=agent._get_slash_mcp_server_commands(),
                slash_dynamic_rules=agent._get_slash_dynamic_rules(),
                terminal_resize_callback=getattr(
                    agent, "_handle_terminal_columns_changed_during_input", None
                ),
                language_provider=lambda: getattr(agent, "display_language", None),
            )
        else:
            print("⚠️ Unknown input handler type")
    except Exception as e:
        print(f"⚠️ Failed to initialize the input handler: {e}")


def setup_runtime_services(agent: Any) -> None:
    agent._workspace_runtime_generation = 0
    agent._project_context_refresh_gate = threading.Lock()
    agent._project_context_refresh_inflight = False
    agent._project_context_index = ProjectContextIndex(
        workspace_root=agent.workspace_root,
        storage_dir=(agent.workspace_config_dir / "indexes"),
    )
    try:
        agent._schedule_project_context_refresh_background(force=False, reason="startup")
    except Exception:
        pass
    agent._schedule_model_validation_background()
    agent.memory_service = None
    agent._last_memory_reflect_at = 0.0
    agent._schedule_memory_service_background()
    agent.tool_dispatcher = ToolDispatcher(agent, agent._execute_tool_call_legacy)
