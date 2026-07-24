from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .console_utils import _ansi_gray, _ansi_rgb

STATUS_MODEL_COLOR_HEX = "#d7ba7d"
STATUS_WORKSPACE_COLOR_HEX = "#98c379"
STATUS_MODEL_COLOR_RGB = (0xD7, 0xBA, 0x7D)
STATUS_WORKSPACE_COLOR_RGB = (0x98, 0xC3, 0x79)


def clamp_status_token_usage_percent(value: Any) -> int:
    try:
        v = int(value or 0)
    except Exception:
        v = 0
    if v < 0:
        return 0
    if v > 999:
        return 999
    return v


def build_status_bar_render_data(
    model_name: str,
    workspace_name: str,
    active_chat_name: str,
    last_context_usage_percent: Any,
    reasoning_effort: str = "",
) -> Tuple[List[Tuple[str, str]], str, str]:
    usage_pct = clamp_status_token_usage_percent(last_context_usage_percent)
    usage_text = f"({usage_pct}%)"
    level = str(reasoning_effort or "").strip()
    status_bar_fragments: List[Tuple[str, str]] = [
        ("", "  "),
        (f"fg:{STATUS_MODEL_COLOR_HEX}", str(model_name)),
    ]
    if level:
        status_bar_fragments.append((f"fg:{STATUS_MODEL_COLOR_HEX} italic", f" {level}"))
    status_bar_fragments.extend(
        [
            ("", " "),
            (f"fg:{STATUS_WORKSPACE_COLOR_HEX}", str(workspace_name)),
            ("", " "),
            ("", str(active_chat_name)),
        ]
    )
    # ESC[3m = italic (ESC[23m clears italic). Keep it in the model color.
    level_plain = (
        f"\x1b[3m{_ansi_rgb(' ' + level, *STATUS_MODEL_COLOR_RGB)}\x1b[23m"
        if level
        else ""
    )
    status_bar_plain = (
        f"  {_ansi_rgb(str(model_name), *STATUS_MODEL_COLOR_RGB)}{level_plain} "
        f"{_ansi_rgb(str(workspace_name), *STATUS_WORKSPACE_COLOR_RGB)} "
        f"{str(active_chat_name)}"
    )
    return status_bar_fragments, status_bar_plain, usage_text


def refresh_status_context_usage_snapshot(
    session_memory_service: Any,
    user_input_hint: str = "",
    context_hint: str = "",
) -> None:
    try:
        svc = session_memory_service
        if svc is not None and hasattr(svc, "refresh_context_usage_snapshot"):
            svc.refresh_context_usage_snapshot(
                user_input_hint=str(user_input_hint or ""),
                context_hint=str(context_hint or ""),
            )
    except Exception:
        pass
