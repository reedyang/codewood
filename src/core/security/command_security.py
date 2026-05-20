import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Optional


def confirm_allowlist_path(agent: Any) -> Path:
    return agent.ai_workspace_dir / "confirm_allowlist.json"


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
        print(f"⚠️ 无法读取脚本以计算免确认哈希: {e}")
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
        print(f"⚠️ 读取 confirm_allowlist.json 失败: {e}")
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
        print(f"⚠️ 写入 confirm_allowlist.json 失败: {e}")
        return False


def shell_command_in_allowlist(agent: Any, command: str) -> bool:
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
            print("⚠️ 无法记录该脚本到免确认列表：哈希计算失败。")
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
        print(f"⚠️ 删除 confirm_allowlist.json 失败: {e}")
    return {
        "success": True,
        "message": (
            "已清空免确认列表，恢复每次询问"
            f"{'（已删除 confirm_allowlist.json）' if removed else ''}"
        ),
    }
