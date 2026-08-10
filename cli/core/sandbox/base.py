"""Platform-agnostic sandbox backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional


class SandboxBackend(ABC):
    """Interface implemented by each platform's sandbox.

    The contract is deliberately small: a backend knows whether it is
    available on this platform, whether the one-time provisioning ran, and
    how to launch a command inside the sandbox. The concrete process object
    returned by :meth:`spawn` must duck-type the subset of
    ``subprocess.Popen`` used by the shell tool (``stdout``/``stdin`` file
    objects, ``pid``, ``poll()``, ``wait()``, ``kill()``, ``returncode``).
    """

    name: str = "unsupported"

    @abstractmethod
    def is_supported(self) -> bool:
        """Whether this backend can run on the current platform."""

    @abstractmethod
    def is_provisioned(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> bool:
        """Whether the one-time sandbox setup completed successfully."""

    @abstractmethod
    def status(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """Return a status dict for the settings page / CLI."""

    def provision(
        self,
        config_dir: Any,
        workspace_root: Optional[str],
        level: str = "workspace_write",
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Run the one-time setup (idempotent). Must be called elevated.

        ``progress`` (optional) receives a human-readable line as each step
        starts/completes so callers can stream setup progress.
        """
        return {"ok": False, "message": "provisioning not supported on this platform"}

    def verify_credentials(
        self, config_dir: Any, fresh: bool = False
    ) -> Optional[bool]:
        """Return whether the stored secret can log on the sandbox users.

        ``None`` means the check is not applicable on this backend (or the
        users/secret are missing). The settings page uses this to surface a
        re-setup entry point when the users exist but their passwords no
        longer match this data directory's secret. ``fresh=True`` bypasses
        any cached result.
        """
        return None

    def cleanup_workspace_acls(
        self, workspace_root: Optional[str], config_dir: Any = None
    ) -> None:
        """Strip sandbox-managed ACLs from a workspace tree (best effort).

        Called before sandbox users are recreated and when a workspace is
        deleted. Unsupported backends do nothing.
        """
        return None

    @abstractmethod
    def spawn(
        self,
        command: str,
        cwd: str,
        env: dict,
        stdin_data: Optional[bytes],
        level: str,
        network: bool,
        config_dir: Any = None,
    ) -> Any:
        """Start ``command`` inside the sandbox and return a process object."""


class UnsupportedSandboxBackend(SandboxBackend):
    """Stub for platforms without a sandbox implementation yet.

    Keeping the stub in the ABC module means the settings page and the shell
    tool can always call the same interface: they will simply report that
    sandboxing is unavailable instead of crashing.
    """

    name = "unsupported"

    def is_supported(self) -> bool:
        return False

    def is_provisioned(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> bool:
        return False

    def status(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> Dict[str, Any]:
        return {
            "supported": False,
            "provisioned": False,
            "name": self.name,
            "message": "Sandboxing is not supported on this platform yet.",
        }

    def spawn(
        self,
        command: str,
        cwd: str,
        env: dict,
        stdin_data: Optional[bytes],
        level: str,
        network: bool,
        config_dir: Any = None,
    ) -> Any:
        raise RuntimeError("sandbox backend is not supported on this platform")
