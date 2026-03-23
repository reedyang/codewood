import ollama
import os
import sys
import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, Set
import shutil
from datetime import datetime

def _decode_subprocess_output(data: Optional[bytes]) -> str:
    """
    Decode shell stdout/stderr: prefer UTF-8 (Python tools / baidu_search.py), else system locale.
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


# 导入历史记录管理器
from .history_manager import HistoryManager
from .skills_loader import build_skills_routing_prefix, build_skills_system_append, load_skills_merged

# Import knowledge manager; KNOWLEDGE_AVAILABLE is set by knowledge_manager (e.g. False when ChromaDB fails on Python 3.14)
try:
    from .knowledge_manager import KnowledgeManager, KNOWLEDGE_AVAILABLE
except ImportError:
    KnowledgeManager = None  # type: ignore
    KNOWLEDGE_AVAILABLE = False
    print("⚠️ 知识库功能不可用")

# 导入tab补全模块
import os
import platform

# 根据操作系统选择合适的输入处理器
if platform.system() == "Windows":
    try:
        from .windows_input import create_windows_input_handler
        TAB_COMPLETION_AVAILABLE = True
        INPUT_HANDLER_TYPE = "windows"
    except ImportError:
        TAB_COMPLETION_AVAILABLE = False
        INPUT_HANDLER_TYPE = "none"
else:
    try:
        from .tab_completer import create_tab_completer
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


def _ansi_red(text: str) -> str:
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return text
    return f"\033[31m{text}\033[0m"


def _ansi_yellow(text: str) -> str:
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return text
    return f"\033[33m{text}\033[0m"


class SmartShellAgent:
    def __init__(self, model_name: str = "gemma3:4b", work_directory: Optional[str] = None, provider: str = "ollama", openai_conf: Optional[dict] = None, openwebui_conf: Optional[dict] = None, params: Optional[dict] = None, normal_config: Optional[dict] = None, vision_config: Optional[dict] = None, config_dir: Optional[str] = None, builtin_skills_dir: Optional[str] = None):
        """
        初始化Smart Shell
        Args:
            model_name: 模型名称（兼容旧格式）
            work_directory: 工作目录
            provider: 模型服务提供方（兼容旧格式）
            openai_conf: openai参数（兼容旧格式）
            openwebui_conf: openwebui参数（兼容旧格式）
            params: 通用参数（兼容旧格式）
            normal_config: 普通任务模型配置（新格式）
            vision_config: 视觉模型配置（新格式）
            config_dir: 配置文件目录（可选，用于指定历史记录保存位置）
            builtin_skills_dir: 内建 Agent Skills 根目录（通常为 main.py 同目录下的 skills/）；未传则使用 agent 包上级目录的 skills/
        """
        self.work_directory = Path(work_directory) if work_directory else Path.cwd()
        self.conversation_history = []
        self.operation_results = []
        # Session-local paths created by action "script"; may be auto-removed after shell runs them
        self._ephemeral_script_paths: Set[str] = set()
        # All path keys for files AI created this session (scripts + outputs detected from shell), for freedom auto-confirm
        self._ai_created_path_keys: Set[str] = set()
        # Basename of last ephemeral script auto-removed after shell (avoid redundant delete + freedom prompt)
        self._last_auto_removed_ephemeral: Optional[str] = None
        
        # 初始化历史记录管理器，使用指定的配置目录或自动查找
        if config_dir:
            # 使用指定的配置目录
            self.history_manager = HistoryManager(config_dir)
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
                
            self.history_manager = HistoryManager(str(config_dir))
            self.config_dir = Path(config_dir)

        # AI-generated script files live under config_dir/workspace/; shell cwd stays work_directory so outputs go there.
        self.ai_workspace_dir = self.config_dir / "workspace"
        try:
            self.ai_workspace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⚠️ 无法创建 AI workspace 目录 {self.ai_workspace_dir}: {e}")

        # 加载配置以确定知识库开关（默认开启）、自由模式开关（默认关闭）
        self.knowledge_enabled = True
        self.freedom_enabled = False
        try:
            cfg_path = self.config_dir / "config.json"
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg_data = json.load(f)
                self.knowledge_enabled = bool(cfg_data.get("knowledge_enabled", True))
                self.freedom_enabled = bool(cfg_data.get("freedom_enabled", False))
        except Exception as e:
            print(f"⚠️ 读取配置中的知识库开关失败，默认开启: {e}")
        
        # 初始化知识库管理器
        self.knowledge_manager = None
        if KNOWLEDGE_AVAILABLE and self.knowledge_enabled:
            try:
                # 使用轻量级的中文向量模型
                embedding_model = "nomic-embed-text"
                self.knowledge_manager = KnowledgeManager(str(config_dir), embedding_model)
                # 启动时同步知识库
                self.knowledge_manager.sync_knowledge_base()
            except Exception as e:
                print(f"⚠️ 知识库初始化失败: {e}")
                self.knowledge_manager = None

        # 继续初始化其余组件（双模型配置、系统提示词、输入处理器）
        if normal_config and vision_config:
            self.dual_model_mode = True
            self.normal_config = normal_config
            self.vision_config = vision_config
            # 设置普通任务模型
            self.normal_provider = normal_config.get("provider", "ollama")
            self.normal_params = normal_config.get("params", {})
            self.normal_model_name = self.normal_params.get("model", "gemma3:4b")
            # 设置视觉模型
            self.vision_provider = vision_config.get("provider", "ollama")
            self.vision_params = vision_config.get("params", {})
            self.vision_model_name = self.vision_params.get("model", "qwen2.5vl:7b")
            # 兼容旧接口
            self.model_name = self.normal_model_name
            self.provider = self.normal_provider
            self.params = self.normal_params
            self.openai_conf = self.normal_params if self.normal_provider == "openai" else None
            self.openwebui_conf = self.normal_params if self.normal_provider == "openwebui" else None
        else:
            # 兼容旧格式
            self.dual_model_mode = False
            self.model_name = model_name
            self.provider = provider
            self.openai_conf = openai_conf
            self.openwebui_conf = openwebui_conf
            self.params = params
            # 兼容params统一配置
            if self.provider == 'openai' and self.openai_conf is None and params is not None:
                self.openai_conf = params
            if self.provider == 'openwebui' and self.openwebui_conf is None and params is not None:
                self.openwebui_conf = params

        # 验证模型
        self._validate_model()

        # 系统提示词
        prompt_path = os.path.join(os.path.dirname(__file__), 'system_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            self.system_prompt = f.read()

        self._builtin_skills_root = (
            Path(builtin_skills_dir).expanduser().resolve()
            if builtin_skills_dir
            else Path(__file__).resolve().parent.parent / "skills"
        )
        self.skills = load_skills_merged(self.config_dir, self._builtin_skills_root)
        self._skills_routing_prefix = build_skills_routing_prefix(self.skills)
        self._skills_system_append = build_skills_system_append(self.skills)

        # 初始化输入处理器，确保属性存在
        self.input_handler = None
        if TAB_COMPLETION_AVAILABLE:
            try:
                if INPUT_HANDLER_TYPE == "windows":
                    # 构建初始历史供 prompt_toolkit 使用
                    try:
                        initial_history = self.history_manager.get_all_history()
                    except Exception:
                        initial_history = []
                    self.input_handler = create_windows_input_handler(self.work_directory, initial_history)
                elif INPUT_HANDLER_TYPE == "readline":
                    self.input_handler = create_tab_completer(self.work_directory)
                else:
                    print("⚠️ 未知的输入处理器类型")
            except Exception as e:
                print(f"⚠️ 输入处理器初始化失败: {e}")
        else:
            print("⚠️ Tab补全功能不可用")

    def _save_knowledge_enabled_to_config(self) -> bool:
        """将知识库开关状态保存到 config.json"""
        try:
            cfg_path = self.config_dir / "config.json"
            cfg_data = {}
            if cfg_path.exists():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["knowledge_enabled"] = bool(self.knowledge_enabled)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ 保存知识库开关到配置失败: {e}")
            return False

    def _enable_knowledge(self) -> Dict[str, Any]:
        """开启知识库功能，持久化并即时生效"""
        if self.knowledge_enabled and self.knowledge_manager is not None:
            return {"success": True, "message": "知识库已处于开启状态"}
        self.knowledge_enabled = True
        saved = self._save_knowledge_enabled_to_config()
        if not KNOWLEDGE_AVAILABLE:
            if sys.version_info >= (3, 14):
                return {"success": False, "error": "知识库依赖 ChromaDB 当前不兼容 Python 3.14，请使用 Python 3.12 或 3.13 以启用知识库"}
            return {"success": False, "error": "缺少知识库依赖，无法启用（请执行 pip install -r requirements.txt）"}
        try:
            embedding_model = "nomic-embed-text"
            self.knowledge_manager = KnowledgeManager(str(self.config_dir), embedding_model)
            self.knowledge_manager.sync_knowledge_base()
            return {"success": True, "message": f"知识库已开启{'（已保存配置）' if saved else ''}"}
        except Exception as e:
            self.knowledge_manager = None
            return {"success": False, "error": f"启用知识库失败: {e}"}

    def _disable_knowledge(self) -> Dict[str, Any]:
        """关闭知识库功能，持久化并即时生效"""
        if not self.knowledge_enabled and self.knowledge_manager is None:
            return {"success": True, "message": "知识库已处于关闭状态"}
        self.knowledge_enabled = False
        saved = self._save_knowledge_enabled_to_config()
        # 释放引用（让底层资源由GC清理）
        self.knowledge_manager = None
        return {"success": True, "message": f"知识库已关闭{'（已保存配置）' if saved else ''}"}

    def _save_freedom_enabled_to_config(self) -> bool:
        """将自由模式开关状态保存到 config.json"""
        try:
            cfg_path = self.config_dir / "config.json"
            cfg_data = {}
            if cfg_path.exists():
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg_data = json.load(f) or {}
                except Exception:
                    cfg_data = {}
            cfg_data["freedom_enabled"] = bool(self.freedom_enabled)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"⚠️ 保存自由模式开关到配置失败: {e}")
            return False

    def _enable_freedom(self) -> Dict[str, Any]:
        """开启自由模式：可逆操作在需确认前由 AI 判定，可逆则自动执行"""
        if self.freedom_enabled:
            return {"success": True, "message": "自由模式已处于开启状态"}
        self.freedom_enabled = True
        saved = self._save_freedom_enabled_to_config()
        return {
            "success": True,
            "message": f"自由模式已开启：可逆操作将自动跳过确认{'（已保存配置）' if saved else ''}",
        }

    def _disable_freedom(self) -> Dict[str, Any]:
        if not self.freedom_enabled:
            return {"success": True, "message": "自由模式已处于关闭状态"}
        self.freedom_enabled = False
        saved = self._save_freedom_enabled_to_config()
        return {"success": True, "message": f"自由模式已关闭{'（已保存配置）' if saved else ''}"}

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

    def _parse_safe_auto_response(self, text: str) -> Tuple[bool, str]:
        """Parse script freedom review JSON: {\"safe_auto\": bool, \"reason\": ...}."""
        if not text or not isinstance(text, str):
            return False, "空响应"
        s = text.strip()
        if s.startswith("❌"):
            return False, s[:120]
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
                            if "safe_auto" in obj:
                                r = obj["safe_auto"]
                                if isinstance(r, str):
                                    r = r.strip().lower() in ("true", "1", "yes", "是")
                                reason = str(obj.get("reason", "")).strip()[:240]
                                ok = bool(r)
                                return ok, (reason or ("允许自动" if ok else "需确认"))
                        except json.JSONDecodeError:
                            pass
                        break
        return False, "无法解析脚本审查结果"

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

    def _ai_assess_script_freedom(self, script_path: Path, content: str) -> Tuple[bool, str]:
        """Ask classifier whether script only touches AI/workspace outputs and not system config."""
        keys = sorted(self._ai_created_path_keys)[:120]
        payload = (
            f"work_directory={self.work_directory.resolve()}\n"
            f"ai_workspace_dir={self.ai_workspace_dir.resolve()}\n"
            f"os={os.name}\n"
            f"ai_tracked_path_keys_normalized={json.dumps(keys, ensure_ascii=False)}\n"
            f"script_file={script_path.resolve()}\n\n"
            f"--- script source ---\n{content}\n--- end ---"
        )
        raw = self.call_ai(
            payload,
            context="",
            stream=False,
            include_knowledge=False,
            freedom_script_review=True,
        )
        if not isinstance(raw, str):
            return False, "模型返回类型异常"
        return self._parse_safe_auto_response(raw)

    def _ai_assess_reversible(self, command: Dict[str, Any]) -> Tuple[bool, str]:
        payload = json.dumps(command, ensure_ascii=False)
        raw = self.call_ai(
            payload, context="", stream=False, include_knowledge=False, minimal_classifier=True
        )
        if not isinstance(raw, str):
            return False, "模型返回类型异常"
        if raw.strip().startswith("❌"):
            return False, raw.strip()[:120]
        return self._parse_reversibility_response(raw)

    def _freedom_auto_confirm(self, command: Dict[str, Any]) -> bool:
        """Return True to skip interactive confirmation (move/delete/shell/script/git write)."""
        if not getattr(self, "freedom_enabled", False):
            return False
        action = command.get("action")
        params = command.get("params") or {}

        if action == "script":
            print("🦅 自由模式：创建/覆盖脚本为会话内操作，跳过确认。")
            return True

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
                k = self._ephemeral_path_key(sp)
                # AI-written script file: review source for non-AI file damage / system config
                if k in self._ephemeral_script_paths and sp.is_file():
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
                    print("🦅 自由模式：正在审查脚本是否仅影响工作区/AI 产出且无系统级副作用…")
                    safe, sreason = self._ai_assess_script_freedom(sp, body)
                    if safe:
                        print(f"🦅 脚本审查通过 — {sreason}")
                        return True
                    print(f"🦅 脚本审查未通过自动执行 — {sreason}")
                    reversible, reason = self._ai_assess_reversible(command)
                    if reversible:
                        print(f"🦅 操作级可逆判定 — {reason}")
                    else:
                        print(f"🦅 操作级判定 — {reason}")
                    return reversible

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

    def _validate_model(self):
        """验证模型是否可用（仅ollama模式）"""
        if self.dual_model_mode:
            # 双模型模式：验证两个模型
            self._validate_single_model(self.normal_provider, self.normal_model_name, "普通任务模型")
            self._validate_single_model(self.vision_provider, self.vision_model_name, "视觉模型")
        else:
            # 单模型模式：验证单个模型
            self._validate_single_model(self.provider, self.model_name, "模型")
    
    def _validate_single_model(self, provider: str, model_name: str, model_type: str):
        """验证单个模型是否可用"""
        if provider != "ollama":
            return
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
                print(f"⚠️ 警告: {model_type} '{model_name}' 不在可用模型列表中")
                print(f"📋 可用模型: {available_models}")
                if available_models:
                    print(f"💡 建议使用: {available_models[0]}")
                print(f"💡 请检查 llm-filemgr.json 中的 {model_type.lower().replace('模型', '_model')} 配置")
        except ImportError:
            print(f"❌ 错误: 未安装 ollama 包，无法验证 {model_type}。请运行: pip install ollama")
        except Exception as e:
            print(f"⚠️ 验证{model_type}时出错: {e}")
            print(f"💡 请确保 Ollama 服务正在运行")

    def call_ai(
        self,
        user_input: str,
        context: str = "",
        stream: bool = False,
        include_knowledge: bool = True,
        minimal_classifier: bool = False,
        freedom_script_review: bool = False,
    ):
        """调用大模型API获取AI回复，支持流式输出。stream=True时返回生成器"""
        try:
            # 确保os未被局部变量遮蔽
            import os
            os_info = os.uname() if hasattr(os, 'uname') else os.name
            date_time = datetime.now().strftime("%Y-%m-%d %A %H:%M:%S")

            if freedom_script_review:
                if stream:
                    return "❌ 错误：脚本自由模式审查不支持流式模式。"
                script_reviewer_system = (
                    "You review script source BEFORE it runs (Smart Shell freedom mode). "
                    'Reply with ONLY one JSON object (no markdown code fence): '
                    '{"safe_auto": true or false, "reason": "brief Chinese"}. '
                    "safe_auto=true ONLY if the script is unlikely to: "
                    "(1) modify or delete files except under work_directory, under ai_workspace_dir (config-side workspace for AI intermediates), "
                    "and except files listed as ai_tracked_path_keys (session AI-created), or clearly NEW outputs under those dirs; "
                    "(2) modify system configuration: Windows registry/services/firewall/hosts/machine env, Linux /etc system files, etc. "
                    "Reading a user path (e.g. CSV) without deleting it, while writing new files only under work_directory or ai_workspace_dir, is usually safe_auto=true. "
                    "When uncertain, set safe_auto=false."
                )
                messages = [
                    {"role": "system", "content": script_reviewer_system},
                    {
                        "role": "user",
                        "content": (
                            f"当前操作系统: {os_info}\n本地时间: {date_time}\n\n{user_input}"
                        ),
                    },
                ]
                record_history = False
            elif minimal_classifier:
                if stream:
                    return "❌ 错误：内部可逆性判定不支持流式模式。"
                classifier_system = (
                    "You classify smart-shell JSON commands for reversibility. "
                    "Reply with ONLY one JSON object (no markdown code fence): "
                    '{"reversible": true or false, "reason": "brief"}. '
                    "reversible=true only if the user can undo without permanent data loss, or the operation is read-only. "
                    "Typically reversible: move within workspace; mkdir; git status/log/diff/show; harmless shell (dir/ls/type/cat). "
                    "Creating directory junctions/symlinks (Windows mklink /J or /D, Unix ln -s) is reversible: "
                    "undo is removing the link only; the target directory contents are not deleted by removing the link. "
                    "script action that only writes a new helper file is reversible (delete the file to undo). "
                    "shell running a local .bat/.cmd/.ps1 that only creates junctions/symlinks or lists files is reversible. "
                    "Typically NOT reversible: delete/rmtree, batch delete, shell with rm -rf / del critical / format / diskpart, "
                    "git push/commit/merge/rebase/reset/checkout/cherry-pick that changes repo state, "
                    "script or shell that overwrites or wipes unique user data, ffmpeg when unique data would be lost. "
                    "When uncertain, set reversible to false."
                )
                messages = [
                    {"role": "system", "content": classifier_system},
                    {
                        "role": "user",
                        "content": (
                            f"当前工作目录: {self.work_directory}\n操作系统: {os_info}\n本地时间: {date_time}\n"
                            f"待判定命令 JSON:\n{user_input}"
                        ),
                    },
                ]
                record_history = False
            else:
                record_history = True
                messages = [
                    {
                        "role": "system",
                        "content": (
                            f"{self._skills_routing_prefix}{self.system_prompt}\n{self._skills_system_append}"
                            f"当前操作系统信息：{os_info}\n当前日期时间：{date_time}"
                        ),
                    }
                ]
                for msg in self.conversation_history[-5:]:
                    messages.append(msg)

                # 从知识库获取相关上下文（可开关）
                knowledge_context = ""
                if include_knowledge:
                    # 若允许查询但管理器为空，尝试懒加载初始化一次（需开关开启且依赖可用）
                    if self.knowledge_manager is None and getattr(self, 'knowledge_enabled', True) and KNOWLEDGE_AVAILABLE:
                        try:
                            embedding_model = "nomic-embed-text"
                            self.knowledge_manager = KnowledgeManager(str(self.config_dir), embedding_model)
                            # 尝试同步（若已同步会做快速检查）
                            self.knowledge_manager.sync_knowledge_base()
                        except Exception as e:
                            # 初始化失败则保持为空，并继续不使用知识库
                            self.knowledge_manager = None
                            print(f"⚠️ 知识库懒加载初始化失败: {e}")
                    if self.knowledge_manager:
                        try:
                            print("🔎 正在查询知识库...")
                            knowledge_context = self.knowledge_manager.get_knowledge_context(user_input)
                            if knowledge_context:
                                print("📚 从知识库检索到相关信息")
                            else:
                                print("ℹ️ 知识库未找到相关信息")
                        except Exception as e:
                            print(f"⚠️ 知识库检索失败: {e}")

                current_input = f"当前工作目录: {self.work_directory}\n"
                if self.operation_results:
                    current_input += f"最近的操作结果: {self.operation_results[-1]}\n"
                if context:
                    current_input += f"操作上下文: {context}\n"
                if knowledge_context and knowledge_context != "":
                    current_input += f"知识库相关信息:\n{knowledge_context}\n"
                current_input += f"用户输入: {user_input}"
                messages.append({"role": "user", "content": current_input})

            # 根据模式选择模型配置
            if self.dual_model_mode:
                # 双模型模式：使用普通任务模型
                provider = self.normal_provider
                model_name = self.normal_model_name
                params = self.normal_params
                openai_conf = params if provider == "openai" else None
                openwebui_conf = params if provider == "openwebui" else None
                
                # 检查普通任务模型配置
                if not provider or not model_name:
                    return "❌ 错误：普通任务模型未正确配置。请检查 llm-filemgr.json 中的 normal_model 配置。"
            else:
                # 单模型模式：使用原有配置
                provider = self.provider
                model_name = self.model_name
                openai_conf = self.openai_conf
                openwebui_conf = self.openwebui_conf
                
                # 检查单模型配置
                if not provider or not model_name:
                    return "❌ 错误：模型未正确配置。请检查 llm-filemgr.json 配置文件。"

            if provider == "openai" and openai_conf:
                import requests
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                api_key = openai_conf.get("api_key")
                base_url = openai_conf.get("base_url", "https://api.openai.com/v1")
                model = model_name
                
                # 检查OpenAI配置
                if not api_key:
                    return "❌ 错误：OpenAI API密钥未配置。请在 llm-filemgr.json 中设置 api_key。"
                
                url = base_url.rstrip("/") + "/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": stream
                }
                resp = requests.post(url, headers=headers, json=payload, verify=False, timeout=120, stream=stream)
                resp.raise_for_status()
                if stream:
                    def gen():
                        buffer = ""
                        first_chunk = True
                        for line in resp.iter_lines():
                            if not line or not line.startswith(b"data: "):
                                continue
                            data = line[6:]
                            if data.strip() == b"[DONE]":
                                break
                            try:
                                data_str = data.decode('utf-8', errors='replace')
                                delta = json.loads(data_str)["choices"][0]["delta"].get("content", "")
                                if delta:
                                    # 如果是第一个chunk，去除开头的空白字符
                                    if first_chunk:
                                        delta = delta.lstrip()
                                        first_chunk = False
                                    if delta:  # 再次检查是否为空
                                        buffer += delta
                                        yield delta
                            except Exception:
                                continue
                        if record_history:
                            self.conversation_history.append({"role": "user", "content": user_input})
                            self.conversation_history.append({"role": "assistant", "content": buffer})
                    return gen()
                else:
                    data = resp.json()
                    ai_response = data["choices"][0]["message"]["content"]
                    if record_history:
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": ai_response})
                    return ai_response
            elif provider == "openwebui" and openwebui_conf:
                import requests
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                api_key = openwebui_conf.get("api_key")
                base_url = openwebui_conf.get("base_url", "http://localhost:8080/v1")
                model = model_name
                
                # 检查OpenWebUI配置
                if not api_key:
                    return "❌ 错误：OpenWebUI API密钥未配置。请在 llm-filemgr.json 中设置 api_key。"
                
                url = base_url.rstrip("/") + "/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": stream
                }
                resp = requests.post(url, headers=headers, json=payload, verify=False, timeout=120, stream=stream)
                resp.raise_for_status()
                if stream:
                    def gen():
                        buffer = ""
                        first_chunk = True
                        for line in resp.iter_lines(decode_unicode=True):
                            if not line or not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                                if delta:
                                    # 如果是第一个chunk，去除开头的空白字符
                                    if first_chunk:
                                        delta = delta.lstrip()
                                        first_chunk = False
                                    if delta:  # 再次检查是否为空
                                        buffer += delta
                                        yield delta
                            except Exception:
                                continue
                        if record_history:
                            self.conversation_history.append({"role": "user", "content": user_input})
                            self.conversation_history.append({"role": "assistant", "content": buffer})
                    return gen()
                else:
                    data = resp.json()
                    ai_response = data["choices"][0]["message"]["content"]
                    if record_history:
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": ai_response})
                    return ai_response
            else:
                # 检查是否为Ollama提供者
                if provider != "ollama":
                    return f"❌ 错误：不支持的模型提供者 '{provider}'。支持的提供者：ollama, openai, openwebui"
                
                try:
                    import ollama
                except ImportError:
                    return "❌ 错误：未安装 ollama 包。请运行：pip install ollama"
                
                if stream:
                    response = ollama.chat(
                        model=model_name,
                        messages=messages,
                        stream=True
                    )
                    def gen():
                        buffer = ""
                        first_chunk = True
                        for chunk in response:
                            delta = chunk.get("message", {}).get("content", "")
                            if delta:
                                # 如果是第一个chunk，去除开头的空白字符
                                if first_chunk:
                                    delta = delta.lstrip()
                                    first_chunk = False
                                if delta:  # 再次检查是否为空
                                    buffer += delta
                                    yield delta
                        if record_history:
                            self.conversation_history.append({"role": "user", "content": user_input})
                            self.conversation_history.append({"role": "assistant", "content": buffer})
                    return gen()
                else:
                    response = ollama.chat(
                        model=model_name,
                        messages=messages,
                        stream=False
                    )
                    ai_response = response['message']['content']
                    if record_history:
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": ai_response})
                    return ai_response
        except Exception as e:
            error_msg = f"调用大模型API时出错: {str(e)} (provider: {provider}, model: {model_name})"
            return error_msg

    def call_ai_multimodal(self, user_input: str, image_path: str, context: str = "", stream: bool = False):
        """调用支持多模态的大模型API进行图片分析，支持流式输出"""
        try:
            import os
            import base64
            os_info = os.uname() if hasattr(os, 'uname') else os.name
            
            # 读取并编码图片
            with open(image_path, 'rb') as image_file:
                image_data = base64.b64encode(image_file.read()).decode('utf-8')
            
            # 构建多模态消息 - 使用简化的系统提示，避免生成JSON命令
            system_prompt = """你是一个图片分析助手。请直接分析用户提供的图片，描述图片中的内容、物体、场景、文字等信息。不要生成任何JSON命令或代码，只提供自然语言的分析结果。"""
            
            messages = [{"role": "system", "content": system_prompt}]
            
            # 添加包含图片的消息 - 使用正确的Ollama格式
            messages.append({
                "role": "user", 
                "content": user_input,
                "images": [image_data]
            })

            # 根据模式选择模型配置
            if self.dual_model_mode:
                # 双模型模式：使用视觉模型
                provider = self.vision_provider
                model_name = self.vision_model_name
                params = self.vision_params
                openai_conf = params if provider == "openai" else None
                openwebui_conf = params if provider == "openwebui" else None
                
                # 检查视觉模型配置
                if not provider or not model_name:
                    return "❌ 错误：视觉模型未正确配置。请检查 llm-filemgr.json 中的 vision_model 配置。"
            else:
                # 单模型模式：使用原有配置
                provider = self.provider
                model_name = self.model_name
                openai_conf = self.openai_conf
                openwebui_conf = self.openwebui_conf
                
                # 检查单模型配置
                if not provider or not model_name:
                    return "❌ 错误：模型未正确配置。请检查 llm-filemgr.json 配置文件。"

            if provider == "ollama":
                try:
                    import ollama
                except ImportError:
                    return "❌ 错误：未安装 ollama 包。请运行：pip install ollama"
                
                if stream:
                    response = ollama.chat(
                        model=model_name,
                        messages=messages,
                        stream=True
                    )
                    def gen():
                        buffer = ""
                        first_chunk = True
                        for chunk in response:
                            delta = chunk.get("message", {}).get("content", "")
                            if delta:
                                # 如果是第一个chunk，去除开头的空白字符
                                if first_chunk:
                                    delta = delta.lstrip()
                                    first_chunk = False
                                if delta:  # 再次检查是否为空
                                    buffer += delta
                                    yield delta
                        self.conversation_history.append({"role": "user", "content": user_input})
                        self.conversation_history.append({"role": "assistant", "content": buffer})
                    return gen()
                else:
                    response = ollama.chat(
                        model=model_name,
                        messages=messages,
                        stream=False
                    )
                    ai_response = response['message']['content']
                    self.conversation_history.append({"role": "user", "content": user_input})
                    self.conversation_history.append({"role": "assistant", "content": ai_response})
                    return ai_response
            else:
                # 对于不支持多模态的提供者，回退到文本模式
                return f"⚠️ 警告：{provider} 提供者不支持多模态功能，回退到文本模式。\n" + self.call_ai(user_input, context, stream, include_knowledge=False)
                
        except Exception as e:
            error_msg = f"调用多模态大模型API时出错: {str(e)} (provider: {provider}, model: {model_name})"
            return error_msg

    @staticmethod
    def _extract_balanced_json_object(text: str, start: int) -> Optional[str]:
        """Slice from start (must be '{') through the matching '}', respecting JSON string rules."""
        if start >= len(text) or text[start] != "{":
            return None
        depth = 0
        i = start
        in_str = False
        esc = False
        while i < len(text):
            c = text[i]
            if in_str:
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
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1
        return None

    def extract_json_command(self, text: str) -> Optional[Dict]:
        """从AI回复中提取JSON命令（优先完整 ```json 代码块，再尝试平衡括号对象）。"""
        try:
            # 1) Full fenced block: ```json ... ``` (avoid regex that stops at first '}')
            search_pos = 0
            while True:
                m = re.search(r"```(?:json)?\s*", text[search_pos:], re.IGNORECASE)
                if not m:
                    break
                block_start = search_pos + m.end()
                close = text.find("```", block_start)
                if close == -1:
                    break
                raw = text[block_start:close].strip()
                if raw:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict) and "action" in parsed:
                            return parsed
                    except json.JSONDecodeError:
                        pass
                search_pos = close + 3

            # 2) Balanced object starting at each '{"action"' (handles nested params)
            pos = 0
            while True:
                key = '"action"'
                idx = text.find(key, pos)
                if idx == -1:
                    break
                open_brace = text.rfind("{", 0, idx)
                if open_brace == -1:
                    pos = idx + len(key)
                    continue
                sub = self._extract_balanced_json_object(text, open_brace)
                if sub:
                    try:
                        parsed = json.loads(sub)
                        if isinstance(parsed, dict) and "action" in parsed:
                            return parsed
                    except json.JSONDecodeError:
                        pass
                pos = idx + len(key)

            # 3) Single-line JSON (legacy)
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith("{") and '"action"' in line:
                    try:
                        parsed = json.loads(line)
                        if "action" in parsed:
                            return parsed
                    except json.JSONDecodeError:
                        continue

            return None
        except Exception as e:
            print(f"⚠️ JSON提取错误: {e}")
            return None

    def action_list_directory(self, path: Optional[str] = None, file_filter: Optional[str] = None) -> Dict[str, Any]:
        """列出目录内容"""
        target_path = Path(path) if path else self.work_directory
        
        if not target_path.exists():
            return {"success": False, "error": f"目录 '{target_path}' 不存在"}
        
        if not target_path.is_dir():
            return {"success": False, "error": f"'{target_path}' 不是一个目录"}
        
        items = []
        try:
            for item in target_path.iterdir():
                # 应用文件过滤器
                if file_filter:
                    if item.is_file():
                        # 检查文件扩展名或名称是否匹配过滤器
                        if not (file_filter.lower() in item.name.lower() or 
                               item.suffix.lower() == f".{file_filter.lower()}" or
                               item.name.lower().endswith(f".{file_filter.lower()}")):
                            continue
                    else:
                        # 对于目录，只检查名称是否包含过滤器
                        if file_filter.lower() not in item.name.lower():
                            continue
                
                item_info = {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                    "modified": datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                }
                items.append(item_info)
        except PermissionError:
            return {"success": False, "error": "权限不足，无法访问目录"}
        
        sorted_items = sorted(items, key=lambda x: (x["type"], x["name"]))
        filter_info = f" (过滤: {file_filter})" if file_filter else ""
        return {
            "success": True,
            "path": str(target_path),
            "items": sorted_items,
            "total_files": len([i for i in sorted_items if i["type"] == "file"]),
            "total_dirs": len([i for i in sorted_items if i["type"] == "directory"]),
            "filter": file_filter,
            "filter_info": filter_info
        }

    def action_intelligent_filter(self, file_list_result: Dict[str, Any], filter_condition: str) -> Dict[str, Any]:
        """使用AI智能过滤文件列表"""
        try:
            # 构建文件信息文本
            files_info = []
            for item in file_list_result.get("items", []):
                info = f"- {item['name']} | {item['type']} | {item['size']} bytes | 修改时间: {item['modified']}"
                files_info.append(info)
            
            files_text = "\n".join(files_info)
            
            # 构建AI提示 - 明确这是数据分析任务，不是命令生成
            ai_prompt = f"""
你现在是一个数据分析助手，不是文件管理命令生成器。

任务：从以下文件列表中筛选出符合条件的文件。

筛选条件：{filter_condition}

文件数据：
{files_text}

分析要求：
1. 仔细检查每个文件的信息（名称、大小、时间等）
2. 判断哪些文件符合筛选条件
3. 只返回符合条件的文件名，每行一个
4. 不要返回JSON、不要生成命令、不要添加解释

示例（假设要筛选大于500字节的文件）：
large_document.txt
big_image.jpg

现在开始分析："""
            
            # 调用AI进行筛选（不查询知识库）
            ai_response = self.call_ai(ai_prompt, include_knowledge=False)
            
            # 解析AI回复，提取符合条件的文件名
            if "无符合条件的文件" in ai_response:
                filtered_items = []
            else:
                lines = ai_response.strip().split('\n')
                valid_names = []
                original_items = {item['name']: item for item in file_list_result.get("items", [])}
                
                for line in lines:
                    line = line.strip()
                    # 跳过空行、说明文字、JSON格式等
                    if (line and 
                        not line.startswith('请') and 
                        not line.startswith('根据') and 
                        not line.startswith('文件') and
                        not line.startswith('筛选') and
                        not line.startswith('可选') and
                        not line.startswith('示例') and
                        not line.startswith('{') and
                        not line.startswith('```') and
                        line != ''):
                        
                        # 移除可能的序号、标记符号等
                        clean_name = line.replace('- ', '').replace('* ', '').replace('+ ', '').strip()
                        
                        # 检查是否是有效的文件名（存在于原始列表中）
                        if clean_name in original_items:
                            valid_names.append(clean_name)
                
                # 根据AI返回的文件名筛选原始列表
                filtered_items = []
                for name in valid_names:
                    filtered_items.append(original_items[name])
            
            # 构建结果，保持与list_directory相同的格式
            return {
                "success": True,
                "path": file_list_result.get("path", ""),
                "items": filtered_items,
                "total_files": len([i for i in filtered_items if i["type"] == "file"]),
                "total_dirs": len([i for i in filtered_items if i["type"] == "directory"]),
                "filter": filter_condition,
                "filter_info": f" (智能过滤: {filter_condition})"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"智能过滤失败: {str(e)}",
                "original_result": file_list_result
            }

    def action_change_directory(self, path: str) -> Dict[str, Any]:
        """切换工作目录"""
        try:
            if path == "..":
                new_path = self.work_directory.parent
            elif path == ".":
                new_path = self.work_directory
            elif path.startswith("/") or path.startswith("\\") or (len(path) > 1 and path[1] == ":"):
                # 绝对路径
                new_path = Path(path)
            else:
                # 相对路径
                new_path = self.work_directory / path
            
            # 解析路径
            new_path = new_path.resolve()
            
            if not new_path.exists():
                return {"success": False, "error": f"目录 '{path}' 不存在"}
            
            if not new_path.is_dir():
                return {"success": False, "error": f"'{path}' 不是一个目录"}
            
            old_dir = self.work_directory
            self.work_directory = new_path
            
            # 更新输入处理器的工作目录
            if self.input_handler:
                self.input_handler.update_work_directory(new_path)
            
            return {
                "success": True,
                "old_directory": str(old_dir),
                "new_directory": str(new_path),
                "message": f"已切换到目录: {new_path}"
            }
            
        except Exception as e:
            return {"success": False, "error": f"切换目录失败: {str(e)}"}

    def action_rename_file(self, old_name: str, new_name: str) -> Dict[str, Any]:
        """重命名文件或文件夹"""
        try:
            old_path = self.work_directory / old_name
            new_path = self.work_directory / new_name
            
            if not old_path.exists():
                return {"success": False, "error": f"文件 '{old_name}' 不存在"}
            
            if new_path.exists():
                return {"success": False, "error": f"目标文件 '{new_name}' 已存在"}
            
            old_path.rename(new_path)
            return {
                "success": True,
                "old_name": old_name,
                "new_name": new_name,
                "message": f"成功将 '{old_name}' 重命名为 '{new_name}'"
            }
            
        except Exception as e:
            return {"success": False, "error": f"重命名失败: {str(e)}"}

    def action_move_file(self, source: str, destination: str, confirmed: bool = False) -> Dict[str, Any]:
        """移动文件或文件夹，支持通配符批量移动"""
        import glob
        try:
            # 判断是否为通配符批量移动
            if '*' in source or '?' in source:
                pattern = str((self.work_directory / source).resolve())
                matched_files = [Path(p) for p in glob.glob(pattern) if Path(p).is_file()]
                if not matched_files:
                    return {"success": False, "error": f"未找到匹配的文件: {source}"}
                if destination.startswith("/") or destination.startswith("\\") or (len(destination) > 1 and destination[1] == ":"):
                    dest_path = Path(destination)
                else:
                    dest_path = self.work_directory / destination
                dest_path.mkdir(parents=True, exist_ok=True)
                
                # 请求用户确认批量移动
                if not confirmed:
                    confirmation = input(f"您确定要批量移动 {len(matched_files)} 个文件到 '{dest_path}' 吗？(y/n): ")
                    if confirmation.lower() != 'y':
                        return {
                            "success": False,
                            "error": f"用户取消了批量移动操作",
                            "confirmation_needed": False
                        }
                
                moved = []
                for file_path in matched_files:
                    target = dest_path / file_path.name
                    shutil.move(str(file_path), str(target))
                    moved.append(file_path.name)
                return {
                    "success": True,
                    "source": source,
                    "destination": str(dest_path),
                    "moved_files": moved,
                    "message": f"成功批量移动 {len(moved)} 个文件到 '{dest_path}'"
                }
            else:
                source_path = self.work_directory / source
                if destination.startswith("/") or destination.startswith("\\") or (len(destination) > 1 and destination[1] == ":"):
                    dest_path = Path(destination)
                else:
                    dest_path = self.work_directory / destination
                if not source_path.exists():
                    return {"success": False, "error": f"源文件 '{source}' 不存在"}
                
                # 请求用户确认单文件移动
                if not confirmed:
                    confirmation = input(f"您确定要将 '{source}' 移动到 '{dest_path}' 吗？(y/n): ")
                    if confirmation.lower() != 'y':
                        return {
                            "success": False,
                            "error": f"用户取消了移动操作",
                            "confirmation_needed": False
                        }
                
                shutil.move(str(source_path), str(dest_path))
                return {
                    "success": True,
                    "source": source,
                    "destination": str(dest_path),
                    "message": f"成功将 '{source}' 移动到 '{dest_path}'"
                }
        except Exception as e:
            return {"success": False, "error": f"移动失败: {str(e)}"}

    def action_delete_file(self, file_name: str, confirmed: bool = False) -> Dict[str, Any]:
        """删除文件或文件夹，支持通配符批量删除"""
        import glob
        # 判断是否为通配符批量删除
        if '*' in file_name or '?' in file_name:
            pattern = str((self.work_directory / file_name).resolve())
            matched_files = [Path(p) for p in glob.glob(pattern)]
            if not matched_files:
                return {"success": False, "error": f"未找到匹配的文件: {file_name}"}
            if not confirmed:
                confirmation = input(f"您确定要批量删除 {len(matched_files)} 个文件/目录吗？(y/n): ")
                if confirmation.lower() != 'y':
                    return {
                        "success": False,
                        "warning": f"用户拒绝批量删除 '{file_name}', 请跳过这些文件/目录",
                        "confirmation_needed": False
                    }
            results = []
            for file_path in matched_files:
                try:
                    if not file_path.exists():
                        results.append({"file": str(file_path), "success": False, "error": "不存在"})
                        continue
                    if file_path.is_dir():
                        shutil.rmtree(file_path)
                        results.append({"file": str(file_path), "success": True, "type": "directory", "message": f"成功删除目录 '{file_path.name}'"})
                    else:
                        file_path.unlink()
                        results.append({"file": str(file_path), "success": True, "type": "file", "message": f"成功删除文件 '{file_path.name}'"})
                except Exception as e:
                    results.append({"file": str(file_path), "success": False, "error": f"删除失败: {str(e)}"})
            all_success = all(r.get("success", False) for r in results)
            return {"success": all_success, "deleted": results, "count": len(results)}

        # 单文件/目录删除
        if not confirmed:
            confirmation = input(f"您确定要删除 '{file_name}' 吗？(y/n): ")
            if confirmation.lower() != 'y':
                return {
                    "success": False,
                    "warning": f"用户拒绝删除 '{file_name}'，请跳过这个文件/目录",
                    "confirmation_needed": False
                }
        try:
            file_path = self.work_directory / file_name
            if not file_path.exists():
                return {"success": False, "error": f"文件 '{file_name}' 不存在"}
            if file_path.is_dir():
                shutil.rmtree(file_path)
                return {
                    "success": True,
                    "file_name": file_name,
                    "type": "directory",
                    "message": f"成功删除目录 '{file_name}'"
                }
            else:
                file_path.unlink()
                return {
                    "success": True,
                    "file_name": file_name,
                    "type": "file",
                    "message": f"成功删除文件 '{file_name}'"
                }
        except Exception as e:
            return {"success": False, "error": f"删除失败: {str(e)}"}

    def action_create_directory(self, dir_name: str) -> Dict[str, Any]:
        """创建新文件夹"""
        try:
            dir_path = self.work_directory / dir_name
            
            if dir_path.exists():
                return {"success": False, "error": f"文件夹 '{dir_name}' 已存在"}
            
            dir_path.mkdir(parents=True)
            return {
                "success": True,
                "dir_name": dir_name,
                "full_path": str(dir_path),
                "message": f"成功创建文件夹 '{dir_name}'"
            }
            
        except Exception as e:
            return {"success": False, "error": f"创建文件夹失败: {str(e)}"}

    def action_get_file_info(self, file_name: str) -> Dict[str, Any]:
        """获取文件信息"""
        try:
            file_path = self.work_directory / file_name
            
            if not file_path.exists():
                return {"success": False, "error": f"文件 '{file_name}' 不存在"}
            
            stat = file_path.stat()
            return {
                "success": True,
                "name": file_name,
                "type": "directory" if file_path.is_dir() else "file",
                "size": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "permissions": oct(stat.st_mode)[-3:],
                "full_path": str(file_path)
            }
            
        except Exception as e:
            return {"success": False, "error": f"获取文件信息失败: {str(e)}"}

    def action_ffmpeg(self, source: str, target: str, options: Optional[str] = None) -> Dict[str, Any]:
        """调用ffmpeg处理媒体文件"""
        import subprocess
        if not source or not target:
            print("⚠️ 缺少 source 或 target 参数")
            return {"success": False, "error": "缺少 source 或 target 参数"}
        
        # 检查源文件是否存在
        source_path = self.work_directory / source
        if not source_path.exists():
            print(f"⚠️ 源文件 '{source}' 不存在")
            return {"success": False, "error": f"源文件 '{source}' 不存在"}

        ffmpeg_cmd = ["ffmpeg", "-y", "-i", source]
        if options:
            ffmpeg_cmd += options.split()
        ffmpeg_cmd.append(target)
        print(f"🔄 正在执行 ffmpeg 命令: {' '.join(ffmpeg_cmd)}")
        try:
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace"
            )
            if result.returncode == 0:
                return {"success": True, "message": "媒体文件处理成功"}
            else:
                return {"success": False, "error": f"ffmpeg 执行失败: {result.stderr}"}
        except FileNotFoundError:
            return {"success": False, "error": "未检测到 ffmpeg，请确保已安装并配置好 PATH 环境变量"}
        except Exception as e:
            return {"success": False, "error": f"ffmpeg 执行异常: {str(e)}"}
    
    def action_summarize_file(self, file_path: str, max_lines: int = 50) -> dict:
        """总结文本文件内容"""
        try:
            abs_path = Path(file_path)
            if not abs_path.is_absolute():
                abs_path = self.work_directory / file_path
            if not abs_path.exists():
                return {"success": False, "error": f"文件 '{file_path}' 不存在"}
            if not abs_path.is_file():
                return {"success": False, "error": f"'{file_path}' 不是一个文件"}
            stat = abs_path.stat()
            text_exts = ['.txt', '.md', '.json', '.py', '.csv', '.log', '.ini', '.yaml', '.yml']
            if abs_path.suffix.lower() not in text_exts and stat.st_size > 1024*1024:
                return {"success": False, "error": "仅支持文本文件或小于1MB的文件总结"}
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = []
                    for i, line in enumerate(f):
                        if i >= max_lines:
                            lines.append('... (内容过长已截断)')
                            break
                        lines.append(line.rstrip('\n'))
                    content = '\n'.join(lines)
            except Exception as e:
                return {"success": False, "error": f"无法读取文件内容: {str(e)}"}
            prompt = f"请用中文简要总结以下文件内容（200字以内）：\n{content}"
            summary = self.call_ai(prompt, include_knowledge=False)
            return {"success": True, "summary": summary, "file": str(abs_path)}
        except Exception as e:
            return {"success": False, "error": f"总结文件失败: {str(e)}"}

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
                for base in (self.work_directory, self.ai_workspace_dir):
                    try:
                        q = (base / p).resolve()
                        q.relative_to(base.resolve())
                        self._ai_created_path_keys.add(self._ephemeral_path_key(q))
                        return
                    except ValueError:
                        continue
            else:
                q = p.resolve()
                for base in (self.work_directory, self.ai_workspace_dir):
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
        for pat in (
            r"to_excel\s*\(\s*['\"]([^'\"]+)['\"]",
            r"to_csv\s*\(\s*['\"]([^'\"]+)['\"]",
            r"ExcelWriter\s*\(\s*['\"]([^'\"]+)['\"]",
        ):
            for m in re.finditer(pat, command, re.I):
                self._try_register_ai_output_literal(m.group(1))

    def _is_ai_created_path(self, path_str: str) -> bool:
        if not path_str or not str(path_str).strip():
            return False
        try:
            p = Path(path_str.strip())
            if not p.is_absolute():
                for base in (self.work_directory, self.ai_workspace_dir):
                    q = (base / p).resolve()
                    if self._ephemeral_path_key(q) in self._ai_created_path_keys:
                        return True
                return False
            p = p.resolve()
            return self._ephemeral_path_key(p) in self._ai_created_path_keys
        except OSError:
            return False

    def _parse_shell_invoked_script_path(self, command: str) -> Optional[Path]:
        """
        Path to the script/data file invoked by shell (e.g. second arg of `python x.py`).
        Returns None for `python -c ...` (no script file).
        """
        import shlex

        s = command.strip()
        if not s:
            return None
        if s.lower().startswith("call "):
            s = s[5:].strip()
        try:
            parts = shlex.split(s, posix=os.name != "nt")
        except ValueError:
            parts = s.split()
        if not parts:
            return None
        base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
        if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
            return self._parse_shell_invoked_script_path(" ".join(parts[2:]))
        exe = base0
        if exe in ("python", "pythonw", "py") and len(parts) >= 2:
            if parts[1] in ("-c", "-m") or parts[1].startswith("-"):
                return None
            tok = parts[1].strip('"').strip("'")
            if tok.startswith(".\\") or tok.startswith("./"):
                tok = tok[2:]
            p = Path(tok)
            if not p.is_absolute():
                p_wd = (self.work_directory / p).resolve()
                if p_wd.is_file():
                    return p_wd
                p_ws = (self.ai_workspace_dir / p).resolve()
                if p_ws.is_file():
                    return p_ws
                return p_wd
            try:
                return p.resolve()
            except OSError:
                return p
        tok = parts[0].strip('"').strip("'")
        low = tok.lower()
        if low.endswith((".py", ".ps1", ".bat", ".cmd")):
            if tok.startswith(".\\") or tok.startswith("./"):
                tok = tok[2:]
            p = Path(tok)
            if not p.is_absolute():
                p_wd = (self.work_directory / p).resolve()
                if p_wd.is_file():
                    return p_wd
                p_ws = (self.ai_workspace_dir / p).resolve()
                if p_ws.is_file():
                    return p_ws
                return p_wd
            try:
                return p.resolve()
            except OSError:
                return p
        return None

    def _parse_shell_invoked_executable(self, command: str) -> Optional[Path]:
        """Best-effort: path to the primary script/exe the user asked to run (first token)."""
        import shlex
        s = command.strip()
        if not s:
            return None
        if s.lower().startswith("call "):
            s = s[5:].strip()
        try:
            parts = shlex.split(s, posix=os.name != "nt")
        except ValueError:
            parts = s.split()
        if not parts:
            return None
        base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
        if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
            token = parts[2]
        else:
            token = parts[0]
        token = token.strip('"').strip("'")
        if token.startswith(".\\") or token.startswith("./"):
            token = token[2:]
        p = Path(token)
        if not p.is_absolute():
            p_wd = (self.work_directory / p).resolve()
            if p_wd.is_file():
                return p_wd
            p_ws = (self.ai_workspace_dir / p).resolve()
            if p_ws.is_file():
                return p_ws
            return p_wd
        try:
            return p.resolve()
        except OSError:
            return p

    def _try_remove_ephemeral_script_after_shell(self, command: str) -> Optional[str]:
        """Returns basename if an ephemeral script was removed, else None."""
        invoked = self._parse_shell_invoked_script_path(command)
        if invoked is None:
            return None
        key = self._ephemeral_path_key(invoked)
        if key not in self._ephemeral_script_paths:
            return None
        try:
            if invoked.is_file():
                name = invoked.name
                invoked.unlink()
                self._ephemeral_script_paths.discard(key)
                self._ai_created_path_keys.discard(key)
                print(f"🗑️ 已自动删除本会话创建的临时脚本: {name}")
                return name
        except OSError as e:
            print(f"⚠️ 自动删除临时脚本失败 ({invoked}): {e}")
        return None
    
    def action_shell_command(self, command: str, confirmed: bool = False) -> dict:
        """Run a shell command; capture stdout/stderr for AI context while echoing to the terminal."""
        if not command.strip():
            return {"success": False, "error": "命令不能为空"}
        if not confirmed:
            confirm = input(f"⚠️ 确认执行系统命令: {command} ? (y/n): ")
            if confirm.lower() != "y":
                return {"success": False, "error": "用户取消了操作"}

        import subprocess
        import sys
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.work_directory.resolve()),
                capture_output=True,
            )
            out = _decode_subprocess_output(completed.stdout)
            err = _decode_subprocess_output(completed.stderr)
            if out:
                print(out, end="" if out.endswith("\n") else "\n")
            if err:
                print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)

            return_code = completed.returncode
            base_out: Dict[str, Any] = {
                "output": out,
                "stderr": err,
                "return_code": return_code,
            }

            if return_code == 0:
                self._register_outputs_from_shell_command(command)
                removed = self._try_remove_ephemeral_script_after_shell(command)
                if removed:
                    self._last_auto_removed_ephemeral = removed
                    return {
                        "success": True,
                        "message": (
                            f"命令执行成功；已自动删除临时脚本 «{removed}»。"
                            "请勿再对该文件执行 delete。"
                        ),
                        "auto_removed_ephemeral_script": removed,
                        **base_out,
                    }
                return {"success": True, "message": "命令执行成功", **base_out}
            return {
                "success": False,
                "error": f"命令执行失败，退出码: {return_code}",
                **base_out,
            }

        except Exception as e:
            return {"success": False, "error": f"系统命令执行异常: {str(e)}"}
        
    def action_create_script(
        self, filename: str, content: str, confirmed: bool = False, overwrite: bool = False
    ) -> dict:
        """Create a script under config_dir/workspace/. Only the basename is used (no subpaths)."""
        if not filename or not content:
            return {"success": False, "error": "缺少文件名或内容"}
        safe_name = self._safe_script_basename(filename)
        if not safe_name:
            return {"success": False, "error": "无效的文件名"}
        print(f"请求创建脚本文件: {safe_name} → {self.ai_workspace_dir / safe_name}")
        print(f"内容:\n{content}")
        if not confirmed:
            confirm = input(f"⚠️ 确认创建脚本文件: {safe_name} ? (y/n): ")
            if confirm.lower() != "y":
                return {"success": False, "error": "用户取消了操作"}

        try:
            # AI script files go under config_dir/workspace (not user work_directory)
            script_path = self.ai_workspace_dir / safe_name
            existed_before = script_path.exists()
            if existed_before and not overwrite:
                return {
                    "success": False,
                    "error": (
                        f"文件 '{safe_name}' 已存在。"
                        "若需覆盖，请在 JSON 的 params 中设置 \"overwrite\": true。"
                    ),
                }
            with open(script_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(content)
            # 可选：为 .sh/.bat/.ps1/.py 等脚本加可执行权限（仅Linux/Mac）
            import stat
            if script_path.suffix in ['.sh', '.py', '.pl', '.rb'] and hasattr(os, 'chmod'):
                try:
                    os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IXUSR)
                except Exception:
                    pass
            resolved = script_path.resolve()
            self._register_ephemeral_script(resolved)
            verb = "覆盖写入" if overwrite and existed_before else "创建"
            return {
                "success": True,
                "filename": safe_name,
                "full_path": str(resolved),
                "message": f"成功{verb}脚本文件 '{safe_name}'（位于 config 侧 workspace）",
            }
        except Exception as e:
            return {"success": False, "error": f"创建脚本文件失败: {str(e)}"}

    def action_read_file(self, file_path: str, max_lines: int = 100) -> dict:
        """读取文本文件内容，返回前max_lines行，支持自动编码检测，适合预览文本文件。"""
        try:
            abs_path = Path(file_path)
            if not abs_path.is_absolute():
                p1 = self.work_directory / file_path
                p2 = self.ai_workspace_dir / file_path
                if p1.is_file():
                    abs_path = p1
                elif p2.is_file():
                    abs_path = p2
                else:
                    abs_path = p1
            if not abs_path.exists():
                return {"success": False, "error": f"文件 '{file_path}' 不存在"}
            if not abs_path.is_file():
                return {"success": False, "error": f"'{file_path}' 不是一个文件"}
            stat = abs_path.stat()
            text_exts = ['.txt', '.md', '.json', '.py', '.csv', '.log', '.ini', '.yaml', '.yml']
            if abs_path.suffix.lower() not in text_exts and stat.st_size > 1024*1024:
                return {"success": False, "error": "仅支持文本文件或小于1MB的文件读取"}
            # 自动尝试多种编码
            encodings = ['utf-8', 'gbk', 'gb2312', 'utf-16', 'latin1']
            content = None
            for enc in encodings:
                try:
                    with open(abs_path, 'r', encoding=enc, errors='replace') as f:
                        lines = []
                        for i, line in enumerate(f):
                            if i >= max_lines:
                                lines.append('... (内容过长已截断)')
                                break
                            lines.append(line.rstrip('\n'))
                        content = '\n'.join(lines)
                    break
                except Exception:
                    continue
            if content is None:
                return {"success": False, "error": "无法读取文件内容，可能编码不受支持"}
            return {"success": True, "file": str(abs_path), "content": content}
        except Exception as e:
            return {"success": False, "error": f"读取文件失败: {str(e)}"}

    def action_analyze_image(self, file_path: str, prompt: str = "") -> dict:
        """分析图片内容，支持多种图片格式"""
        try:
            abs_path = Path(file_path)
            if not abs_path.is_absolute():
                p1 = self.work_directory / file_path
                p2 = self.ai_workspace_dir / file_path
                if p1.is_file():
                    abs_path = p1
                elif p2.is_file():
                    abs_path = p2
                else:
                    abs_path = p1
            if not abs_path.exists():
                return {"success": False, "error": f"图片文件 '{file_path}' 不存在"}
            if not abs_path.is_file():
                return {"success": False, "error": f"'{file_path}' 不是一个文件"}
            
            # 检查文件扩展名
            image_exts = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif']
            if abs_path.suffix.lower() not in image_exts:
                return {"success": False, "error": f"不支持的文件格式: {abs_path.suffix}"}
            
            # 构建分析提示
            if prompt:
                analysis_prompt = f"请分析这张图片：{prompt}\n\n图片路径：{str(abs_path)}"
            else:
                analysis_prompt = f"请详细描述这张图片的内容，包括：\n1. 图片中的主要物体和场景\n2. 颜色和构图\n3. 文字内容（如果有）\n4. 图片的整体风格和特点\n\n图片路径：{str(abs_path)}"
            
            # 调用AI进行图片分析
            analysis = self.call_ai_multimodal(analysis_prompt, str(abs_path))
            
            return {"success": True, "analysis": analysis, "file": str(abs_path)}
        except Exception as e:
            return {"success": False, "error": f"图片分析失败: {str(e)}"}

    def action_git(self, command: str, args: Optional[str] = None, confirmed: bool = False) -> dict:
        """执行Git命令，支持所有Git操作，写操作需要用户确认"""
        try:
            import subprocess
            import sys
            
            # 构建完整的Git命令
            if args:
                full_command = f"git {command} {args}"
            else:
                full_command = f"git {command}"
            
            # 检查是否为写操作，需要用户确认
            write_commands = [
                'add', 'commit', 'push', 'pull', 'merge', 'rebase', 'reset', 
                'checkout', 'branch', 'tag', 'remote', 'fetch', 'clone', 'init',
                'stash', 'cherry-pick', 'revert', 'clean', 'rm', 'mv'
            ]
            
            is_write_operation = command.lower() in write_commands
            
            if is_write_operation and not confirmed:
                # 显示将要执行的命令并请求用户确认
                print(f"⚠️ 即将执行Git写操作: {full_command}")
                confirm = input("确认执行此Git命令吗？(y/n): ")
                if confirm.lower() != 'y':
                    return {
                        "success": False, 
                        "command": full_command,
                        "error": "用户取消了Git写操作",
                        "message": "Git命令已取消"
                    }
            
            # 检查是否在Git仓库中
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--git-dir"],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    cwd=str(self.work_directory),
                    timeout=10
                )
                if result.returncode != 0:
                    return {"success": False, "error": "当前目录不是Git仓库"}
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Git仓库检查超时"}
            except FileNotFoundError:
                return {"success": False, "error": "Git未安装或不在PATH中"}
            
            # 执行Git命令，使用UTF-8编码并处理编码错误
            process = subprocess.Popen(
                full_command,
                shell=True,
                stdin=sys.stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(self.work_directory)
            )
            
            stdout, stderr = process.communicate()
            return_code = process.returncode
            
            if return_code == 0:
                return {
                    "success": True, 
                    "command": full_command,
                    "output": stdout.strip() if stdout else "",
                    "message": "Git命令执行成功"
                }
            else:
                return {
                    "success": False, 
                    "command": full_command,
                    "error": stderr.strip() if stderr else f"Git命令执行失败，退出码: {return_code}",
                    "output": stdout.strip() if stdout else ""
                }
                
        except Exception as e:
            return {"success": False, "error": f"Git命令执行异常: {str(e)}"}

    def action_diff(self, file1: str, file2: str, options: Optional[str] = None) -> dict:
        """跨平台文件比较：Windows上优先使用diff.exe，否则使用fc命令；其他平台使用diff命令"""
        try:
            import subprocess
            import sys
            import os
            import shutil
            import platform
            from pathlib import Path
            
            # 检查文件是否存在
            file1_path = Path(file1)
            file2_path = Path(file2)
            
            if not file1_path.exists():
                return {"success": False, "error": f"文件不存在: {file1}"}
            if not file2_path.exists():
                return {"success": False, "error": f"文件不存在: {file2}"}
            
            # 根据操作系统选择合适的比较命令
            if platform.system() == "Windows":
                # Windows平台：优先使用diff.exe，否则使用fc命令
                if shutil.which("diff.exe"):
                    # 使用diff.exe
                    if options:
                        full_command = f"diff.exe {options} \"{file1}\" \"{file2}\""
                    else:
                        full_command = f"diff.exe \"{file1}\" \"{file2}\""
                    command_type = "diff.exe"
                else:
                    # 使用fc命令
                    if options:
                        full_command = f"cmd /c fc {options} \"{file1}\" \"{file2}\""
                    else:
                        full_command = f"cmd /c fc \"{file1}\" \"{file2}\""
                    command_type = "fc"
            else:
                # 其他平台：使用diff命令
                if options:
                    full_command = f"diff {options} \"{file1}\" \"{file2}\""
                else:
                    full_command = f"diff \"{file1}\" \"{file2}\""
                command_type = "diff"
            
            # 执行比较命令，使用UTF-8编码并处理编码错误
            process = subprocess.Popen(
                full_command,
                shell=True,
                stdin=sys.stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(self.work_directory)
            )
            
            stdout, stderr = process.communicate()
            return_code = process.returncode
            
            # 根据命令类型处理返回码
            if command_type == "fc":
                # fc命令的特殊处理：返回码1表示有差异，0表示无差异
                if return_code in [0, 1]:
                    return {
                        "success": True, 
                        "command": full_command,
                        "command_type": command_type,
                        "output": stdout.strip() if stdout else "",
                        "has_differences": return_code == 1,
                        "message": "文件比较完成" + ("，发现差异" if return_code == 1 else "，文件相同")
                    }
                else:
                    return {
                        "success": False, 
                        "command": full_command,
                        "command_type": command_type,
                        "error": stderr.strip() if stderr else f"fc命令执行失败，退出码: {return_code}",
                        "output": stdout.strip() if stdout else ""
                    }
            else:
                # diff/diff.exe命令：返回码0表示无差异，1表示有差异，2表示错误
                if return_code in [0, 1]:
                    return {
                        "success": True, 
                        "command": full_command,
                        "command_type": command_type,
                        "output": stdout.strip() if stdout else "",
                        "has_differences": return_code == 1,
                        "message": "文件比较完成" + ("，发现差异" if return_code == 1 else "，文件相同")
                    }
                else:
                    return {
                        "success": False, 
                        "command": full_command,
                        "command_type": command_type,
                        "error": stderr.strip() if stderr else f"{command_type}命令执行失败，退出码: {return_code}",
                        "output": stdout.strip() if stdout else ""
                    }
                
        except Exception as e:
            return {"success": False, "error": f"文件比较命令执行异常: {str(e)}"}

    def execute_command(self, command: Dict) -> Dict[str, Any]:
        """执行AI生成的命令，支持批量命令和cls命令"""
        print(f"🔍 正在执行命令: {command}")
        action = command.get("action")
        params = command.get("params", {})

        if action == "cls":
            import os
            os.system('cls' if os.name == 'nt' else 'clear')
            return {"success": True, "message": "屏幕已清空"}

        elif action == "batch":
            commands = params.get("commands", [])
            results = []
            all_success = True
            for subcmd in commands:
                sub_action = subcmd.get("action")
                sub_result = self.execute_command(subcmd)
                results.append({"action": sub_action, "result": sub_result})
                
                # 检查用户是否取消了子命令
                if not sub_result.get("success", True) and (
                    "用户取消了操作" in sub_result.get("error", "") or 
                    "用户拒绝" in sub_result.get("error", "") or
                    "用户取消" in sub_result.get("error", "")
                ):
                    # 用户取消了某个子命令，停止执行剩余命令
                    return {"success": False, "error": "用户取消了操作", "results": results}
                
                if not sub_result.get("success", True):
                    all_success = False
            return {"success": all_success, "results": results}

        elif action == "list":
            path = params.get("path")
            file_filter = params.get("filter")
            smart_filter = params.get("smart_filter")  # 智能过滤条件

            # 首先获取所有文件
            result = self.action_list_directory(path, file_filter)

            if result["success"]:
                # 如果有智能过滤条件，使用AI进行筛选
                if smart_filter:
                    print(f"🧠 正在使用AI智能过滤: {smart_filter}")
                    filtered_result = self.action_intelligent_filter(result, smart_filter)
                    if filtered_result["success"]:
                        result = filtered_result

                title_extra = result.get("filter_info", "")
                if smart_filter and "智能过滤" not in title_extra:
                    title_extra += f" [智能过滤: {smart_filter}]"
                print(f"\n📁 目录内容 ({result['path']}){title_extra}:")
                print("-" * 80)
                for item in result["items"]:
                    icon = "📁" if item["type"] == "directory" else "📄"
                    print(f"{icon} {item['name']:<40} {item['size']:>10} bytes  {item['modified']}")
                print("-" * 80)
                print(f"📊 统计: {result['total_dirs']} 个文件夹, {result['total_files']} 个文件")
                if file_filter:
                    print(f"🔍 已应用过滤器: {file_filter}")
                if smart_filter:
                    print(f"🧠 智能过滤条件: {smart_filter}")
            else:
                print(f"❌ {result['error']}")

            return result

        elif action == "cd":
            path = params.get("path", "")
            result = self.action_change_directory(path)

            if not result["success"]:
                print(f"❌ {result['error']}")

            return result

        elif action == "rename":
            old_name = params.get("old_name")
            new_name = params.get("new_name")
            if old_name and new_name:
                result = self.action_rename_file(old_name, new_name)

                if result["success"]:
                    print(f"✅ {result['message']}")
                else:
                    print(f"❌ {result['error']}")

                return result

        elif action == "move":
            source = params.get("source")
            destination = params.get("destination")
            if source and destination:
                move_cmd = {"action": "move", "params": {"source": source, "destination": destination}}
                confirmed = self._freedom_auto_confirm(move_cmd)
                result = self.action_move_file(source, destination, confirmed=confirmed)

                if result["success"]:
                    print(f"✅ {result['message']}")
                else:
                    print(f"❌ {result['error']}")

                return result

        elif action == "delete":
            # 支持多种参数名: file_name, path, name
            file_name = params.get("file_name") or params.get("path") or params.get("name")
            if file_name:
                target_path = self.work_directory / file_name
                base = Path(file_name).name
                if (
                    not target_path.exists()
                    and self._last_auto_removed_ephemeral
                    and base.lower() == self._last_auto_removed_ephemeral.lower()
                ):
                    print(
                        f"ℹ️ «{base}» 已由上一步 shell 成功后自动删除，跳过重复的 delete（无需 freedom 确认）。"
                    )
                    self._last_auto_removed_ephemeral = None
                    return {
                        "success": True,
                        "message": f"文件 «{base}» 已不存在（已由系统自动清理）",
                        "skipped_duplicate_delete": True,
                    }
                del_cmd = {"action": "delete", "params": {"path": file_name}}
                confirmed = self._freedom_auto_confirm(del_cmd)
                result = self.action_delete_file(file_name, confirmed=confirmed)

                if result["success"]:
                    print(f"✅ {result['message']}")
                elif result.get("confirmation_needed"):
                    print(f"⚠️ {result['warning']}")
                    print(f"💡 如需确认删除，请使用：删除{file_name}并确认")

                return result
            else:
                print("❌ 删除命令缺少文件名参数")
                return {"success": False, "error": "缺少文件名参数"}

        elif action == "mkdir":
            path = params.get("path")
            if path:
                result = self.action_create_directory(path)

                if result["success"]:
                    print(f"✅ {result['message']}")
                else:
                    print(f"❌ {result['error']}")

                return result

        elif action == "info":
            # 支持多种参数名: file_name, path, name
            file_name = params.get("file_name") or params.get("path") or params.get("name")
            if file_name:
                result = self.action_get_file_info(file_name)

                if result["success"]:
                    print(f"\n📋 文件信息：")
                    print(f"名称: {result['name']}")
                    print(f"类型: {result['type']}")
                    print(f"大小: {result['size']} bytes")
                    print(f"创建时间: {result['created']}")
                    print(f"修改时间: {result['modified']}")
                    print(f"权限: {result['permissions']}")
                    print(f"完整路径: {result['full_path']}")
                else:
                    print(f"❌ {result['error']}")

                return result
            else:
                print("❌ 查看文件信息命令缺少文件名参数")
                return {"success": False, "error": "缺少文件名参数"}

        elif action == "ffmpeg":
            source = params.get("source")
            target = params.get("target")
            options = params.get("options")
            if source and target:
                result = self.action_ffmpeg(source, target, options)
                if result["success"]:
                    print(f"✅ {result['message']}")
                else:
                    print(f"❌ {result['error']}")
                return result
            else:
                print("❌ 命令缺少参数 source 或 target")
                return {"success": False, "error": "缺少 source 或 target 参数"}

        elif action == "summarize":
            file_path = params.get("path")
            if file_path:
                result = self.action_summarize_file(file_path)
                if result["success"]:
                    print(f"\n📄 文件 {result['file']} 总结：")
                    print(result["summary"])
                else:
                    print(f"❌ {result['error']}")
                return result
            else:
                print("❌ summarize命令缺少path参数")
                return {"success": False, "error": "缺少path参数"}

        elif action == "shell":
            shell_cmd = params.get("command")
            if shell_cmd:
                shell_cmd_dict = {"action": "shell", "params": {"command": shell_cmd}}
                confirmed = self._freedom_auto_confirm(shell_cmd_dict)
                result = self.action_shell_command(shell_cmd, confirmed=confirmed)
                if result["success"]:
                    print(f"\n💻 系统命令执行成功: {result['message']}")
                else:
                    print(f"❌ 系统命令执行失败: {result.get('error', '未知错误')}")
                return result
            else:
                print("❌ shell命令缺少command参数")
                return {"success": False, "error": "缺少command参数"}

        elif action == "script":
            filename = params.get("filename")
            content = params.get("content")
            overwrite = bool(params.get("overwrite", False))
            if filename and content:
                assess_content = content if len(content) <= 6000 else content[:6000] + "\n/* ... truncated for reversibility check ... */"
                script_cmd = {"action": "script", "params": {"filename": filename, "content": assess_content}}
                confirmed = self._freedom_auto_confirm(script_cmd)
                result = self.action_create_script(
                    filename, content, confirmed=confirmed, overwrite=overwrite
                )
                if result["success"]:
                    print(f"✅ {result['message']}")
                else:
                    print(f"❌ {result['error']}")
                return result
            else:
                print("❌ script命令缺少filename或content参数")
                return {"success": False, "error": "缺少filename或content参数"}
        
        elif action == "read":
            file_path = params.get("path")
            max_lines = params.get("max_lines", 100)
            if file_path:
                result = self.action_read_file(file_path, max_lines)
                if result["success"]:
                    print(f"\n📄 文件 {result['file']} 内容预览：")
                else:
                    print(f"❌ {result['error']}")
                return result
            else:
                print("❌ read命令缺少path参数")
                return {"success": False, "error": "缺少path参数"}
        
        elif action == "analyze_image":
            file_path = params.get("path")
            prompt = params.get("prompt", "")
            if file_path:
                result = self.action_analyze_image(file_path, prompt)
                if result["success"]:
                    print(f"\n🖼️ 图片分析结果 ({result['file']}):")
                    print("=" * 60)
                    print(result["analysis"])
                    print("=" * 60)
                else:
                    print(f"❌ {result['error']}")
                return result
            else:
                print("❌ analyze_image命令缺少path参数")
                return {"success": False, "error": "缺少path参数"}

        elif action == "git":
            git_command = params.get("command")
            git_args = params.get("args")
            if git_command:
                git_write_subcommands = [
                    "add", "commit", "push", "pull", "merge", "rebase", "reset",
                    "checkout", "branch", "tag", "remote", "fetch", "clone", "init",
                    "stash", "cherry-pick", "revert", "clean", "rm", "mv",
                ]
                needs_confirm = git_command.lower() in git_write_subcommands
                git_cmd = {"action": "git", "params": {"command": git_command, "args": git_args}}
                confirmed = self._freedom_auto_confirm(git_cmd) if needs_confirm else False
                result = self.action_git(git_command, git_args, confirmed=confirmed)
                if result["success"]:
                    print(f"\n🔧 Git命令执行成功: {result['command']}")
                    if result.get("output"):
                        print("📤 输出:")
                        print(result["output"])
                else:
                    # 检查是否为用户取消的情况
                    if "用户取消了Git写操作" in result.get("error", ""):
                        print(f"ℹ️ {result['message']}")
                    else:
                        print(f"❌ Git命令执行失败: {result['error']}")
                    if result.get("output"):
                        print("📤 输出:")
                        print(result["output"])
                return result
            else:
                print("❌ git命令缺少command参数")
                return {"success": False, "error": "缺少command参数"}

        elif action == "diff":
            file1 = params.get("file1")
            file2 = params.get("file2")
            options = params.get("options")
            if file1 and file2:
                result = self.action_diff(file1, file2, options)
                if result["success"]:
                    command_type = result.get("command_type", "unknown")
                    print(f"\n🔍 文件比较完成 (使用 {command_type}): {result['command']}")
                    print(f"📊 结果: {result['message']}")
                    if result.get("output"):
                        print("📤 差异详情:")
                        print(result["output"])
                else:
                    print(f"❌ 文件比较失败: {result['error']}")
                    if result.get("output"):
                        print("📤 输出:")
                        print(result["output"])
                return result
            else:
                print("❌ diff命令缺少file1或file2参数")
                return {"success": False, "error": "缺少file1或file2参数"}

        elif action == "knowledge_sync":
            """同步知识库"""
            if not self.knowledge_enabled:
                return {"success": False, "error": "知识库功能已关闭，可使用 'knowledge on' 开启"}
            if not self.knowledge_manager:
                return {"success": False, "error": "知识库功能不可用"}
            
            try:
                self.knowledge_manager.sync_knowledge_base()
                return {"success": True, "message": "知识库同步完成"}
            except Exception as e:
                return {"success": False, "error": f"知识库同步失败: {str(e)}"}

        elif action == "knowledge_stats":
            """获取知识库统计信息"""
            if not self.knowledge_enabled:
                return {"success": False, "error": "知识库功能已关闭，可使用 'knowledge on' 开启"}
            if not self.knowledge_manager:
                return {"success": False, "error": "知识库功能不可用"}
            
            try:
                stats = self.knowledge_manager.get_knowledge_stats()
                if stats:
                    print(f"\n📊 知识库统计信息:")
                    print(f"📄 文档总数: {stats.get('total_documents', 0)}")
                    print(f"📝 文本片段总数: {stats.get('total_chunks', 0)}")
                    print(f"📁 支持的文件类型: {', '.join(stats.get('supported_extensions', []))}")
                    
                    file_types = stats.get('file_types', {})
                    if file_types:
                        print(f"📋 文件类型分布:")
                        for ext, count in file_types.items():
                            print(f"  {ext}: {count} 个文件")
                else:
                    print("❌ 获取知识库统计信息失败")
                
                return {"success": True, "stats": stats}
            except Exception as e:
                return {"success": False, "error": f"获取知识库统计信息失败: {str(e)}"}

        elif action == "knowledge_search":
            """搜索知识库"""
            if not self.knowledge_enabled:
                return {"success": False, "error": "知识库功能已关闭，可使用 'knowledge on' 开启"}
            if not self.knowledge_manager:
                return {"success": False, "error": "知识库功能不可用"}
            
            query = params.get("query", "")
            top_k = params.get("top_k", 5)
            
            if not query:
                return {"success": False, "error": "缺少搜索查询参数"}
            
            try:
                results = self.knowledge_manager.search_knowledge(query, top_k)
                if results:
                    print(f"\n🔍 知识库搜索结果 (查询: '{query}'):")
                    print("=" * 80)
                    for i, result in enumerate(results, 1):
                        print(f"{i}. 来源: {result['source']}")
                        print(f"   相似度: {1 - result['similarity']:.3f}")
                        print(f"   内容: {result['content'][:200]}...")
                        print("-" * 40)
                else:
                    print(f"🔍 未找到相关结果: '{query}'")
                
                return {"success": True, "results": results, "query": query}
            except Exception as e:
                return {"success": False, "error": f"知识库搜索失败: {str(e)}"}

        elif action == "knowledge_enable" or action == "knowledge_on":
            result = self._enable_knowledge()
            if result.get("success"):
                print(f"✅ {result.get('message', '知识库已开启')}")
            else:
                print(f"❌ {result.get('error', '开启失败')}")
            return result

        elif action == "knowledge_disable" or action == "knowledge_off":
            result = self._disable_knowledge()
            if result.get("success"):
                print(f"✅ {result.get('message', '知识库已关闭')}")
            else:
                print(f"❌ {result.get('error', '关闭失败')}")
            return result

        elif action == "freedom_enable" or action == "freedom_on":
            result = self._enable_freedom()
            if result.get("success"):
                print(f"✅ {result.get('message', '自由模式已开启')}")
            else:
                print(f"❌ {result.get('error', '开启失败')}")
            return result

        elif action == "freedom_disable" or action == "freedom_off":
            result = self._disable_freedom()
            if result.get("success"):
                print(f"✅ {result.get('message', '自由模式已关闭')}")
            else:
                print(f"❌ {result.get('error', '关闭失败')}")
            return result

        return {"success": False, "error": "未知的操作类型"}

    def run(self):
        """运行AI Agent主循环，支持自动多轮命令执行，AI可根据上次执行结果继续生成命令，遇到{"action": "done"}时终止。"""
        import sys
        import os
        os_name = os.name

        # 启动时提示知识库状态
        _win = os_name == "nt"
        if not self.knowledge_enabled:
            print(
                "知识库当前处于关闭状态。可使用 "
                + ("`/knowledge on`" if _win else "'knowledge on'")
                + " 来开启"
            )
        elif not self.knowledge_manager:
            print(
                "知识库已开启但当前不可用。请检查依赖或稍后重试。可使用 "
                + ("`/knowledge off`" if _win else "'knowledge off'")
                + " 暂时关闭。"
            )

        if self.skills:
            _sk_path = self.config_dir / "skills"

        _fon = "`/freedom on`" if _win else "'freedom on'"
        _foff = "`/freedom off`" if _win else "'freedom off'"
        if self.freedom_enabled:
            _enable_windows_console_vt()
            print(_ansi_red("自由模式：已开启"))
            print(
                "  移动/删除/shell/脚本/Git 写操作在执行前会由 AI 判定是否可逆，"
                f"可逆则自动跳过 y/n 确认。输入 {_foff} 可关闭。"
            )
            print(
                _ansi_yellow(
                    "  警告：AI 对「可逆」的判定可能错误；自动跳过确认仍可能导致误删文件、错误 Git 操作或破坏性 shell/脚本执行。"
                )
            )
        else:
            print("自由模式：已关闭")
            print(
                "  需确认的操作将始终询问 y/n。"
                f"输入 {_fon} 可开启（可逆操作可由 AI 判定后自动跳过确认）。"
            )

        print("输入 '/help' 查看帮助")
        print("=" * 80)

        import subprocess
        import re
        system_cmd_patterns = [
            r'^cd(\s+.+)?$',
            r'^(dir|ls|list)(\s+.+)?$',
            r'^(del|delete|rm)(\s+.+)?$',
            r'^(ping)(\s+.+)?$',
            r'^(ipconfig|ifconfig)(\s+.+)?$',
            r'^(type|cat)(\s+.+)?$',
            r'^(echo)(\s+.+)?$',
            r'^(whoami|hostname|date|time)(\s+.+)?$',
            r'^(wmic|net)(\s+.+)?$',
        ]
        system_cmd_re = re.compile('|'.join(system_cmd_patterns), re.IGNORECASE)

        while True:
            try:
                # 获取用户输入，支持历史记录
                user_input = self._get_user_input_with_history()
                
                # 保存到历史记录（非空输入）
                if user_input.strip():
                    self.history_manager.add_entry(user_input)

                stripped_in = user_input.strip()
                if not stripped_in:
                    continue

                # Windows: built-in commands and direct shell require "/" prefix; POSIX unchanged
                builtin_line: Optional[str] = None
                if os_name == "nt":
                    if stripped_in.startswith("/"):
                        builtin_line = stripped_in[1:].lstrip()
                        if not builtin_line:
                            print(
                                "ℹ️ 在 Windows 下，内置命令与本地直接执行的命令均需以 / 开头，"
                                "例如 /exit、/help、/clear screen、/knowledge on、/dir；单独输入 / 无效。"
                            )
                            continue
                else:
                    builtin_line = stripped_in

                if builtin_line is not None:
                    bl = builtin_line.lower()
                    if bl in ('exit', 'quit'):
                        break
                    # clear screen: /clear screen (Windows); POSIX may still use single-token "clear"
                    if bl == 'cls' or bl == 'clear screen' or (os_name != 'nt' and bl == 'clear'):
                        os.system('cls' if os_name == 'nt' else 'clear')
                        continue
                    if bl == 'clear history':
                        self.history_manager.clear_history()
                        if self.input_handler is not None and hasattr(
                            self.input_handler, "reset_command_history"
                        ):
                            self.input_handler.reset_command_history(
                                self.history_manager.get_all_history()
                            )
                        print("✅ 历史记录已清除")
                        continue
                    if bl == "clear context":
                        self.conversation_history.clear()
                        self.operation_results.clear()
                        self._last_auto_removed_ephemeral = None
                        print("✅ 已清空 AI 上下文（对话历史与近期操作结果缓存，不影响命令行输入历史）")
                        continue

                    if bl == 'knowledge on':
                        self.execute_command({"action": "knowledge_on", "params": {}})
                        continue
                    if bl == 'knowledge off':
                        self.execute_command({"action": "knowledge_off", "params": {}})
                        continue

                    if bl == 'freedom on':
                        self.execute_command({"action": "freedom_on", "params": {}})
                        continue
                    if bl == 'freedom off':
                        self.execute_command({"action": "freedom_off", "params": {}})
                        continue

                    if self.knowledge_enabled and self.knowledge_manager:
                        if bl == 'knowledge sync':
                            self.execute_command({"action": "knowledge_sync", "params": {}})
                            continue

                        if bl == 'knowledge stats':
                            self.execute_command({"action": "knowledge_stats", "params": {}})
                            continue

                        if bl.startswith('knowledge search '):
                            query = builtin_line[len('knowledge search ') :]
                            if query.strip():
                                self.execute_command({
                                    "action": "knowledge_search",
                                    "params": {"query": query.strip()},
                                })
                            else:
                                print("❌ 请提供搜索查询内容")
                            continue
                    else:
                        if bl.startswith('knowledge '):
                            _kh = "`/knowledge on`" if os_name == "nt" else "'knowledge on'"
                            print(f"ℹ️ 知识库已关闭，可使用 {_kh} 开启")
                            continue

                    if bl == 'help':
                        print("\n🌟 Smart Shell 帮助信息")
                        print("=" * 80)
                        print("\n📌 内置命令：")
                        if os_name == "nt":
                            print("  1. /exit, /quit                 - 退出程序")
                            print("  2. /cls, /clear screen          - 清空屏幕")
                            print("  3. /clear history               - 清除命令历史记录")
                            print("  4. /clear context           - 清空 AI 上下文与操作结果缓存")
                            print("  5. /help                        - 显示此帮助信息")
                        else:
                            print("  1. exit, quit                   - 退出程序")
                            print("  2. cls, clear screen (或 clear) - 清空屏幕")
                            print("  3. clear history                - 清除命令历史记录")
                            print("  4. clear context                - 清空 AI 对话上下文与操作结果缓存")
                            print("  5. help                         - 显示此帮助信息")

                        if self.knowledge_enabled:
                            print("\n📚 知识库命令：")
                            if os_name == "nt":
                                print("  6. /knowledge on|off            - 开关（状态写入 config.json）")
                                print("  7. /knowledge sync              - 同步文档")
                                print("  8. /knowledge stats             - 统计信息")
                                print("  9. /knowledge search <query>    - 搜索知识库")
                            else:
                                print("  6. knowledge on/off             - 开关知识库（状态写入 config.json）")
                                print("  7. knowledge sync               - 同步知识库文档")
                                print("  8. knowledge stats              - 查看统计信息")
                                print("  9. knowledge search <query>   - 搜索知识库")

                        print("\n🦅 自由模式命令：")
                        if os_name == "nt":
                            print("  /freedom on|/freedom off  - 可逆操作自动跳过确认（写入 config.json）")
                        else:
                            print("  freedom on/off  - 可逆操作自动跳过确认（状态写入 config.json）")

                        print("\n📌 系统命令（不经 AI，本机直接执行）：")
                        if os_name == "nt":
                            print("  Windows：必须以 / 开头，例如 /dir、/ping、/type file.txt、/git status")
                            print("  （盘符切换如 d: 仍可直接输入，无需 /）")
                        else:
                            print("  常见系统命令（如 cd、ls、cat 等）可直接输入；可执行文件也可直接运行")
                        print("\n📌 自然语言命令：")
                        print("您可以使用自然语言描述您的需求，例如：")
                        print("  1. 创建一个名为test的文件夹")
                        print("  2. 将文件a.txt重命名为b.txt")
                        print("  3. 分析这张图片的内容")
                        print("  4. 总结这个文本文件")
                        print("  5. 将视频转换为mp4格式")
                        print("  6. 比较两个文件的差异")
                        print("  7. 查找最近修改的文件")
                        print("  8. 删除所有临时文件")

                        if self.knowledge_manager:
                            print("  9. 同步知识库")
                            print("  10. 查看知识库统计")
                            print("  11. 在知识库中搜索特定内容")

                        print("\n💡 提示：")
                        print("  - Tab键可以自动补全文件路径")
                        print("  - 上下方向键可以浏览历史命令")
                        print("  - AI会理解您的自然语言指令并执行相应操作")
                        if self.knowledge_manager:
                            print("  - 知识库会自动检索相关信息来辅助AI回答")
                        if self.skills:
                            print(
                                f"  - 已载入 {len(self.skills)} 个 Agent Skills（内建 {self._builtin_skills_root} + 外部 {self.config_dir / 'skills'}），"
                                "任务匹配时模型会优先遵循对应 SKILL.md"
                            )
                        print("=" * 80)
                        continue

                # Windows: single drive letter (e.g. "d:" or "D:") -> switch to that drive root, do not trigger AI
                if os_name == 'nt' and re.match(r'^[a-zA-Z]:\s*$', stripped_in):
                    drive_letter = stripped_in[0].upper()
                    result = self.action_change_directory(drive_letter + ":\\")
                    if not result["success"]:
                        print(f"❌ {result['error']}")
                    continue

                # Direct local execution without AI: Windows requires leading "/"; POSIX keeps legacy ("/" is absolute paths)
                run_direct_shell: Optional[str] = None
                if os_name == "nt":
                    if stripped_in.startswith("/"):
                        run_direct_shell = stripped_in[1:].lstrip()
                        if not run_direct_shell:
                            print(
                                "ℹ️ 在 Windows 下，不经过 AI 直接执行的系统命令或可执行文件需以 / 开头，"
                                "例如 /dir、/ping 127.0.0.1、/git status；单独输入 / 无效。"
                            )
                            continue
                else:
                    if system_cmd_re.match(stripped_in) or self._is_executable_file(stripped_in):
                        run_direct_shell = stripped_in

                if run_direct_shell is not None:
                    ui = run_direct_shell
                    if self._is_executable_file(ui):
                        self._execute_file_directly(ui)
                        continue

                    user_input_cmd = ui
                    if system_cmd_re.match(ui):
                        if user_input_cmd.lower().startswith('ls') and os_name == 'nt':
                            user_input_cmd = 'dir ' + user_input_cmd[2:].strip()
                        elif user_input_cmd.lower().startswith('list') and os_name == 'nt':
                            user_input_cmd = 'dir ' + user_input_cmd[4:].strip()
                        elif user_input_cmd.lower().startswith('dir') and os_name != 'nt':
                            user_input_cmd = 'ls ' + user_input_cmd[3:].strip()

                        try:
                            if user_input_cmd.lower().startswith('cd '):
                                path = user_input_cmd[3:].strip()
                                result = self.action_change_directory(path)
                                if not result["success"]:
                                    print(f"❌ {result['error']}")
                            else:
                                try:
                                    process = subprocess.Popen(
                                        user_input_cmd,
                                        shell=True,
                                        stdin=sys.stdin,
                                        stdout=sys.stdout,
                                        stderr=sys.stderr,
                                        cwd=str(self.work_directory)
                                    )
                                    process.wait()
                                except Exception as e:
                                    print(f"❌ 命令执行异常: {e}")
                        except Exception as e:
                            print(f"❌ 系统命令执行异常: {e}")
                        continue

                    if os_name == "nt":
                        # e.g. /git status — not in the small whitelist but still direct shell
                        try:
                            process = subprocess.Popen(
                                ui,
                                shell=True,
                                stdin=sys.stdin,
                                stdout=sys.stdout,
                                stderr=sys.stderr,
                                cwd=str(self.work_directory)
                            )
                            process.wait()
                        except Exception as e:
                            print(f"❌ 命令执行异常: {e}")
                        continue

                last_result = None
                self._last_auto_removed_ephemeral = None
                original_user_task = user_input.strip()
                next_input = user_input
                is_first_round = True  # 标记是否为第一轮
                followup_json_misses = 0
                max_followup_json_misses = 4
                while True:
                    # 获取AI回复
                    print("🤖 AI正在思考...")
                    # 流式输出AI回复
                    # 只在第一轮用户输入时查询知识库，后续所有命令执行结果回传都不查询
                    stream_gen = self.call_ai(
                        user_input if last_result is None else next_input,
                        context=json.dumps(last_result, ensure_ascii=False) if last_result else "",
                        stream=True,
                        include_knowledge=is_first_round  # 只有第一轮查询知识库
                    )
                    ai_response = ""
                    try:
                        for chunk in stream_gen:
                            print(chunk, end="", flush=True)
                            ai_response += chunk
                        # AI输出完成后添加换行符
                        print()
                    except Exception as e:
                        print(f"\n❌ AI流式输出异常: {e}")
                    # 提取并执行命令
                    command = self.extract_json_command(ai_response)
                    if not command:
                        # After a command with last_action false (or any mid-chain step), model must keep emitting JSON
                        if last_result is not None and followup_json_misses < max_followup_json_misses:
                            followup_json_misses += 1
                            print(
                                f"\n⚠️ 未解析到 JSON 操作指令（续步重试 {followup_json_misses}/{max_followup_json_misses}）。"
                                "已提醒模型必须输出 ```json 代码块。"
                            )
                            next_input = (
                                next_input
                                + "\n\n【系统约束】上一条回复中没有任何可执行的 ```json``` 指令。"
                                "当前多步任务尚未结束。你必须在下一条回复中**包含恰好一个** ```json 代码块**，"
                                "内含一条操作 JSON（例如 script、shell、batch、move 等）；"
                                "仅当用户任务已全部完成时，才输出 {\"action\": \"done\"}。"
                                "禁止仅用纯文字罗列结果代替 JSON。"
                                "若原始需求含「创建脚本」「执行脚本」等，下一步必须是 script 或 shell/batch，"
                                "禁止再用 list + last_action:true 结束。"
                            )
                            is_first_round = False
                            continue
                        print("\nℹ️ 未检测到可执行 JSON 指令，结束本轮。")
                        break
                    if command.get("action") == "done":
                        followup_json_misses = 0
                        print("✅ AI已声明所有操作完成。");
                        break

                    # After last_action:false, refuse list+last_action:true when task clearly needs script/shell
                    if last_result is not None and self.operation_results:
                        prev_wrap = self.operation_results[-1]
                        prev_cmd = prev_wrap.get("command") or {}
                        if prev_cmd.get("last_action") is not True:
                            if command.get("action") == "list" and command.get("last_action") is True:
                                task_hints = (
                                    "脚本",
                                    "执行",
                                    "junction",
                                    "联结",
                                    "mklink",
                                    "批处理",
                                    ".bat",
                                    "自动",
                                    "运行",
                                    "软链",
                                    "符号",
                                )
                                if any(h in original_user_task for h in task_hints):
                                    followup_json_misses += 1
                                    if followup_json_misses > max_followup_json_misses:
                                        print("\n❌ 多次收到无效的提前结束指令，终止本轮。")
                                        break
                                    print(
                                        "\n⚠️ 已拒绝执行：上一步为 last_action:false，"
                                        "当前需求含脚本/执行/junction 等，不能用 list + last_action:true 收尾。"
                                    )
                                    next_input = (
                                        f"【用户原始需求】\n{original_user_task}\n\n"
                                        f"【上一有效步骤及返回】\n"
                                        f"{json.dumps(self.operation_results[-1], ensure_ascii=False)}\n\n"
                                        "【错误】请输出 script（写入 .bat/.ps1 等），再 shell 执行；"
                                        "或使用 batch。不要 list 工作目录结束。"
                                    )
                                    is_first_round = False
                                    continue

                    followup_json_misses = 0
                    print("⚡ 执行操作...")
                    result = self.execute_command(command)
                    # 保存操作结果
                    self.operation_results.append({
                        "command": command,
                        "result": result,
                        "timestamp": datetime.now().isoformat()
                    })
                    last_result = result
                    
                    # 检查用户是否取消了操作
                    if not result.get("success", True) and (
                        "用户取消了操作" in result.get("error", "") or 
                        "用户拒绝" in result.get("error", "") or
                        "用户取消" in result.get("error", "")
                    ):
                        is_first_round = False
                        followup_json_misses = 0
                        # 向AI发送明确的取消消息，要求输出done命令
                        next_input = (
                            f"【用户原始需求】\n{original_user_task}\n\n"
                            "用户取消了操作，请不要再继续执行任何命令，直接输出'{\"action\": \"done\"}'"
                        )
                        continue
                    
                    # 第一轮结束后，后续轮次不再查询知识库
                    is_first_round = False
                    # 续步时必须带上原始需求，否则模型容易忘记「创建脚本/执行」等后续步骤
                    next_input = (
                        f"【用户原始需求（须全部完成；未完成前禁止用无意义的 list + last_action:true 结束）】\n"
                        f"{original_user_task}\n\n"
                        f"【上一条已执行命令及系统返回】\n"
                        f"{json.dumps(self.operation_results[-1], ensure_ascii=False)}\n\n"
                        "请根据原始需求继续输出下一条 ```json``` 指令。"
                        "若需求包含创建并执行脚本、批处理、junction 等，通常应先 script 写入文件，再 shell 执行；"
                        "不要仅列出当前工作目录来结束。"
                    )

                    if result.get("success", True) and command.get("last_action") == True:
                        print("✅ 操作已完成")
                        break

            except KeyboardInterrupt:
                print("\n👋 程序已中断，再见！")
                break
            except Exception as e:
                print(f"❌ 发生错误: {str(e)}")

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
        import sys
        import platform
        
        prompt = f"🤖 [{str(self.work_directory)}]: "
        
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
                # 尝试使用prompt_toolkit
                from prompt_toolkit import PromptSession
                from prompt_toolkit.history import InMemoryHistory
                
                # 创建历史记录
                history = InMemoryHistory()
                for entry in self.history_manager.get_all_history():
                    history.append_string(entry)
                
                # 创建会话
                session = PromptSession(history=history)
                
                # 获取用户输入
                user_input = session.prompt(prompt).strip()
                
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
                    print("\n👋 程序已中断，再见！")
                    sys.exit(0)
            except Exception as e:
                # 如果prompt_toolkit出错，回退到标准input
                print(f"⚠️ prompt_toolkit 出错，回退到标准输入: {e}")
                try:
                    user_input = input(prompt).strip()
                    if user_input:
                        self.history_manager.add_entry(user_input)
                    return user_input
                except KeyboardInterrupt:
                    print("\n👋 程序已中断，再见！")
                    sys.exit(0)
        else:
            # 非Windows系统使用简单的input
            try:
                user_input = input(prompt).strip()
                if user_input:
                    self.history_manager.add_entry(user_input)
                return user_input
            except KeyboardInterrupt:
                print("\n👋 程序已中断，再见！")
                sys.exit(0)
    
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
        
        try:
            # 在Windows下，如果是Python文件，需要特殊处理
            if user_input.endswith('.py') or user_input.split()[0].endswith('.py'):
                # Python文件
                cmd = ['python', user_input]
            else:
                # 其他可执行文件
                cmd = user_input
            
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
