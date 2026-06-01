#!/usr/bin/env python3
"""
Application main entry point.

Usage:
    python src/main.py   # Run with model settings from the config file
"""

import sys
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Add the project root to Python path so the src package imports consistently
# whether this file is launched as a script or imported by tests.
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))
from src.core.config.config_env import resolve_string_values_in_data
from src.core.config.config_jsonc import (
    CONFIG_JSONC_FILENAME,
    load_config_jsonc,
    save_config_jsonc,
)
from src.config.app_info import get_app_config_dirname, get_app_name, get_app_version
from src.core.config.model_providers import parse_configured_models
from src.core.console_utils import _ansi_red
from src.core.console_title import restore_app_console_title

CONFIG_TEMPLATE_RELATIVE_PATH = Path("src/config") / "config.template.jsonc"


def _format_startup_usage() -> str:
    return _format_startup_usage_with_executable("python src/main.py")


def _format_startup_usage_with_executable(executable_name: str) -> str:
    command = str(executable_name or "").strip() or "python src/main.py"
    return (
        "Usage:\n"
        f"  {command} [OPTIONS] [WORKSPACE]\n"
        f"  {command} [OPTIONS] [WORKSPACE] <COMMAND> [PROMPT]"
    )


def _format_startup_help(executable_name: str = "python src/main.py") -> str:
    return (
        f"Version: {get_app_version()}\n"
        f"{_format_startup_usage_with_executable(executable_name)}\n"
        "\n"
        "Commands:\n"
        f"  exec                       Run {get_app_name()} and execute your prompt non-interactively, then exit\n"
        "\n"
        "Arguments:\n"
        "  [WORKSPACE]                Workspace name or path to enter on startup\n"
        "  [PROMPT]                   Prompt text used by the exec command\n"
        "\n"
        "Options:\n"
        "  -m, --model <MODEL>        Select startup model (for example: openai:gpt-4o-mini)\n"
        f"  -h, --help                 Print help for {get_app_name()} and exit\n"
    )


def _parse_startup_cli_args(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse startup CLI args with flexible ordering."""
    executable_name = "python src/main.py"
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
            "executable_name": executable_name,
        }, None

    workspace_selector: str | None = None
    exec_task: str | None = None
    model_selector: str | None = None
    show_help = False
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
        positionals.append(token)
        idx += 1

    if positionals:
        if positionals[0] == "exec":
            if len(positionals) < 2:
                return None, "❌ Missing task text after exec.\n" + usage_text
            exec_task = " ".join(positionals[1:]).strip()
        else:
            workspace_selector = positionals[0]
            if len(positionals) >= 2:
                if positionals[1] != "exec":
                    return None, "❌ Unsupported arguments.\n" + usage_text
                if len(positionals) < 3:
                    return None, "❌ Missing task text after exec.\n" + usage_text
                exec_task = " ".join(positionals[2:]).strip()

    if exec_task is not None and not exec_task.strip():
        return None, "❌ Task text cannot be empty.\n" + usage_text

    return {
        "workspace_selector": workspace_selector,
        "exec_task": exec_task,
        "model_selector": model_selector,
        "show_help": show_help,
        "executable_name": executable_name,
    }, None


def _get_user_config_template_path() -> Path:
    """Return repository template path used to generate user config."""
    return project_root / CONFIG_TEMPLATE_RELATIVE_PATH


def _load_user_config_template() -> dict:
    """Load startup template content from src/config/config.template.jsonc."""
    template_path = _get_user_config_template_path()
    data = load_config_jsonc(template_path)
    return data if isinstance(data, dict) else {}


def _create_user_config_template(user_home: Path) -> Path:
    """Create ~/.<app>/config.jsonc with a starter template and return the file path."""
    config_path = user_home / get_app_config_dirname() / CONFIG_JSONC_FILENAME
    save_config_jsonc(config_path, _load_user_config_template())
    return config_path


def _print_model_settings_update_notice(config_path: str | Path) -> None:
    normalized_path = str(Path(str(config_path)).expanduser())
    print(_ansi_red(f"Please update the model settings in: {normalized_path}"))


def _print_startup_basic_overview(
    model_name: str = "(not configured)",
    workspace_name: str = "Default",
    workspace_dir: str | None = None,
) -> None:
    """Reuse the exact runtime startup overview renderer for consistent style/colors."""
    try:
        from src.runtime.runtime_loop import _print_startup_overview

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
                    "use_simulated_tools": bool(
                        model_item.get("use_simulated_tools", False)
                    ),
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
            "use_simulated_tools": bool(
                first_model.get("use_simulated_tools", False)
            ),
            "params_raw": first_provider["params_raw"],
            "provider_models": first_provider["models"],
        }
    elif ":" in requested:
        req_provider, req_name = requested.split(":", 1)
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
                    sorted({f"{m.get('provider')}:{m.get('name')}" for m in matches})
                )
                return (
                    None,
                    None,
                    None,
                    "❌ Configuration error: model name is ambiguous. "
                    f"Please use provider:model, candidates: {selectors}",
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
                sorted({f"{m.get('provider')}:{m.get('name')}" for m in matches})
            )
            return (
                None,
                None,
                None,
                "❌ Configuration error: model name is ambiguous. "
                f"Please use provider:model, candidates: {selectors}",
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
    params["use_simulated_tools"] = bool(
        selected.get("use_simulated_tools", False)
    )

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
    agent._save_current_workspace_position()
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


def main(argv: list[str] | None = None):
    """Main function."""
    restore_app_console_title()

    raw_argv = list(argv) if argv is not None else []
    cli_args, cli_error = _parse_startup_cli_args(raw_argv)
    if cli_error:
        print(cli_error)
        return 1
    if isinstance(cli_args, dict) and bool(cli_args.get("show_help", False)):
        executable_name = str(cli_args.get("executable_name") or "python src/main.py").strip()
        print(_format_startup_help(executable_name=executable_name))
        return 0

    work_directory = None
    config = None
    config_path = None
    
    # 优先查找用户主目录下的应用配置目录/config.jsonc
    user_home = str(Path.home())
    config_dirname = get_app_config_dirname()
    user_config = os.path.join(user_home, config_dirname, CONFIG_JSONC_FILENAME)
    local_config = os.path.join(str(project_root), config_dirname, CONFIG_JSONC_FILENAME)
    
    config_dir = None  # 配置文件目录，用于历史记录保存
    # Built-in Agent Skills live at the project root, outside src/.
    builtin_skills_dir = str(project_root / "skills")

    if os.path.exists(user_config):
        config_path = user_config
        config_dir = os.path.dirname(user_config)  # 获取配置文件所在目录
    elif os.path.exists(local_config):
        config_path = local_config
        config_dir = os.path.dirname(local_config)  # 获取配置文件所在目录
    
    if config_path:
        try:
            config = load_config_jsonc(Path(config_path))
            config = resolve_string_values_in_data(config)
        except Exception as e:
            print(_ansi_red(f"Failed to read config file: {e}"))
            config = None

    if config_dir:
        from src.core.logging.app_logging import get_logger, setup_app_logging
        setup_app_logging(Path(config_dir))
        get_logger().info("%s started, config_dir=%s", get_app_name(), config_dir)
    
    if not config:
        _print_startup_basic_overview()
        if not config_path:
            try:
                created_path = _create_user_config_template(Path.home())
                print(_ansi_red("Config file not found. Created template successfully."))
                _print_model_settings_update_notice(created_path)
            except Exception as e:
                print(_ansi_red(f"Config file not found, and failed to create template: {e}"))
                _print_model_settings_update_notice(Path.home() / get_app_config_dirname() / CONFIG_JSONC_FILENAME)
        else:
            _print_model_settings_update_notice(config_path)
        return 1
    model_selector = ""
    if isinstance(cli_args, dict):
        model_selector = str(cli_args.get("model_selector") or "").strip()
    model_override_selector = ""
    if model_selector:
        model_override_selector = f"{provider}:{model_name}" if False else ""
    provider, model_name, model_config, config_error = _extract_model_runtime_config(
        config,
        requested_model=model_selector or None,
    )
    if config_error:
        _print_startup_basic_overview()
        _print_model_settings_update_notice(config_path or (Path.home() / get_app_config_dirname() / CONFIG_JSONC_FILENAME))
        return 1
    template_value_error = _validate_template_placeholder_values(
        provider=provider,
        model_name=model_name,
        model_config=model_config,
    )
    if template_value_error:
        _print_startup_basic_overview(model_name=model_name)
        _print_model_settings_update_notice(config_path or (Path.home() / get_app_config_dirname() / CONFIG_JSONC_FILENAME))
        return 1

    params = model_config.get("params", {})
    model_override_selector = ""
    if model_selector:
        model_override_selector = f"{provider}:{model_name}"

    # 配置就绪后再加载重型 agent 模块，缩短「启动」到「模型信息」之间的等待
    from src.agent import Agent

    workspace_selector = ""
    exec_task = ""
    if isinstance(cli_args, dict):
        workspace_selector = str(cli_args.get("workspace_selector") or "").strip()
        exec_task = str(cli_args.get("exec_task") or "").strip()

    if provider == "openai" and params:
        agent = None
        try:
            agent = Agent(
                model_name=model_name,
                work_directory=work_directory,
                provider="openai",
                params=params,
                model_config=model_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
            ok, ws_error = _apply_startup_workspace(agent, workspace_selector or None)
            if not ok:
                print(str(ws_error or "❌ Failed to apply startup workspace."))
                return 1
            ok, model_error = _apply_startup_model_override(agent, model_override_selector or None)
            if not ok:
                print(str(model_error or "❌ Failed to apply startup model override."))
                return 1
            if exec_task:
                agent._queued_user_input = exec_task
                agent._startup_exec_turn_pending = True
            agent.run()
            return 0
        except Exception as e:
            print(f"❌ OpenAI API mode runtime error: {str(e)}")
            return 1
        finally:
            if agent is not None:
                try:
                    agent.shutdown(wait=False)
                except Exception:
                    pass
    elif provider == "ollama" and params:
        # ollama：不在此处 import ollama（未使用 ollama 的配置不会加载该包）；校验在 Agent 后台线程中完成
        agent = None
        try:
            agent = Agent(
                model_name=model_name,
                work_directory=work_directory,
                provider="ollama",
                params=params,
                model_config=model_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
            ok, ws_error = _apply_startup_workspace(agent, workspace_selector or None)
            if not ok:
                print(str(ws_error or "❌ Failed to apply startup workspace."))
                return 1
            ok, model_error = _apply_startup_model_override(agent, model_override_selector or None)
            if not ok:
                print(str(model_error or "❌ Failed to apply startup model override."))
                return 1
            if exec_task:
                agent._queued_user_input = exec_task
                agent._startup_exec_turn_pending = True
            agent.run()
            return 0
        except KeyboardInterrupt:
            print("\n👋 Program exited")
            return 0
        except Exception as e:
            print(f"❌ Runtime error: {str(e)}")
            return 1
        finally:
            if agent is not None:
                try:
                    agent.shutdown(wait=False)
                except Exception:
                    pass
    else:
        print(f"Model provider '{provider}' is not supported")
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:])) 
