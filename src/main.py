#!/usr/bin/env python3
"""
Smart Shell main entry point.

Usage:
    python src/main.py   # Run with model settings from the config file
"""

import sys
import os
from pathlib import Path
from types import SimpleNamespace

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
from src.core.config.model_providers import parse_configured_models
from src.core.console_utils import _ansi_red


DEFAULT_USER_CONFIG_TEMPLATE = {
    "model_providers": [
        {
            "provider": "openai",
            "params": {
                "api_key": "<YOUR API KEY>",
                "base_url": "https://api.openai.com/v1",
                "models": [
                    {
                        "name": "<YOUR MODEL NAME>",
                        "context_window": 131072,
                    }
                ],
            },
        }
    ],
    "execution_policy": "moderate",
    "project_context_first_round_evidence": True,
    "max_tool_rounds": None,
    "memory_enabled": False,
    "mcp_tools_enabled": False,
}


def _create_user_config_template(user_home: Path) -> Path:
    """Create ~/.smartshell/config.jsonc with a starter template and return the file path."""
    config_path = user_home / ".smartshell" / CONFIG_JSONC_FILENAME
    save_config_jsonc(config_path, DEFAULT_USER_CONFIG_TEMPLATE)
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
        print("Smart Shell")
        print("")


def _extract_model_runtime_config(config: dict):
    """Extract runtime model config from the new model_providers format."""
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, list) or not model_providers:
        return None, None, None, "❌ Configuration error: missing 'model_providers' configuration."

    first_provider = model_providers[0]
    if not isinstance(first_provider, dict):
        return None, None, None, "❌ Configuration error: model_providers[0] must be an object."

    provider = str(first_provider.get("provider", "")).strip()
    params_raw = first_provider.get("params", {})
    if not isinstance(params_raw, dict):
        return None, None, None, "❌ Configuration error: model_providers[0].params must be an object."

    parsed_models = parse_configured_models(params_raw)
    if not parsed_models:
        return None, None, None, "❌ Configuration error: model_providers[0].params.models is missing or empty."

    first_model = parsed_models[0]
    model_name = str(first_model.get("name") or "").strip()
    if not provider or not model_name:
        return None, None, None, "❌ Configuration error: model_providers[0].provider or the first model is empty."

    params = dict(params_raw)
    params["models"] = [str(item.get("name") or "").strip() for item in parsed_models]
    params["model"] = model_name
    params["context_window"] = int(first_model.get("context_window") or 0)

    model_config = {
        "provider": provider,
        "params": params,
    }
    return provider, model_name, model_config, None


def _validate_template_placeholder_values(
    provider: str,
    model_name: str,
    model_config: dict,
) -> str | None:
    """Ensure runtime config does not keep template placeholder values."""
    template_provider = ""
    template_api_key = ""
    template_model_name = ""
    try:
        providers = DEFAULT_USER_CONFIG_TEMPLATE.get("model_providers")
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


def _set_windows_console_title():
    """Set a Unicode console title on Windows without relying on batch encoding."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(f"Smart Shell")
    except Exception:
        pass


def main():
    """Main function."""
    _set_windows_console_title()

    work_directory = None
    config = None
    config_path = None
    
    # 优先查找用户主目录下的.smartshell/config.jsonc
    user_home = str(Path.home())
    user_config = os.path.join(user_home, ".smartshell", CONFIG_JSONC_FILENAME)
    local_config = os.path.join(str(project_root), ".smartshell", CONFIG_JSONC_FILENAME)
    
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
        get_logger().info("Smart Shell started, config_dir=%s", config_dir)
    
    if not config:
        _print_startup_basic_overview()
        if not config_path:
            try:
                created_path = _create_user_config_template(Path.home())
                print(_ansi_red("Config file not found. Created template successfully."))
                _print_model_settings_update_notice(created_path)
            except Exception as e:
                print(_ansi_red(f"Config file not found, and failed to create template: {e}"))
                _print_model_settings_update_notice(Path.home() / ".smartshell" / CONFIG_JSONC_FILENAME)
        else:
            _print_model_settings_update_notice(config_path)
        return 1
    provider, model_name, model_config, config_error = _extract_model_runtime_config(config)
    if config_error:
        _print_startup_basic_overview()
        _print_model_settings_update_notice(config_path or (Path.home() / ".smartshell" / CONFIG_JSONC_FILENAME))
        return 1
    template_value_error = _validate_template_placeholder_values(
        provider=provider,
        model_name=model_name,
        model_config=model_config,
    )
    if template_value_error:
        _print_startup_basic_overview(model_name=model_name)
        _print_model_settings_update_notice(config_path or (Path.home() / ".smartshell" / CONFIG_JSONC_FILENAME))
        return 1

    params = model_config.get("params", {})

    # 配置就绪后再加载重型 agent 模块，缩短「启动」到「模型信息」之间的等待
    from src.smart_shell_agent import SmartShellAgent

    if provider == "openai" and params:
        agent = None
        try:
            agent = SmartShellAgent(
                model_name=model_name,
                work_directory=work_directory,
                provider="openai",
                params=params,
                model_config=model_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
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
        # ollama：不在此处 import ollama（未使用 ollama 的配置不会加载该包）；校验在 SmartShellAgent 后台线程中完成
        agent = None
        try:
            agent = SmartShellAgent(
                model_name=model_name,
                work_directory=work_directory,
                provider="ollama",
                params=params,
                model_config=model_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
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
    sys.exit(main()) 
