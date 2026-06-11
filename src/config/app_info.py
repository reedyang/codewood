"""Application metadata and branding helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict

APP_INFO: Dict[str, str] = {
    "name": "Code Wood",
    "version": "0.0.1",
    "author": "Reed Yang",
    "description": "Code Wood AI Agent",
}


def get_app_name() -> str:
    name = str(APP_INFO.get("name") or "").strip()
    return name


def get_app_description() -> str:
    description = str(APP_INFO.get("description") or "").strip()
    return description or f"{get_app_name()} AI Agent"


def get_app_version() -> str:
    version = str(APP_INFO.get("version") or "").strip()
    return version or "0.0.1"


def get_app_display_version(prefix: str = "v") -> str:
    version = get_app_version()
    if version.lower().startswith(prefix.lower()):
        return version
    return f"{prefix}{version}"


def _slug_parts() -> list[str]:
    name = get_app_name()
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", name.lower()) if p]
    return parts or ["app"]


def get_app_slug_kebab() -> str:
    return "-".join(_slug_parts())


def get_app_slug_snake() -> str:
    return "_".join(_slug_parts())


def get_app_slug_compact() -> str:
    return "".join(_slug_parts())


def get_app_config_dirname() -> str:
    return f".{get_app_slug_compact()}"


def get_app_logger_root() -> str:
    return get_app_slug_compact()


def get_app_log_filename() -> str:
    return f"{get_app_logger_root()}.log"


def get_app_env_prefix() -> str:
    return get_app_slug_snake().upper()


def get_app_env_var(suffix: str) -> str:
    normalized_suffix = str(suffix or "").strip().upper().replace("-", "_")
    normalized_suffix = re.sub(r"[^A-Z0-9_]+", "_", normalized_suffix)
    normalized_suffix = re.sub(r"_+", "_", normalized_suffix).strip("_")
    if not normalized_suffix:
        return get_app_env_prefix()
    return f"{get_app_env_prefix()}_{normalized_suffix}"


def get_app_client_name() -> str:
    return get_app_slug_kebab()


def get_app_client_model_name() -> str:
    return f"{get_app_client_name()}-client"


def get_app_bundled_bin_dir() -> Path:
    """Return the absolute path to the project-bundled ``bin/`` directory.

    The repository ships pre-built executables (notably ``rg``) in
    ``<project_root>/bin``. This helper resolves that directory based
    on this module's location: ``app_info.py`` lives at
    ``<project_root>/src/config/app_info.py``, so the project root is
    two levels above this file.
    """
    return Path(__file__).resolve().parent.parent.parent / "bin"


def prepend_bundled_bin_to_path() -> str:
    """Ensure the bundled ``bin/`` directory is the first entry on PATH.

    Modifying ``os.environ`` once at startup is enough because every
    subprocess spawned via ``subprocess.Popen`` / ``subprocess.run``
    inherits the parent environment by default, and call sites that
    pass ``env=os.environ.copy()`` also pick up the prepended value.
    Pre-bundled tools such as ``rg`` therefore resolve transparently
    even inside pipelines (``rg ... | head``) and compound commands
    (``rg ... && other``) where head-of-command rewrites cannot reach.

    Idempotent: re-invocation will not duplicate the entry.

    Returns the resolved bundled-bin path as a string. When the
    bundled directory does not exist (e.g. running from a stripped
    install), PATH is left unchanged and an empty string is returned.
    """
    try:
        bin_dir = get_app_bundled_bin_dir()
    except Exception:
        return ""
    try:
        if not bin_dir.is_dir():
            return ""
    except Exception:
        return ""

    try:
        bin_str = str(bin_dir.resolve())
    except Exception:
        bin_str = str(bin_dir)

    sep = os.pathsep
    current = os.environ.get("PATH", "") or ""
    entries = current.split(sep) if current else []

    # Treat case-insensitive matches on Windows so we don't double-prepend
    # when the same path was already inserted with different casing.
    normalize = (lambda p: p.casefold()) if os.name == "nt" else (lambda p: p)
    target_norm = normalize(bin_str)

    if entries and normalize(entries[0]) == target_norm:
        # Already the first entry; nothing to do.
        return bin_str

    deduped = [e for e in entries if normalize(e) != target_norm]
    new_path = bin_str if not deduped else bin_str + sep + sep.join(deduped)
    os.environ["PATH"] = new_path
    return bin_str


# Git for Windows install root candidates. The default 64-bit installer
# lays down ``C:\Program Files\Git`` with ``usr\bin`` underneath; we
# append both so the model can fall back to ``bash``, GNU coreutils
# (``grep``, ``sed``, ``awk``, ``find``, ``sort``, ...), ``curl``,
# ``ssh``, etc. when no native Windows tool fits. Appending (rather
# than prepending) is deliberate: Windows ships ``find.exe`` and
# ``sort.exe`` of its own under ``System32`` with completely
# different semantics, and a head-of-PATH override would silently
# break long-standing Windows usage.
_WINDOWS_GIT_PATH_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Git",
    r"C:\Program Files\Git\usr\bin",
)


def append_windows_git_tools_to_path() -> list[str]:
    """Append known Git-for-Windows tool directories to PATH.

    On Windows the model frequently reaches for tools that ship with
    Git for Windows (``git`` itself, plus the bundled busybox/MSYS2
    coreutils such as ``bash``, ``grep``, ``sed``, ``awk``, ``find``,
    ``sort``, ``curl``, ``ssh``). When Git is installed but its
    ``cmd`` / ``usr\\bin`` directories aren't on PATH (a common state
    when launching from a non-Git terminal), those calls fail.

    This helper checks the well-known install locations
    (``C:\\Program Files\\Git`` and ``C:\\Program Files\\Git\\usr\\bin``)
    and appends any that exist to the **end** of PATH, so the Windows
    defaults under ``System32`` keep priority. Idempotent — repeated
    calls do not duplicate entries — and a no-op on non-Windows
    platforms.

    Returns the list of resolved directories that were ensured on
    PATH (in append order). Empty list when the platform is not
    Windows or none of the candidates exist.
    """
    if os.name != "nt":
        return []

    sep = os.pathsep
    current = os.environ.get("PATH", "") or ""
    entries = current.split(sep) if current else []

    normalize = lambda p: p.casefold()
    existing_norm = {normalize(e) for e in entries}

    appended: list[str] = []
    for candidate in _WINDOWS_GIT_PATH_CANDIDATES:
        try:
            if not Path(candidate).is_dir():
                continue
        except Exception:
            continue
        cand_norm = normalize(candidate)
        if cand_norm in existing_norm:
            appended.append(candidate)
            continue
        entries.append(candidate)
        existing_norm.add(cand_norm)
        appended.append(candidate)

    if appended:
        os.environ["PATH"] = sep.join(entries)
    return appended


def get_app_runtime_attr_name(suffix: str, *, leading_underscore: bool = False) -> str:
    raw = str(suffix or "").strip().lower().replace("-", "_")
    normalized = re.sub(r"[^a-z0-9_]+", "_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    base = get_app_slug_snake()
    name = f"{base}_{normalized}" if normalized else base
    return f"_{name}" if leading_underscore else name
