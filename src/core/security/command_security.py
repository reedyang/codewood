import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, List, Optional

from ..localization import translate


def confirm_allowlist_path(agent: Any) -> Path:
    return agent.workspace_config_dir / "confirm_allowlist.json"


def normalize_path_allowlist_key(p: Path) -> str:
    try:
        r = p.resolve()
    except OSError:
        r = p
    s = str(r)
    return s.lower() if os.name == "nt" else s


def shell_script_allowlist_key(agent: Any, command: str) -> Optional[str]:
    invoked = agent._parse_shell_invoked_script_path(command)
    if invoked is None:
        return None
    return normalize_path_allowlist_key(invoked)


def salted_sha256(text: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}\n{text}".encode("utf-8")).hexdigest()


def shell_script_hash(agent: Any, script_path: Path) -> Optional[str]:
    salt = getattr(agent, "_confirm_allowlist_salt", "") or ""
    if not salt:
        return None
    try:
        body = script_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(
            translate(
                "warning.allowlist_hash_read_failed",
                getattr(agent, "display_language", "en") or "en",
                error=e,
            )
        )
        return None
    return salted_sha256(body, salt)


def shell_executable_allowlist_key(agent: Any, command: str) -> str:
    from ...actions.command_actions import _split_shell_like, _unwrap_shell_command_layers

    s = _unwrap_shell_command_layers(command.strip())
    if not s:
        return ""
    parts = _split_shell_like(s)
    if not parts:
        return ""
    tok = parts[0].strip('"').strip("'")
    p = Path(tok)
    if p.is_absolute():
        try:
            r = p.resolve()
            if r.is_file():
                return normalize_path_allowlist_key(r)
        except OSError:
            pass
        return str(p).lower() if os.name == "nt" else str(p)
    if any(sep in tok for sep in ("/", "\\")) or tok.startswith("."):
        if tok.startswith(".\\") or tok.startswith("./"):
            tok = tok[2:]
        rel = Path(tok)
        p_wd, p_temp, p_ws = agent._workspace_relative_script_triple(rel)
        for cand in (p_wd, p_temp, p_ws):
            try:
                if cand.is_file():
                    return normalize_path_allowlist_key(cand)
            except OSError:
                continue
        return normalize_path_allowlist_key(p_wd)
    return Path(tok).name.lower() if os.name == "nt" else Path(tok).name


def load_confirm_allowlist(agent: Any) -> None:
    agent._allowlist_shell_paths = {}
    agent._allowlist_shell_exes = set()
    agent._allowlist_script = set()
    agent._confirm_allowlist_salt = ""
    p = confirm_allowlist_path(agent)
    if not p.is_file():
        agent._confirm_allowlist_salt = secrets.token_hex(16)
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            agent._confirm_allowlist_salt = secrets.token_hex(16)
            return
        salt = data.get("salt")
        agent._confirm_allowlist_salt = (
            salt.strip() if isinstance(salt, str) and salt.strip() else secrets.token_hex(16)
        )
        for x in data.get("shell_scripts") or []:
            if not isinstance(x, dict):
                continue
            path_v = x.get("path")
            hash_v = x.get("hash")
            if not isinstance(path_v, str) or not path_v.strip():
                continue
            if not isinstance(hash_v, str) or not hash_v.strip():
                continue
            t = path_v.strip()
            if os.name == "nt":
                t = t.lower()
            agent._allowlist_shell_paths[t] = hash_v.strip().lower()
        for x in data.get("shell_exe_tokens") or []:
            if isinstance(x, str) and x.strip():
                t = x.strip()
                agent._allowlist_shell_exes.add(t.lower() if os.name == "nt" else t)
    except Exception as e:
        print(
            translate(
                "warning.confirm_allowlist_read_failed",
                getattr(agent, "display_language", "en") or "en",
                error=e,
            )
        )
        agent._confirm_allowlist_salt = secrets.token_hex(16)


def save_confirm_allowlist(agent: Any) -> bool:
    try:
        p = confirm_allowlist_path(agent)
        if not agent._confirm_allowlist_salt:
            agent._confirm_allowlist_salt = secrets.token_hex(16)
        payload = {
            "version": 3,
            "salt": agent._confirm_allowlist_salt,
            "shell_scripts": [
                {"path": k, "hash": v}
                for k, v in sorted(agent._allowlist_shell_paths.items(), key=lambda x: x[0])
            ],
            "shell_exe_tokens": sorted(agent._allowlist_shell_exes),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(
            translate(
                "warning.confirm_allowlist_write_failed",
                getattr(agent, "display_language", "en") or "en",
                error=e,
            )
        )
        return False


def shell_command_in_allowlist(agent: Any, command: str) -> bool:
    if _is_workspace_read_command(agent, command):
        return True
    sk = shell_script_allowlist_key(agent, command)
    if sk is not None:
        expected = agent._allowlist_shell_paths.get(sk)
        if not expected:
            return False
        sp = agent._parse_shell_invoked_script_path(command)
        if sp is None:
            return False
        actual = shell_script_hash(agent, sp)
        return bool(actual) and actual == expected
    ek = shell_executable_allowlist_key(agent, command)
    return bool(ek) and ek in agent._allowlist_shell_exes


def _is_workspace_read_command(agent: Any, command: str) -> bool:
    from ...actions.command_actions import (
        _split_shell_like,
        _strip_wrapping_quotes,
        _token_exe_base,
        _unwrap_shell_command_layers,
    )

    workspace_root = getattr(agent, "workspace_root", None)
    if not workspace_root:
        return False
    try:
        workspace_root_path = Path(str(workspace_root)).resolve()
    except OSError:
        return False

    s = _unwrap_shell_command_layers(str(command or "").strip())
    if not s:
        return False
    # Keep this whitelist strict: no pipelines/redirection/command chaining.
    if re.search(r"(\|\||&&|[|;<>])", s):
        return False

    parts = _split_shell_like(s)
    if not parts:
        return False
    exe = _token_exe_base(_strip_wrapping_quotes(parts[0]))
    read_exes = {"cat", "type", "more", "less", "head", "tail", "get-content", "gc"}
    if exe not in read_exes:
        return False

    path_tokens = _extract_read_path_tokens(exe, parts[1:])
    if not path_tokens:
        return False
    for token in path_tokens:
        path = _resolve_read_path_token(workspace_root_path, token)
        if path is None:
            return False
        try:
            if not bool(agent._is_path_under(path, workspace_root_path)):
                return False
        except Exception:
            return False
    return True


def _extract_read_path_tokens(exe: str, args: List[str]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        tok = str(args[i] or "").strip()
        if not tok:
            i += 1
            continue
        low = tok.lower()
        if low == "--":
            out.extend([x for x in args[i + 1 :] if str(x or "").strip()])
            break
        if exe in {"get-content", "gc"}:
            if low in {"-path", "-literalpath", "-lp"} and i + 1 < len(args):
                out.append(args[i + 1])
                i += 2
                continue
            if low.startswith("-path:") or low.startswith("-literalpath:"):
                _, _, rhs = tok.partition(":")
                if rhs.strip():
                    out.append(rhs.strip())
                i += 1
                continue
            if tok.startswith("-"):
                i += 1
                continue
            out.append(tok)
            i += 1
            continue
        if tok.startswith("-"):
            if exe in {"head", "tail"} and low in {"-n", "-c", "--lines", "--bytes"} and i + 1 < len(args):
                i += 2
                continue
            i += 1
            continue
        if exe == "more" and tok.startswith("/"):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _resolve_read_path_token(workspace_root: Path, token: str) -> Optional[Path]:
    cleaned = str(token or "").strip().strip('"').strip("'")
    if not cleaned:
        return None
    # Keep whitelist strict: reject wildcards and shell variables.
    if any(x in cleaned for x in ("*", "?", "$(", "${", "%", "`")):
        return None
    if cleaned.startswith(".\\") or cleaned.startswith("./"):
        cleaned = cleaned[2:]
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    try:
        return (workspace_root / candidate).resolve()
    except OSError:
        return workspace_root / candidate


def shell_confirm_should_offer_always(agent: Any, command: str) -> bool:
    invoked = agent._parse_shell_invoked_script_path(command)
    if invoked is None:
        return True
    try:
        k = agent._ephemeral_path_key(invoked)
    except OSError:
        return True
    return k not in agent._ephemeral_script_paths


def script_basename_in_allowlist(agent: Any, safe_name: str) -> bool:
    return bool(safe_name) and safe_name in agent._allowlist_script


def add_shell_command_allowlist(agent: Any, command: str) -> None:
    sk = shell_script_allowlist_key(agent, command)
    if sk is not None:
        sp = agent._parse_shell_invoked_script_path(command)
        if sp is None:
            return
        h = shell_script_hash(agent, sp)
        if not h:
            print("⚠️ Unable to add this script to the skip-confirm list: hash computation failed.")
            return
        agent._allowlist_shell_paths[sk] = h
    else:
        ek = shell_executable_allowlist_key(agent, command)
        if ek:
            agent._allowlist_shell_exes.add(ek)
    save_confirm_allowlist(agent)


def add_script_basename_allowlist(agent: Any, safe_name: str) -> None:
    if not safe_name:
        return
    agent._allowlist_script.add(safe_name)
    save_confirm_allowlist(agent)


def reset_always_confirm_skip(agent: Any) -> dict:
    agent._allowlist_shell_paths.clear()
    agent._allowlist_shell_exes.clear()
    agent._allowlist_script.clear()
    agent._confirm_allowlist_salt = ""
    removed = False
    try:
        p = confirm_allowlist_path(agent)
        if p.is_file():
            p.unlink()
            removed = True
    except OSError as e:
        print(
            translate(
                "warning.confirm_allowlist_delete_failed",
                getattr(agent, "display_language", "en") or "en",
                error=e,
            )
        )
    return {
        "success": True,
        "message": (
            "Skip-confirm list cleared. Confirmation prompts are now restored for each operation"
            f"{' (confirm_allowlist.json deleted)' if removed else ''}"
        ),
    }

