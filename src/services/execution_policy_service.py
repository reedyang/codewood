import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..core.security import command_security


def _print_with_auto_hide_tracking(agent: Any, text: str) -> None:
    msg = str(text or "")
    print(msg)


def confirm_allowlist_path(agent: Any) -> Path:
    return command_security.confirm_allowlist_path(agent)


def freedom_script_review_cache_path(agent: Any) -> Path:
    return agent.ai_workspace_dir / "freedom_script_review_cache.json"


def load_freedom_script_review_cache(agent: Any) -> None:
    agent._freedom_script_review_entries = {}
    p = freedom_script_review_cache_path(agent)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        ent = data.get("entries")
        if isinstance(ent, dict):
            agent._freedom_script_review_entries = {
                str(k): v for k, v in ent.items() if isinstance(v, dict)
            }
    except Exception as e:
        print(f"⚠️ 读取 freedom_script_review_cache.json 失败: {e}")


def save_freedom_script_review_cache(agent: Any) -> bool:
    try:
        p = freedom_script_review_cache_path(agent)
        payload = {
            "version": 1,
            "entries": dict(sorted(agent._freedom_script_review_entries.items())),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(f"⚠️ 写入 freedom_script_review_cache.json 失败: {e}")
        return False


def sha256_utf8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def freedom_script_eligible_for_combined_review(sp: Path) -> bool:
    """Local script types that get combined AI review in freedom mode (not python -c)."""
    if not sp.is_file():
        return False
    suf = sp.suffix.lower()
    if suf not in (".py", ".ps1", ".bat", ".cmd"):
        return False
    return True


def freedom_try_cached_user_script_review(
    agent: Any,
    path_key: str,
    script_body: str,
    command: Dict[str, Any],
) -> Optional[Tuple[bool, str]]:
    """If cache matches path + script hash + command JSON hash, return (skip, reason)."""
    cmd_json = json.dumps(command, ensure_ascii=False, sort_keys=True)
    h_body = sha256_utf8(script_body)
    h_cmd = sha256_utf8(cmd_json)
    rec = agent._freedom_script_review_entries.get(path_key)
    if not isinstance(rec, dict):
        return None
    if rec.get("script_sha256") != h_body or rec.get("command_sha256") != h_cmd:
        return None
    skip = bool(rec.get("skip_confirm"))
    reason = rec.get("reason") if isinstance(rec.get("reason"), str) else ""
    if not reason:
        reason = "（缓存无说明）"
    return (skip, reason)


def freedom_save_user_script_review_cache(
    agent: Any,
    path_key: str,
    script_body: str,
    command: Dict[str, Any],
    skip: bool,
    reason: str,
) -> None:
    cmd_json = json.dumps(command, ensure_ascii=False, sort_keys=True)
    agent._freedom_script_review_entries[path_key] = {
        "script_sha256": sha256_utf8(script_body),
        "command_sha256": sha256_utf8(cmd_json),
        "skip_confirm": skip,
        "reason": (reason or "")[:800],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_freedom_script_review_cache(agent)


def normalize_path_allowlist_key(p: Path) -> str:
    return command_security.normalize_path_allowlist_key(p)


def shell_script_allowlist_key(agent: Any, command: str) -> Optional[str]:
    """Resolved script file path key; ignores arguments. None if no script file (e.g. python -c)."""
    return command_security.shell_script_allowlist_key(agent, command)


def salted_sha256(text: str, salt: str) -> str:
    return command_security.salted_sha256(text, salt)


def shell_script_hash(agent: Any, script_path: Path) -> Optional[str]:
    """Compute salted hash for an allowlisted script file."""
    return command_security.shell_script_hash(agent, script_path)


def shell_executable_allowlist_key(agent: Any, command: str) -> str:
    """Stable key for invocations without a script path."""
    return command_security.shell_executable_allowlist_key(agent, command)


def load_confirm_allowlist(agent: Any) -> None:
    """Load shell targets that skip confirm with path+salted-hash verification."""
    return command_security.load_confirm_allowlist(agent)


def save_confirm_allowlist(agent: Any) -> bool:
    return command_security.save_confirm_allowlist(agent)


def shell_command_in_allowlist(agent: Any, command: str) -> bool:
    return command_security.shell_command_in_allowlist(agent, command)


def shell_confirm_should_offer_always(agent: Any, command: str) -> bool:
    """Do not offer 'a' when shell runs a session-ephemeral AI script."""
    return command_security.shell_confirm_should_offer_always(agent, command)


def script_basename_in_allowlist(agent: Any, safe_name: str) -> bool:
    return command_security.script_basename_in_allowlist(agent, safe_name)


def add_shell_command_allowlist(agent: Any, command: str) -> None:
    return command_security.add_shell_command_allowlist(agent, command)


def add_script_basename_allowlist(agent: Any, safe_name: str) -> None:
    return command_security.add_script_basename_allowlist(agent, safe_name)


def reset_always_confirm_skip(agent: Any) -> Dict[str, Any]:
    """Clear allowlist and restore y/n prompts."""
    return command_security.reset_always_confirm_skip(agent)


def prompt_confirm_yes_no_maybe_always(
    agent: Any,
    prompt_core: str,
    *,
    offer_always: bool,
    kind: str,
    shell_command: Optional[str] = None,
    script_basename: Optional[str] = None,
) -> bool:
    """
    kind: 'shell' | 'script' | 'text_file'. Returns True if user proceeds.
    The **a / always** option is only used for **shell**.
    """
    if kind == "shell" and shell_command is not None and shell_command_in_allowlist(
        agent, shell_command
    ):
        return True
    if offer_always:
        line = f"{prompt_core} (y/n/a，a=将本条加入免确认列表): "
    else:
        line = f"{prompt_core} (y/n): "
    raw = input(line).strip().lower()
    if offer_always and raw in ("a", "always"):
        if kind == "shell" and shell_command is not None:
            add_shell_command_allowlist(agent, shell_command)
        print(
            f"ℹ️ 已写入 {confirm_allowlist_path(agent)}。"
            "可使用 /always_confirm-reset 清空列表。"
        )
        return True
    return raw in ("y", "yes")


def parse_reversibility_response(text: str) -> Tuple[bool, str]:
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
                            return ok, (reason or ("安全" if ok else "不安全"))
                    except json.JSONDecodeError:
                        pass
                    break
    return False, "无法解析安全性判定"


def parse_combined_freedom_response(
    text: str,
) -> Tuple[bool, bool, Optional[bool], str]:
    """Parse one-shot freedom JSON: safe_auto, reversible, manipulation (optional), reason."""
    if not text or not isinstance(text, str):
        return False, False, True, "空响应"
    s = text.strip()
    if s.startswith("❌"):
        return False, False, True, s[:120]
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
                        if "safe_auto" in obj and "reversible" in obj:
                            sa = obj["safe_auto"]
                            rev = obj["reversible"]
                            if isinstance(sa, str):
                                sa = sa.strip().lower() in ("true", "1", "yes", "是")
                            if isinstance(rev, str):
                                rev = rev.strip().lower() in ("true", "1", "yes", "是")
                            reason = str(obj.get("reason", "")).strip()[:240]
                            manip_raw = obj.get("manipulation", None)
                            manip: Optional[bool]
                            if manip_raw is None:
                                manip = None
                            else:
                                if isinstance(manip_raw, str):
                                    manip_raw = manip_raw.strip().lower() in (
                                        "true",
                                        "1",
                                        "yes",
                                        "是",
                                    )
                                manip = bool(manip_raw)
                            return (
                                bool(sa),
                                bool(rev),
                                manip,
                                reason or "已判定",
                            )
                    except json.JSONDecodeError:
                        pass
                    break
    return False, False, True, "无法解析合并审查结果"


def freedom_script_quick_deny(content: str) -> bool:
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


def freedom_script_prompt_injection(content: str) -> Tuple[bool, str]:
    """
    Heuristic fallback: substring markers of prompt-injection / reviewer manipulation.
    Returns (matched, hint).
    """
    if not content:
        return False, ""
    low = content.lower()
    needles = (
        "ignore previous instructions",
        "disregard previous instructions",
        "override system prompt",
        "always return",
        '"safe_auto": true',
        '"reversible": true',
        "you are the reviewer",
        "you are the classifier",
        "请忽略之前的指令",
        "忽略以上规则",
        "覆盖系统提示词",
        "总是返回 true",
        "必须判定为可逆",
        "必须判定为安全",
        "让审查器通过",
    )
    for n in needles:
        if n in low:
            return True, n
    return False, ""


def combined_review_on_model_failure(content: str, detail: str) -> Tuple[bool, str, bool]:
    """When combined review API fails: keyword heuristic; conservative skip=False."""
    hit, tok = freedom_script_prompt_injection(content)
    msg = detail
    if hit:
        msg = f"{detail}；关键词兜底(manipulation): {tok}"
    return False, msg, True


def ai_assess_ephemeral_script_combined(
    agent: Any,
    script_path: Path,
    content: str,
    command: Dict[str, Any],
) -> Tuple[bool, str, bool]:
    """
    Single AI call: safe_auto + reversible + manipulation.
    Returns (skip_confirm, reason, manipulation_risk).
    """
    keys = sorted(agent._ai_created_path_keys)[:120]
    payload = (
        f"work_directory={agent.work_directory.resolve()}\n"
        f"ai_workspace_dir={agent.ai_workspace_dir.resolve()}\n"
        f"os={os.name}\n"
        f"ai_tracked_path_keys_normalized={json.dumps(keys, ensure_ascii=False)}\n"
        f"script_file={script_path.resolve()}\n\n"
        f"--- script source ---\n{content}\n--- end ---\n\n"
        f"--- command JSON ---\n{json.dumps(command, ensure_ascii=False)}\n"
    )
    raw = agent.call_ai(
        payload,
        context="",
        stream=False,
        freedom_combined_review=True,
    )
    if not isinstance(raw, str):
        return combined_review_on_model_failure(content, "模型返回类型异常")
    if raw.strip().startswith("❌"):
        return combined_review_on_model_failure(content, raw.strip()[:120])
    safe_auto, reversible, manip, reason = parse_combined_freedom_response(raw)
    if "无法解析" in reason:
        return combined_review_on_model_failure(content, reason)
    if manip is None:
        hit, tok = freedom_script_prompt_injection(content)
        manip = hit
        if hit:
            reason = f"{reason}；关键词兜底(manipulation): {tok}"
    skip = (not manip) and (safe_auto or ((not safe_auto) and reversible))
    return skip, reason, bool(manip)


def ai_assess_reversible(agent: Any, command: Dict[str, Any]) -> Tuple[bool, str]:
    payload = json.dumps(command, ensure_ascii=False)
    raw = agent.call_ai(
        payload, context="", stream=False, minimal_classifier=True
    )
    if not isinstance(raw, str):
        return False, "模型返回类型异常"
    if raw.strip().startswith("❌"):
        return False, raw.strip()[:120]
    return parse_reversibility_response(raw)


def freedom_auto_confirm(agent: Any, command: Dict[str, Any]) -> bool:
    """Return True to skip interactive confirmation (move/delete/shell/text_file/git write)."""
    policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
    if policy == "confirmation":
        return False
    if policy == "unlimited":
        return True
    action = command.get("tool") or command.get("action")
    params = command.get("args")
    if not isinstance(params, dict):
        params = command.get("params") or {}

    if action == "shell":
        cmd = params.get("command") or ""
        s = (cmd or "").strip()
        agent._manual_confirm_required_shell_once = False
        # If user selected "always" before, skip AI reversibility review entirely.
        load_confirm_allowlist(agent)
        if shell_command_in_allowlist(agent, s):
            _print_with_auto_hide_tracking(agent, "🦅 自由模式：命中免确认列表，跳过 AI 审核并直接执行。")
            return True

        if re.search(
            r"(?i)(?:^|[\s;&|])(?:py(?:thon)?(?:\d(?:\.\d)?)?|pythonw)\s+-\s*c\s+", s
        ):
            _print_with_auto_hide_tracking(agent, "🦅 自由模式：工作目录内联 Python（-c），跳过确认。")
            agent._manual_confirm_required_shell_once = False
            return True

        sp = agent._parse_shell_invoked_script_path(s)
        if sp is not None:
            sk = normalize_path_allowlist_key(sp)
            expected = agent._allowlist_shell_paths.get(sk)
            if expected:
                actual = shell_script_hash(agent, sp)
                if actual and actual == expected:
                    _print_with_auto_hide_tracking(agent, "🦅 自由模式：命中免确认脚本哈希校验，跳过 AI 审核并直接执行。")
                    agent._manual_confirm_required_shell_once = False
                    return True

            k = agent._ephemeral_path_key(sp)
            session_ephemeral = k in agent._ephemeral_script_paths
            combined_eligible = sp.is_file() and (
                session_ephemeral or freedom_script_eligible_for_combined_review(sp)
            )
            if combined_eligible:
                try:
                    body = sp.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    _print_with_auto_hide_tracking(agent, f"⚠️ 无法读取待审查脚本: {e}")
                    body = ""
                max_len = 200_000
                if len(body) > max_len:
                    body = body[:max_len] + "\n# ... [truncated for review] ..."
                if freedom_script_quick_deny(body):
                    _print_with_auto_hide_tracking(
                        agent,
                        "🦅 自由模式：脚本内容命中高风险启发规则（如注册表/系统配置相关），"
                        "改由操作安全判定。",
                    )
                    reversible, reason = ai_assess_reversible(agent, command)
                    if reversible:
                        _print_with_auto_hide_tracking(agent, f"🦅 判定为安全，自动跳过确认 — {reason}")
                        agent._manual_confirm_required_shell_once = False
                    else:
                        _print_with_auto_hide_tracking(agent, f"🦅 判定为不安全或不确定，仍需手动确认 — {reason}")
                        agent._manual_confirm_required_shell_once = True
                    return reversible
                use_cache = not session_ephemeral
                if use_cache:
                    cached = freedom_try_cached_user_script_review(agent, k, body, command)
                    if cached is not None:
                        skip_c, reason_c = cached
                        tag = "可自动跳过确认" if skip_c else "需手动确认"
                        _print_with_auto_hide_tracking(
                            agent,
                            f"🦅 自由模式：已使用配置文件中的脚本审核缓存（脚本与命令哈希一致），{tag} — {reason_c}"
                        )
                        agent._manual_confirm_required_shell_once = not bool(skip_c)
                        return skip_c
                _print_with_auto_hide_tracking(
                    agent,
                    "🦅 自由模式：正在审查脚本安全、诱导内容…"
                )
                skip, reason, inj_risk = ai_assess_ephemeral_script_combined(
                    agent, sp, body, command
                )
                if use_cache:
                    freedom_save_user_script_review_cache(
                        agent, k, body, command, skip, reason
                    )
                if inj_risk:
                    _print_with_auto_hide_tracking(
                        agent,
                        "🚫 自由模式：合并审查判定脚本存在审查诱导/提示词注入风险 — "
                        f"{reason}",
                    )
                    _print_with_auto_hide_tracking(agent, "🚫 建议不要执行该脚本；如必须执行，请先人工审查并手动确认。")
                    agent._manual_confirm_required_shell_once = True
                    return False
                if skip:
                    _print_with_auto_hide_tracking(agent, f"🦅 判定为可自动跳过确认 — {reason}")
                    agent._manual_confirm_required_shell_once = False
                else:
                    _print_with_auto_hide_tracking(agent, f"🦅 判定为需手动确认 — {reason}")
                    agent._manual_confirm_required_shell_once = True
                return skip

            if k in agent._ai_created_path_keys:
                _print_with_auto_hide_tracking(agent, "🦅 自由模式：命令作用于本会话已跟踪的 AI 产出路径，跳过确认。")
                agent._manual_confirm_required_shell_once = False
                return True

        _print_with_auto_hide_tracking(agent, "🦅 自由模式：正在请 AI 判定操作是否安全…")
        reversible, reason = ai_assess_reversible(agent, command)
        if reversible:
            _print_with_auto_hide_tracking(agent, f"🦅 判定为安全，自动跳过确认 — {reason}")
            agent._manual_confirm_required_shell_once = False
        else:
            _print_with_auto_hide_tracking(agent, f"🦅 判定为不安全或不确定，仍需手动确认 — {reason}")
            agent._manual_confirm_required_shell_once = True
        return reversible

    _print_with_auto_hide_tracking(agent, "🦅 自由模式：正在请 AI 判定操作是否安全…")
    reversible, reason = ai_assess_reversible(agent, command)
    if reversible:
        _print_with_auto_hide_tracking(agent, f"🦅 判定为安全，自动跳过确认 — {reason}")
    else:
        _print_with_auto_hide_tracking(agent, f"🦅 判定为不安全或不确定，仍需手动确认 — {reason}")
    return reversible
