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


def _set_windows_console_title():
    """Set a Unicode console title on Windows without relying on batch encoding."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(f"{chr(0x1F916)} Smart Shell")
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
        except Exception as e:
            print(f"⚠️ 配置文件读取失败: {e}")
            config = None

    if config_dir:
        from src.app_logging import get_logger, setup_app_logging
        setup_app_logging(Path(config_dir))
        get_logger().info("Smart Shell 启动，config_dir=%s", config_dir)
    
    # 解析配置
    normal_config = None
    vision_config = None
    
    if config:
        # 检查是否为新的双模型配置格式
        if "normal_model" in config:
            normal_config = config.get("normal_model", {})
            provider = normal_config.get('provider', 'unknown')
            params = normal_config.get('params', {})
            model_name = params.get('model', 'unknown')
            print(f"普通任务模型: {normal_config.get('provider', 'unknown')} - {normal_config.get('params', {}).get('model', 'unknown')}")

        if "vision_model" in config:
            vision_config = config.get("vision_model", {})
            print(f"视觉模型: {vision_config.get('provider', 'unknown')} - {vision_config.get('params', {}).get('model', 'unknown')}")
        else:
            print("未配置视觉模型, 不支持视觉任务")

        if not normal_config:
            print("未配置普通任务模型")
            return 1
        
    else:
        # 默认配置
        print("📋 未找到配置文件")
        return 1

    # 配置就绪后再加载重型 agent 模块，缩短「启动」到「模型信息」之间的等待
    from src.smart_shell_agent import SmartShellAgent

    # 如果使用双模型配置
    if normal_config and vision_config:
        try:
            agent = SmartShellAgent(
                work_directory=work_directory,
                normal_config=normal_config,
                vision_config=vision_config,
                config_dir=config_dir,
                builtin_skills_dir=builtin_skills_dir,
            )
            agent.run()
            return 0
        except Exception as e:
            print(f"❌ 双模型配置运行错误: {str(e)}")
            return 1
    
    # 启动 Agent
    if provider == "openai" and params:
        try:
            agent = SmartShellAgent(
                model_name=model_name,
                work_directory=work_directory,
                provider="openai",
                params=params,
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
