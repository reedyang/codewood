#!/usr/bin/env python3
"""
Smart Shell main entry point.

Usage:
    python src/main.py   # Run with model settings from the config file
"""

import sys
import json
import os
from pathlib import Path

# Add the project root to Python path so the src package imports consistently
# whether this file is launched as a script or imported by tests.
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))
from src.core.config.config_env import resolve_string_values_in_data
from src.core.config.model_providers import parse_configured_models


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
    
    # 优先查找用户主目录下的.smartshell/config.json
    user_home = str(Path.home())
    user_config = os.path.join(user_home, ".smartshell/config.json")
    local_config = os.path.join(project_root, ".smartshell/config.json")
    
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
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            config = resolve_string_values_in_data(config)
        except Exception as e:
            print(f"⚠️ Failed to read config file: {e}")
            config = None

    if config_dir:
        from src.core.logging.app_logging import get_logger, setup_app_logging
        setup_app_logging(Path(config_dir))
        get_logger().info("Smart Shell started, config_dir=%s", config_dir)
    
    if not config:
        # 默认配置
        print("📋 Config file not found")
        return 1
    provider, model_name, model_config, config_error = _extract_model_runtime_config(config)
    if config_error:
        print(config_error)
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
    elif provider == "openwebui" and params:
        agent = None
        try:
            agent = SmartShellAgent(
                model_name=model_name,
                work_directory=work_directory,
                provider="openwebui",
                params=params,
                model_config=model_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
            agent.run()
            return 0
        except Exception as e:
            print(f"❌ OpenWebUI API mode runtime error: {str(e)}")
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
