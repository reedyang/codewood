import hashlib
import json
import os
import secrets
import shlex
from pathlib import Path
from typing import Any, Optional


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
    s = command.strip()
    if not s:
        return ""
    if s.lower().startswith("call "):
        s = s[5:].strip()
    try:
        parts = shlex.split(s, posix=os.name != "nt")
    except ValueError:
        parts = s.split()
    if not parts:
        return ""
    base0 = parts[0].replace("\\", "/").split("/")[-1].lower().rstrip(".exe")
    if len(parts) >= 3 and base0 == "cmd" and parts[1].lower() in ("/c", "/k"):
        return shell_executable_allowlist_key(agent, " ".join(parts[2:]))
    tok = parts[0].strip('"').strip("'")
    if tok.startswith(".\\") or tok.startswith("./"):
        tok = tok[2:]
    p = Path(tok)
    if p.is_absolute() or (os.name == "nt" and len(tok) >= 2 and tok[1] == ":"):
        try:
            r = p.resolve()
            if r.is_file():
                return normalize_path_allowlist_key(r)
        except OSError:
            pass
        return str(p).lower() if os.name == "nt" else str(p)
    return Path(tok).name.lower() if os.name == "nt" else Path(tok).name


def load_confirm_allowlist(agent: Any) -> None:
    agent._allowlist_shell_paths = {}
    agent._allowlist_shell_exes = set()
    agent._allowlist_script = set()
    agent._confirm_allowlist_salt = ""
    p = agent._confirm_allowlist_path()
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
        p = agent._confirm_allowlist_path()
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
