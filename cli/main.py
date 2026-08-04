#!/usr/bin/env python3
"""
Application main entry point.

Usage:
    python cli/main.py   # Run with model settings from the config file
"""

import sys
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

# Required for PyInstaller builds: ``multiprocessing.spawn`` starts a new
# instance of the frozen executable.  ``freeze_support()`` detects the
# spawned child and runs the target function instead of the main app.
if getattr(sys, "frozen", False):
    import multiprocessing
    multiprocessing.freeze_support()

# Add the project root to Python path so the src package imports consistently
# whether this file is launched as a script or imported by tests.
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))
from cli.core.config.config_env import resolve_string_values_in_data
from cli.core.config.config_jsonc import (
    CONFIG_JSONC_FILENAME,
    load_config_jsonc,
    save_config_jsonc,
)
from cli.config.app_info import (
    append_windows_git_tools_to_path,
    get_app_bundled_bin_dir,
    get_app_config_dirname,
    get_app_global_config_dir,
    get_app_name,
    get_app_version,
    prepend_bundled_bin_to_path,
)
from cli.core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text
from cli.core.config.model_providers import DEFAULT_OLLAMA_PORT
from cli.core.config.model_providers import basic_chat_only_context_warning
from cli.core.config.model_providers import parse_configured_models
from cli.core.config.model_providers import parse_port
from cli.core.console_utils import _ansi_red
from cli.core.console_title import restore_app_console_title

CONFIG_TEMPLATE_RELATIVE_PATH = Path("cli/config") / "config.template.jsonc"
PROJECT_DOCS_URL = "https://github.com/reedyang/codewood"


def _format_startup_usage() -> str:
    return _format_startup_usage_with_executable("python cli/main.py")


def _format_startup_usage_with_executable(executable_name: str) -> str:
    command = str(executable_name or "").strip() or "python cli/main.py"
    return (
        "Usage:\n"
        f"  {command} [OPTIONS]\n"
        f"  {command} [OPTIONS] <COMMAND> [PROMPT]"
    )


def _format_startup_help(executable_name: str = "python cli/main.py") -> str:
    return (
        f"Version: {get_app_version()}\n"
        f"{_format_startup_usage_with_executable(executable_name)}\n"
        "\n"
        "Commands:\n"
        f"  exec                       Run {get_app_name()} and execute your prompt non-interactively, then exit\n"
        f"  app                        Launch the {get_app_name()} desktop GUI (no console window)\n"
        f"  serve                      Run {get_app_name()} as a headless localhost server for the desktop GUI\n"
        "\n"
        "Arguments:\n"
        "  [PROMPT]                   Prompt text used by the exec command\n"
        "\n"
        "Options:\n"
        "  -w, --workspace <WORKSPACE>  Workspace name or path to enter on startup\n"
        "  -m, --model <MODEL>          Select startup model (for example: openai/gpt-4o-mini)\n"
        "      --host <HOST>            Bind host for serve mode (default: 127.0.0.1)\n"
        "      --port <PORT>            Bind port for serve mode (default: 0 = ephemeral)\n"
        f"  -h, --help                   Print help for {get_app_name()} and exit\n"
    )


def _parse_startup_cli_args(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse startup CLI args with flexible ordering."""
    executable_name = "python cli/main.py"
    filtered_argv: list[str] = []
    idx = 0
    while idx < len(argv):
        token = str(argv[idx] or "").strip()
        if token == "--executable-name":
            if idx + 1 >= len(argv):
                return None, "❌ Missing value for --executable-name.\n" + _format_startup_usage()
            name = str(argv[idx + 1] or "").strip()
            if not name:
                return None, "❌ Executable name cannot be empty.\n" + _format_startup_usage()
            executable_name = name
            idx += 2
            continue
        filtered_argv.append(token)
        idx += 1

    usage_text = _format_startup_usage_with_executable(executable_name)

    if not filtered_argv:
        return {
            "workspace_selector": None,
            "exec_task": None,
            "model_selector": None,
            "show_help": False,
            "serve_mode": False,
            "app_mode": False,
            "serve_host": "127.0.0.1",
            "serve_port": 0,
            "executable_name": executable_name,
        }, None

    workspace_selector: str | None = None
    exec_task: str | None = None
    model_selector: str | None = None
    show_help = False
    serve_host = "127.0.0.1"
    serve_port = 0
    positionals: list[str] = []

    idx = 0
    while idx < len(filtered_argv):
        token = filtered_argv[idx]
        if token in ("-h", "--help"):
            show_help = True
            idx += 1
            continue
        if token in ("-m", "--model"):
            if idx + 1 >= len(filtered_argv):
                return None, "❌ Missing model name for -m/--model.\n" + usage_text
            model_selector = str(filtered_argv[idx + 1] or "").strip()
            if not model_selector:
                return None, "❌ Model name cannot be empty.\n" + usage_text
            idx += 2
            continue
        if token in ("-w", "--workspace"):
            if idx + 1 >= len(filtered_argv):
                return None, "❌ Missing workspace name for -w/--workspace.\n" + usage_text
            workspace_selector = str(filtered_argv[idx + 1] or "").strip()
            if not workspace_selector:
                return None, "❌ Workspace cannot be empty.\n" + usage_text
            idx += 2
            continue
        if token == "--host":
            if idx + 1 >= len(filtered_argv):
                return None, "❌ Missing value for --host.\n" + usage_text
            serve_host = str(filtered_argv[idx + 1] or "").strip() or "127.0.0.1"
            idx += 2
            continue
        if token == "--port":
            if idx + 1 >= len(filtered_argv):
                return None, "❌ Missing value for --port.\n" + usage_text
            raw_port = str(filtered_argv[idx + 1] or "").strip()
            try:
                serve_port = int(raw_port)
            except ValueError:
                return None, "❌ Port must be an integer.\n" + usage_text
            if serve_port < 0 or serve_port > 65535:
                return None, "❌ Port must be between 0 and 65535.\n" + usage_text
            idx += 2
            continue
        positionals.append(token)
        idx += 1

    serve_mode = False
    app_mode = False
    if positionals:
        if positionals[0] == "exec":
            if len(positionals) < 2:
                return None, "❌ Missing task text after exec.\n" + usage_text
            exec_task = " ".join(positionals[1:]).strip()
        elif positionals[0] == "serve":
            if len(positionals) > 1:
                return None, "❌ The serve command takes no positional arguments.\n" + usage_text
            serve_mode = True
        elif positionals[0] == "app":
            if len(positionals) > 1:
                return None, "❌ The app command takes no positional arguments.\n" + usage_text
            app_mode = True
        else:
            return None, "❌ Unsupported arguments.\n" + usage_text

    if exec_task is not None and not exec_task.strip():
        return None, "❌ Task text cannot be empty.\n" + usage_text

    return {
        "workspace_selector": workspace_selector,
        "exec_task": exec_task,
        "model_selector": model_selector,
        "show_help": show_help,
        "serve_mode": serve_mode,
        "app_mode": app_mode,
        "serve_host": serve_host,
        "serve_port": serve_port,
        "executable_name": executable_name,
    }, None


def _get_user_config_template_path() -> Path:
    """Return repository template path used to generate user config."""
    return project_root / CONFIG_TEMPLATE_RELATIVE_PATH


def _load_user_config_template() -> dict:
    """Load startup template content from cli/config/config.template.jsonc."""
    template_path = _get_user_config_template_path()
    data = load_config_jsonc(template_path)
    return data if isinstance(data, dict) else {}


def _starter_user_config() -> dict:
    """Minimal starter config written for a first-run TUI user.

    Defined inline (not read from the repo template file) so we ship a single,
    fill-in-the-blanks OpenAI provider example. Users edit the ``<YOUR ...>``
    placeholders; see https://github.com/reedyang/codewood for all options.
    """
    return {
        "model_providers": [
            {
                "provider": "OpenAI",
                "params": {
                    "api_key": "<YOUR API KEY>",
                    "base_url": "https://api.openai.com/v1",
                    "api_mode": "chat",
                    "models": [
                        {
                            "name": "<YOUR MODEL NAME>",
                            "context_window": "256k",
                            "streaming": True,
                            "multimodal": True,
                            "thinking": True,
                            "reasoning_effort": ["none", "low", "medium", "high", "xhigh", "max"],
                        }
                    ],
                },
            }
        ],
    }


def _create_user_config_template() -> Path:
    """Create ``~/.config/<app>/config.jsonc`` with a starter template.

    Returns the created config file path.
    """
    config_path = get_app_global_config_dir() / CONFIG_JSONC_FILENAME
    save_config_jsonc(config_path, _starter_user_config())
    return config_path


def _print_model_settings_update_notice(config_path: str | Path, language: str = DEFAULT_DISPLAY_LANGUAGE) -> None:
    normalized_path = str(Path(str(config_path)).expanduser())
    print(_ansi_red(text("main.update_model_settings", language, path=normalized_path)))


def _set_basic_chat_only_context_prompt_warning_for_agent(agent: Any) -> None:
    params = getattr(agent, "params", {}) or {}
    raw_context_window = params.get("context_window") if isinstance(params, dict) else None
    warning = basic_chat_only_context_warning(raw_context_window)
    if not warning:
        return
    set_warning = getattr(agent, "_set_pending_prompt_warning", None)
    if callable(set_warning):
        set_warning(warning)
    else:
        setattr(agent, "_pending_prompt_warning_line", warning)


def _print_startup_basic_overview(
    model_name: str = "(not configured)",
    workspace_name: str = "Default",
    workspace_dir: str | None = None,
) -> None:
    """Reuse the exact runtime startup overview renderer for consistent style/colors."""
    try:
        from cli.runtime.runtime_loop import _print_startup_overview

        _print_startup_overview(
            SimpleNamespace(
                model_name=str(model_name or "").strip() or "(not configured)",
                workspace_name=str(workspace_name or "").strip() or "Default",
                workspace_root=str(workspace_dir or "").strip() or str(Path.cwd()),
                _startup_chat_state_warning="",
            )
        )
    except Exception:
        # Best-effort fallback: avoid crashing early startup reminder paths.
        print(get_app_name())
        print("")


def _extract_model_runtime_config(config: dict, requested_model: str | None = None):
    """Extract runtime model config from model_providers, with optional startup model override."""
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, list) or not model_providers:
        return None, None, None, "❌ Configuration error: missing 'model_providers' configuration."

    catalog: list[dict[str, Any]] = []
    provider_entries: list[dict[str, Any]] = []
    for item in model_providers:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        params_raw = item.get("params", {})
        if not provider or not isinstance(params_raw, dict):
            continue
        parsed_models = parse_configured_models(params_raw)
        if not parsed_models:
            continue
        provider_entries.append(
            {
                "provider": provider,
                "params_raw": params_raw,
                "models": parsed_models,
            }
        )
        for model_item in parsed_models:
            model_name = str(model_item.get("name") or "").strip()
            if not model_name:
                continue
            catalog.append(
                {
                    "provider": provider,
                    "name": model_name,
                    "context_window": int(model_item.get("context_window") or 0),
                    "streaming": bool(model_item.get("streaming", True)),
                    "multimodal": bool(model_item.get("multimodal", True)),
                    "extra_headers": dict(model_item.get("extra_headers") or {}),
                    "params_raw": params_raw,
                    "provider_models": parsed_models,
                }
            )

    if not provider_entries:
        return None, None, None, "❌ Configuration error: model_providers entries are invalid or have no models."

    requested = str(requested_model or "").strip()
    selected: dict[str, Any] | None = None

    if not requested:
        first_provider = provider_entries[0]
        first_model = first_provider["models"][0]
        selected = {
            "provider": str(first_provider["provider"]),
            "name": str(first_model.get("name") or "").strip(),
            "context_window": int(first_model.get("context_window") or 0),
            "streaming": bool(first_model.get("streaming", True)),
            "multimodal": bool(first_model.get("multimodal", True)),
            "extra_headers": dict(first_model.get("extra_headers") or {}),
            "params_raw": first_provider["params_raw"],
            "provider_models": first_provider["models"],
        }
    elif "/" in requested:
        req_provider, req_name = requested.split("/", 1)
        req_provider = req_provider.strip().casefold()
        req_name = req_name.strip().casefold()
        for item in catalog:
            if str(item.get("provider") or "").casefold() == req_provider and str(item.get("name") or "").casefold() == req_name:
                selected = item
                break
        # Model names may include ":" (for example ollama names), so fallback to pure-name lookup.
        if selected is None:
            req_name_full = requested.casefold()
            matches = [
                item
                for item in catalog
                if str(item.get("name") or "").casefold() == req_name_full
            ]
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                selectors = ", ".join(
                    sorted({f"{m.get('provider')}/{m.get('name')}" for m in matches})
                )
                return (
                    None,
                    None,
                    None,
                    "❌ Configuration error: model name is ambiguous. "
                    f"Please use provider/model, candidates: {selectors}",
                )
            else:
                return (
                    None,
                    None,
                    None,
                    f"❌ Configuration error: model '{requested}' is not found in model_providers.",
                )
    else:
        req_name = requested.casefold()
        matches = [
            item
            for item in catalog
            if str(item.get("name") or "").casefold() == req_name
        ]
        if not matches:
            return (
                None,
                None,
                None,
                f"❌ Configuration error: model '{requested}' is not found in model_providers.",
            )
        if len(matches) > 1:
            selectors = ", ".join(
                sorted({f"{m.get('provider')}/{m.get('name')}" for m in matches})
            )
            return (
                None,
                None,
                None,
                "❌ Configuration error: model name is ambiguous. "
                f"Please use provider/model, candidates: {selectors}",
            )
        selected = matches[0]

    provider = str(selected.get("provider") or "").strip() if selected else ""
    model_name = str(selected.get("name") or "").strip() if selected else ""
    if not provider or not model_name:
        return None, None, None, "❌ Configuration error: selected provider/model is empty."

    params_raw = selected.get("params_raw", {}) if isinstance(selected, dict) else {}
    provider_models = selected.get("provider_models", []) if isinstance(selected, dict) else []
    params = dict(params_raw) if isinstance(params_raw, dict) else {}
    params["models"] = [
        str(item.get("name") or "").strip()
        for item in provider_models
        if str(item.get("name") or "").strip()
    ]
    params["model"] = model_name
    params["context_window"] = int(selected.get("context_window") or 0)
    params["streaming"] = bool(selected.get("streaming", True))
    params["multimodal"] = bool(selected.get("multimodal", True))
    params["extra_headers"] = dict(selected.get("extra_headers") or {})
    # The Ollama-native HTTP backend is selected via ``api_mode``;
    # ``provider`` is now just a label/prefix. Default the port for
    # any model whose effective ``api_mode`` resolves to ``ollama``,
    # whether that came from an explicit ``api_mode: "ollama"`` or
    # from the legacy ``provider: "ollama"`` shorthand.
    from cli.ai.ai_provider_clients import resolve_api_mode

    if resolve_api_mode(params=params, provider=provider) == "ollama":
        params["port"] = parse_port(params.get("port"), default_value=DEFAULT_OLLAMA_PORT)

    model_config = {
        "provider": provider,
        "params": params,
    }
    return provider, model_name, model_config, None


def _validate_template_placeholder_values(
    provider: str,
    model_name: str,
    model_config: dict,
    template_config: dict | None = None,
) -> str | None:
    """Ensure runtime config does not keep template placeholder values."""
    template_provider = ""
    template_api_key = ""
    template_model_name = ""
    try:
        effective_template = (
            template_config
            if isinstance(template_config, dict)
            else _load_user_config_template()
        )
        providers = effective_template.get("model_providers")
        if isinstance(providers, list) and providers:
            first_provider = providers[0]
            if isinstance(first_provider, dict):
                template_provider = str(first_provider.get("provider") or "").strip()
                template_params = first_provider.get("params", {})
                if isinstance(template_params, dict):
                    template_api_key = str(template_params.get("api_key") or "").strip()
                    parsed_models = parse_configured_models(template_params)
                    if parsed_models:
                        template_model_name = str(parsed_models[0].get("name") or "").strip()
    except Exception:
        return None

    issues = []
    runtime_params = model_config.get("params", {}) if isinstance(model_config, dict) else {}
    runtime_api_key = str(runtime_params.get("api_key") or "").strip()
    runtime_provider = str(provider or "").strip()
    runtime_model_name = str(model_name or "").strip()

    provider_matches_template = (
        (not template_provider)
        or runtime_provider.lower() == template_provider.lower()
    )
    if (
        template_api_key
        and runtime_api_key
        and runtime_api_key == template_api_key
        and provider_matches_template
    ):
        issues.append(
            f"api_key is still the template value ({template_api_key})."
        )
    if template_model_name and runtime_model_name and runtime_model_name == template_model_name:
        issues.append(
            f"model name is still the template value ({template_model_name})."
        )
    if not issues:
        return None
    return "template_placeholder_values_in_use"


def _apply_startup_workspace(agent: Any, selector: str | None) -> tuple[bool, str | None]:
    """Switch to a startup workspace by name/id/path."""
    raw = str(selector or "").strip()
    if not raw:
        return True, None

    entry = agent._workspace_entry_by_selector(raw)
    if entry is None:
        try:
            root = agent._workspace_path_from_arg(raw)
        except Exception:
            root = None
        if root is None or (not root.exists()) or (not root.is_dir()):
            return False, f"❌ Workspace '{raw}' not found by name/id/path."
        entry = agent._workspace_entry_by_root(root)
        if entry is None:
            workspace_id = agent._workspace_id_for_path(root)
            workspaces = agent._workspaces_state.setdefault("workspaces", {})
            if not isinstance(workspaces, dict):
                workspaces = {}
                agent._workspaces_state["workspaces"] = workspaces
            counter = 2
            base_id = workspace_id
            while workspace_id in workspaces:
                workspace_id = f"{base_id}_{counter}"
                counter += 1
            entry = {
                "id": workspace_id,
                "name": root.name or str(root),
                "kind": "custom",
                "root": str(root),
                "storage": str(root / get_app_config_dirname()),
            }
            workspaces[workspace_id] = entry

    agent._save_current_workspace_position()
    agent._apply_workspace_entry(entry, agent.work_directory)
    agent._refresh_workspace_runtime()
    # Post-apply: globals point at the new workspace but the session still
    # carries the previous chat; save position metadata only to avoid
    # duplicating its history into a same-id chat of the new workspace.
    agent._save_current_workspace_position(sync_messages=False)
    return True, None


def _apply_startup_model_override(
    agent: Any,
    selector: str | None,
) -> tuple[bool, str | None]:
    """Force runtime+active-chat model to selector when user passed -m/--model."""
    requested = str(selector or "").strip()
    if not requested:
        return True, None
    try:
        result = str(agent._switch_model_by_selector(requested) or "").strip()
    except Exception as exc:
        return False, f"❌ Failed to apply startup model '{requested}': {exc}"
    if result.startswith("❌"):
        return False, result
    return True, None


_GUI_DETACHED_ENV = "CODEWOOD_GUI_DETACHED"


def _launched_from_explorer() -> bool:
    """Return True when our nearest non-self ancestor is Explorer (Windows).

    Used to tell a double-click / shortcut launch apart from a run inside an
    existing shell. We walk the parent-process chain and skip our own
    executable's frames (PyInstaller one-file builds run as a bootloader
    process plus an app child, so the immediate parent is usually our own
    exe) until we reach the first foreign process: ``explorer.exe`` means a
    double-click; a shell (cmd/powershell/…) means a terminal launch.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            return False
        ppid_by_pid: dict[int, int] = {}
        name_by_pid: dict[int, str] = {}
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while ok:
                pid = int(entry.th32ProcessID)
                ppid_by_pid[pid] = int(entry.th32ParentProcessID)
                name_by_pid[pid] = entry.szExeFile.decode("ascii", "ignore").lower()
                ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

        own_names = {
            "codewood.exe",
            "python.exe",
            "pythonw.exe",
            os.path.basename(sys.executable).lower(),
        }
        current = ppid_by_pid.get(os.getpid())
        seen: set[int] = set()
        while current and current not in seen:
            seen.add(current)
            name = name_by_pid.get(current, "")
            if name and name not in own_names:
                return name == "explorer.exe"
            current = ppid_by_pid.get(current)
    except Exception:
        return False
    return False


def _hide_owned_console_window() -> None:
    """Hide the console window for a double-click launch (Windows only).

    A double-click of the console-mode executable allocates a console that
    we don't want flashing on screen while the GUI starts. When launched
    from an existing terminal we leave the console alone so we never hide
    the user's own shell.
    """
    if not _launched_from_explorer():
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _free_own_console() -> None:
    """Hide and detach from this process's own console (Windows only).

    The detached GUI child is a console-subsystem one-file build, so Windows
    gives it a private console. We hide its window immediately and then call
    ``FreeConsole`` to destroy it outright, leaving the GUI with no console
    window at all (and no window the user could close to kill the GUI).
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE before freeing to avoid a flash
        kernel32.FreeConsole()
    except Exception:
        pass


def _spawn_detached_gui() -> int:
    """Re-launch ourselves as a detached, console-free GUI process.

    This lets ``codewood app`` (and a double-click) return control to the
    caller immediately: the parent exits while the GUI keeps running in an
    independent process with no console window attached.
    """
    import subprocess

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "app"]
        cwd = str(Path(sys.executable).resolve().parent)
    else:
        cmd = [sys.executable, str(project_root / "cli" / "main.py"), "app"]
        cwd = str(project_root)

    # Strip PyInstaller's private bootstrap variables (_PYI*/_MEIPASS2) so the
    # child — itself a frozen one-file build — resolves its own bundle instead
    # of inheriting the parent's extraction directory (which breaks imports and
    # the bundled .NET runtime used by the WebView2 backend).
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("_PYI") or k.startswith("_MEI"))}
    env[_GUI_DETACHED_ENV] = "1"
    if os.name == "nt":
        env.setdefault("PYTHONNET_RUNTIME", "netfx")

    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW (rather than DETACHED_PROCESS) so the child — itself
        # a console-subsystem one-file build — never gets a visible console
        # window; the child also frees its console on startup. NEW_PROCESS_GROUP
        # keeps it independent of the launching terminal's Ctrl+C.
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    # Hide our own console first so a double-click launch never flashes a
    # window before the detached GUI takes over (no-op when run from a shell).
    _hide_owned_console_window()
    try:
        subprocess.Popen(  # noqa: S603 - launching our own trusted executable
            cmd,
            cwd=cwd,
            env=env,
            close_fds=True,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"❌ Failed to launch the desktop GUI: {exc}")
        return 1
    return 0


def _log_gui_error(message: str) -> None:
    """Best-effort error log for the detached GUI process (no console)."""
    try:
        import tempfile

        log_path = Path(tempfile.gettempdir()) / "codewood-gui-error.log"
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except Exception:
        pass


def _is_missing_webview_backend_error(exc: BaseException) -> bool:
    """True when the GUI failed only because no native webview backend exists.

    On Linux, pywebview needs GTK (PyGObject + WebKit2) or Qt (qtpy) Python
    bindings, which are system packages absent from a bare ``pip`` venv — e.g.
    a default WSL install. pywebview signals this with a message naming both
    backends; importing ``webview`` can also fail outright. We treat these as a
    soft failure so the caller can fall back to the terminal UI instead of
    aborting. Windows always ships WebView2, so this never matches there.
    """
    if os.name == "nt":
        return False
    text_blob = f"{type(exc).__name__}: {exc}".lower()
    needles = (
        "qt or gtk",
        "gtk with python",
        "pywebview",
        "no module named 'webview'",
        "no module named 'gi'",
        "no module named 'qtpy'",
    )
    return any(n in text_blob for n in needles)


def _launch_gui_app() -> int | None:
    """Launch the desktop GUI host (which spawns the backend serve process).

    The GUI host modules live under ``desktop/host``. In a frozen build they
    are bundled as data under ``<_MEIPASS>/host`` (see build/pack.bat); in
    development they are imported directly from the source tree.

    Returns the GUI exit code, or ``None`` to signal the caller that no native
    webview backend is available (Linux without GTK/Qt) and it should fall
    back to the terminal UI.
    """
    if os.environ.get(_GUI_DETACHED_ENV) == "1":
        # The detached child owns a private console; destroy it so the GUI
        # has no console window attached to it.
        _free_own_console()
    else:
        _hide_owned_console_window()

    # Force pywebview's EdgeChromium backend to host the .NET Framework
    # runtime (always present on Windows 10/11). Without this, pythonnet may
    # auto-select coreclr and intermittently fail with "Failed to create a
    # .NET runtime (coreclr)", especially inside the frozen one-file build.
    if os.name == "nt":
        os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")

    if getattr(sys, "frozen", False):
        host_dir = os.path.join(getattr(sys, "_MEIPASS", ""), "host")
    else:
        host_dir = str(project_root / "desktop" / "host")
    if host_dir and host_dir not in sys.path:
        sys.path.insert(0, host_dir)
    try:
        import gui  # type: ignore

        return int(gui.main() or 0)
    except Exception as exc:  # pragma: no cover - defensive
        # No native webview backend (typically Linux/WSL without GTK or Qt):
        # don't abort — fall back to the terminal UI. Returning ``None`` lets
        # the caller continue into the TUI path.
        if _is_missing_webview_backend_error(exc):
            note = (
                "⚠ Desktop GUI unavailable (no GTK/Qt webview backend); "
                "falling back to the terminal UI.\n"
                "  To enable the GUI on Linux, install the system webview "
                "bindings, e.g. on Debian/Ubuntu:\n"
                "    sudo apt install python3-gi python3-gi-cairo "
                "gir1.2-webkit2-4.1 gir1.2-gtk-3.0"
            )
            if os.environ.get(_GUI_DETACHED_ENV) == "1":
                _log_gui_error(note)
            else:
                print(note)
            return None
        message = f"❌ Failed to launch the desktop GUI: {exc}"
        if os.environ.get(_GUI_DETACHED_ENV) == "1":
            _log_gui_error(message)
        else:
            print(message)
        return 1


def _resolve_gui_launch(cli_args: dict) -> int | None:
    """Decide whether to launch the GUI, and how.

    Returns an exit code when the GUI path handles the run, or ``None`` to
    fall through to the terminal UI. The GUI is launched only when ``app`` is
    requested explicitly; a double-click of the executable now falls through
    to the terminal UI (TUI) instead of auto-launching the GUI.
    """
    detached_child = os.environ.get(_GUI_DETACHED_ENV) == "1"
    app_requested = bool(cli_args.get("app_mode", False))

    if not app_requested:
        return None

    # The detached child (or a dev run) runs the GUI inline; a frozen,
    # still-attached launch re-spawns itself detached so the caller's prompt
    # returns immediately and no console window lingers.
    if detached_child or not getattr(sys, "frozen", False):
        return _launch_gui_app()
    return _spawn_detached_gui()


def _force_utf8_std_streams() -> None:
    """Make stdout/stderr tolerate non-ASCII output on every platform.

    On a Chinese (or other non-UTF-8) Windows install, Python defaults its
    console streams to the legacy ANSI code page (e.g. GBK/cp936). The agent
    and startup banners legitimately print Unicode symbols such as ``⚠``
    (U+26A0) and ``❌`` (U+274C); writing those to a GBK stream raises
    ``UnicodeEncodeError`` and, in serve mode, kills the backend before it can
    emit its handshake — surfacing only as "Backend exited before sending a
    handshake" in the GUI.

    Reconfigure both streams to UTF-8 with ``errors="replace"`` so unknown
    glyphs degrade to a placeholder instead of crashing the process. Wrapped
    in best-effort guards: ``reconfigure`` exists on Python 3.7+ text streams
    but may be absent when stdout has been replaced by a non-standard object.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _serve_without_valid_model(
    cli_args: Any,
    config_dir: Optional[str],
    work_directory: Optional[str],
    builtin_skills_dir: Optional[str],
) -> int:
    """Start the GUI backend with a placeholder agent (no valid model).

    Used when the GUI launches but ``config.jsonc`` is missing or has no
    usable model: instead of aborting (which would surface as a backend
    error in the GUI), we bring up the server so the main UI loads. The
    frontend detects the empty model catalog and shows a centered modal
    guiding the user into Model settings, where they can configure a
    provider and have it applied without restarting.
    """
    from cli.agent import Agent
    from cli.server.serve_app import ServeApp

    serve_host = str(cli_args.get("serve_host") or "127.0.0.1") if isinstance(cli_args, dict) else "127.0.0.1"
    serve_port = int(cli_args.get("serve_port") or 0) if isinstance(cli_args, dict) else 0

    agent = None
    try:
        # Construct with the constructor defaults (placeholder model). The
        # agent can't run turns until a model is configured, but it can serve
        # state, browse settings, and persist a new model_providers list.
        agent = Agent(
            work_directory=work_directory,
            config_dir=config_dir,
            builtin_skills_dir=builtin_skills_dir,
        )
        try:
            _apply_startup_workspace(agent, None)
        except Exception:
            pass
        _set_basic_chat_only_context_prompt_warning_for_agent(agent)
        return ServeApp(agent).run(host=serve_host, port=serve_port)
    finally:
        if agent is not None:
            try:
                agent.shutdown(wait=False)
            except Exception:
                pass


def main(argv: list[str] | None = None):
    """Main function."""
    # Harden the console encoding before ANYTHING prints, so a non-UTF-8
    # system locale can't crash startup on the first Unicode symbol.
    _force_utf8_std_streams()
    restore_app_console_title()

    # Prepend the bundled ``bin/`` directory to PATH so pre-shipped
    # executables such as ``rg.exe`` resolve transparently in shell
    # commands the agent (or any subprocess we spawn) runs. Doing this
    # once here is enough — every later ``subprocess.Popen`` /
    # ``subprocess.run`` call either inherits ``os.environ`` directly
    # or copies it (``env=os.environ.copy()``), so the prepended path
    # is visible everywhere, including inside pipelines and compound
    # commands where the per-command rg head-rewrite cannot reach.
    prepend_bundled_bin_to_path()
    # Ensure ripgrep (rg) binary is present; if not, start a background
    # download from GitHub releases for the current platform.
    from cli.config.rg_downloader import ensure_rg_async
    ensure_rg_async(get_app_bundled_bin_dir())
    # On Windows, also append the Git-for-Windows tool directories so
    # the model can reach GNU userland (bash, grep, sed, awk, curl,
    # ssh, …) when they're installed but the launching shell didn't
    # put them on PATH. Appended at the tail to preserve System32
    # priority for ``find.exe``/``sort.exe`` and friends.
    append_windows_git_tools_to_path()

    raw_argv = list(argv) if argv is not None else []
    cli_args, cli_error = _parse_startup_cli_args(raw_argv)
    if cli_error:
        print(cli_error)
        return 1
    if isinstance(cli_args, dict) and bool(cli_args.get("show_help", False)):
        executable_name = str(cli_args.get("executable_name") or "python cli/main.py").strip()
        print(_format_startup_help(executable_name=executable_name))
        return 0

    # GUI launch short-circuits before any config/model work: it only starts
    # the desktop host, which spawns its own ``serve`` backend process that
    # performs the real configuration loading. This also covers double-click
    # launches of the frozen executable (see _resolve_gui_launch).
    if isinstance(cli_args, dict):
        gui_exit_code = _resolve_gui_launch(cli_args)
        if gui_exit_code is not None:
            return gui_exit_code

    work_directory = None
    config = None
    config_path = None
    ui_language = DEFAULT_DISPLAY_LANGUAGE
    
    # The application config lives at ``~/.config/<app>/config.jsonc``. There is
    # no fallback to a ``.codewood`` directory in the home dir or the code root.
    user_config = str(get_app_global_config_dir() / CONFIG_JSONC_FILENAME)

    config_dir = None  # Config directory used for history persistence
    # Built-in Agent Skills live at the project root, outside cli/.
    # When frozen by PyInstaller, __file__ resolves to sys._MEIPASS + "/main.py"
    # (without the "cli/" prefix), so project_root / "skills" would be wrong.
    # Use sys._MEIPASS (the _internal/ directory) when available.
    if getattr(sys, "frozen", False):
        builtin_skills_dir = str(Path(sys._MEIPASS) / "skills")
    else:
        builtin_skills_dir = str(project_root / "skills")

    if os.path.exists(user_config):
        config_path = user_config
        config_dir = os.path.dirname(user_config)  # Get the directory that contains the config file.
    
    if config_path:
        try:
            config = load_config_jsonc(Path(config_path))
            config = resolve_string_values_in_data(config)
            if isinstance(config, dict):
                ui_language = normalize_display_language(config.get("language")) or DEFAULT_DISPLAY_LANGUAGE
        except Exception as e:
            print(_ansi_red(text("main.config_read_failed", ui_language, error=e)))
            config = None

    if config_dir:
        from cli.core.logging.app_logging import get_logger, setup_app_logging
        setup_app_logging(Path(config_dir))
        get_logger().info("%s started, config_dir=%s", get_app_name(), config_dir)
    
    serve_mode = bool(cli_args.get("serve_mode", False)) if isinstance(cli_args, dict) else False

    if not config:
        # In GUI/serve mode we must not abort and must NOT create a template
        # config file. Start the backend with a placeholder (no-model) agent so
        # the GUI shows the main UI and guides the user into Model settings.
        if serve_mode:
            return _serve_without_valid_model(cli_args, config_dir, work_directory, builtin_skills_dir)
        # TUI mode: create a single starter template config (only when none
        # exists) and point the user at the docs, then exit so they can fill it.
        _print_startup_basic_overview()
        if not config_path:
            try:
                created_path = _create_user_config_template()
                print(_ansi_red(text("main.config_created_template", ui_language)))
                print(text("main.config_template_docs_hint", ui_language, url=PROJECT_DOCS_URL))
                _print_model_settings_update_notice(created_path, ui_language)
            except Exception as e:
                print(_ansi_red(text("main.config_create_template_failed", ui_language, error=e)))
                _print_model_settings_update_notice(get_app_global_config_dir() / CONFIG_JSONC_FILENAME, ui_language)
        else:
            _print_model_settings_update_notice(config_path, ui_language)
        return 1
    model_selector = ""
    if isinstance(cli_args, dict):
        model_selector = str(cli_args.get("model_selector") or "").strip()
    provider, model_name, model_config, config_error = _extract_model_runtime_config(
        config,
        requested_model=model_selector or None,
    )
    if config_error:
        _print_startup_basic_overview()
        _print_model_settings_update_notice(config_path or (get_app_global_config_dir() / CONFIG_JSONC_FILENAME), ui_language)
        if serve_mode:
            return _serve_without_valid_model(cli_args, config_dir, work_directory, builtin_skills_dir)
        return 1
    template_value_error = _validate_template_placeholder_values(
        provider=provider,
        model_name=model_name,
        model_config=model_config,
    )
    if template_value_error:
        _print_startup_basic_overview(model_name=model_name)
        _print_model_settings_update_notice(config_path or (get_app_global_config_dir() / CONFIG_JSONC_FILENAME), ui_language)
        if serve_mode:
            return _serve_without_valid_model(cli_args, config_dir, work_directory, builtin_skills_dir)
        return 1

    params = model_config.get("params", {})
    model_override_selector = ""
    if model_selector:
        model_override_selector = f"{provider}/{model_name}"

    # Load the heavy agent module only after configuration is ready to reduce the wait between startup and model info.
    from cli.agent import Agent

    workspace_selector = ""
    exec_task = ""
    if isinstance(cli_args, dict):
        workspace_selector = str(cli_args.get("workspace_selector") or "").strip()
        exec_task = str(cli_args.get("exec_task") or "").strip()

    # ``provider`` is now just a label/prefix; the OpenAI-compatible
    # vs Ollama-native HTTP path is selected by ``api_mode`` inside
    # the AI client. ``main()`` therefore takes a single launch path
    # regardless of provider name. Ollama is intentionally NOT
    # imported here — callers that don't need it never load the
    # package; the actual validation runs in a background thread
    # inside the Agent.
    if not params:
        print(text("main.model_provider_unsupported", ui_language, provider=provider))
        return 1
    agent = None
    try:
        agent = Agent(
            model_name=model_name,
            work_directory=work_directory,
            provider=provider,
            params=params,
            model_config=model_config,
            config_dir=config_dir,
            builtin_skills_dir=builtin_skills_dir,
        )
        ok, ws_error = _apply_startup_workspace(agent, workspace_selector or None)
        if not ok:
            print(text("main.startup_workspace_failed", ui_language) if not ws_error else str(ws_error))
            return 1
        ok, model_error = _apply_startup_model_override(agent, model_override_selector or None)
        if not ok:
            print(text("main.startup_model_override_failed", ui_language) if not model_error else str(model_error))
            return 1
        _set_basic_chat_only_context_prompt_warning_for_agent(agent)
        if isinstance(cli_args, dict) and bool(cli_args.get("serve_mode", False)):
            from cli.server.serve_app import ServeApp

            serve_host = str(cli_args.get("serve_host") or "127.0.0.1")
            serve_port = int(cli_args.get("serve_port") or 0)
            return ServeApp(agent).run(host=serve_host, port=serve_port)
        if exec_task:
            agent._queued_user_input = exec_task
            agent._startup_exec_turn_pending = True
        agent.run()
        return 0
    except KeyboardInterrupt:
        print(text("main.program_exited", ui_language))
        return 0
    except Exception as e:
        print(text("main.runtime_error", ui_language, error=str(e)))
        # In serve mode the GUI only ever sees the handshake line; an
        # exception here means it never came, so record the traceback to the
        # app log to make "Backend exited before sending a handshake"
        # diagnosable after the fact.
        if config_dir:
            try:
                from cli.core.logging.app_logging import get_logger

                get_logger().exception("Backend startup failed before handshake")
            except Exception:
                pass
        return 1
    finally:
        if agent is not None:
            try:
                agent.shutdown(wait=False)
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:])) 
