import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.security import command_security


CONFIRM_SUPPLEMENT_ATTR = "_confirm_supplement_text"


def set_confirm_supplement(agent: Any, text: str) -> None:
    """Store the user's reject-with-supplement text on the agent.

    The confirm gate returns a plain ``False`` (declined) to its callers, so
    the supplementary text must ride along on the agent until the caller
    builds the tool result (``agent._confirm_declined_result``) that becomes
    the role:tool message the model receives.
    """
    setattr(agent, CONFIRM_SUPPLEMENT_ATTR, str(text or ""))


def get_confirm_supplement(agent: Any) -> str:
    return str(getattr(agent, CONFIRM_SUPPLEMENT_ATTR, "") or "")


def clear_confirm_supplement(agent: Any) -> None:
    setattr(agent, CONFIRM_SUPPLEMENT_ATTR, "")


def _print_with_auto_hide_tracking(agent: Any, text: str) -> None:
    msg = str(text or "")
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        print(msg)
    tracker = getattr(agent, "_register_tool_call_feedback_interstitial_output", None)
    if callable(tracker):
        try:
            tracker(msg)
        except Exception:
            pass


def _t(agent: Any, key: str, fallback: Optional[str] = None, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), fallback=fallback, **kwargs)


def confirm_allowlist_path(agent: Any) -> Path:
    return command_security.confirm_allowlist_path(agent)


def freedom_script_review_cache_path(agent: Any) -> Path:
    return agent.workspace_config_dir / "freedom_script_review_cache.json"


def load_freedom_script_review_cache(agent: Any) -> None:
    agent._freedom_script_review_entries = {}
    p = freedom_script_review_cache_path(agent)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        if data.get("version") != 2:
            return
        ent = data.get("entries")
        if isinstance(ent, dict):
            agent._freedom_script_review_entries = {
                str(k): v for k, v in ent.items() if isinstance(v, dict)
            }
    except Exception as e:
        _print_with_auto_hide_tracking(
            agent,
            _t(
                agent,
                "execution_policy.cache.read_failed",
                fallback="⚠️ Failed to read freedom_script_review_cache.json: {error}",
                error=e,
            ),
        )


def save_freedom_script_review_cache(agent: Any) -> bool:
    try:
        p = freedom_script_review_cache_path(agent)
        payload = {
            "version": 2,
            "entries": dict(sorted(agent._freedom_script_review_entries.items())),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        _print_with_auto_hide_tracking(
            agent,
            _t(
                agent,
                "execution_policy.cache.write_failed",
                fallback="⚠️ Failed to write freedom_script_review_cache.json: {error}",
                error=e,
            ),
        )
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
        reason = _t(agent, "execution_policy.review.no_cache_reason", fallback="(no cache reason provided)")
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


def freedom_remove_user_script_review_cache_entry(agent: Any, path: Path) -> bool:
    """Drop the cached combined-review entry for a script file that was
    detected as deleted, so a re-created file gets a fresh review.  Several
    candidate key forms are matched because ``Path.resolve()`` on Windows may
    expand 8.3 short names to long names (and vice versa) depending on whether
    the file still exists when the key is computed."""
    candidates: set = set()
    try:
        candidates.add(agent._ephemeral_path_key(path))
    except Exception:
        pass
    try:
        candidates.add(os.path.normcase(str(path.resolve())))
    except Exception:
        pass
    try:
        candidates.add(os.path.normcase(os.path.abspath(str(path))))
    except Exception:
        candidates.add(os.path.normcase(str(path)))
    removed = False
    for key in list(agent._freedom_script_review_entries.keys()):
        if os.path.normcase(key) in candidates:
            agent._freedom_script_review_entries.pop(key, None)
            removed = True
    if removed:
        try:
            return save_freedom_script_review_cache(agent)
        except Exception:
            return False
    return False


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


def _confirm_choice_via_selection(
    agent: Any,
    prompt_core: str,
    *,
    offer_always: bool,
    display_command: Optional[str] = None,
    preview_segments: Optional[List[Dict[str, Any]]] = None,
    code_language: Optional[str] = None,
) -> Optional[str]:
    """Render the confirmation as a single-choice question with an extra
    "reject & supplement info" action.

    Reuses the same selection UX as the ``request_user_input`` tool (a GUI
    inline panel via ``_confirm_choice_provider``, or the TUI arrow-key
    selector). The user's pick is mapped **locally** back to one of
    ``\"y\" | \"n\" | \"a\" | \"n_supplement\"`` — it is never sent to the
    model. When the user picks "reject & supplement info", the supplementary
    text is stored on the agent via ``set_confirm_supplement`` and
    ``\"n_supplement\"`` is returned so the caller can keep the task running
    with the user's feedback. Returns ``None`` when no selection UI is
    available so the caller can fall back to the plain text prompt.
    """
    label_yes = _t(agent, "execution_policy.prompt.choice_yes", fallback="Yes, execute")
    label_no = _t(agent, "execution_policy.prompt.choice_no", fallback="No, cancel")
    label_always = _t(
        agent,
        "execution_policy.prompt.choice_always",
        fallback="Always (add to skip-confirm list)",
    )
    label_reject = _t(
        agent,
        "execution_policy.prompt.choice_reject_supplement",
        fallback="Reject & supplement info",
    )
    # Index-aligned option labels and their local y/n/a mapping. The trailing
    # reject-with-supplement action is rendered by the selection UIs (as an
    # inline free-text row in the TUI, a dedicated panel action in the GUI)
    # and never participates in the index mapping below.
    options = [label_yes, label_no]
    mapping = {label_yes: "y", label_no: "n"}
    if offer_always:
        options.append(label_always)
        mapping[label_always] = "a"

    # GUI: a structured confirm provider renders the inline choice panel and
    # blocks until the user picks. It returns the mapped y/n/a directly, or
    # ``"n_supplement"`` after storing the user's supplementary text on the
    # agent.
    gui_provider = getattr(agent, "_confirm_choice_provider", None)
    if callable(gui_provider):
        try:
            try:
                raw = gui_provider(
                    prompt_core,
                    list(options),
                    bool(offer_always),
                    display_command,
                    preview_segments,
                )
            except TypeError:
                try:
                    # Provider with ``command`` but no ``preview_segments``.
                    raw = gui_provider(
                        prompt_core,
                        list(options),
                        bool(offer_always),
                        display_command,
                    )
                except TypeError:
                    # Older provider signature without ``command``.
                    raw = gui_provider(prompt_core, list(options), bool(offer_always))
        except Exception:
            return None
        ans = str(raw or "").strip().lower()
        if ans == "n_supplement":
            return "n_supplement"
        if ans in ("y", "yes"):
            return "y"
        if ans in ("a", "always") and offer_always:
            return "a"
        # Empty / dismissed / anything else => treat as cancel (no execute).
        return "n"

    # TUI: arrow-key selector with the fixed options plus a trailing
    # "reject & supplement info" free-text row (the ``Other`` row relabeled).
    input_handler = getattr(agent, "input_handler", None)
    interactive = getattr(input_handler, "prompt_request_user_input_selection", None)
    if not callable(interactive):
        return None
    try:
        import sys

        if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
            return None
    except Exception:
        return None
    # Call the selector with the richest signature it supports, degrading the
    # keyword set on TypeError so older selectors still render the choice (just
    # without the live diff / highlighting). KeyboardInterrupt => cancel.
    kwarg_sets = [
        dict(allow_other=True, other_label=label_reject, command=display_command, preview_segments=preview_segments, code_language=code_language),
        dict(allow_other=True, other_label=label_reject, command=display_command, preview_segments=preview_segments),
        dict(allow_other=True, other_label=label_reject, command=display_command),
        dict(allow_other=True, other_label=label_reject),
    ]
    picked = None
    selected = False
    for idx, kwargs in enumerate(kwarg_sets):
        try:
            picked = interactive(prompt_core, list(options), False, **kwargs)
            selected = True
            break
        except TypeError:
            if idx == len(kwarg_sets) - 1:
                # Even the minimal signature is unsupported; fall back to text.
                return None
            continue
        except KeyboardInterrupt:
            return "n"
        except Exception:
            return None
    if not selected:
        return None
    if picked is None:
        # Esc / cancel => do not execute.
        return "n"
    label = str(picked).strip()
    if label in mapping:
        return mapping[label]
    # Not a fixed option: the user typed a supplement on the reject row.
    if label:
        set_confirm_supplement(agent, label)
        return "n_supplement"
    return "n"


def prompt_confirm_yes_no_maybe_always(
    agent: Any,
    prompt_core: str,
    *,
    offer_always: bool,
    kind: str,
    shell_command: Optional[str] = None,
    script_basename: Optional[str] = None,
    display_command: Optional[str] = None,
    preview_segments: Optional[List[Dict[str, Any]]] = None,
    code_language: Optional[str] = None,
    confirm_reason: Optional[str] = None,
) -> bool:
    """
    kind: 'shell' | 'script' | 'text_file'. Returns True if user proceeds.
    The **a / always** option is only used for **shell**.

    ``display_command`` is the command/script text to surface to the user. The
    selection-based UIs render it on its own styled line (syntax-highlighted in
    the GUI, color-emphasized in the TUI); the plain-text fallback appends it
    to the prompt.
    """
    if kind == "shell" and shell_command is not None and shell_command_in_allowlist(
        agent, shell_command
    ):
        return True

    clear_confirm_supplement(agent)

    if confirm_reason:
        prompt_core = f"{prompt_core}\n  AI review: {confirm_reason}"

    # Preferred path: a fixed-option single-choice question (same style as the
    # request_user_input tool) instead of a y/n/a text prompt. The user's pick
    # is mapped to y/n/a locally and never forwarded to the model. A
    # "reject & supplement info" pick stores the user's text on the agent
    # (``set_confirm_supplement``) so the caller can attach it to the
    # role:tool result and keep the task running. Falls back to the plain text
    # prompt below when no selection UI is available.
    selection = _confirm_choice_via_selection(
        agent,
        prompt_core,
        offer_always=offer_always,
        display_command=display_command,
        preview_segments=preview_segments,
        code_language=code_language,
    )
    if selection == "n_supplement":
        return False
    if selection is not None:
        raw = selection
    else:
        yes_no_always_suffix = _t(
            agent,
            "execution_policy.prompt.yes_no_always_suffix",
            fallback=" (y/n/a/r, a=add this entry to skip-confirm list, r=reject & supplement info): ",
        )
        yes_no_suffix = _t(
            agent,
            "execution_policy.prompt.yes_no_suffix",
            fallback=" (y/n/r, r=reject & supplement info): ",
        )
        # The selection UIs show the command on its own line; for the plain-text
        # fallback we inline it after the question so the user still sees it.
        prompt_with_command = prompt_core
        if display_command:
            prompt_with_command = f"{prompt_core}\n{display_command}"
        if offer_always:
            line = f"{prompt_with_command}{yes_no_always_suffix}"
        else:
            line = f"{prompt_with_command}{yes_no_suffix}"
        suspend_monitor = getattr(agent, "_suspended_input", None)
        if callable(suspend_monitor):
            raw = suspend_monitor(line).strip().lower()
        else:
            raw = input(line).strip().lower()
        if raw in ("r", "reject"):
            supplement_prompt = _t(
                agent,
                "execution_policy.prompt.reject_supplement_input",
                fallback="Supplementary info to continue (leave empty to just reject): ",
            )
            if callable(suspend_monitor):
                supplement = str(suspend_monitor(supplement_prompt) or "").strip()
            else:
                supplement = input(supplement_prompt).strip()
            if supplement:
                set_confirm_supplement(agent, supplement)
            return False
    if offer_always and raw in ("a", "always"):
        if kind == "shell" and shell_command is not None:
            add_shell_command_allowlist(agent, shell_command)
        _print_with_auto_hide_tracking(
            agent,
            _t(
                agent,
                "execution_policy.prompt.saved_to_allowlist",
                fallback="ℹ️ Saved to {path}. Use /always_confirm-reset to clear the list.",
                path=confirm_allowlist_path(agent),
            ),
        )
        return True
    return raw in ("y", "yes")


def parse_writes_files_response(text: str, agent: Any = None) -> Tuple[bool, str]:
    """Parse model JSON; on failure treat as writes_files (still require confirm)."""
    if not text or not isinstance(text, str):
        return True, _t(agent, "execution_policy.writes_files.empty_response", fallback="Empty response")
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
                        if "writes_files" in obj:
                            r = obj["writes_files"]
                            if isinstance(r, str):
                                r = r.strip().lower() in ("true", "1", "yes", "yes")
                            reason = str(obj.get("reason", "")).strip()[:200]
                            ok = bool(r)
                            return ok, (reason or ("safe" if ok else "unsafe"))
                    except json.JSONDecodeError:
                        pass
                    break
    return True, _t(agent, "execution_policy.writes_files.unable_to_parse", fallback="Unable to parse safety classification")


def parse_combined_freedom_response(
    text: str,
    agent: Any = None,
) -> Tuple[bool, bool, Optional[bool], str]:
    """Parse one-shot freedom JSON: safe_auto, writes_files, manipulation (optional), reason."""
    if not text or not isinstance(text, str):
        return False, True, True, _t(agent, "execution_policy.combined.empty_response", fallback="Empty response")
    s = text.strip()
    if s.startswith("❌"):
        return False, True, True, s[:120]
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
                        if "safe_auto" in obj and "writes_files" in obj:
                            sa = obj["safe_auto"]
                            wf = obj["writes_files"]
                            if isinstance(sa, str):
                                sa = sa.strip().lower() in ("true", "1", "yes", "yes")
                            if isinstance(wf, str):
                                wf = wf.strip().lower() in ("true", "1", "yes", "yes")
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
                                        "yes",
                                    )
                                manip = bool(manip_raw)
                            return (
                                bool(sa),
                                bool(wf),
                                manip,
                                reason or "classified",
                            )
                    except json.JSONDecodeError:
                        pass
                    break
    return False, False, True, _t(agent, "execution_policy.combined.unable_to_parse", fallback="Unable to parse combined review result")


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
        '"writes_files": true',
        "you are the reviewer",
        "you are the classifier",
        "ignore the previous instructions",
        "ignore the above rules",
        "override the system prompt",
        "always return true",
        "must be judged writes_files",
        "must be judged safe",
        "let the reviewer pass",
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
        msg = f"{detail}; keyword fallback (manipulation): {tok}"
    return False, msg, True


def ai_assess_ephemeral_script_combined(
    agent: Any,
    script_path: Path,
    content: str,
    command: Dict[str, Any],
) -> Tuple[bool, str, bool]:
    """
    Single AI call: safe_auto + writes_files + manipulation.
    Returns (skip_confirm, reason, manipulation_risk).
    """
    keys = sorted(agent._ai_created_path_keys)[:120]
    payload = (
        f"work_directory={agent.work_directory.resolve()}\n"
        f"workspace_config_dir={agent.workspace_config_dir.resolve()}\n"
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
        return combined_review_on_model_failure(
            content,
            _t(agent, "execution_policy.review.model_invalid_type", fallback="Model returned an invalid type"),
        )
    if raw.strip().startswith("❌"):
        return combined_review_on_model_failure(content, raw.strip()[:120])
    safe_auto, writes_files, manip, reason = parse_combined_freedom_response(raw, agent)
    if "Unable to parse" in reason:
        return combined_review_on_model_failure(content, reason)
    if manip is None:
        hit, tok = freedom_script_prompt_injection(content)
        manip = hit
        if hit:
            reason = _t(
                agent,
                "execution_policy.review.keyword_fallback",
                fallback="{reason}; keyword fallback (manipulation): {token}",
                reason=reason,
                token=tok,
            )
    skip = (not manip) and (not writes_files) and safe_auto
    return skip, reason, bool(manip)


def ai_assess_writes_files(agent: Any, command: Dict[str, Any]) -> Tuple[bool, str]:
    payload = json.dumps(command, ensure_ascii=False)
    raw = agent.call_ai(
        payload, context="", stream=False, minimal_classifier=True
    )
    if not isinstance(raw, str):
        return True, _t(agent, "execution_policy.review.model_invalid_type", fallback="Model returned an invalid type")
    if raw.strip().startswith("❌"):
        return True, raw.strip()[:120]
    return parse_writes_files_response(raw, agent)


def freedom_auto_confirm(agent: Any, command: Dict[str, Any]) -> bool:
    """Return True to skip interactive confirmation (move/delete/shell/text_file/git write)."""
    policy = str(getattr(agent, "execution_policy", "confirmation")).lower()
    mode_label_key = (
        "execution_policy.mode_label.moderate"
        if policy == "moderate"
        else "execution_policy.mode_label.unlimited"
        if policy == "unlimited"
        else "execution_policy.mode_label.confirmation"
    )
    fallback_label = (
        "Moderate mode"
        if policy == "moderate"
        else "Unlimited mode"
        if policy == "unlimited"
        else "Execution policy"
    )
    mode_label = _t(agent, mode_label_key, fallback=fallback_label)
    mode_prefix = f"🦅 {mode_label}:"
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
            return True

        if re.search(
            r"(?i)(?:^|[\s;&|])(?:py(?:thon)?(?:\d(?:\.\d)?)?|pythonw)\s+-\s*c\s+", s
        ):
            agent._manual_confirm_required_shell_once = False
            return True

        sp = agent._parse_shell_invoked_script_path(s)
        if sp is not None:
            k = agent._ephemeral_path_key(sp)
            session_ephemeral = k in agent._ephemeral_script_paths
            combined_eligible = sp.is_file() and (
                session_ephemeral or freedom_script_eligible_for_combined_review(sp)
            )
            if combined_eligible:
                try:
                    body = sp.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    _print_with_auto_hide_tracking(
                        agent,
                        _t(
                            agent,
                            "execution_policy.review.unable_to_read_script",
                            fallback="⚠️ Unable to read script for review: {error}",
                            error=e,
                        ),
                    )
                    body = ""
                max_len = 200_000
                if len(body) > max_len:
                    body = body[:max_len] + "\n# ... [truncated for review] ..."
                if freedom_script_quick_deny(body):
                    _print_with_auto_hide_tracking(
                        agent,
                        f"{mode_prefix} {_t(agent, 'execution_policy.review.high_risk_heuristics', fallback='script content matched high-risk heuristics (for example registry/system config related), falling back to operation safety classification.')}",
                    )
                    writes_files, reason = ai_assess_writes_files(agent, command)
                    agent._manual_confirm_required_shell_once = bool(writes_files)
                    return not writes_files
                use_cache = not session_ephemeral
                if use_cache:
                    cached = freedom_try_cached_user_script_review(agent, k, body, command)
                    if cached is not None:
                        skip_c, reason_c = cached
                        agent._manual_confirm_required_shell_once = not bool(skip_c)
                        return skip_c
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
                        f"🚫 {mode_label}: {_t(agent, 'execution_policy.review.prompt_injection_risk', fallback='combined review detected script review-manipulation/prompt-injection risk - {reason}', reason=reason)}",
                    )
                    _print_with_auto_hide_tracking(
                        agent,
                        _t(
                            agent,
                            "execution_policy.review.recommended_manual_review",
                            fallback="🚫 Recommended not to execute this script; if execution is required, perform manual review and confirm manually first.",
                        ),
                    )
                    agent._manual_confirm_required_shell_once = True
                    return False
                if skip:
                    agent._manual_confirm_required_shell_once = False
                else:
                    agent._manual_confirm_required_shell_once = True
                    agent._last_auto_confirm_reason = reason
                return skip

            if k in agent._ai_created_path_keys:
                agent._manual_confirm_required_shell_once = False
                return True

        writes_files, reason = ai_assess_writes_files(agent, command)
        agent._manual_confirm_required_shell_once = bool(writes_files)
        return not writes_files

    writes_files, _ = ai_assess_writes_files(agent, command)
    return not writes_files

