from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import tempfile


AI_WORKSPACE_TOP_LEVEL_DIR_NAMES = frozenset({"temp", "skills"})
AI_WORKSPACE_TOP_LEVEL_DIR_NAMES_FOLD = frozenset(
    x.casefold() for x in AI_WORKSPACE_TOP_LEVEL_DIR_NAMES
)


class PathPolicy:
    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def is_path_under(self, child: Path, root: Path) -> bool:
        try:
            child.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    def workspace_skills_root(self) -> Path:
        return (self.agent.ai_workspace_dir / "skills").resolve()

    def resolve_user_path(self, raw_path: str) -> Path:
        p_raw = (raw_path or "").strip()
        if not p_raw:
            return self.agent.work_directory
        norm = p_raw.replace("\\", "/").lstrip("./")
        if norm == "workspace":
            return self.agent.ai_workspace_dir.resolve()
        if norm.startswith("workspace/skills/"):
            rest = norm[len("workspace/skills/") :]
            return (self.workspace_skills_root() / Path(rest)).resolve()
        if norm.startswith("workspace/"):
            rest = norm[len("workspace/") :]
            return (self.agent.ai_workspace_dir / Path(rest)).resolve()
        if norm.startswith("skills/"):
            rest = norm[len("skills/") :]
            return (self.workspace_skills_root() / Path(rest)).resolve()
        p = Path(p_raw)
        if p.is_absolute():
            return p.resolve()
        return (self.agent.work_directory / p).resolve()

    def is_workspace_skill_path(self, path: Path) -> bool:
        try:
            return self.is_path_under(path.resolve(), self.workspace_skills_root())
        except Exception:
            return False

    def is_smart_shell_protected_path(self, path: Path) -> bool:
        if self.is_workspace_skill_path(path):
            return False
        if self.is_path_under(path, self.agent.ai_workspace_dir):
            return False
        return self.is_path_under(path, self.agent._self_repo_root) or self.is_path_under(
            path, self.agent.config_dir
        )

    def reject_ai_workspace_root_level_write(self, path: Path) -> Optional[str]:
        msg = (
            "禁止在 Smart Shell workspace 根目录直接创建该路径。"
            "请使用子目录，例如 workspace/temp/…（临时）、workspace/skills/…（技能），"
            "或落在既有顶层目录（knowledge、memory、knowledge_db、logs）之下。"
        )
        try:
            r = path.resolve()
            aw = self.agent.ai_workspace_dir.resolve()
            if not self.is_path_under(r, aw):
                return None
            rel = r.relative_to(aw)
            if len(rel.parts) == 0:
                return msg
            if len(rel.parts) == 1:
                top = rel.parts[0]
                if top.casefold() in AI_WORKSPACE_TOP_LEVEL_DIR_NAMES_FOLD:
                    return None
                return msg
            return None
        except (ValueError, OSError):
            return None

    @staticmethod
    def _allow() -> Dict[str, Any]:
        return {"allowed": True, "error": ""}

    @staticmethod
    def _deny(error: str) -> Dict[str, Any]:
        return {"allowed": False, "error": str(error or "")}

    def can_write_path(self, path: Path, action: str) -> Dict[str, Any]:
        rej = self.reject_ai_workspace_root_level_write(path)
        if rej:
            return self._deny(rej)
        if self.is_smart_shell_protected_path(path):
            return self._deny(self.blocked_by_self_protection(action).get("error", ""))
        return self._allow()

    def can_modify_path(
        self,
        path_or_paths: Path | Iterable[Path],
        action: str,
        *,
        enforce_workspace_write_guard: bool = False,
    ) -> Dict[str, Any]:
        paths = (
            [path_or_paths]
            if isinstance(path_or_paths, Path)
            else [p for p in path_or_paths if isinstance(p, Path)]
        )
        if enforce_workspace_write_guard:
            for p in paths:
                rej = self.reject_ai_workspace_root_level_write(p)
                if rej:
                    return self._deny(rej)
        if any(self.is_smart_shell_protected_path(p) for p in paths):
            return self._deny(self.blocked_by_self_protection(action).get("error", ""))
        return self._allow()

    def can_run_shell_in_workdir(
        self,
        *,
        is_dependency_install: bool,
        is_ai_workspace_script: bool,
    ) -> Dict[str, Any]:
        if self.is_path_under(self.agent.work_directory, self.agent._self_repo_root):
            if not (is_dependency_install or is_ai_workspace_script):
                return self._deny(
                    (
                        "已拦截 shell 命令：当前位于 smart-shell 目录内，仅允许依赖安装命令"
                        "或执行 ai_workspace_dir 下的 AI 临时脚本。"
                    )
                )
        return self._allow()

    def can_read_for_grep(self, path: Path) -> bool:
        try:
            r = path.resolve()
            wd = self.agent.work_directory.resolve()
            aw = self.agent.ai_workspace_dir.resolve()
        except OSError:
            return False
        return self.is_path_under(r, wd) or self.is_path_under(r, aw)

    def can_write_grep_output(self, path: Path) -> Dict[str, Any]:
        rej = self.reject_ai_workspace_root_level_write(path)
        if rej:
            return self._deny(rej)
        try:
            r = path.resolve()
            wd = self.agent.work_directory.resolve()
            aw = self.agent.ai_workspace_dir.resolve()
            tmp = Path(tempfile.gettempdir()).resolve()
        except OSError:
            return self._deny("output_path 必须位于当前工作目录、AI 工作区或系统临时目录下")
        if self.is_path_under(r, wd) or self.is_path_under(r, aw) or self.is_path_under(r, tmp):
            return self._allow()
        return self._deny("output_path 必须位于当前工作目录、AI 工作区或系统临时目录下")

    @staticmethod
    def blocked_by_self_protection(action: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error": (
                f"已拦截操作 '{action}'：运行时保护已启用，"
                "AI 不可修改 smart-shell 自身（代码/配置）；`workspace/skills` 子目录除外。"
            ),
        }
