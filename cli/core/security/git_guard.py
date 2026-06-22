import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def parse_git_clone_command(command: str) -> Optional[Tuple[str, Optional[str]]]:
    s = str(command or "").strip()
    if not s:
        return None
    try:
        parts = shlex.split(s, posix=os.name != "nt")
    except ValueError:
        parts = s.split()
    if len(parts) < 3:
        return None
    if parts[0].lower() != "git" or parts[1].lower() != "clone":
        return None
    repo_url = ""
    target_dir: Optional[str] = None
    positional: List[str] = []
    i = 2
    while i < len(parts):
        tok = str(parts[i])
        if tok.startswith("-"):
            # Skip option value when present for common two-token options.
            if tok in ("-b", "--branch", "-o", "--origin", "--depth", "-c", "--config") and i + 1 < len(parts):
                i += 2
                continue
            i += 1
            continue
        positional.append(tok)
        i += 1
    if positional:
        repo_url = positional[0]
    if len(positional) >= 2:
        target_dir = positional[1]
    repo_url = str(repo_url or "").strip()
    if not repo_url:
        return None
    return repo_url, (str(target_dir).strip() if target_dir else None)


def repo_name_from_url(repo_url: str) -> str:
    raw = str(repo_url or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw.split("/")[-1].strip().lower()


def detect_git_remote_origin(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return ""


def is_git_repo_dir(path: Path) -> bool:
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    if (p / ".git").exists():
        return True
    try:
        proc = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
        return proc.returncode == 0 and "true" in (proc.stdout or "").strip().lower()
    except Exception:
        return False


def guard_git_clone_precheck(work_directory: Path, shell_cmd: str, shell_force: bool) -> Optional[Dict[str, Any]]:
    parsed = parse_git_clone_command(shell_cmd)
    if not parsed:
        return None
    repo_url, _target = parsed
    repo_name = repo_name_from_url(repo_url)
    wd = Path(work_directory).resolve()

    wd_is_repo = is_git_repo_dir(wd)
    wd_remote = detect_git_remote_origin(wd) if wd_is_repo else ""
    wd_name_match = bool(repo_name and wd.name.strip().lower() == repo_name)
    wd_remote_match = bool(wd_remote and wd_remote.strip().lower() == repo_url.strip().lower())
    if wd_is_repo and (wd_name_match or wd_remote_match):
        return None

    first_level_dirs: List[Path] = []
    try:
        first_level_dirs = sorted([p for p in wd.iterdir() if p.is_dir()], key=lambda x: x.name.lower())
    except Exception:
        first_level_dirs = []

    candidates: List[str] = []
    for d in first_level_dirs:
        if not is_git_repo_dir(d):
            continue
        d_remote = detect_git_remote_origin(d)
        d_name_match = bool(repo_name and d.name.strip().lower() == repo_name)
        d_remote_match = bool(d_remote and d_remote.strip().lower() == repo_url.strip().lower())
        if d_name_match or d_remote_match:
            mark = "remote-match" if d_remote_match else "name-match"
            candidates.append(f"{d.name} ({mark})")

    # Hard stop: matching repo candidate already exists under current dir.
    if candidates and not shell_force:
        return {
            "success": False,
            "retryable": False,
            "blocked_by_guard": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": (
                "A potential target repo already exists in a first-level subdirectory of the current directory. "
                "Please verify and switch to the existing repo before continuing."
            ),
            "error": (
                f"Direct clone of `{repo_url}` was blocked. Matching first-level subdirectories under `{wd}`: "
                + ", ".join(candidates)
                + ". Please reuse the existing repo first; only rerun clone with force=true after confirming no usable copy exists."
            ),
        }

    # If current directory is not target repo, require explicit confirmation to clone.
    if (not wd_is_repo or not (wd_name_match or wd_remote_match)) and not shell_force:
        top_dirs_preview = ", ".join(p.name for p in first_level_dirs[:30]) if first_level_dirs else "(none)"
        return {
            "success": False,
            "retryable": False,
            "blocked_by_guard": True,
            "needs_user_input": True,
            "input_type": "supplement",
            "question": "Please confirm whether a target repo already exists in first-level subdirectories before deciding to clone.",
            "error": (
                f"Unconfirmed git clone was blocked (repo={repo_url}). "
                f"Current directory `{wd}` is not the target repo; checked first-level subdirectories: {top_dirs_preview}. "
                "To continue cloning, explicitly confirm and rerun with force=true."
            ),
        }
    return None
