from __future__ import annotations

import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Any


def _t(agent: Any, key: str, **kwargs: Any) -> str:
    from ..core.localization import get_display_language, translate

    return translate(key, get_display_language(agent), **kwargs)


def _unwrap_nested_output_stream(stream: Any) -> Any:
    """Drill through slash-output indent wrappers to reach the real terminal
    stream. This mirrors the helper in ``chat_command_controller`` so that
    full-screen reloads triggered by a slash command (where stdout is
    indented by ``_build_internal_slash_output_stream``) do not inherit the
    indentation."""
    cur = stream
    seen = set()
    while cur is not None:
        sid = id(cur)
        if sid in seen:
            break
        seen.add(sid)
        nxt = getattr(cur, "_base_stream", None)
        if nxt is None:
            nxt = getattr(cur, "_primary", None)
        if nxt is None:
            break
        cur = nxt
    return stream if cur is None else cur


def language_usage(agent: Any) -> str:
    from ..core.localization import get_display_language, language_display_name

    lang = get_display_language(agent)
    current_name = language_display_name(lang)
    return (
        f"{_t(agent, 'common.usage')}\n"
        f"  /language <language code>\n\n"
        f"{_t(agent, 'language.current_label')} {current_name}\n"
        f"{_t(agent, 'language.available_label')}\n"
        f"  - en - {language_display_name('en')}\n"
        f"  - zh-CN - {language_display_name('zh-CN')}\n"
    )


def handle_language_builtin_command(agent: Any, builtin_line: str) -> bool:
    from ..core.localization import (
        apply_display_language,
        get_display_language,
        language_display_name,
        normalize_display_language,
    )

    raw = str(builtin_line or "").strip()
    if not raw.lower().startswith("language"):
        return False
    parts = shlex.split(raw)
    if len(parts) == 1:
        print(language_usage(agent))
        return True
    if len(parts) != 2:
        print(language_usage(agent))
        return True

    arg = str(parts[1] or "").strip()
    current = get_display_language(agent)

    normalized = normalize_display_language(arg)
    if normalized is None:
        print(_t(agent, "language.unsupported_with_usage", language=arg, usage=language_usage(agent)))
        return True

    if normalized == current:
        print(_t(agent, "language.already_set", language=language_display_name(normalized)))
        return True

    result = apply_display_language(agent, normalized)
    if not result.get("success"):
        print(_t(agent, "language.update_failed", error=result.get("error")))
        return True

    reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if callable(reload_fn):
        # The runtime loop normally records this slash command into chat
        # history in its ``finally`` block AFTER the controller returns, but
        # the reload below replays history right now — so the just-issued
        # ``/language ...`` line would be missing from the redrawn transcript.
        # Pre-record it here, and set a one-shot suppression flag so the
        # runtime's recorder doesn't append a duplicate.
        recorded_command = f"/{raw}"
        confirmation = _t(
            agent,
            "language.updated",
            language=language_display_name(normalized),
        )
        # Always terminate the recorded slash output with a newline so the
        # next prompt (and its status bar) start on a fresh line after the
        # chat-history reload replays this entry. Without the trailing
        # newline, ``_format_internal_slash_output`` emits the success line
        # without one and the prompt arrow ``› `` gets glued to the end of
        # the success text.
        if not confirmation.endswith("\n"):
            confirmation = f"{confirmation}\n"
        recorder = getattr(agent, "_record_internal_slash_execution_history", None)
        if callable(recorder):
            try:
                recorder(
                    raw_user_command=recorded_command,
                    output_text=confirmation,
                )
                # Make the runtime loop skip its own recording for this turn
                # so the entry is not duplicated.
                setattr(
                    agent,
                    "_suppress_next_internal_slash_history_record_once",
                    True,
                )
            except Exception:
                pass

        # Preserve the active chat's first-visible-index so the user sees the
        # same conversation starting point after re-rendering — same behaviour
        # as ``/chat reload``. We then escape any slash-output indentation
        # wrapper around stdout/stderr so the cleared screen, the startup
        # banner, and the replayed chat all render against the real terminal
        # stream (otherwise everything would be prefixed by 2 spaces).
        try:
            remember = getattr(
                agent, "_remember_active_chat_history_first_visible_index", None
            )
            if callable(remember):
                remember(0)
        except Exception:
            pass
        real_stdout = _unwrap_nested_output_stream(sys.stdout)
        real_stderr = _unwrap_nested_output_stream(sys.stderr)
        try:
            if (real_stdout is not sys.stdout) or (real_stderr is not sys.stderr):
                with redirect_stdout(real_stdout), redirect_stderr(real_stderr):
                    reload_fn()
            else:
                reload_fn()
        except Exception as e:
            print(
                _t(
                    agent,
                    "language.updated_with_reload_warning",
                    language=language_display_name(normalized),
                    error=e,
                )
            )
    else:
        print(_t(agent, "language.updated", language=language_display_name(normalized)))
    return True
