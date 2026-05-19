#!/usr/bin/env python3
"""
Smart Shell主启动脚本

用法：
    python src/main.py   # 使用配置文件中的模型配置
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


def _extract_model_runtime_config(config: dict):
    """Extract runtime model config from the new model_providers format."""
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, list) or not model_providers:
        return None, None, None, "❌ 配置错误：缺少 model_providers 配置"

    first_provider = model_providers[0]
    if not isinstance(first_provider, dict):
        return None, None, None, "❌ 配置错误：model_providers[0] 必须是对象"

    provider = str(first_provider.get("provider", "")).strip()
    params_raw = first_provider.get("params", {})
    if not isinstance(params_raw, dict):
        return None, None, None, "❌ 配置错误：model_providers[0].params 必须是对象"

    models = params_raw.get("models")
    if not isinstance(models, list) or not models:
        return None, None, None, "❌ 配置错误：model_providers[0].params.models 缺失或为空"

    model_name = str(models[0]).strip()
    if not provider or not model_name:
        return None, None, None, "❌ 配置错误：model_providers[0].provider 或首个 model 为空"

    params = dict(params_raw)
    params["model"] = model_name

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
    """主函数"""
    _set_windows_console_title()
    print("启动 Smart Shell...")

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
            print(f"⚠️ 配置文件读取失败: {e}")
            config = None

    if config_dir:
        from src.core.logging.app_logging import get_logger, setup_app_logging
        setup_app_logging(Path(config_dir))
        get_logger().info("Smart Shell 启动，config_dir=%s", config_dir)
    
    if not config:
        # 默认配置
        print("📋 未找到配置文件")
        return 1
    provider, model_name, model_config, config_error = _extract_model_runtime_config(config)
    if config_error:
        print(config_error)
        return 1

    params = model_config.get("params", {})
    print(f"模型: {provider} - {model_name}")

    # 配置就绪后再加载重型 agent 模块，缩短「启动」到「模型信息」之间的等待
    from src.smart_shell_agent import SmartShellAgent

    if provider == "openai" and params:
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
            print(f"❌ OpenAI API模式运行错误: {str(e)}")
            return 1
    elif provider == "openwebui" and params:
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
            print(f"❌ OpenWebUI API模式运行错误: {str(e)}")
            return 1
    elif provider == "ollama" and params:
        # ollama：不在此处 import ollama（未使用 ollama 的配置不会加载该包）；校验在 SmartShellAgent 后台线程中完成
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
            print("\n👋 程序已退出")
            return 0
        except Exception as e:
            print(f"❌ 运行错误: {str(e)}")
            return 1
    else:
        print(f"模型 provider {provider} 不被支持")
        return 1

if __name__ == "__main__":
    sys.exit(main()) 
