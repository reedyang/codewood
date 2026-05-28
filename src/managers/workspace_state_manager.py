import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..config.app_info import get_app_config_dirname


class WorkspaceStateManager:
    """Encapsulates workspace state read/write and selection logic."""

    def __init__(self, agent: Any, default_workspace_id: str, default_workspace_name: str) -> None:
        self._agent = agent
        self._default_workspace_id = default_workspace_id
        self._default_workspace_name = default_workspace_name

    def path_identity_key(self, path: Path) -> str:
        value = str(self._agent._resolve_path_lenient(path))
        return value.casefold() if os.name == "nt" else value

    def workspace_id_for_path(self, path: Path) -> str:
        digest = hashlib.sha1(self.path_identity_key(path).encode("utf-8")).hexdigest()
        return f"ws_{digest[:12]}"

    def default_workspace_entry(self) -> Dict[str, Any]:
        root = self._agent._resolve_path_lenient(self._agent.config_dir / "workspace")
        return {
            "id": self._default_workspace_id,
            "name": self._default_workspace_name,
            "kind": "default",
            "root": str(root),
            "storage": str(root),
        }

    def workspace_root_path(self, entry: Dict[str, Any]) -> Path:
        if (
            str(entry.get("id") or "") == self._default_workspace_id
            or str(entry.get("kind") or "").lower() == "default"
        ):
            return self._agent._resolve_path_lenient(self._agent.config_dir / "workspace")
        raw = entry.get("root") or entry.get("path") or entry.get("storage") or ""
        root = self._agent._resolve_path_lenient(Path(str(raw)).expanduser())
        config_dirname = get_app_config_dirname()
        if root.name.casefold() == config_dirname.casefold():
            return root.parent
        return root

    def workspace_storage_path(self, entry: Dict[str, Any]) -> Path:
        if (
            str(entry.get("id") or "") == self._default_workspace_id
            or str(entry.get("kind") or "").lower() == "default"
        ):
            return self._agent._resolve_path_lenient(self._agent.config_dir / "workspace")
        storage = entry.get("storage")
        if storage:
            return self._agent._resolve_path_lenient(Path(str(storage)).expanduser())
        return self.workspace_root_path(entry) / get_app_config_dirname()

    def workspace_current_dir_path(self, entry: Dict[str, Any]) -> Optional[Path]:
        raw = entry.get("current_dir")
        if not raw:
            return None
        return self._agent._resolve_path_lenient(Path(str(raw)).expanduser())

    def load_workspace_state(self) -> Dict[str, Any]:
        raw_state: Dict[str, Any] = {}
        if self._agent.workspace_registry_path.exists():
            try:
                with open(self._agent.workspace_registry_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    raw_state = loaded
            except Exception as e:
                print(f"⚠️ Failed to read workspace registry, falling back to default workspace: {e}")

        raw_workspaces = raw_state.get("workspaces", {})
        if not isinstance(raw_workspaces, dict):
            raw_workspaces = {}

        default_entry = self.default_workspace_entry()
        old_default = raw_workspaces.get(self._default_workspace_id)
        if isinstance(old_default, dict) and old_default.get("current_dir"):
            default_entry["current_dir"] = str(
                self._agent._resolve_path_lenient(Path(str(old_default.get("current_dir"))))
            )

        workspaces: Dict[str, Dict[str, Any]] = {self._default_workspace_id: default_entry}
        for key, raw_entry in raw_workspaces.items():
            if key == self._default_workspace_id or not isinstance(raw_entry, dict):
                continue
            root_raw = raw_entry.get("root") or raw_entry.get("path")
            if not root_raw and raw_entry.get("storage"):
                storage_path = self._agent._resolve_path_lenient(Path(str(raw_entry.get("storage"))))
                root_path = (
                    storage_path.parent
                    if storage_path.name.casefold() == get_app_config_dirname().casefold()
                    else storage_path
                )
            elif root_raw:
                root_path = self._agent._resolve_path_lenient(Path(str(root_raw)))
            else:
                continue

            workspace_id = str(raw_entry.get("id") or key or self.workspace_id_for_path(root_path)).strip()
            if not workspace_id or workspace_id == self._default_workspace_id:
                workspace_id = self.workspace_id_for_path(root_path)
            name = str(raw_entry.get("name") or root_path.name or str(root_path)).strip()
            entry: Dict[str, Any] = {
                "id": workspace_id,
                "name": name,
                "kind": "custom",
                "root": str(root_path),
                "storage": str(root_path / get_app_config_dirname()),
            }
            if raw_entry.get("current_dir"):
                entry["current_dir"] = str(
                    self._agent._resolve_path_lenient(Path(str(raw_entry.get("current_dir"))))
                )
            workspaces[workspace_id] = entry

        active = str(raw_state.get("active") or self._default_workspace_id)
        if active not in workspaces:
            active = self._default_workspace_id
        return {"version": 1, "active": active, "workspaces": workspaces}

    def save_workspace_state(self) -> None:
        try:
            self._agent.config_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = self._agent.workspace_registry_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._agent._workspaces_state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self._agent.workspace_registry_path)
        except Exception as e:
            print(f"⚠️ Failed to save workspace registry: {e}")

    def ensure_workspace_dirs(self) -> None:
        try:
            self._agent.ai_workspace_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⚠️ Failed to create AI workspace directory {self._agent.ai_workspace_dir}: {e}")
        self._agent.ai_workspace_temp_dir = self._agent.ai_workspace_dir / "temp"
        try:
            self._agent.ai_workspace_temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⚠️ Failed to create workspace temp directory {self._agent.ai_workspace_temp_dir}: {e}")

    def apply_workspace_entry(self, entry: Dict[str, Any], fallback_dir: Path) -> None:
        workspace_id = str(entry.get("id") or self._default_workspace_id)
        if workspace_id == self._default_workspace_id:
            entry.update(self.default_workspace_entry())
        root = self.workspace_root_path(entry)
        storage = self.workspace_storage_path(entry)
        self._agent.workspace_id = workspace_id
        self._agent.workspace_name = str(
            entry.get("name")
            or (self._default_workspace_name if workspace_id == self._default_workspace_id else root.name)
        ).strip()
        self._agent.workspace_kind = str(
            entry.get("kind") or ("default" if workspace_id == self._default_workspace_id else "custom")
        ).lower()
        self._agent.workspace_root = root
        self._agent.ai_workspace_dir = storage
        self.ensure_workspace_dirs()

        current_dir = self.workspace_current_dir_path(entry)
        if current_dir is not None and current_dir.exists() and current_dir.is_dir():
            self._agent.work_directory = current_dir
        elif self._agent.workspace_kind != "default" and root.exists() and root.is_dir():
            self._agent.work_directory = root
        else:
            self._agent.work_directory = self._agent._resolve_path_lenient(fallback_dir)

        self._agent._workspaces_state["active"] = self._agent.workspace_id
        workspaces = self._agent._workspaces_state.setdefault("workspaces", {})
        if isinstance(workspaces, dict):
            workspaces[self._agent.workspace_id] = {
                "id": self._agent.workspace_id,
                "name": self._agent.workspace_name,
                "kind": self._agent.workspace_kind,
                "root": str(self._agent.workspace_root),
                "storage": str(self._agent.ai_workspace_dir),
                **(
                    {"current_dir": str(self._agent.work_directory)}
                    if entry.get("current_dir")
                    else {}
                ),
            }

    def save_current_workspace_position(self) -> None:
        self._agent._sync_active_chat_messages()
        if not hasattr(self._agent, "_workspaces_state"):
            return
        workspaces = self._agent._workspaces_state.setdefault("workspaces", {})
        if not isinstance(workspaces, dict):
            return
        entry = workspaces.get(getattr(self._agent, "workspace_id", self._default_workspace_id))
        if not isinstance(entry, dict):
            workspace_id = getattr(self._agent, "workspace_id", self._default_workspace_id)
            if workspace_id == self._default_workspace_id:
                entry = self.default_workspace_entry()
            else:
                entry = {
                    "id": workspace_id,
                    "name": getattr(self._agent, "workspace_name", str(workspace_id)),
                    "kind": getattr(self._agent, "workspace_kind", "custom"),
                    "root": str(getattr(self._agent, "workspace_root", self._agent.work_directory)),
                    "storage": str(
                        getattr(
                            self._agent,
                            "ai_workspace_dir",
                            self._agent.work_directory / get_app_config_dirname(),
                        )
                    ),
                }
            workspaces[workspace_id] = entry
        entry["current_dir"] = str(self._agent._resolve_path_lenient(self._agent.work_directory))
        self._agent._workspaces_state["active"] = getattr(
            self._agent, "workspace_id", self._default_workspace_id
        )
        self.save_workspace_state()

    def workspace_path_from_arg(self, raw: str) -> Path:
        text = str(raw or "").strip().strip('"').strip("'")
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = self._agent.work_directory / path
        return self._agent._resolve_path_lenient(path)

    def workspace_entry_by_root(
        self, root: Path, ignore_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        key = self.path_identity_key(root)
        workspaces = self._agent._workspaces_state.get("workspaces", {})
        if not isinstance(workspaces, dict):
            return None
        for workspace_id, entry in workspaces.items():
            if ignore_id and workspace_id == ignore_id:
                continue
            if isinstance(entry, dict) and self.path_identity_key(self.workspace_root_path(entry)) == key:
                return entry
        return None

    def workspace_name_exists(self, name: str, ignore_id: Optional[str] = None) -> bool:
        wanted = str(name or "").strip().casefold()
        workspaces = self._agent._workspaces_state.get("workspaces", {})
        if not wanted or not isinstance(workspaces, dict):
            return False
        for workspace_id, entry in workspaces.items():
            if ignore_id and workspace_id == ignore_id:
                continue
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == wanted:
                return True
        return False

    def workspace_entry_by_selector(self, selector: str) -> Optional[Dict[str, Any]]:
        text = str(selector or "").strip().strip('"').strip("'")
        if not text:
            return None
        workspaces = self._agent._workspaces_state.get("workspaces", {})
        if not isinstance(workspaces, dict):
            return None
        if text in workspaces and isinstance(workspaces[text], dict):
            return workspaces[text]
        folded = text.casefold()
        for entry in workspaces.values():
            if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == folded:
                return entry
        try:
            root = self.workspace_path_from_arg(text)
            return self.workspace_entry_by_root(root)
        except Exception:
            return None
