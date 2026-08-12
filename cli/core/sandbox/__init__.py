"""Sandbox execution isolation for shell commands.

Three levels:

- ``read_only``: commands run as a sandbox user with no write grants -
  files can be read (subject to the sandbox user's permissions) but not
  edited anywhere, including the workspace.
- ``workspace_write``: commands run as a sandbox user that may write only
  inside the current workspace (protected subdirectories such as ``.git``
  and the workspace config dir are explicitly denied).
- ``full_access``: commands run as the current user exactly as today
  (the equivalent of Codex's ``danger-full-access``).

On Windows the implementation follows the Codex Windows sandbox design:
dedicated local users (``CodewoodSandboxOffline`` / ``CodewoodSandboxOnline``)
provide the file-system identity, Windows Firewall rules keyed on the user
provide network isolation (the offline user is blocked), and Job Objects
manage the process-tree lifecycle. Other platforms currently report
``supported=False`` and are reserved for future backends (bubblewrap,
seatbelt, ...) behind the same :class:`SandboxBackend` interface.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

SANDBOX_LEVEL_READ_ONLY = "read_only"
SANDBOX_LEVEL_WORKSPACE_WRITE = "workspace_write"
SANDBOX_LEVEL_FULL_ACCESS = "full_access"
SANDBOX_LEVELS = (
    SANDBOX_LEVEL_READ_ONLY,
    SANDBOX_LEVEL_WORKSPACE_WRITE,
    SANDBOX_LEVEL_FULL_ACCESS,
)
DEFAULT_SANDBOX_LEVEL = SANDBOX_LEVEL_FULL_ACCESS
DEFAULT_SANDBOX_NETWORK = True

#: Windows-only local users backing the sandbox (kept in sync with the
#: provisioning code in :mod:`cli.core.sandbox.windows`). Local account names
#: are limited to 20 characters by Windows, so the "Sandbox" infix is dropped.
SANDBOX_USER_OFFLINE = "CodewoodSandOffline"
SANDBOX_USER_ONLINE = "CodewoodSandOnline"

_log = logging.getLogger("codewood.sandbox")


def normalize_sandbox_level(value: Any) -> str:
    """Return a valid sandbox level or the default (``full_access``)."""
    raw = str(value or "").strip().lower()
    if raw in SANDBOX_LEVELS:
        return raw
    if raw == "read-only":
        return SANDBOX_LEVEL_READ_ONLY
    if raw == "workspace-write":
        return SANDBOX_LEVEL_WORKSPACE_WRITE
    if raw in ("danger-full-access", "full", "unrestricted"):
        return SANDBOX_LEVEL_FULL_ACCESS
    return DEFAULT_SANDBOX_LEVEL


def normalize_sandbox_network(value: Any) -> bool:
    """Return a valid network-allow flag (default True)."""
    if isinstance(value, bool):
        return value
    raw = str(value or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_sandbox_backend() -> "SandboxBackend":
    """Return the platform backend; unsupported platforms get a stub."""
    from .base import UnsupportedSandboxBackend

    if sys.platform == "win32":
        try:
            from .windows import WindowsSandboxBackend

            return WindowsSandboxBackend()
        except Exception:  # pragma: no cover - defensive
            # Log instead of silently falling back: a frozen build that is
            # missing this module otherwise reports "unsupported" with no clue.
            _log.exception("failed to load the Windows sandbox backend")
    return UnsupportedSandboxBackend()


@dataclass
class SandboxPlan:
    """A resolved sandbox execution plan for one shell command.

    ``None`` (from :func:`sandbox_plan_for_agent`) means the command runs
    unsandboxed (``full_access`` or an unsupported platform).
    """

    level: str
    network: bool
    backend: "SandboxBackend"
    config_dir: Any = None

    def is_provisioned(
        self, config_dir: Any, workspace_root: Optional[str] = None
    ) -> bool:
        try:
            return bool(self.backend.is_provisioned(config_dir, workspace_root))
        except Exception:
            return False

    def spawn(
        self,
        command: str,
        cwd: str,
        env: dict,
        stdin_data: Optional[bytes] = None,
    ) -> Any:
        """Start ``command`` inside the sandbox and return a process-like object.

        The returned object mirrors the subset of ``subprocess.Popen`` that
        :func:`cli.tools.shell.action_shell_command` relies on:
        ``stdout`` / ``stdin`` file objects, ``pid``, ``poll()``, ``wait()``,
        ``kill()`` and ``returncode``.
        """
        return self.backend.spawn(
            command=command,
            cwd=cwd,
            env=env,
            stdin_data=stdin_data,
            level=self.level,
            network=self.network,
            config_dir=self.config_dir,
        )


def sandbox_plan_for_agent(agent: Any) -> Optional[SandboxPlan]:
    """Return the sandbox plan for a shell command, or ``None`` for full access."""
    level = normalize_sandbox_level(getattr(agent, "sandbox_level", None))
    network = normalize_sandbox_network(getattr(agent, "sandbox_network", None))
    if level == SANDBOX_LEVEL_FULL_ACCESS:
        return None
    backend = get_sandbox_backend()
    if not backend.is_supported():
        return None
    return SandboxPlan(
        level=level,
        network=network,
        backend=backend,
        config_dir=getattr(agent, "config_dir", None),
    )


def refresh_workspace_acls(
    agent: Any,
    workspace_root: Optional[str] = None,
    level: Optional[str] = None,
) -> None:
    """Best-effort: (re)apply sandbox ACLs to the active workspace.

    Called whenever the active workspace changes so the sandbox user (via its
    capability SIDs) can keep writing the new root. No-op for ``full_access``
    or when the sandbox is not provisioned. Never raises.
    """
    try:
        config_dir = getattr(agent, "config_dir", None)
        if level is None:
            # Read from the config file directly: at startup the agent's
            # ``sandbox_level`` attribute may not be initialized yet.
            from .config import CONFIG_KEY_LEVEL, read_sandbox_settings

            level = read_sandbox_settings(config_dir)[CONFIG_KEY_LEVEL]
        level = normalize_sandbox_level(level)
        if level == SANDBOX_LEVEL_FULL_ACCESS:
            return
        backend = get_sandbox_backend()
        if not backend.is_supported() or not backend.is_provisioned(config_dir):
            return
        apply_acl = getattr(backend, "apply_workspace_acls", None)
        if not callable(apply_acl):
            return
        root = workspace_root or getattr(agent, "workspace_root", None)
        if not root:
            return
        apply_acl(str(root), level, config_dir)
    except Exception:
        pass


def cleanup_workspace_acls(agent: Any, workspace_root: Optional[str] = None) -> None:
    """Best-effort: strip sandbox-managed ACLs from a workspace tree.

    Called when a workspace is deleted so the sandbox users / group /
    capability SIDs lose access to the forgotten directory, and before
    provisioning re-creates the sandbox users. Never raises.
    """
    try:
        config_dir = getattr(agent, "config_dir", None)
        backend = get_sandbox_backend()
        cleanup = getattr(backend, "cleanup_workspace_acls", None)
        if not callable(cleanup):
            return
        root = workspace_root or getattr(agent, "workspace_root", None)
        if not root:
            return
        cleanup(str(root), config_dir)
    except Exception:
        pass


def cleanup_all_sandbox_acls(config_dir: Any) -> None:
    """Best-effort: strip sandbox-managed ACLs from every recorded directory.

    Called by the serve process before the elevated setup recreates the
    sandbox users, so the old accounts' ACEs (removable by name) are swept
    while their SIDs still resolve.  No elevation is needed: every recorded
    directory belongs to the current user.  Never raises.
    """
    try:
        backend = get_sandbox_backend()
        if not backend.is_supported():
            return
        cleanup = getattr(backend, "cleanup_all_recorded_acls", None)
        if callable(cleanup):
            cleanup(config_dir)
    except Exception:
        pass


def resume_pending_sandbox_cleanup(config_dir: Any) -> None:
    """Best-effort: continue an interrupted sandbox ACL-removal sweep.

    Called at startup on a background thread: any sweep left unfinished by a
    previous run (tracked in ``sandbox_pending_cleanup.json``) is continued,
    so a cleanup that did not finish before the app exited is not lost.
    Never raises.
    """
    try:
        backend = get_sandbox_backend()
        if not backend.is_supported():
            return
        resume = getattr(backend, "resume_pending_cleanup", None)
        if callable(resume):
            resume(config_dir)
    except Exception:
        pass


def sandbox_block_error(agent: Any) -> Optional[str]:
    """Return an error message when the sandbox is configured but not ready.

    Used to fail closed: a configured sandbox level must never silently fall
    back to an unsandboxed run.
    """
    plan = sandbox_plan_for_agent(agent)
    if plan is None:
        return None
    config_dir = getattr(agent, "config_dir", None)
    if plan.is_provisioned(config_dir):
        return None
    return (
        "The sandbox is set to %r but it is not provisioned. "
        "Run 'codewood sandbox setup' from an elevated terminal, or open "
        "Settings > Security > Sandbox settings and click 'Set up sandbox'."
        % plan.level
    )
