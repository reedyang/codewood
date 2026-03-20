#!/usr/bin/env python3
"""
Smart Shell主启动脚本

用法：
    python main.py       # 使用配置文件中的模型配置
"""

import sys
import json
import os
from pathlib import Path

# 添加agent目录到Python路径
current_dir = Path(__file__).parent
agent_dir = current_dir / "agent"
sys.path.insert(0, str(agent_dir))

from agent.smart_shell_agent import SmartShellAgent

def main():
    """主函数"""
    print("启动 Smart Shell...")
    
    work_directory = None
    config = None
    config_path = None
    
    # 优先查找用户主目录下的.smartshell/config.json
    user_home = str(Path.home())
    user_config = os.path.join(user_home, ".smartshell/config.json")
    local_config = os.path.join(current_dir, ".smartshell/config.json")
    
    config_dir = None  # 配置文件目录，用于历史记录保存
    # Built-in Agent Skills: <main.py 所在目录>/skills/（与 config 侧 skills 合并时，外部优先）
    builtin_skills_dir = str(current_dir / "skills")

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
        # ollama本地
        try:
            import ollama
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
                print(f"⚠️ 指定模型 {model_name} 不可用")
                if available_models:
                    model_name = available_models[0]
                    print(f"💡 使用默认模型: {model_name}")
                else:
                    print("❌ 没有可用的模型")
                    return 1
        except ImportError:
            print("❌ 请先安装 ollama 包: pip install ollama")
            return 1
        except Exception as e:
            print(f"❌ 无法连接到Ollama: {str(e)}")
            print("请确保Ollama服务正在运行")
            return 1
        try:
            agent = SmartShellAgent(
                model_name=model_name,
                work_directory=work_directory,
                provider="ollama",
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