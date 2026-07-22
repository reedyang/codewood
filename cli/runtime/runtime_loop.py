"""Runtime main loop extracted from Agent.run."""
from __future__ import annotations

import json
import io
import os
import re
import shutil
import sys
import time
import unicodedata
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..config.app_info import (
    get_app_display_version,
    get_app_logger_root,
    get_app_name,
    get_app_runtime_attr_name,
)
from ..config.startup_tips import (
    format_tip_with_highlights,
    get_random_startup_tip_entry,
)
from ..core.config.config_jsonc import CONFIG_JSONC_FILENAME
from ..core.console_utils import GUI_CMD_OUTPUT_BEGIN, GUI_CMD_OUTPUT_END, GUI_SUBAGENT_SESSION_BEGIN, GUI_SUBAGENT_SESSION_END

from ..core.text_output_renderer import (
    format_assistant_display_response,
)
from ..core.logging.app_logging import get_logger
from ..core.console_utils import (
    GUI_FORCE_PROMPT_PREFIX,
    GUI_INTERNAL_COMMAND_PREFIX,
)
from ..controllers.builtin_command_router import dispatch_builtin_command
from ..commands import is_command, run_command
from ..tools.registry import (
    IMAGE_INPUT_TOOLS,
    MEMORY_TOOLS,
    PLAN_MODE_EXCLUDED_TOOLS,
    PLAN_MODE_ONLY_TOOLS,
    SMALL_MODEL_EXCLUDED_TOOLS,
)
from ..tools.plan import (
    PLAN_STATUS_COMPLETED,
    PLAN_STATUS_IN_PROGRESS,
    PLAN_STATUS_PENDING,
    UpdatePlanTool,
)
from ..core.console_utils import (
    _ansi_bold,
    _ansi_gray,
    _ansi_cyan,
    _WorkingStatusTicker,
    _render_working_status_line,
    _ansi_yellow,
)


_WORKING_STATUS_MARQUEE_FPS = 10.0
_STREAM_ATTR_TERMINAL_COLUMNS = get_app_runtime_attr_name("terminal_columns")
_STREAM_ATTR_OUTPUT_INDENT_WIDTH = get_app_runtime_attr_name("output_indent_width")
_MODEL_TOOL_RESULT_HISTORY_PREFIX = "[MODEL_TOOL_RESULT]"
_THINKING_TUI_MAX_VISIBLE_LINES = 5
_THINKING_TUI_INDENT = "  "


class _TeeTextStream:
    def __init__(self, primary: Any, mirror: io.StringIO) -> None:
        self._primary = primary
        self._mirror = mirror

    def write(self, text: str) -> int:
        chunk = str(text or "")
        wrote = self._primary.write(chunk)
        self._mirror.write(chunk)
        return int(wrote if isinstance(wrote, int) else len(chunk))

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
            pass
        try:
            self._mirror.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        fn = getattr(self._primary, "isatty", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return False
        return False

    @property
    def encoding(self) -> str:
        return str(getattr(self._primary, "encoding", "") or "utf-8")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)


def _estimate_visible_lines(agent: Any, text: str) -> int:
    s = str(text or "")
    if not s:
        return 0
    fn = getattr(agent, "_estimate_rendered_line_count", None)
    if callable(fn):
        try:
            return max(0, int(fn(s)))
        except Exception:
            pass
    normalized = s.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return max(0, len(parts))


def _terminal_rows_for_stream_reload(agent: Any) -> int:
    fn = getattr(agent, "_terminal_rows", None)
    if callable(fn):
        try:
            rows = int(fn() or 0)
            if rows > 0:
                return rows
        except Exception:
            pass
    try:
        rows = int(shutil.get_terminal_size(fallback=(80, 24)).lines or 24)
        if rows > 0:
            return rows
    except Exception:
        pass
    return 24


def _mark_pending_stream_history_reload(agent: Any, pending: bool) -> None:
    try:
        agent._pending_stream_history_reload_after_output = bool(pending)
    except Exception:
        pass


def _take_pending_stream_history_reload_request(agent: Any) -> bool:
    pending = bool(getattr(agent, "_pending_stream_history_reload_after_output", False))
    try:
        agent._pending_stream_history_reload_after_output = False
    except Exception:
        pass
    return pending


def _thinking_tui_char_display_width(ch: str) -> int:
    if not ch or unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _thinking_tui_text_display_width(text: str) -> int:
    total = 0
    for ch in str(text or ""):
        total += _thinking_tui_char_display_width(ch)
    return total


def _wrap_thinking_tui_line(agent: Any, text: str, width: int) -> List[str]:
    raw = str(text or "")
    if raw == "":
        return [""]
    limit = max(1, int(width or 1))
    wrap_fn = getattr(agent, "_wrap_feedback_text_by_display_width", None)
    if callable(wrap_fn):
        try:
            wrapped = wrap_fn(raw, limit)
            if isinstance(wrapped, list) and wrapped:
                return [str(part) for part in wrapped]
        except Exception:
            pass
    rows: List[str] = []
    current: List[str] = []
    current_w = 0
    for ch in raw:
        ch_w = _thinking_tui_char_display_width(ch)
        if current and current_w + ch_w > limit:
            rows.append("".join(current))
            current = [ch]
            current_w = ch_w
            continue
        current.append(ch)
        current_w += ch_w
    if current or not rows:
        rows.append("".join(current))
    return rows or [""]


def _build_thinking_tui_visible_rows(
    agent: Any,
    thinking_text: str,
    *,
    max_visible_lines: int = _THINKING_TUI_MAX_VISIBLE_LINES,
) -> List[str]:
    normalized = str(thinking_text or "").replace("\r\n", "\n").replace("\r", "\n")
    width = 80
    width_fn = getattr(agent, "_terminal_columns_for_line_estimate", None)
    if callable(width_fn):
        try:
            width = max(1, int(width_fn() or 0))
        except Exception:
            width = 80
    rows: List[str] = []
    wrap_width = max(1, width - len(_THINKING_TUI_INDENT))
    for logical_line in normalized.split("\n"):
        rows.extend(_wrap_thinking_tui_line(agent, logical_line, wrap_width))
    if not rows:
        rows = [""]
    limit = max(1, int(max_visible_lines or 1))
    return rows[-limit:]


class _NullStatusTicker:
    """No-op status ticker used in GUI (serve) mode.

    The desktop GUI renders its own "Working.../Worked for" indicator, so the
    terminal ticker (which writes a plain ``Working...`` line in non-TTY mode)
    would only add noise to the GUI step output.
    """

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _new_working_status_ticker(agent: Any) -> Any:
    """Build a status ticker, or a no-op one when running in GUI mode."""
    if bool(getattr(agent, "_gui_plain_stream", False)):
        return _NullStatusTicker()
    return _WorkingStatusTicker(
        sys.stdout,
        fps=_WORKING_STATUS_MARQUEE_FPS,
        language=getattr(agent, "display_language", None),
    )


def _stop_pre_task_status_ticker_for_console_output(
    agent: Any,
    pre_task_status_ticker: Optional[_WorkingStatusTicker],
) -> None:
    if pre_task_status_ticker is None:
        return None
    pre_task_status_ticker.stop()
    try:
        agent._clear_last_thinking_line()
    except Exception:
        pass
    return None


def _parse_tool_plans_from_model_message(
    message: Any,
) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(message, dict):
        return []
    tool_calls = message.get("tool_calls")
    plans = _parse_tool_plans_from_tool_calls_node(tool_calls)
    if plans:
        return plans
    _visible, pseudo_plans = _split_trailing_pseudo_tool_calls_text(message.get("content"))
    return pseudo_plans


def _parse_tool_args_node(raw_args: Any) -> Dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        raw_text = raw_args.strip()
        if not raw_text:
            return {}
        try:
            parsed = json.loads(raw_text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_tool_plans_from_tool_calls_node(
    tool_calls: Any,
) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(tool_calls, list) or not tool_calls:
        return []
    plans: List[Tuple[str, Dict[str, Any]]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        fn = tool_call.get("function")
        if isinstance(fn, dict):
            tool_name = str(fn.get("name") or "").strip()
            raw_args = fn.get("arguments")
        else:
            tool_name = str(
                tool_call.get("tool")
                or tool_call.get("name")
                or tool_call.get("tool_name")
                or ""
            ).strip()
            raw_args = (
                tool_call.get("args")
                if "args" in tool_call
                else tool_call.get("arguments")
            )
        if not tool_name:
            continue
        plans.append((tool_name, _parse_tool_args_node(raw_args)))
    return plans


def _parse_tool_plans_from_pseudo_tool_payload(
    payload: Any,
) -> List[Tuple[str, Dict[str, Any]]]:
    if isinstance(payload, dict):
        plans = _parse_tool_plans_from_tool_calls_node(payload.get("tool_calls"))
        if plans:
            return plans
        return _parse_tool_plans_from_tool_calls_node([payload])
    if isinstance(payload, list):
        return _parse_tool_plans_from_tool_calls_node(payload)
    return []


def _parse_tool_plan_from_model_message(
    message: Any,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    plans = _parse_tool_plans_from_model_message(message)
    return plans[0] if plans else None


def _recover_latest_history_tool_plans(agent: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """Recover the latest stored assistant tool plan from history.

    This is primarily a fallback for basic-chat / non-standard-tool rounds where
    the provider still returned real ``tool_calls`` on the wire, but the host
    asked ``call_ai(..., return_message=False)`` and therefore only received an
    empty string result. In that case AIOrchestrator has already persisted the
    assistant plan payload to history, so the runtime can recover and execute it
    instead of ending the loop prematurely.
    """
    history = list(getattr(agent, "conversation_history", None) or [])
    if not history:
        return []
    parser = getattr(agent, "_parse_model_tool_plan_history_content", None)
    if not callable(parser):
        return []
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        plans = _parse_tool_plans_from_tool_calls_node(item.get("tool_calls"))
        if plans:
            return plans
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        try:
            parsed = parser(content)
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            continue
        tool_name = str(parsed.get("tool") or "").strip()
        if not tool_name:
            continue
        args = parsed.get("args")
        return [(tool_name, args if isinstance(args, dict) else {})]
    return []


def _extract_nonstandard_tool_plans(
    agent: Any,
    ai_result: Any,
    ai_response: Any,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Extract executable tool plans for non-standard-tool rounds.

    Even when a turn opted out of ``return_message=True``, some providers still
    return a message object with real API-level ``tool_calls``. Others only
    persist that plan to history and return an empty string. Support both.
    """
    message: Optional[Dict[str, Any]] = None
    if isinstance(ai_result, dict):
        message = ai_result
    else:
        final_message = getattr(ai_result, "final_message", None)
        if isinstance(final_message, dict):
            message = final_message
    if isinstance(message, dict):
        direct_plans = _parse_tool_plans_from_tool_calls_node(message.get("tool_calls"))
        if direct_plans:
            return direct_plans
    if ai_response:
        return []
    return _recover_latest_history_tool_plans(agent)


def _build_tool_calls_from_plans(
    assistant_msg: Dict[str, Any],
    plans: List[Tuple[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Reconstruct the OpenAI ``tool_calls`` field from parsed tool plans.

    Some providers/models emit the tool call as raw JSON text instead of a
    structured ``tool_calls`` node on the message. Recover the per-call ``id``
    from the message content so the upcoming ``role: tool`` results keep
    pairing with the correct call.
    """
    content = str((assistant_msg or {}).get("content") or "")
    parsed_ids: Dict[str, str] = {}
    try:
        payload = json.loads(content)
        raw_calls = payload.get("tool_calls") if isinstance(payload, dict) else None
        if isinstance(raw_calls, list):
            for c in raw_calls:
                if not isinstance(c, dict):
                    continue
                fn = c.get("function") or {}
                name = str(fn.get("name") or "").strip()
                cid = str(c.get("id") or "").strip()
                if name and cid:
                    parsed_ids[name] = cid
    except Exception:
        parsed_ids = {}
    out: List[Dict[str, Any]] = []
    for idx, (tool_name, args) in enumerate(plans):
        cid = parsed_ids.get(str(tool_name or ""), "").strip() or f"call_{idx}"
        try:
            arguments = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)
        except Exception:
            arguments = "{}"
        out.append(
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": str(tool_name or ""),
                    "arguments": arguments,
                },
            }
        )
    return out


def _split_trailing_pseudo_tool_calls_text(
    text: Any,
) -> Tuple[str, List[Tuple[str, Dict[str, Any]]]]:
    visible, plans, _pseudo_text = _split_trailing_pseudo_tool_calls_text_details(text)
    return visible, plans


def _split_trailing_pseudo_tool_calls_text_details(
    text: Any,
) -> Tuple[str, List[Tuple[str, Dict[str, Any]]], str]:
    raw = str(text or "")
    if not raw.strip():
        return raw, [], ""
    rstripped = raw.rstrip()

    candidates: List[Tuple[int, str]] = []
    if rstripped.endswith("```"):
        fence_matches = list(re.finditer(r"(?im)^[ \t]*```(?:json|javascript|js)?[ \t]*\n", rstripped))
        if fence_matches:
            fence = fence_matches[-1]
            body_start = fence.end()
            body_end = rstripped.rfind("```")
            if body_end > body_start:
                candidates.append((fence.start(), rstripped[body_start:body_end].strip()))

    for m in reversed(list(re.finditer(r"(?m)^[ \t]*(?:\{|\[)", rstripped))):
        candidates.append((m.start(), rstripped[m.start():].strip()))

    for start, candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        plans = _parse_tool_plans_from_pseudo_tool_payload(payload)
        if not plans:
            continue
        visible = rstripped[:start].rstrip()
        if not visible and isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                visible = content.rstrip()
        pseudo_text = rstripped[start:].strip()
        return visible, plans, pseudo_text
    return raw, [], ""


def _should_prioritize_project_context_for_task(user_task: Any) -> bool:
    text = re.sub(r"\s+", " ", str(user_task or "").strip())
    if not text:
        return False

    lowered = text.casefold()
    score = 0

    strong_terms = (
        "code",
        "source code",
        "source",
        "codebase",
        "repo",
        "repository",
        "project",
        "workspace",
        "file",
        "function",
        "method",
        "class",
        "module",
        "package",
        "dependency",
        "library",
        "api",
        "bug",
        "error",
        "exception",
        "traceback",
        "stack trace",
        "test",
        "tests",
        "build",
        "compile",
        "debug",
        "fix",
        "refactor",
        "implement",
        "call chain",
        "call graph",
    )
    action_terms = (
        "debug",
        "fix",
        "repair",
        "refactor",
        "implement",
        "add",
        "remove",
        "update",
        "modify",
        "change",
        "analyze",
        "analyse",
        "explain",
        "review",
        "optimize",
        "investigate",
        "trace",
        "locate",
        "find",
        "where",
        "how",
        "why",
        "test",
        "build",
        "compile",
        "migrate",
        "port",
    )
    chinese_strong_terms = (
        "代码",
        "源码",
        "代码库",
        "项目",
        "仓库",
        "工作区",
        "文件",
        "函数",
        "方法",
        "类",
        "模块",
        "包",
        "依赖",
        "接口",
        "api",
        "调用链",
        "调用图",
        "错误",
        "异常",
        "报错",
        "测试",
        "编译",
        "构建",
    )
    chinese_action_terms = (
        "修复",
        "调试",
        "重构",
        "实现",
        "优化",
        "修改",
        "更新",
        "分析",
        "解释",
        "排查",
        "定位",
        "查找",
        "新增",
        "删除",
        "测试",
        "编译",
        "构建",
        "迁移",
        "移植",
    )

    for term in strong_terms:
        if term in lowered:
            score += 2
    for term in action_terms:
        if term in lowered:
            score += 1
    for term in chinese_strong_terms:
        if term in text:
            score += 2
    for term in chinese_action_terms:
        if term in text:
            score += 1

    if re.search(r"(?i)(?:`[^`]+`|[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|cs|md|json|ya?ml|toml))", text):
        score += 2
    if re.search(r"(?i)\b(src|tests?|docs?|lib|app|pkg|cmd|module|package|class|function|method|api)\b", lowered):
        score += 1

    return score >= 3


def _looks_like_pseudo_tool_call_text(ai_response: Any) -> bool:
    """
    Detect common signs that the model wrote a tool call in assistant text instead
    of using the API tool_calls field. The runtime first tries to recover
    executable trailing pseudo tool calls from assistant text; this detector is a
    fallback guard for pseudo tool-call text that could not be recovered into
    executable plans and therefore must be retried with standard API tool_calls.
    """
    text = str(ai_response or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "<tool_calls" in lowered or "assistant tool_calls" in lowered:
        return True
    if re.search(r"""(?is)(["']?\btool_calls\b["']?)\s*[:=]\s*\[""", text):
        return True
    if re.search(r"""(?is)```[^\n]*\n.*(["']?\btool_calls\b["']?)\s*[:=]\s*\[""", text):
        return True
    if re.search(r"""(?is)```[^\n]*\n.*(["']?\btool\b["']?)\s*[:=].*(["']?\b(?:args|arguments)\b["']?)\s*[:=]""", text):
        return True
    if re.search(r"(?im)^\s*tool\s*[:=]\s*[\w.-]+\s*$", text):
        return True
    has_tool_name = bool(
        re.search(r"""(?is)(["']?\btool\b["']?|["']?\bname\b["']?)\s*[:=]\s*["']?[\w.-]+""", text)
    )
    has_args = bool(
        re.search(r"""(?is)(["']?\bargs\b["']?|["']?\barguments\b["']?)\s*[:=]""", text)
    )
    if has_tool_name and has_args:
        return True
    return False


# A minimal, syntactically-correct example of an OpenAI-API-shape
# ``tool_calls`` payload. We attach this to the **second** retry
# prompt when the model keeps emitting pseudo tool calls in
# assistant text instead of using the standard ``tool_calls`` field.
# The first retry uses the descriptive prompt only — many models
# self-correct from that alone — but if the model fails again,
# showing the exact JSON shape removes any remaining ambiguity.
#
# The example uses ``shell`` because:
#   * it is the most ubiquitous tool in the catalog (always loaded);
#   * it has a single required string argument (``command``) so the
#     argument-encoding rule is obvious;
#   * the same shape applies to every other tool — only the
#     ``name`` and ``arguments`` content differ.
#
# Notes on the shape: ``arguments`` MUST be a JSON string (not an
# object) per the OpenAI tool-calling spec, so the example shows
# the inner JSON properly escaped. The ``id`` value is illustrative;
# any client-unique identifier is acceptable.
_PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON = (
    "{\n"
    '  "tool_calls": [\n'
    "    {\n"
    '      "id": "call_1",\n'
    '      "type": "function",\n'
    '      "function": {\n'
    '        "name": "shell",\n'
    '        "arguments": "{\\"command\\": \\"rg -n pattern src\\"}"\n'
    "      }\n"
    "    }\n"
    "  ]\n"
    "}"
)


def _build_pseudo_tool_call_retry_prompt(
    *,
    original_user_task: str,
    attempt: int,
) -> str:
    """Return the retry prompt for an unrecovered pseudo tool call.

    ``attempt`` counts retries within the current turn (1 = first
    retry). On the first retry we keep the prompt purely descriptive,
    matching the previous behavior; on the second and later retries
    we append a concrete OpenAI-API-shape ``tool_calls`` JSON example
    so the model has an unambiguous template to mirror.
    """
    base = (
        "Your previous assistant text contained a pseudo tool call, "
        "but no API-standard `tool_calls` were sent and the runtime could not recover it into executable tool plans.\n"
        "Tool calls written as JSON/YAML/tags/pseudocode in assistant text are invalid; "
        "this unrecoverable pseudo tool-call text will not be executed.\n"
        "Retry with one assistant message that has content and tool_calls together: "
        "content should contain only the short visible plan/status/result, "
        "and tool_calls should contain the actual API-standard tool call(s). "
        "Do not print or serialize a JSON/YAML/message object containing `content`, "
        "`tool_calls`, `tool`, `args`, or `arguments` in assistant text. "
        "If no further tool action is needed, reply with natural-language content only and no tool_calls."
    )
    if int(attempt) < 2:
        return base
    return (
        base
        + "\n\nThe previous retry also produced pseudo tool-call text. "
        "For reference, here is the exact shape an API-standard `tool_calls` payload must take "
        "(this is the OpenAI tool-calling format your client uses; only `name` and `arguments` should change):\n\n"
        + _PSEUDO_TOOL_CALL_RETRY_EXAMPLE_JSON
        + "\n\nKey points: `arguments` MUST be a JSON string (not an object), each call has its own client-unique `id`, "
        "and the entire `tool_calls` array belongs to the API field — never inside the visible `content`."
    )


_PLAN_STATUS_MARK = {
    PLAN_STATUS_PENDING: "[ ]",
    PLAN_STATUS_IN_PROGRESS: "[~]",
    PLAN_STATUS_COMPLETED: "[x]",
}


def _summarize_active_plan(agent: Any) -> Optional[Dict[str, Any]]:
    """Read the active chat's plan (if any) and return a small summary
    dict, or ``None`` when there is no usable plan to remind the model about.

    The summary contains:
      - ``items``: validated list of ``{step, status}`` dicts
      - ``has_pending``: True if at least one step is not yet ``completed``
      - ``in_progress_step``: the text of the current ``in_progress`` step,
        or ``""`` when none is marked.
    """
    record = UpdatePlanTool.current_plan(agent)
    if not isinstance(record, dict):
        return None
    raw_items = record.get("plan")
    if not isinstance(raw_items, list) or not raw_items:
        return None
    items: List[Dict[str, str]] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        step = str(entry.get("step") or "").strip()
        status = str(entry.get("status") or "").strip().lower()
        if not step:
            continue
        if status not in _PLAN_STATUS_MARK:
            status = PLAN_STATUS_PENDING
        items.append({"step": step, "status": status})
    if not items:
        return None
    has_pending = any(it["status"] != PLAN_STATUS_COMPLETED for it in items)
    in_progress_step = next(
        (it["step"] for it in items if it["status"] == PLAN_STATUS_IN_PROGRESS),
        "",
    )
    return {
        "items": items,
        "has_pending": has_pending,
        "in_progress_step": in_progress_step,
    }


def _format_active_plan_reminder(summary: Dict[str, Any]) -> str:
    """Render the in-flight plan as a compact ``[Active plan]`` block the
    model can consume in follow-up rounds. Returns ``""`` for empty input."""
    if not isinstance(summary, dict):
        return ""
    items = summary.get("items") or []
    if not items:
        return ""
    lines: List[str] = ["[Active plan]"]
    for idx, item in enumerate(items, start=1):
        status = str(item.get("status") or PLAN_STATUS_PENDING)
        mark = _PLAN_STATUS_MARK.get(status, "[ ]")
        step = str(item.get("step") or "").strip()
        lines.append(f"  {idx}. {mark} {step} ({status})")
    if summary.get("has_pending"):
        lines.append(
            "Keep this plan up to date: call `update_plan` to move the current "
            "step to `in_progress` and mark finished steps as `completed`. "
            "When all steps are done, call `update_plan` one last time so every "
            "step is `completed` before you reply with the final natural-language "
            "answer. The plan-completion requirement does not block clarifying "
            "questions: if you genuinely need more information from the user, "
            "call `request_user_input` (the host will pause for the user's reply); "
            "do not mark pending steps as `completed` just to end the turn."
        )
    else:
        lines.append(
            "All plan steps are `completed`. You may now finish with a natural-"
            "language reply and no further tool_calls."
        )
    return "\n".join(lines)


def _build_plan_finalize_nudge_prompt(
    *,
    original_user_task: str,
    plan_summary: Dict[str, Any],
) -> str:
    """Build the prompt that asks the model to reconcile a stale plan
    before the host returns to the command prompt.

    Centralized so every code path that ends a turn with a pending
    plan emits identical instructions; previously the prompt was
    inlined inside one ``if`` branch and other early-exit branches
    silently dropped the nudge entirely.
    """
    plan_block = _format_active_plan_reminder(plan_summary)
    return (
        f"{plan_block}\n\n"
        "Before finishing, reconcile the active plan with the work that "
        "has actually been done in this turn. Call `update_plan` to mark "
        "every step that is finished as `completed` (and remove or "
        "rewrite steps that no longer apply, providing an `explanation`). "
        "After the plan is up to date, reply with a short natural-"
        "language summary and no further tool_calls; the host will then "
        "return to the command prompt."
    )


def _warn_loop_ended_with_pending_plan(
    agent: Any,
    *,
    plan_finalize_nudged: bool,
    turn_used_request_user_input: bool,
) -> None:
    """Print a user-visible warning when the loop exits with a stale plan.

    The plan-finalize nudge gives the model exactly one extra round
    to flush its plan; if the model still doesn't call ``update_plan``
    after the nudge (or the nudge couldn't fire because of feature
    flags), we surface the situation explicitly. Without this banner
    the user is left wondering "plan 还没完成，为什么循环结束了？" —
    exactly the symptom that motivated this helper.

    Suppresses the warning when the model used ``request_user_input`` this
    turn, because pausing the plan to wait for the user's reply is a
    legitimate, expected handoff.
    """
    if turn_used_request_user_input:
        return
    try:
        summary = _summarize_active_plan(agent)
    except Exception:
        return
    if not summary or not summary.get("has_pending"):
        return
    pending = [
        str(it.get("step") or "").strip()
        for it in summary.get("items", [])
        if str(it.get("status") or "") != PLAN_STATUS_COMPLETED
        and str(it.get("step") or "").strip()
    ]
    if not pending:
        return
    pending_count = len(pending)
    if plan_finalize_nudged:
        en = (
            f"ℹ️ The active plan still has {pending_count} unfinished step(s) "
            "but the model finished without calling `update_plan` after the "
            "reminder. Send another message (e.g. \"continue\") to resume."
        )
        zh = (
            f"ℹ️ 活动计划仍有 {pending_count} 项未完成，但模型在收到提醒后仍未调用 "
            "`update_plan`。请再发一条消息（例如 \"继续\"）以恢复执行。"
        )
    else:
        en = (
            f"ℹ️ The active plan still has {pending_count} unfinished step(s); "
            "the loop ended without nudging the model to update the plan. "
            "Send another message (e.g. \"continue\") to resume."
        )
        zh = (
            f"ℹ️ 活动计划仍有 {pending_count} 项未完成，循环已结束且未提醒模型更新计划。"
            "请再发一条消息（例如 \"继续\"）以恢复执行。"
        )
    try:
        from ..core.localization import get_display_language

        lang = get_display_language(agent)
    except Exception:
        lang = ""
    message = zh if str(lang or "").lower().startswith("zh") else en
    try:
        print(message)
    except Exception:
        pass


def _maybe_offer_plan_execution_choice(
    agent: Any,
    *,
    turn_used_request_user_input: bool,
    plan_ready: bool,
) -> Optional[str]:
    """After a Plan-mode turn drafts a plan, ask the user how to proceed.

    Mirrors the GUI's "Execute now" affordance for the TUI and Codex's
    "Implement this plan?" prompt: once the agent has finished a plan (Plan
    mode sticky, a ``<proposed_plan>`` block was emitted this turn, and the
    turn didn't pause on ``request_user_input``), present an interactive
    selector with two paths:

      * **Yes, implement this plan** — leave Plan mode (switch to Agent mode)
        and queue a short "proceed" message so the next loop iteration runs the
        plan (the ``<proposed_plan>`` block is already in the transcript).
      * **No, and tell <App> what to do differently** — the trailing free-text
        row: highlighting it opens an inline input where the user types revision
        notes. The typed text is queued (Plan mode stays on) so the agent
        refines the plan.

    Returns the queued follow-up message string when the user chose a path,
    or ``None`` when the chooser doesn't apply or the user cancelled (Esc),
    in which case the caller falls back to the normal command prompt.
    """
    # Only offer the choice in Plan mode, and never when the turn handed off
    # to the user via request_user_input (that is its own pending interaction).
    if turn_used_request_user_input:
        return None
    if not bool(getattr(agent, "_plan_mode_sticky", False)):
        return None
    # The plan-ready signal is a ``<proposed_plan>`` block emitted this turn.
    if not plan_ready:
        return None
    return _present_plan_execution_chooser(agent)


def _latest_history_proposed_plan(agent: Any) -> Optional[str]:
    """Return the proposed-plan body when the LAST assistant message carries one.

    Only the final message qualifies (per spec): a plan buried mid-history was
    already acted on. Returns ``None`` when the tail isn't an assistant message
    holding a complete ``<proposed_plan>`` block.
    """
    history = list(getattr(agent, "conversation_history", None) or [])
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "")
        # Skip our internal bookkeeping records (slash/shell/interrupt markers
        # etc.) that ride along as assistant/user entries — they are never the
        # model's prose answer and must not hide a real trailing plan.
        if not content.strip():
            continue
        if role != "assistant":
            return None
        from ..core.proposed_plan import has_proposed_plan, latest_proposed_plan

        if has_proposed_plan(content):
            return latest_proposed_plan(content)
        return None
    return None


def _maybe_offer_plan_execution_choice_on_prompt(agent: Any) -> Optional[str]:
    """Re-offer the plan execute/modify chooser when returning to the prompt.

    The inline chooser only fires at the end of the drafting turn, so after the
    user exits and reloads a chat the affordance vanishes. This reload-time
    variant restores it: whenever the LAST assistant message carries a proposed
    plan the user hasn't dismissed, present the same chooser before the input
    prompt — in BOTH Plan and Agent mode (per spec). Returns the queued
    follow-up string, or ``None`` to fall through to the normal prompt.
    """
    if getattr(agent, "_queued_user_input", None) is not None:
        return None
    # A pending request_user_input prompt owns the interaction: don't let the
    # plan chooser pre-empt the question the agent is waiting on.
    if str(getattr(agent, "_pending_request_user_input_render", "") or "").strip():
        return None
    plan_text = _latest_history_proposed_plan(agent)
    if not plan_text:
        return None
    # Honor a prior "No"/modify dismissal for this exact plan so we don't nag.
    if str(getattr(agent, "_dismissed_proposed_plan_text", "") or "") == plan_text:
        return None
    # On the very first prompt after startup (reloading a chat that ended on a
    # proposed plan), anchor the transcript to the last user message and reprint
    # it before showing the chooser, so the user sees the conversation that led
    # to the plan rather than just the bare execute options.
    #
    # NOTE: ``_startup_prompt_pending`` is never set in __init__; the prompt
    # path reads it with a default of True (see agent._get_user_input_with_history).
    # We must mirror that default here, otherwise the chooser would render
    # before the startup transcript is ever printed.
    if bool(getattr(agent, "_startup_prompt_pending", True)):
        try:
            agent._remember_active_chat_history_tail_anchor()
            agent._reload_chat_history_from_anchor_on_resize()
            # The reload reprinted the transcript; this consumes the startup
            # one-shot so the prompt below doesn't suppress its separator based
            # on a now-stale "first prompt" assumption.
            agent._startup_prompt_pending = False
        except Exception:
            pass
    return _present_plan_execution_chooser(agent, dismiss_text=plan_text)


def _present_plan_execution_chooser(
    agent: Any, dismiss_text: Optional[str] = None
) -> Optional[str]:
    """Show the interactive execute/modify chooser and queue the chosen path.

    Shared by the end-of-turn offer and the reload-time re-offer. ``execute``
    switches off Plan mode (a no-op in Agent mode) and queues a proceed
    message; the free-text ``modify`` row keeps Plan mode and queues the typed
    notes. ``dismiss_text`` (when provided) is remembered on cancel/modify so
    the reload-time variant stops re-prompting for the same plan.
    """
    input_handler = getattr(agent, "input_handler", None)
    interactive = getattr(input_handler, "prompt_request_user_input_selection", None)
    if not (callable(interactive) and _request_user_input_interactive_supported(agent)):
        return None

    from ..core.localization import translate as _translate
    from ..config.app_info import get_app_name

    lang = getattr(agent, "display_language", None) or "en"
    t = lambda key, **kwargs: _translate(key, lang, **kwargs)

    header = t("runtime.plan_choice.header")
    execute_label = t("runtime.plan_choice.execute")
    modify_label = t("runtime.plan_choice.modify", app=get_app_name())

    try:
        picked = interactive(
            header,
            [execute_label],
            False,
            modify_label,
            header,
        )
    except KeyboardInterrupt:
        picked = None
    except Exception:
        return None

    if picked is None:
        # Cancelled (Esc/Ctrl-C): stay put and drop to the prompt. Remember the
        # dismissal so the reload-time re-offer doesn't immediately fire again.
        if dismiss_text:
            agent._dismissed_proposed_plan_text = dismiss_text
        return None
    answer = str(picked).strip()
    if not answer:
        if dismiss_text:
            agent._dismissed_proposed_plan_text = dismiss_text
        return None

    if answer == execute_label:
        # Leave Plan mode and proceed: switch to Agent mode (no-op if already
        # Agent) and queue a short proceed message so the next iteration
        # executes the drafted plan.
        try:
            agent._plan_mode_sticky = False
        except Exception:
            pass
        try:
            manager = getattr(agent, "_chat_state_manager", None)
            persist = getattr(manager, "persist_active_chat_plan_mode", None)
            if callable(persist):
                persist(False)
        except Exception:
            pass
        # This plan is being executed; clear any stale dismissal marker.
        try:
            agent._dismissed_proposed_plan_text = ""
        except Exception:
            pass
        try:
            print(t("runtime.plan_choice.executing"))
        except Exception:
            pass
        return t("runtime.plan_choice.execute_prompt")

    # Anything else is the user's free-text modification feedback. Switch back
    # to Plan mode (a no-op when already in Plan mode, but required when the
    # chooser was re-offered in Agent mode) so the agent refines the plan
    # rather than executing it — mirrors the GUI's "No, and tell ..." path.
    # The new turn will emit a fresh plan, so forget the old dismissal.
    try:
        agent._plan_mode_sticky = True
    except Exception:
        pass
    try:
        manager = getattr(agent, "_chat_state_manager", None)
        persist = getattr(manager, "persist_active_chat_plan_mode", None)
        if callable(persist):
            persist(True)
    except Exception:
        pass
    try:
        agent._dismissed_proposed_plan_text = ""
    except Exception:
        pass
    return answer


def _should_fire_plan_finalize_nudge(
    agent: Any,
    *,
    task_uses_standard_openai_tools: bool,
    plan_finalize_nudged: bool,
    turn_used_request_user_input: bool,
) -> Optional[Dict[str, Any]]:
    """Return the active-plan summary when a plan-finalize nudge is warranted.

    Returns ``None`` (no nudge needed) when any precondition fails:
    the model can't actually call tools to update the plan, the
    nudge was already fired this turn, the model used
    ``request_user_input`` (which is a legitimate handoff to the user),
    or the active plan has no pending steps.

    Centralizing this check guarantees every loop-exit path uses the
    same logic — the previous implementation only checked it inside
    the ``if not fallback_plans:`` branch, so other early exits
    (empty tool name, malformed plan, repeated-call detector, etc.)
    silently abandoned a still-pending plan.
    """
    if not task_uses_standard_openai_tools:
        return None
    if plan_finalize_nudged:
        return None
    if turn_used_request_user_input:
        return None
    summary = _summarize_active_plan(agent)
    if not summary or not summary.get("has_pending"):
        return None
    return summary


def _strip_leaked_internal_history_markers(text: Any) -> str:
    s = str(text or "")
    if not s:
        return ""
    idx = s.find(_MODEL_TOOL_RESULT_HISTORY_PREFIX)
    if idx < 0:
        # Also catch malformed sentinels where the model echoed the prefix
        # without the closing bracket (e.g. "[MODEL_TOOL_RESULT接下来..."),
        # which the live-stream filter likewise treats as a leak.
        bare_prefix = _MODEL_TOOL_RESULT_HISTORY_PREFIX.rstrip("]")
        idx = s.find(bare_prefix)
        if idx < 0:
            return s
    return s[:idx].rstrip()


def _stream_visible_text_with_json_pause(text: str, *, final: bool) -> str:
    s = str(text or "")
    if not s:
        return ""

    starts: List[int] = []

    def _json_container_start_before(pos: int) -> int:
        prefix = s[: max(0, int(pos))]
        matches = list(re.finditer(r"(?m)^[ \t]*(?:\{|\[)\s*$", prefix))
        if matches:
            start = matches[-1].start()
            for prev in reversed(matches[:-1]):
                between = s[prev.end() : start]
                if between.strip():
                    break
                start = prev.start()
            return start
        return pos

    lowered = s.lower()
    internal_marker_idx = s.find(_MODEL_TOOL_RESULT_HISTORY_PREFIX)
    if internal_marker_idx >= 0:
        starts.append(internal_marker_idx)
    else:
        # ``[MODEL_TOOL_RESULT`` (without the trailing ``]``) is a unique enough
        # literal that it is virtually never produced by natural prose. Cut at
        # any occurrence so that malformed sentinels — e.g. when the model
        # echoes the prefix without the closing bracket, or when the bracket
        # arrives in a later chunk — never leak to the terminal.
        bare_prefix = _MODEL_TOOL_RESULT_HISTORY_PREFIX.rstrip("]")
        bare_idx = s.find(bare_prefix)
        if bare_idx >= 0:
            starts.append(bare_idx)
        elif not final:
            # Withhold the tail of the buffer while it still looks like the
            # beginning of an internal "[MODEL_TOOL_RESULT]" sentinel that may
            # have been split across streaming chunks. Without this, partial
            # prefixes such as "[MODEL_TOOL_RES" would leak to the terminal
            # before the full sentinel arrives.
            marker = _MODEL_TOOL_RESULT_HISTORY_PREFIX
            # Find the longest non-empty prefix of ``marker`` that is also a
            # suffix of ``s``. We require length >= 2 so a lone "[" inside
            # normal prose (e.g. a markdown link) is not silently withheld.
            max_check = min(len(marker) - 1, len(s))
            for prefix_len in range(max_check, 1, -1):
                if s.endswith(marker[:prefix_len]):
                    starts.append(len(s) - prefix_len)
                    break
    for marker in ("<tool_calls", "<|assistant"):
        idx = lowered.find(marker)
        if idx >= 0:
            starts.append(idx)

    # Angle-bracket pseudo tool calls. Some models emit a tool invocation as
    # literal text like ``<requestuserinput{...}>`` or ``<request_user_input
    # {...}>`` instead of a real ``tool_calls`` entry. These must never reach
    # the user. Cut at any ``<`` that is immediately followed by an identifier
    # and then a JSON opener / whitespace+JSON / ``(`` — a shape that natural
    # prose effectively never produces. Matches both ``requestuserinput`` and
    # ``request_user_input`` spellings (and any other tool the model mangles).
    for m in re.finditer(r"(?is)<[a-z_][a-z0-9_]*\s*(?:\{|\[)", s):
        starts.append(m.start())

    # ``<proposed_plan>`` is a real protocol block we DO want to keep in the
    # final text (the GUI renders it as a card and the host parses it), but a
    # partial opening tag streamed before the block is complete would leak the
    # literal ``<proposed_plan`` / ``<propose`` text. While streaming, withhold
    # from the first ``<proposed_plan`` opener onward until the matching close
    # tag has arrived; once complete (or final), let it through untouched.
    if not final:
        open_idx = lowered.find("<proposed_plan")
        if open_idx >= 0 and "</proposed_plan>" not in lowered:
            starts.append(open_idx)
        else:
            # Withhold a trailing partial of the literal ``<proposed_plan>``
            # opener split across chunks (e.g. buffer ends with ``<propos``).
            opener = "<proposed_plan>"
            max_check = min(len(opener) - 1, len(s))
            for prefix_len in range(max_check, 1, -1):
                if lowered.endswith(opener[:prefix_len]):
                    starts.append(len(s) - prefix_len)
                    break
        # Withhold a trailing partial of ``<tool_calls`` / ``<|assistant``
        # split across chunks so ``<tool`` never flashes before the full tag.
        for opener in ("<tool_calls", "<|assistant"):
            max_check = min(len(opener) - 1, len(s))
            for prefix_len in range(max_check, 1, -1):
                if lowered.endswith(opener[:prefix_len]):
                    starts.append(len(s) - prefix_len)
                    break
        # Withhold a still-open inline-math span (``$\rightarrow`` before its
        # closing ``$``, or ``\(`` before ``\)``) so the raw LaTeX never flashes
        # in the live TUI stream before it is converted to a Unicode glyph once
        # the span completes. Only triggers when the open marker is followed by a
        # backslash (a LaTeX command), so ordinary prose dollar signs are left
        # alone. The downstream formatter converts the completed span.
        dollar_open = s.rfind("$")
        if dollar_open >= 0 and s.count("$") % 2 == 1:
            tail = s[dollar_open + 1 :]
            if "\\" in tail and "\n" not in tail:
                starts.append(dollar_open)
        paren_open = s.rfind("\\(")
        if paren_open >= 0 and "\\)" not in s[paren_open:]:
            tail = s[paren_open + 2 :]
            if "\n" not in tail:
                starts.append(paren_open)

    toolish_key_pattern = r"""(?is)(["']?\b(?:tool|tool_calls|args|arguments)\b["']?)\s*[:=]"""
    for m in re.finditer(r"(?im)^[ \t]*```[^\n]*\n", s):
        fence_header = m.group(0)
        block = s[m.start() :]
        fence_body = block[len(fence_header) :]
        fence_closed = bool(re.search(r"(?m)^```", fence_body))
        jsonish_fence = bool(re.search(r"(?i)```\s*(?:json|javascript|js)?\s*\n", fence_header))
        jsonish_body = bool(re.match(r"\s*(?:\{|\[)", fence_body))
        if (jsonish_fence or jsonish_body) and re.search(toolish_key_pattern, block):
            starts.append(m.start())
        elif (not final) and (not fence_closed) and (jsonish_fence or jsonish_body):
            starts.append(m.start())

    for m in re.finditer(r"""(?im)^[ \t]*(?:\{|\[)?[ \t]*(?:"tool"|'tool'|tool)\s*[:=]""", s):
        starts.append(_json_container_start_before(m.start()))
    for m in re.finditer(r"""(?im)^[ \t]*(?:\{|\[)[^\n]*(?:"tool"|'tool')""", s):
        starts.append(m.start())
    for m in re.finditer(r"""(?im)^[ \t]*(?:\{|\[)?[ \t]*(?:"tool_calls"|'tool_calls'|tool_calls)\s*[:=]""", s):
        starts.append(_json_container_start_before(m.start()))
    # Catch fully-serialized message envelopes where the model emits a
    # whole JSON object such as ``{"role":"assistant","content":"...","tool_calls":[...]}``
    # on a single line. The earlier patterns require ``"tool_calls"`` /
    # ``"tool"`` to be the *first* key after ``{``; this one allows other
    # keys (``role``, ``content``, ...) to appear before the toolish key
    # so the JSON tail is still cut off rather than streamed to the user.
    for m in re.finditer(
        r"""(?im)^[ \t]*(?:\{|\[)[^\n]*?(?:"tool_calls"|'tool_calls'|"arguments"|'arguments')""",
        s,
    ):
        starts.append(m.start())
    for m in re.finditer(r"""(?im)^[ \t]*\{[ \t]*(?:"content"|'content')\s*:""", s):
        candidate = s[m.start() :]
        if (not final) or re.search(r"""(?is)(?:"tool_calls"|'tool_calls')\s*:""", candidate):
            starts.append(m.start())
    # When ``final=True`` and a single line begins with ``{`` and is
    # already long enough that *no* prose would naturally start that
    # way (e.g. the assistant accidentally dumped a serialized message
    # envelope), be aggressive: cut at that ``{`` if the line also
    # contains the canonical envelope markers ``"role":`` or ``"name":``
    # alongside any toolish key indicator we expect from the API surface.
    if final:
        for m in re.finditer(
            r"""(?im)^[ \t]*\{[^\n]*?(?:"role"|'role'|"name"|'name')[^\n]*?(?:"tool_calls"|'tool_calls'|"function"|'function'|"arguments"|'arguments')""",
            s,
        ):
            starts.append(m.start())
        # Additional final-mode aggressive cut: ``\n\n{"`` whose opening
        # quote is immediately followed by a non-ASCII / non-identifier
        # character cannot be valid prose JSON (JSON keys are typically
        # ASCII identifiers). This pattern catches the case where the
        # model echoes its visible prose back inside a malformed
        # ``{"<prose>...`` envelope with no surrounding key=value
        # structure. Without this, the dangling JSON tail leaks into
        # the persisted history even though the streaming cutter
        # already hid it from the live terminal.
        for m in re.finditer(r"""(?m)(?:\n\n|\A)[ \t]*\{[ \t]*["']""", s):
            brace_idx = s.find("{", m.start())
            if brace_idx < 0:
                continue
            tail = s[brace_idx:]
            after_open = re.match(r"""[ \t]*\{[ \t]*["']""", tail)
            if not after_open:
                continue
            rest = tail[after_open.end():]
            if not rest:
                continue
            first_char = rest[0]
            if first_char.isascii() and (first_char.isalnum() or first_char == "_"):
                # Looks like a real JSON key opener; rely on the
                # envelope/toolish patterns above to handle it.
                continue
            starts.append(brace_idx)

    if not final:
        # Partial starts are kept in a tiny cache until they either become a
        # normal word/text fragment or complete into a recoverable pseudo tool
        # call suffix.
        for m in re.finditer(r"(?m)^[ \t]*(?:\{|\[)\s*$", s):
            starts.append(_json_container_start_before(m.start()))
        for m in re.finditer(r"""(?im)^[ \t]*(?:\{|\[)?[ \t]*(?:"?t(?:o(?:o(?:l)?)?)?|'?t(?:o(?:o(?:l)?)?)?)$""", s):
            starts.append(_json_container_start_before(m.start()))
        for m in re.finditer(r"""(?im)^[ \t]*(?:\{|\[)?[ \t]*(?:"?tool_?(?:c(?:a(?:l(?:l(?:s)?)?)?)?)?|'?tool_?(?:c(?:a(?:l(?:l(?:s)?)?)?)?)?)$""", s):
            starts.append(_json_container_start_before(m.start()))
        for m in re.finditer(r"(?im)^[ \t]*<\|?assistant(?:\s+tool_calls?)?$", s):
            starts.append(m.start())
        for m in re.finditer(r"(?im)^[ \t]*<tool_calls?$", s):
            starts.append(m.start())
        # While streaming, withhold any paragraph that starts with a JSON
        # object opener ``{"`` after a blank-line separator when the
        # body shows ANY of the following "this is a serialized
        # message-envelope leak rather than legitimate prose JSON"
        # signals. Hold the tail back until ``final=True`` so the
        # aggressive envelope/toolish patterns above can decide whether
        # to cut or release.
        for m in re.finditer(r"""(?m)(?:\n\n|^)[ \t]*\{[ \t]*["']""", s):
            brace_idx = s.find("{", m.start())
            if brace_idx < 0:
                continue
            tail = s[brace_idx:]
            # If a short, well-formed JSON object has already closed in
            # the buffer, treat it as benign prose JSON.
            if "}" in tail and len(tail) < 80:
                continue
            envelope_signal = re.search(
                r"""(?i)["'](?:role|name|content|tool|tool_calls|arguments|function)["']\s*:""",
                tail,
            )
            # Detect a "string-keyed object whose first key character is
            # non-ASCII or otherwise non-key-like". Legitimate prose
            # JSON has ASCII-letter/underscore keys (``{"foo":1}``);
            # when the character right after ``{"`` is CJK, whitespace,
            # punctuation, or anything that cannot start a sane key,
            # this is almost always the model echoing its visible prose
            # back inside a malformed JSON envelope.
            non_key_after_open = False
            after_open = re.match(r"""[ \t]*\{[ \t]*["']""", tail)
            if after_open:
                rest = tail[after_open.end():]
                if rest:
                    first_char = rest[0]
                    if not (first_char.isascii() and (first_char.isalnum() or first_char == "_")):
                        non_key_after_open = True
                else:
                    # Buffer ends exactly at ``{"`` (or with whitespace
                    # between). We don't yet know whether the next char
                    # will be a valid JSON key — but the streamer is
                    # append-only, so if we don't withhold now the
                    # ``{"`` will leak to the terminal and a later
                    # retract is invisible to the user. Be conservative
                    # and withhold the brace until ``final=True`` or
                    # until the next chunk arrives and disambiguates.
                    non_key_after_open = True
            if envelope_signal or non_key_after_open or len(tail) >= 60:
                starts.append(brace_idx)
        # Also withhold when the buffer ends right at ``\n\n{`` (with
        # optional trailing whitespace) without a following quote yet.
        # The next chunk will determine whether it becomes ``{"...``
        # (envelope leak) or harmless prose; until then, holding the
        # lone ``{`` back prevents a one-character leak that the
        # append-only streamer cannot retract.
        trailing_brace = re.search(r"(?:\n\n|\A)[ \t]*(\{)[ \t]*\Z", s)
        if trailing_brace:
            starts.append(trailing_brace.start(1))

    if not starts:
        return s
    return s[: min(starts)].rstrip()


def _strip_channel_thought_markers(text: str) -> str:
    """Remove ``<|channel>thought`` / ``<channel|>`` reasoning markers,
    keeping only the visible text after the closing tag. When the content
    consists entirely of markers the result is an empty string."""
    s = str(text or "")
    # Find the last ``<channel|>`` closing tag: everything before it (including
    # any ``<|channel>thought`` opener and the reasoning body) is hidden.
    close_idx = s.rfind("<channel|>")
    if close_idx >= 0:
        s = s[close_idx + len("<channel|>"):]
    # Strip any remaining opener without a matching closer.
    open_idx = s.find("<|channel>thought")
    if open_idx >= 0:
        # Find the next ``<channel|>`` after the opener.
        next_close = s.find("<channel|>", open_idx + len("<|channel>thought"))
        if next_close >= 0:
            s = s[:open_idx] + s[next_close + len("<channel|>"):]
        else:
            s = s[:open_idx]
    return s.strip()


def _format_stream_visible_text(text: str) -> str:
    """Apply display-time transforms to a streamed visible-text snapshot.

    The live TUI append path writes deltas of the visible text directly, which
    bypasses the history/reload formatter — so without this the raw
    ``$\\rightarrow$`` LaTeX and ``<proposed_plan>`` protocol tags would show
    verbatim during streaming (and only render correctly after ``/chat reload``).
    We convert inline LaTeX math to Unicode and reframe completed proposed-plan
    blocks here so live output matches the reloaded output.

    The streaming cutter withholds INCOMPLETE inline-math and proposed-plan
    spans, so every span this sees is already complete; the transforms are
    therefore append-only (a completed span converts once and stays converted),
    which keeps the append-stream delta math valid.
    """
    if not text:
        return text
    from ..core.text_output_renderer import (
        _reframe_proposed_plan_blocks,
        _strip_hidden_blocks,
        convert_inline_latex_math,
    )

    return _reframe_proposed_plan_blocks(convert_inline_latex_math(_strip_hidden_blocks(text)))


# Block- and inline-level Markdown the live append stream cannot render
# incrementally (rendering reflows earlier characters, breaking append-only
# delta math). When present, the reply is re-rendered once after streaming so
# the final terminal output matches a ``/chat reload``.
_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
_MD_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_MD_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_MD_QUOTE_RE = re.compile(r"^\s*>\s?\S")
_MD_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S")
# Display-math fence: ``$$`` or ``\[`` opening a block the append stream cannot
# render incrementally (it converts to centered multi-line Unicode).
_MD_MATH_BLOCK_RE = re.compile(r"^\s*(?:\$\$|\\\[)")
_MD_INLINE_RES = (
    re.compile(r"\*\*[^*\n]+\*\*"),
    re.compile(r"(?<![A-Za-z0-9_])__[^_\n]+__(?![A-Za-z0-9_])"),
    re.compile(r"~~[^~\n]+~~"),
    re.compile(r"`[^`\n]+`"),
    re.compile(r"\[[^\]\n]+\]\([^)\n]+\)"),
    re.compile(r"(?<!\*)\*(?!\s)[^*\n]+?(?<!\s)\*(?!\*)"),
)


def _text_has_renderable_markdown(text: str) -> bool:
    """True when ``text`` contains Markdown the append stream cannot render live.

    Used to decide whether a one-shot post-stream re-render is needed. Plain
    prose (no Markdown) returns False so the live append output is left as-is.
    """
    s = str(text or "")
    if not s:
        return False
    from ..core.text_output_renderer import _is_md_table_delimiter_row

    lines = s.split("\n")
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if (
            _MD_HEADING_RE.match(line)
            or _MD_FENCE_RE.match(line)
            or _MD_HR_RE.match(line)
            or _MD_QUOTE_RE.match(line)
            or _MD_LIST_RE.match(line)
            or _MD_MATH_BLOCK_RE.match(line)
        ):
            return True
        if (
            "|" in line
            and idx + 1 < len(lines)
            and _is_md_table_delimiter_row(lines[idx + 1])
        ):
            return True
    return any(pattern.search(s) for pattern in _MD_INLINE_RES)


def _consume_streaming_ai_response(
    agent: Any,
    ai_result: Any,
    before_first_visible_output: Optional[Callable[[], None]] = None,
) -> Tuple[Optional[str], bool]:
    from ..core.localization import translate as _translate

    _mark_pending_stream_history_reload(agent, False)
    if isinstance(ai_result, str):
        return ai_result, False
    if ai_result is None:
        return None, False
    if isinstance(ai_result, (bytes, bytearray)):
        try:
            return ai_result.decode("utf-8", errors="replace"), False
        except Exception:
            return str(ai_result), False
    iterator = getattr(ai_result, "__iter__", None)
    if not callable(iterator):
        return None, False

    raw_chunks: List[str] = []
    shown_visible = ""
    # ``shown_display`` tracks the formatted (LaTeX→Unicode, proposed-plan
    # reframed) text already written to the live append stream, so deltas are
    # computed against the FORMATTED output rather than the raw visible text.
    # Only the GUI keeps raw output (it formats client-side); the TUI append /
    # plain paths format here so live output matches a later ``/chat reload``.
    shown_display = ""
    streamed_any = False
    first_visible_output_ready = False
    last_rendered_block = ""
    last_rendered_lines = 0
    # GUI (serve) mode: emit clean append-only deltas instead of the
    # terminal's in-place block re-render (which depends on ANSI cursor
    # control that a non-TTY SSE sink cannot honor). The assistant reply is
    # also bracketed so the GUI can render it separately from step output.
    gui_plain = bool(getattr(agent, "_gui_plain_stream", False))
    is_tty = (not gui_plain) and bool(getattr(sys.stdout, "isatty", lambda: False)())
    can_format_render = (not gui_plain) and callable(
        getattr(agent, "_format_assistant_chat_display_message", None)
    )
    append_stream_builder = getattr(agent, "_build_internal_slash_output_stream", None)
    can_append_stream = bool(is_tty and callable(append_stream_builder))
    append_stream = None
    # Mirror of every byte written to the terminal during the live append
    # stream. The append path cannot render block/inline Markdown live (it
    # reflows earlier text), so once the reply finishes we count the exact
    # visual rows it occupied (from this mirror) to clear them and re-render the
    # reply through the full Markdown path.
    append_mirror = io.StringIO() if can_append_stream else None
    append_sink = (
        _TeeTextStream(sys.stdout, append_mirror) if append_mirror is not None else None
    )
    thinking_header = _translate(
        "runtime.thinking",
        getattr(agent, "display_language", None) or "en",
        fallback="Thinking",
    )

    # Thinking display state (TUI only — GUI uses a dedicated SSE hook).
    _thinking_prev = ""
    _thinking_lines_rendered = 0
    _thinking_ticker_stopped = False
    _thinking_visible_rows: List[str] = []
    _thinking_last_width = 0

    def _count_thinking_screen_lines(rows: List[str], term_width: int) -> int:
        content_width = max(1, term_width - len(_THINKING_TUI_INDENT))
        lines = 1
        for row in rows:
            w = _thinking_tui_text_display_width(row)
            if w <= 0:
                lines += 1
            else:
                lines += (w + content_width - 1) // content_width
        return lines

    def _clear_rendered_thinking_tui_block() -> None:
        nonlocal _thinking_lines_rendered
        if _thinking_lines_rendered <= 0:
            _thinking_lines_rendered = 0
            return
        try:
            sys.stdout.write("\r\x1b[2K")
            for _ in range(max(0, _thinking_lines_rendered - 1)):
                sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass
        _thinking_lines_rendered = 0

    def _redraw_thinking_tui_rows(rows: List[str]) -> None:
        nonlocal _thinking_lines_rendered, _thinking_visible_rows
        _thinking_visible_rows = list(rows or [""])
        _clear_rendered_thinking_tui_block()
        try:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.write(f"{_THINKING_TUI_INDENT}{thinking_header}")
            for row in _thinking_visible_rows:
                sys.stdout.write("\n")
                if row:
                    sys.stdout.write("\x1b[2m")
                    sys.stdout.write(f"{_THINKING_TUI_INDENT}{row}")
                    sys.stdout.write("\x1b[0m")
                else:
                    sys.stdout.write(_THINKING_TUI_INDENT)
            sys.stdout.flush()
            _thinking_lines_rendered = 1 + len(_thinking_visible_rows)
        except Exception:
            _thinking_lines_rendered = 0

    def _ensure_thinking_tui_started() -> None:
        nonlocal _thinking_ticker_stopped, _thinking_lines_rendered, _thinking_visible_rows
        if _thinking_ticker_stopped:
            return
        stopper = getattr(agent, "_active_status_ticker_stopper", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass
        try:
            agent._active_status_ticker_stopper = None
        except Exception:
            pass
        _thinking_ticker_stopped = True
        _thinking_visible_rows = [""]
        try:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.write(f"{_THINKING_TUI_INDENT}{thinking_header}\n{_THINKING_TUI_INDENT}")
            sys.stdout.flush()
            _thinking_lines_rendered = 2
        except Exception:
            _thinking_lines_rendered = 0

    def _append_thinking_tui_char(ch: str) -> None:
        nonlocal _thinking_lines_rendered, _thinking_visible_rows, _thinking_prev, _thinking_last_width
        if not _thinking_visible_rows:
            _thinking_visible_rows = [""]
        width = 80
        width_fn = getattr(agent, "_terminal_columns_for_line_estimate", None)
        if callable(width_fn):
            try:
                width = max(1, int(width_fn() or 0))
            except Exception:
                width = 80
        width = max(1, width - len(_THINKING_TUI_INDENT))
        if _thinking_last_width and width != _thinking_last_width:
            _thinking_lines_rendered = _count_thinking_screen_lines(
                _thinking_visible_rows,
                width + len(_THINKING_TUI_INDENT),
            )
            visible_rows = _build_thinking_tui_visible_rows(
                agent,
                _thinking_prev,
                max_visible_lines=_THINKING_TUI_MAX_VISIBLE_LINES,
            )
            _redraw_thinking_tui_rows(visible_rows)
            _thinking_last_width = width
            return
        _thinking_last_width = width
        current = _thinking_visible_rows[-1]
        if ch == "\n":
            _thinking_visible_rows.append("")
            if len(_thinking_visible_rows) > _THINKING_TUI_MAX_VISIBLE_LINES:
                _thinking_visible_rows = _thinking_visible_rows[-_THINKING_TUI_MAX_VISIBLE_LINES :]
                _redraw_thinking_tui_rows(_thinking_visible_rows)
                return
            try:
                sys.stdout.write(f"\n{_THINKING_TUI_INDENT}")
                _thinking_lines_rendered = 1 + len(_thinking_visible_rows)
            except Exception:
                pass
            return
        ch_w = _thinking_tui_char_display_width(ch)
        current_w = _thinking_tui_text_display_width(current)
        if current and current_w + ch_w > width:
            _thinking_visible_rows.append(ch)
            if len(_thinking_visible_rows) > _THINKING_TUI_MAX_VISIBLE_LINES:
                _thinking_visible_rows = _thinking_visible_rows[-_THINKING_TUI_MAX_VISIBLE_LINES :]
                _redraw_thinking_tui_rows(_thinking_visible_rows)
                return
            try:
                sys.stdout.write(f"\n{_THINKING_TUI_INDENT}")
                sys.stdout.write("\x1b[2m")
                sys.stdout.write(ch)
                sys.stdout.write("\x1b[0m")
                _thinking_lines_rendered = 1 + len(_thinking_visible_rows)
            except Exception:
                pass
            return
        _thinking_visible_rows[-1] = current + ch
        try:
            sys.stdout.write("\x1b[2m")
            sys.stdout.write(ch)
            sys.stdout.write("\x1b[0m")
        except Exception:
            pass

    def _render_thinking_tui(new_thinking: str) -> None:
        nonlocal _thinking_prev, _thinking_lines_rendered, _thinking_ticker_stopped

        # Compute delta from last rendered state
        had_prefix = new_thinking.startswith(_thinking_prev)
        delta = new_thinking[len(_thinking_prev) :] if had_prefix else new_thinking
        if not delta:
            return
        _thinking_prev = new_thinking

        # GUI hook: forward thinking deltas to the bridge so the GUI can render
        # them in its own thinking panel.
        gui_thinking_hook = getattr(agent, "_gui_thinking_chunk", None)
        if callable(gui_thinking_hook):
            try:
                gui_thinking_hook(delta)
            except Exception:
                pass
        if gui_plain or not is_tty:
            return

        _ensure_thinking_tui_started()
        if not had_prefix:
            visible_rows = _build_thinking_tui_visible_rows(
                agent,
                new_thinking,
                max_visible_lines=_THINKING_TUI_MAX_VISIBLE_LINES,
            )
            _redraw_thinking_tui_rows(visible_rows)
            return
        try:
            for ch in delta:
                _append_thinking_tui_char(ch)
            sys.stdout.flush()
        except Exception:
            pass

    def _clear_thinking_tui() -> None:
        _clear_rendered_thinking_tui_block()

    def _append_term_out() -> Any:
        return append_sink if append_sink is not None else sys.stdout

    def _gui_mark(begin: bool) -> None:
        if not gui_plain:
            return
        hook = getattr(
            agent, "_gui_assistant_begin" if begin else "_gui_assistant_end", None
        )
        if callable(hook):
            try:
                hook()
            except Exception:
                pass

    def _clear_previous_block() -> None:
        nonlocal last_rendered_lines
        if not is_tty or last_rendered_lines <= 0:
            last_rendered_lines = 0
            return
        try:
            for _ in range(min(int(last_rendered_lines), 2000)):
                sys.stdout.write("\x1b[1A\r\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass
        last_rendered_lines = 0

    def _render_visible_block(visible_text: str) -> bool:
        nonlocal last_rendered_block, last_rendered_lines
        display_response = format_assistant_display_response(visible_text)
        if not display_response:
            if last_rendered_lines > 0:
                _clear_previous_block()
            last_rendered_block = ""
            return False
        rendered = agent._format_assistant_chat_display_message(display_response)
        if rendered == last_rendered_block:
            return True
        _clear_previous_block()
        try:
            sys.stdout.write(f"{rendered}\n")
            sys.stdout.flush()
        except Exception:
            return False
        last_rendered_block = rendered
        last_rendered_lines = max(1, _estimate_visible_lines(agent, rendered))
        return True

    def _on_before_first_visible_output() -> None:
        nonlocal first_visible_output_ready
        if first_visible_output_ready:
            return
        first_visible_output_ready = True
        _clear_thinking_tui()
        _gui_mark(True)
        if callable(before_first_visible_output):
            try:
                before_first_visible_output()
            except Exception:
                pass

    def _ensure_append_stream() -> Any:
        nonlocal append_stream
        if append_stream is not None:
            return append_stream
        builder = append_stream_builder if callable(append_stream_builder) else None
        if builder is None:
            return None
        term_cols = None
        term_cols_fn = getattr(agent, "_terminal_columns_for_line_estimate", None)
        if callable(term_cols_fn):
            try:
                term_cols = int(term_cols_fn() or 0)
            except Exception:
                term_cols = None
        term_out = _append_term_out()
        try:
            if term_cols and term_cols > 0:
                append_stream = builder(term_out, terminal_columns=term_cols)
            else:
                append_stream = builder(term_out)
        except TypeError:
            try:
                append_stream = builder(term_out)
            except Exception:
                append_stream = None
        except Exception:
            append_stream = None
        if append_stream is None:
            return None
        try:
            term_out.write(f"{_ansi_gray('•')} ")
            term_out.flush()
        except Exception:
            try:
                term_out.write("• ")
                term_out.flush()
            except Exception:
                pass
        try:
            append_stream._line_start = False
            append_stream._visual_col = 2
        except Exception:
            pass
        return append_stream

    def _close_ai_result() -> None:
        close_fn = getattr(ai_result, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    consume_interrupt = getattr(agent, "_consume_task_interrupt_requested", None)
    try:
        for chunk in ai_result:
            if callable(consume_interrupt) and bool(consume_interrupt()):
                raise KeyboardInterrupt
            # Check for new thinking content (side-channel on the stream result)
            thinking_now = getattr(ai_result, "thinking_text", "") or ""
            if thinking_now:
                _render_thinking_tui(thinking_now)
            piece = str(chunk or "")
            if not piece:
                continue
            raw_chunks.append(piece)
            visible_now = _stream_visible_text_with_json_pause("".join(raw_chunks), final=False)
            if visible_now == shown_visible:
                continue
            visible_was_trimmed = (
                bool(visible_now)
                and shown_visible.startswith(visible_now)
                and not shown_visible[len(visible_now) :].strip()
            )
            if visible_was_trimmed:
                shown_visible = visible_now
                continue
            if not streamed_any:
                _on_before_first_visible_output()
                try:
                    agent._hide_previous_shell_output_if_needed()
                except Exception:
                    pass
                try:
                    agent._ensure_terminal_line_start()
                except Exception:
                    pass
            if can_append_stream:
                # Format here so the live TUI stream matches a later reload
                # (Unicode math + reframed proposed-plan), and delta against the
                # formatted text already shown.
                display_now = _format_stream_visible_text(visible_now)
                delta = (
                    display_now[len(shown_display) :]
                    if display_now.startswith(shown_display)
                    else display_now
                )
                if delta:
                    target_stream = _ensure_append_stream()
                    if target_stream is not None:
                        try:
                            target_stream.write(delta)
                            target_stream.flush()
                            streamed_any = True
                        except Exception:
                            _append_term_out().write(delta)
                            _append_term_out().flush()
                            streamed_any = True
                    else:
                        _append_term_out().write(delta)
                        _append_term_out().flush()
                        streamed_any = True
                shown_display = display_now
            elif can_format_render:
                rendered_ok = _render_visible_block(visible_now)
                if rendered_ok:
                    streamed_any = True
                elif visible_now:
                    delta = visible_now[len(shown_visible) :] if visible_now.startswith(shown_visible) else visible_now
                    if delta:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        streamed_any = True
            else:
                delta = visible_now[len(shown_visible) :] if visible_now.startswith(shown_visible) else visible_now
                if delta:
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                    streamed_any = True
            shown_visible = visible_now
    except KeyboardInterrupt:
        if first_visible_output_ready:
            _gui_mark(False)
        _close_ai_result()
        raise

    ai_response = "".join(raw_chunks)
    visible_final = _stream_visible_text_with_json_pause(ai_response, final=True)
    final_already_shown = (
        visible_final == shown_visible
        or (
            bool(visible_final)
            and shown_visible.startswith(visible_final)
            and not shown_visible[len(visible_final) :].strip()
        )
    )
    if not final_already_shown:
        if not streamed_any:
            _on_before_first_visible_output()
            try:
                agent._hide_previous_shell_output_if_needed()
            except Exception:
                pass
            try:
                agent._ensure_terminal_line_start()
            except Exception:
                pass
        if can_append_stream:
            display_final = _format_stream_visible_text(visible_final)
            tail = (
                display_final[len(shown_display) :]
                if display_final.startswith(shown_display)
                else display_final
            )
            if tail:
                target_stream = _ensure_append_stream()
                if target_stream is not None:
                    try:
                        target_stream.write(tail)
                        target_stream.flush()
                        streamed_any = True
                    except Exception:
                        _append_term_out().write(tail)
                        _append_term_out().flush()
                        streamed_any = True
                else:
                    _append_term_out().write(tail)
                    _append_term_out().flush()
                    streamed_any = True
            shown_display = display_final
        elif can_format_render:
            rendered_ok = _render_visible_block(visible_final)
            if rendered_ok:
                streamed_any = True
            elif visible_final:
                tail = (
                    visible_final[len(shown_visible) :]
                    if visible_final.startswith(shown_visible)
                    else visible_final
                )
                if tail:
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                    streamed_any = True
        else:
            tail = (
                visible_final[len(shown_visible) :]
                if visible_final.startswith(shown_visible)
                else visible_final
            )
            if tail:
                sys.stdout.write(tail)
                sys.stdout.flush()
                streamed_any = True
        shown_visible = visible_final
    elif visible_final:
        shown_visible = visible_final
    # The live append path cannot render block/inline Markdown (tables,
    # headings, lists, bold, code, ...) because doing so reflows already-printed
    # characters and breaks append-only delta math. Once the reply is complete,
    # re-render it once through the full Markdown path: clear the exact rows the
    # append stream occupied (counted from the tee mirror) and reprint the
    # formatted block so the final terminal output matches a ``/chat reload``.
    should_reload_after_stream = False
    mirror_text = ""
    if can_append_stream and streamed_any and append_mirror is not None:
        mirror_text = append_mirror.getvalue()
        streamed_rows = mirror_text.count("\n") + (
            0 if mirror_text.endswith("\n") else 1
        )
        term_rows = _terminal_rows_for_stream_reload(agent)
        should_reload_after_stream = streamed_rows >= max(1, term_rows - 1)
        _mark_pending_stream_history_reload(agent, should_reload_after_stream)
    if (
        can_append_stream
        and streamed_any
        and append_mirror is not None
        and _text_has_renderable_markdown(visible_final)
        and not should_reload_after_stream
    ):
        try:
            display_final_md = format_assistant_display_response(visible_final)
            rendered_block = (
                agent._format_assistant_chat_display_message(display_final_md)
                if display_final_md
                else ""
            )
        except Exception:
            rendered_block = ""
        if rendered_block:
            streamed_rows = mirror_text.count("\n") + (
                0 if mirror_text.endswith("\n") else 1
            )
            try:
                if not mirror_text.endswith("\n"):
                    sys.stdout.write("\n")
                last_rendered_lines = streamed_rows
                _clear_previous_block()
                sys.stdout.write(f"{rendered_block}\n")
                sys.stdout.flush()
                shown_visible = visible_final
            except Exception:
                pass
    if streamed_any:
        if not shown_visible.endswith("\n"):
            sys.stdout.write("\n")
        # Trailing blank line below the model reply for readability.
        sys.stdout.write("\n")
        sys.stdout.flush()
        try:
            agent._last_terminal_block_kind = "assistant"
            agent._terminal_cursor_at_line_start = True
        except Exception:
            pass
    if first_visible_output_ready:
        _gui_mark(False)
    _clear_thinking_tui()
    return ai_response, streamed_any


def _replace_latest_assistant_history_content(
    agent: Any,
    old_content: Any,
    new_content: Any,
    *,
    pseudo_tool_call_text: str = "",
    pseudo_tool_call_tools: Optional[List[str]] = None,
) -> None:
    old_text = str(old_content or "")
    new_text = str(new_content or "")
    pseudo_text = str(pseudo_tool_call_text or "").strip()
    if old_text == new_text and not pseudo_text:
        return
    hist = getattr(agent, "conversation_history", None)
    if not isinstance(hist, list):
        return
    for msg in reversed(hist):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").strip().lower() != "assistant":
            continue
        if str(msg.get("content") or "") != old_text:
            continue
        msg["content"] = new_text
        if pseudo_text:
            msg["pseudo_tool_call_text"] = pseudo_text
            if pseudo_tool_call_tools:
                msg["pseudo_tool_call_tools"] = [
                    str(x).strip()
                    for x in pseudo_tool_call_tools
                    if str(x).strip()
                ]
        try:
            agent._sync_active_chat_messages()
        except Exception:
            pass
        return


def _update_latest_assistant_clean_content(agent: Any, clean_content: str) -> None:
    if not isinstance(clean_content, str):
        return
    hist = getattr(agent, "conversation_history", None)
    if not isinstance(hist, list):
        return
    for msg in reversed(hist):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").strip().lower() != "assistant":
            continue
        # Remove any stale _clean_content that duplicates raw content.
        if "_clean_content" in msg and str(msg["_clean_content"] or "") == str(msg.get("content") or ""):
            del msg["_clean_content"]
        raw_content = str(msg.get("content") or "")
        # Strip ``<|channel>thought`` / ``<channel|>`` reasoning markers so
        # they don't cause ``_clean_content`` to be treated as identical to
        # the raw text (which would prevent recording it).
        clean_visible = _strip_channel_thought_markers(clean_content)
        raw_visible = _strip_channel_thought_markers(raw_content)
        existing_clean = str(msg.get("_clean_content") or "")
        if existing_clean == clean_visible:
            return
        if raw_visible == clean_visible:
            msg.pop("_clean_content", None)
        else:
            msg["_clean_content"] = clean_visible
        try:
            agent._sync_active_chat_messages()
        except Exception:
            pass
        return


def _ensure_thinking_in_latest_assistant_message(agent: Any, thinking: str, thinking_from_content: bool = False) -> None:
    """Ensure the most recent assistant message in conversation history has
    ``_thinking`` set, so it persists to chat state and is available on reload."""
    if not isinstance(thinking, str) or not thinking:
        return
    hist = getattr(agent, "conversation_history", None)
    if not isinstance(hist, list):
        return
    for msg in reversed(hist):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").strip().lower() != "assistant":
            continue
        if msg.get("_thinking"):
            return
        msg["_thinking"] = thinking
        if thinking_from_content:
            msg["_thinking_from_content"] = True
        try:
            agent._sync_active_chat_messages()
        except Exception:
            pass
        return


def _model_tool_result_was_aborted(tool_name: str, result: Any) -> bool:
    if str(tool_name or "").strip() != "shell":
        return False
    if not isinstance(result, dict):
        return False
    if bool(result.get("aborted_by_user", False)):
        return True
    return "command aborted by user" in str(result.get("output") or "").lower()


def _build_apply_patch_failure_hints(
    error_text: str,
    args: Dict[str, Any],
    t: Callable[[str, Optional[str]], str],
) -> List[str]:
    err = str(error_text or "").strip()
    low = err.lower()
    patch_text = str((args or {}).get("patch") or "")
    path_text = str((args or {}).get("path") or "").strip()
    hints: List[str] = []

    def add(en: str, zh: str) -> None:
        msg = str(t(en, zh) or "").strip()
        if msg and msg not in hints:
            hints.append(msg)

    if ("missing path/patch" in low) or ("requires both path and patch" in low):
        add(
            "Hint: include both `path` and non-empty `patch` in apply_patch args.",
            "提示：`apply_patch` 参数必须同时包含 `path` 和非空 `patch`。",
        )
    if ("patch content cannot be empty" in low) or (not patch_text.strip()):
        add(
            "Hint: patch body is empty; regenerate a complete unified diff hunk.",
            "提示：patch 内容为空；请重新生成完整的 unified diff hunk。",
        )
    if (
        ("no applicable hunks found" in low)
        or ("expected '@@ ... @@'" in low)
        or ("invalid hunk header" in low)
        or ("unsupported hunk line prefix" in low)
    ):
        add(
            "Hint: patch format is invalid; use standard unified diff with `@@` hunks and `+/-/ ` prefixes.",
            "提示：patch 格式不合法；请使用标准 unified diff（含 `@@` hunk，行前缀为 `+/-/ `）。",
        )
    if (
        ("patch context mismatch" in low)
        or ("patch deletion mismatch" in low)
        or ("hunk start line out of range" in low)
        or ("patch anchor not found" in low)
    ):
        add(
            "Hint: file content drifted; re-read the latest file and regenerate a smaller, context-accurate hunk.",
            "提示：文件内容可能已漂移；请先重新读取最新文件，再生成更小且上下文精确的 hunk。",
        )
    if ("not a file" in low) or ("does not exist" in low):
        if path_text:
            add(
                f"Hint: verify target path exists and is a text file: `{path_text}`.",
                f"提示：请确认目标路径存在且为文本文件：`{path_text}`。",
            )
        else:
            add(
                "Hint: verify target path exists and points to a text file.",
                "提示：请确认目标路径存在且指向文本文件。",
            )
    if ("operation cancelled by user" in low) or ("cancelled by user" in low):
        add(
            "Hint: patch was cancelled by user confirmation; request confirmation before retry.",
            "提示：该 patch 是用户确认时取消；重试前请先征得用户确认。",
        )
    if not hints:
        add(
            "Hint: re-read target file and retry with a minimal unified diff patch.",
            "提示：请重新读取目标文件，并用最小化 unified diff patch 重试。",
        )
    return hints


def _reload_chat_history_after_aborted_command(agent: Any) -> None:
    try:
        agent._suppress_next_prompt_chat_reload_once = True
    except Exception:
        pass
    reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if not callable(reload_fn):
        return
    try:
        reload_fn(include_startup_overview=True)
    except TypeError:
        reload_fn()
    except Exception:
        pass


def _reload_chat_history_after_streamed_assistant_output(agent: Any) -> None:
    try:
        remember = getattr(agent, "_remember_active_chat_history_tail_anchor", None)
        if callable(remember):
            remember()
    except Exception:
        pass
    reload_fn = getattr(agent, "_reload_chat_history_from_anchor_on_resize", None)
    if not callable(reload_fn):
        return
    try:
        reload_fn()
    except TypeError:
        try:
            reload_fn(include_startup_overview=True)
        except Exception:
            pass
    except Exception:
        pass


def _render_aborted_direct_shell_feedback(agent: Any, command: str, result: Any) -> None:
    try:
        agent._suppress_next_prompt_chat_reload_once = True
    except Exception:
        pass
    rendered_lines = 0
    cursor_at_line_start = True
    if isinstance(result, dict):
        try:
            rendered_lines = int(result.get("rendered_output_lines") or 0)
        except Exception:
            rendered_lines = 0
        try:
            cursor_at_line_start = bool(result.get("cursor_at_line_start", True))
        except Exception:
            cursor_at_line_start = True
    repaint = getattr(agent, "_repaint_direct_shell_command_feedback_if_failed", None)
    if callable(repaint):
        try:
            repaint(
                command,
                rendered_output_lines=rendered_lines,
                cursor_at_line_start=cursor_at_line_start,
                failed=True,
            )
        except Exception:
            pass
    banner = getattr(agent, "_print_conversation_interrupted_banner", None)
    if callable(banner):
        try:
            banner()
        except Exception:
            pass


def _resolve_worked_summary_terminal_width(agent: Any, default: int = 80) -> int:
    width = max(20, int(default or 80))
    try:
        fn2 = getattr(agent, "_terminal_columns_for_line_estimate", None)
        if callable(fn2):
            cols2 = int(fn2() or 0)
            if cols2 > 0:
                return max(20, cols2)
    except Exception:
        pass
    try:
        fn = getattr(agent, "_terminal_columns_for_prompt_separator", None)
        if callable(fn):
            cols = int(fn(default=width) or 0)
            if cols > 0:
                return max(20, cols)
    except Exception:
        pass
    try:
        cols2 = int(getattr(os.get_terminal_size(), "columns", 0) or 0)
        if cols2 > 0:
            return max(20, cols2)
    except Exception:
        pass
    return width


def _format_worked_for_summary_line(elapsed_seconds: int, terminal_width: int, language: Any = None) -> str:
    from ..core.localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text

    total = max(0, int(elapsed_seconds or 0))
    minutes, seconds = divmod(total, 60)
    if minutes <= 0:
        elapsed = f"{seconds}s"
    else:
        elapsed = f"{minutes}m {seconds}s"
    lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
    head = f"─ {text('status.worked_for', lang)} {elapsed} "
    width = max(20, int(terminal_width or 80))
    head_width = _startup_text_display_width(head)
    if head_width >= width:
        trimmed = []
        used = 0
        for ch in head:
            ch_w = _startup_text_display_width(ch)
            if used + ch_w > width:
                break
            trimmed.append(ch)
            used += ch_w
        return "".join(trimmed)
    return head + ("─" * (width - head_width))


def build_request_user_input_prompt_block(
    agent: Any,
    question: str,
    options: List[str],
    multi_select: bool = False,
) -> str:
    """Build the TUI ``request_user_input`` prompt text (numbered options + hint).

    Shared by the live prompt and the history-replay re-render so a pending
    clarifying prompt created by another process (e.g. the GUI) shows the
    exact same selection block when the TUI reloads/switches to that chat.
    """
    from ..core.localization import translate as _translate

    lang = getattr(agent, "display_language", None) or "en"
    t = lambda key, fallback=None, **kwargs: _translate(key, lang, fallback, **kwargs)

    visible_options = [str(o) for o in (options or [])]
    other_index = len(visible_options) + 1
    other_label = t("runtime.request_user_input.option_other")
    lines: List[str] = [
        t("runtime.request_user_input.required"),
        t("runtime.request_user_input.question", question=str(question or "")),
    ]
    for idx, label in enumerate(visible_options, start=1):
        lines.append(f"  {idx}. {label}")
    lines.append(f"  {other_index}. {other_label}")
    if multi_select:
        lines.append(t("runtime.request_user_input.multi_hint", other_index=other_index))
    else:
        lines.append(t("runtime.request_user_input.single_hint", other_index=other_index))
    return "\n".join(lines)


def build_request_user_input_header_block(agent: Any, question: str) -> str:
    """Question header only — no numbered option list or numbered-prompt hint.

    Used by the interactive arrow-key selector, which renders the options
    itself. Printing the full numbered block there too would show the
    options twice (a non-selectable list above the live selector).
    """
    from ..core.localization import translate as _translate

    lang = getattr(agent, "display_language", None) or "en"
    t = lambda key, fallback=None, **kwargs: _translate(key, lang, fallback, **kwargs)
    return "\n".join(
        [
            t("runtime.request_user_input.required"),
            t("runtime.request_user_input.question", question=str(question or "")),
        ]
    )


def _solicit_request_user_input_answer(
    agent: Any,
    question: str,
    options: List[str],
    multi_select: bool = False,
) -> Tuple[str, bool]:
    """Collect the user's answer to an ``request_user_input`` clarifying question.

    Returns ``(supplement_text, handoff_to_main_loop)``. An empty
    ``supplement_text`` means the user cancelled — the caller pauses the
    task. ``handoff_to_main_loop`` becomes ``True`` when the user typed a
    leading ``/`` or ``!`` (TUI only); the input is re-queued so the next
    main-loop iteration handles it just like any normal turn.

    ``multi_select`` toggles between single-pick (one option) and
    multi-pick (any subset, joined with ``; `` in the supplement). The
    GUI provider receives the flag and renders checkboxes accordingly;
    the TUI parses comma/space-separated digits (e.g. ``1,3``) and an
    optional trailing freeform fragment (e.g. ``1,3, also include FOO``).

    The host can install ``agent._request_user_input_provider`` to fully
    replace the TUI prompt. The hook signature is
    ``provider(question, options, multi_select)`` — older single-arg
    hooks are still tolerated via a graceful fallback.
    """
    from ..core.localization import translate as _translate

    lang = getattr(agent, "display_language", None) or "en"
    t = lambda key, fallback=None, **kwargs: _translate(key, lang, fallback, **kwargs)

    # Persist the pending prompt to the chat record up-front so any other
    # process opening the same chat (e.g. the desktop GUI when the TUI
    # triggered the call) can re-render the selection panel instead of
    # showing stale Execute-now/plan UI. The id is best-effort: GUI's own
    # provider will overwrite it with its own per-request id below.
    pending_payload: Dict[str, Any] = {
        "id": _request_user_input_pending_id(agent),
        "question": str(question or ""),
        "options": list(options or []),
        "multi_select": bool(multi_select),
    }
    setter = getattr(agent, "_set_pending_request_user_input", None)
    if callable(setter):
        try:
            setter(pending_payload)
        except Exception:
            pass

    provider = getattr(agent, "_request_user_input_provider", None)
    if callable(provider):
        try:
            # New 3-arg signature; old hooks took (question, options).
            # Try the new shape first and fall back so an out-of-date
            # provider doesn't crash the loop.
            try:
                raw = provider(question, list(options), bool(multi_select))
            except TypeError:
                raw = provider(question, list(options))
        except KeyboardInterrupt:
            try:
                print(t("runtime.request_user_input.supplement_cancelled"))
            except Exception:
                pass
            _clear_request_user_input_pending(agent)
            return ("", False)
        except Exception:
            _clear_request_user_input_pending(agent)
            return ("", False)
        answer = str(raw or "").strip()
        if not answer:
            _clear_request_user_input_pending(agent)
            return ("", False)
        _clear_request_user_input_pending(agent)
        if answer.startswith("/") or answer.startswith("!"):
            agent._queued_user_input = answer
            return (answer, True)
        return (answer, False)

    visible_options = list(options)
    other_index = len(visible_options) + 1

    # Preferred TUI path: an interactive arrow-key selector (↑/↓ to move,
    # Space/Enter to pick, inline input for "Other", Enter to submit a
    # multi-select). Falls back to the plain numbered-prompt flow below when
    # the input handler can't provide it (no prompt_toolkit, non-tty, etc.).
    input_handler = getattr(agent, "input_handler", None)
    interactive = getattr(input_handler, "prompt_request_user_input_selection", None)
    if callable(interactive) and _request_user_input_interactive_supported(agent):
        # The interactive selector renders the question header ("Need your
        # input" + the question) itself and runs with ``erase_when_done=False``,
        # so that header stays in the transcript after the widget tears down.
        # We must NOT pre-print the header here too — doing so showed the
        # "Need your input" / question block twice (a stale copy above the live
        # selector, and again once it committed).
        try:
            picked = interactive(question, list(visible_options), bool(multi_select))
        except KeyboardInterrupt:
            picked = None
        except Exception:
            picked = "__fallback__"
        if picked != "__fallback__":
            if picked is None:
                try:
                    print(t("runtime.request_user_input.supplement_cancelled"))
                except Exception:
                    pass
                _clear_request_user_input_pending(agent)
                return ("", False)
            answer = str(picked).strip()
            if not answer:
                try:
                    print(t("runtime.request_user_input.no_supplement"))
                except Exception:
                    pass
                _clear_request_user_input_pending(agent)
                return ("", False)
            _clear_request_user_input_pending(agent)
            return (answer, False)

    # TUI fallback. Layout depends on the mode:
    #   single-select  -> "Pick one (or N+1 for Other):"
    #   multi-select   -> "Pick one or more (comma-separated, or include Other):"
    # Build the prompt block once so we can stash it for resize re-rendering
    # too — without that, prompt_toolkit's redraw after a terminal resize
    # would scroll the options off the screen and the user is left typing
    # blindly.
    prompt_block = build_request_user_input_prompt_block(
        agent, question, visible_options, multi_select
    )
    try:
        agent._pending_request_user_input_render = prompt_block  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        print(prompt_block)
    except Exception:
        pass

    while True:
        try:
            raw_input_line = agent._get_user_input_with_history().strip()
        except KeyboardInterrupt:
            try:
                print(t("runtime.request_user_input.supplement_cancelled"))
            except Exception:
                pass
            _clear_request_user_input_pending(agent)
            return ("", False)
        if not raw_input_line:
            try:
                print(t("runtime.request_user_input.no_supplement"))
            except Exception:
                pass
            _clear_request_user_input_pending(agent)
            return ("", False)
        if raw_input_line.startswith("/") or raw_input_line.startswith("!"):
            agent._queued_user_input = raw_input_line
            _clear_request_user_input_pending(agent)
            return (raw_input_line, True)

        if not multi_select:
            # Single-select: a bare digit selects an option (or Other);
            # anything else is taken as a direct freeform answer.
            if raw_input_line.isdigit():
                try:
                    pick = int(raw_input_line)
                except ValueError:
                    pick = -1
                if 1 <= pick <= len(visible_options):
                    _clear_request_user_input_pending(agent)
                    return (visible_options[pick - 1], False)
                if pick == other_index:
                    try:
                        print(t("runtime.request_user_input.other_prompt"))
                    except Exception:
                        pass
                    continue
                try:
                    print(t("runtime.request_user_input.invalid_choice"))
                except Exception:
                    pass
                continue
            _clear_request_user_input_pending(agent)
            return (raw_input_line, False)

        # Multi-select: accept "1,3", "1 3", "1,3, free text after"
        # (split on the first non-digit/comma/space run and treat the
        # trailing fragment as the Other freeform). Out-of-range or
        # duplicated digits are rejected with a nudge.
        picked, other_text, error = _parse_multi_select_line(
            raw_input_line, len(visible_options), other_index
        )
        if error == "invalid":
            try:
                print(t("runtime.request_user_input.invalid_choice"))
            except Exception:
                pass
            continue
        if error == "need_other_text":
            # User ticked Other but didn't supply text; re-prompt them
            # for the freeform fragment without losing the picks so far.
            try:
                print(t("runtime.request_user_input.other_prompt"))
            except Exception:
                pass
            try:
                extra = agent._get_user_input_with_history().strip()
            except KeyboardInterrupt:
                try:
                    print(t("runtime.request_user_input.supplement_cancelled"))
                except Exception:
                    pass
                _clear_request_user_input_pending(agent)
                return ("", False)
            if not extra:
                # Bare empty input cancels the Other branch but keeps
                # any already-ticked options.
                pass
            else:
                other_text = extra
        # Build the final supplement: picked option labels in input
        # order, then the freeform text (if any), joined with "; ".
        parts: List[str] = [visible_options[i - 1] for i in picked]
        if other_text:
            parts.append(other_text)
        if not parts:
            try:
                print(t("runtime.request_user_input.invalid_choice"))
            except Exception:
                pass
            continue
        _clear_request_user_input_pending(agent)
        return ("; ".join(parts), False)


def _request_user_input_interactive_supported(agent: Any) -> bool:
    """Whether the interactive arrow-key selector can run for this prompt.

    Requires a real interactive stdin/stdout TTY and a non-GUI (no custom
    ``_request_user_input_provider``) session. The GUI installs its own provider
    and is handled earlier, so this only gates the TUI path.
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if not sys.stdout or not sys.stdout.isatty():
            return False
    except Exception:
        return False
    # GUI/headless runs route through ``_request_user_input_provider``; never show
    # the terminal widget there.
    if callable(getattr(agent, "_request_user_input_provider", None)):
        return False
    return True


def _request_user_input_pending_id(agent: Any) -> str:
    """Return a short opaque id for the in-flight TUI request_user_input prompt.

    The GUI provider generates its own per-request id, but the TUI path
    has nothing to wait on, so we just need something stable enough for
    de-dup if a second writer somehow lands. Falls back to a timestamp
    when ``secrets`` is unavailable for any reason.
    """
    try:
        import secrets as _secrets  # local: cheap, avoids touching module-level imports

        return _secrets.token_hex(6)
    except Exception:
        return str(int(time.time() * 1000))


def _clear_request_user_input_pending(agent: Any) -> None:
    """Remove both the chat-record marker and the in-memory render stash."""
    clearer = getattr(agent, "_clear_pending_request_user_input", None)
    if callable(clearer):
        try:
            clearer()
        except Exception:
            pass
    try:
        agent._pending_request_user_input_render = ""  # type: ignore[attr-defined]
    except Exception:
        pass


def _parse_multi_select_line(
    line: str,
    option_count: int,
    other_index: int,
) -> Tuple[List[int], str, str]:
    """Parse a TUI multi-select reply.

    Returns ``(picked_indices, other_text, error)``. ``error`` is one of:
    - ``""`` — input is valid (possibly empty picks if user only typed
      Other text with no leading numbers)
    - ``"invalid"`` — a digit was out of range or no usable tokens found
    - ``"need_other_text"`` — Other was ticked but no freeform fragment
      came with it; caller should re-prompt for the text.
    """
    s = (line or "").strip()
    if not s:
        return ([], "", "invalid")

    # Walk the leading run of digit/comma/space characters as the picks;
    # anything after that is the Other freeform fragment.
    cut = 0
    for ch in s:
        if ch.isdigit() or ch in ", ":
            cut += 1
        else:
            break
    head = s[:cut]
    tail = s[cut:].strip(" ,;\t")

    picks_raw = [tok for tok in head.replace(",", " ").split() if tok]
    picked: List[int] = []
    other_chosen = False
    seen_picks: set = set()
    for tok in picks_raw:
        if not tok.isdigit():
            return ([], "", "invalid")
        try:
            n = int(tok)
        except ValueError:
            return ([], "", "invalid")
        if n == other_index:
            other_chosen = True
            continue
        if not (1 <= n <= option_count):
            return ([], "", "invalid")
        if n in seen_picks:
            continue
        seen_picks.add(n)
        picked.append(n)

    if not picks_raw and not tail:
        return ([], "", "invalid")

    # If the leading digits include Other but no trailing freeform was
    # typed, ask the caller to collect the freeform text on the next
    # prompt. Without this the model would see a useless "Other" with
    # no content.
    if other_chosen and not tail:
        return (picked, "", "need_other_text")

    return (picked, tail, "")


def _print_worked_for_summary_line(agent: Any, elapsed_seconds: int) -> None:
    printer = getattr(agent, "_print_task_worked_summary_line", None)
    if callable(printer):
        try:
            printer(int(max(0, int(elapsed_seconds or 0))))
        except Exception:
            pass
    else:
        width = _resolve_worked_summary_terminal_width(agent, default=80)
        line = _format_worked_for_summary_line(elapsed_seconds, width, language=getattr(agent, "display_language", None))
        try:
            sys.stdout.write(f"\n{_ansi_gray(line)}\n\n")
            sys.stdout.flush()
        except Exception:
            print("")
            print(_ansi_gray(line))
            print("")
    recorder = getattr(agent, "_record_task_worked_summary_history", None)
    if callable(recorder):
        try:
            recorder(int(max(0, int(elapsed_seconds or 0))))
        except Exception:
            pass


def _refresh_context_usage_after_task_boundary(
    agent: Any,
    user_input_hint: str = "",
    context_hint: str = "",
) -> None:
    """
    Force a context-usage refresh at conversation boundary moments
    (turn finished/cancelled/request_user_input pause), so the status bar
    reflects the latest in-context anchor immediately.
    """
    try:
        refresh_fn = getattr(agent, "_refresh_status_context_usage_snapshot", None)
        if callable(refresh_fn):
            refresh_fn(
                user_input_hint=str(user_input_hint or ""),
                context_hint=str(context_hint or ""),
            )
    except KeyboardInterrupt:
        pass
    except Exception:
        pass
    try:
        svc = getattr(agent, "session_memory_service", None)
        schedule_refresh = getattr(svc, "schedule_context_usage_refresh_async", None)
        if callable(schedule_refresh):
            schedule_refresh(
                user_input_hint=str(user_input_hint or ""),
                context_hint=str(context_hint or ""),
                expected_chat_id=str(getattr(agent, "active_chat_id", "") or "").strip(),
            )
    except KeyboardInterrupt:
        pass
    except Exception:
        pass


def _emit_flow_log(message: str) -> None:
    msg = f"[Flow] {message}"
    try:
        get_logger(f"{get_app_logger_root()}.runtime.flow").info(msg)
    except Exception:
        pass


def _gui_round_mark(agent: Any, begin: bool) -> None:
    """Bracket one model round so the GUI can show a per-round wait timer.

    No-op outside GUI plain-stream mode (the TUI ignores it). ``begin`` fires
    just before the model request is sent; the matching end fires once the model
    has fully responded, before this round's tool output streams.
    """
    if not bool(getattr(agent, "_gui_plain_stream", False)):
        return
    hook = getattr(agent, "_gui_round_begin" if begin else "_gui_round_end", None)
    if callable(hook):
        try:
            hook()
        except Exception:
            pass


def _ensure_tui_active_chat(agent: Any) -> None:
    """Create a new chat on first user message when the workspace has none (TUI only).

    If the agent already has an active chat this is a no-op.  Named and (once
    created) activated identically to the GUI ``new_chat`` path, picking up the
    localised default name.
    """
    if getattr(agent, "active_chat_id", ""):
        return
    from ..core.localization import get_display_language, translate
    name = translate("chat.new.default_name", get_display_language(agent))
    with agent._chat_state_lock:
        cid = agent._next_chat_id()
        agent._chat_entries().append(agent._new_chat_entry(cid, name=name))
        agent._save_chat_state()
    agent._activate_chat(cid, announce=False, clear_screen=False, print_history=False)

def _sanitize_prompt_pollution(text: str, work_directory: Any) -> str:
    s = str(text or "")
    if not s:
        return s
    cleaned = s.replace("\r", "").strip()
    if not cleaned:
        return ""

    wd = str(work_directory or "").strip()
    if wd:
        prompt_prefix = f"{wd}>"
        while cleaned.startswith(prompt_prefix):
            cleaned = cleaned[len(prompt_prefix):].lstrip()
    while cleaned.startswith("›"):
        cleaned = cleaned[1:].lstrip()

    if re.match(r"^>{2,}\s*\S", cleaned):
        cleaned = re.sub(r"^>+\s*", "", cleaned, count=1)

    return cleaned


def _should_record_command_input_history(user_input: str) -> bool:
    text = str(user_input or "").strip()
    if not text:
        return False
    return True


def _sync_command_input_history(agent: Any, user_input: str) -> None:
    """
    Sync persisted and in-memory command history after each non-empty input.
    - Normal commands: record with de-duplication (move to latest).
    - Slash built-ins: record the same way so reload and future sessions keep
      the user's full input history.
    """
    text = str(user_input or "").strip()
    if not text:
        return

    if _should_record_command_input_history(text):
        agent.history_manager.add_entry(text)

    if agent.input_handler is not None and hasattr(
        agent.input_handler, "reset_command_history"
    ):
        agent.input_handler.reset_command_history(
            agent.history_manager.get_all_history()
        )


def _format_startup_directory(workspace_dir: Any) -> str:
    raw = str(workspace_dir or "")
    if not raw:
        return raw
    try:
        current = Path(raw).expanduser().resolve(strict=False)
        home = Path.home().resolve(strict=False)
        relative = current.relative_to(home)
    except Exception:
        return raw

    rel_text = str(relative)
    if not rel_text or rel_text == ".":
        return "~"
    return f"~{os.sep}{rel_text}"


def _startup_text_display_width(text: str) -> int:
    width = 0
    for ch in str(text or ""):
        if unicodedata.combining(ch):
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _truncate_startup_text(text: str, max_width: int) -> str:
    raw = str(text or "")
    limit = max(0, int(max_width or 0))
    if _startup_text_display_width(raw) <= limit:
        return raw
    if limit <= 0:
        return ""
    suffix = "…"
    suffix_w = _startup_text_display_width(suffix)
    if limit <= suffix_w:
        return suffix
    out: List[str] = []
    used = 0
    body_limit = limit - suffix_w
    for ch in raw:
        ch_w = _startup_text_display_width(ch)
        if used + ch_w > body_limit:
            break
        out.append(ch)
        used += ch_w
    return "".join(out) + suffix


def _pad_startup_text(text: str, target_width: int) -> str:
    pad = max(0, int(target_width or 0) - _startup_text_display_width(text))
    return " " * pad


def _startup_terminal_columns(default: int = 80) -> int:
    width = 0
    stream = sys.stdout
    try:
        fn = getattr(stream, _STREAM_ATTR_TERMINAL_COLUMNS, None)
        if callable(fn):
            width = int(fn() or 0)
    except Exception:
        width = 0
    if width <= 0:
        try:
            if hasattr(stream, "fileno"):
                width = int(os.get_terminal_size(stream.fileno()).columns or 0)
        except Exception:
            width = 0
    if width <= 0:
        try:
            width = int(os.get_terminal_size(sys.__stdout__.fileno()).columns or 0)
        except Exception:
            width = 0
    if width <= 0:
        try:
            width = int(shutil.get_terminal_size(fallback=(int(default or 80), 24)).columns or 0)
        except Exception:
            width = 0
    return max(1, int(width or default or 80))


def _startup_output_prefix_width() -> int:
    try:
        return max(0, int(getattr(sys.stdout, _STREAM_ATTR_OUTPUT_INDENT_WIDTH, 0) or 0))
    except Exception:
        return 0


def _try_record_user_task_message(agent: Any, user_task: str, already_recorded: bool) -> bool:
    """Best-effort: persist the user task before waiting for model response."""
    if bool(already_recorded):
        return True
    text = str(user_task or "").strip()
    if not text:
        return False
    try:
        append_fn = getattr(agent, "_append_chat_message", None)
        if callable(append_fn):
            append_fn("user", text)
            # A new user turn supersedes any leftover request_user_input
            # prompt from a prior abandoned round, so the GUI doesn't
            # keep showing a stale selection panel.
            try:
                clearer = getattr(agent, "_clear_pending_request_user_input", None)
                if callable(clearer):
                    clearer()
            except Exception:
                pass
            try:
                agent._pending_request_user_input_render = ""  # type: ignore[attr-defined]
            except Exception:
                pass
            return True
    except Exception:
        return False
    return False


def _print_startup_overview(agent: Any) -> None:
    # The startup overview is a terminal-only banner (``>_ App (version)`` plus
    # model/workspace/directory lines). In the GUI every chat runs its own
    # ``run_agent_loop`` thread, so a chat created while the GUI is live would
    # otherwise emit this TUI banner as the new chat's first "output". Suppress
    # it entirely in GUI plain-stream mode — the GUI shows this information in
    # its own chrome.
    if bool(getattr(agent, "_gui_plain_stream", False)):
        return

    from ..core.localization import get_display_language, translate

    lang = get_display_language(agent)
    t = lambda key, fallback=None, **kwargs: translate(key, lang, fallback, **kwargs)

    model_name = str(getattr(agent, "model_name", "") or "")
    workspace_name = str(getattr(agent, "workspace_name", "") or "")
    workspace_dir = _format_startup_directory(getattr(agent, "workspace_root", "") or "")
    app_name = get_app_name()
    version = get_app_display_version()

    line1 = f">_ {app_name} ({version})"
    # Right-pad the three label prefixes so all values (model/workspace/
    # directory) start at the same visual column. The English locale already
    # carries trailing spaces inside the translation, but CJK locales like
    # ``zh-CN`` only carry the bare label + fullwidth colon and therefore
    # have different prefix widths (e.g. ``模型：`` = 6 cells vs. ``工作区：``
    # = 8). Padding here keeps the renderer locale-agnostic.
    prefix_model_raw = t("startup.model_prefix")
    prefix_workspace_raw = t("startup.workspace_prefix")
    prefix_directory_raw = t("startup.directory_prefix")
    prefix_target_width = max(
        _startup_text_display_width(prefix_model_raw),
        _startup_text_display_width(prefix_workspace_raw),
        _startup_text_display_width(prefix_directory_raw),
    )
    prefix_model = prefix_model_raw + _pad_startup_text(
        prefix_model_raw, prefix_target_width
    )
    prefix_workspace = prefix_workspace_raw + _pad_startup_text(
        prefix_workspace_raw, prefix_target_width
    )
    prefix_directory = prefix_directory_raw + _pad_startup_text(
        prefix_directory_raw, prefix_target_width
    )
    # Build line2 from the same visible glyphs that the colorized renderer
    # below emits ("/model" + model_change_suffix), so that the plain-text
    # width used for pad/truncate calculations matches the actual on-screen
    # width. Using the longer ``model_change_hint`` here would over-pad the
    # plain measurement and shift the right border for line2 only.
    line2 = (
        f"{prefix_model}{model_name}  /model"
        f"{t('startup.model_change_suffix')}"
    )
    line3 = f"{prefix_workspace}{workspace_name}"
    line4 = f"{prefix_directory}{workspace_dir}"

    term_cols = _startup_terminal_columns(default=80)
    prefix_width = _startup_output_prefix_width()
    # Leave one spare column so terminals that auto-wrap at the right edge do
    # not push the closing border onto a new line when slash output adds indent.
    max_box_width = max(4, term_cols - prefix_width - 1)
    desired_inner_width = (
        max(
            _startup_text_display_width(line1),
            _startup_text_display_width(line2),
            _startup_text_display_width(line3),
            _startup_text_display_width(line4),
        )
        + 2
    )
    width = max(2, min(desired_inner_width, max_box_width - 2))
    content_width = max(1, width - 1)
    line1_fit = _truncate_startup_text(line1, content_width)
    line2_fit = _truncate_startup_text(line2, content_width)
    line3_fit = _truncate_startup_text(line3, content_width)
    line4_fit = _truncate_startup_text(line4, content_width)

    top = "╭" + ("─" * width) + "╮"
    mid2 = "│" + (" " * width) + "│"
    bottom = "╰" + ("─" * width) + "╯"

    print(_ansi_gray(top))
    # Header line: use terminal default foreground for main text, prefix gray.
    if line1_fit == line1:
        line1_rendered = _ansi_gray(">_ ") + app_name + _ansi_gray(f" ({version})")
    else:
        line1_rendered = line1_fit
    print(_ansi_gray("│ ") + line1_rendered + _ansi_gray(_pad_startup_text(line1_fit, content_width)) + _ansi_gray("│"))
    print(_ansi_gray(mid2))
    # model line (uses the padded prefix computed above so all values share
    # one visual column).
    if line2_fit == line2:
        line2_rendered = (
            _ansi_gray(prefix_model)
            + model_name
            + _ansi_gray("  ")
            + _ansi_cyan("/model")
            + _ansi_gray(t("startup.model_change_suffix"))
        )
    else:
        line2_rendered = line2_fit
    print(_ansi_gray("│ ") + line2_rendered + _ansi_gray(_pad_startup_text(line2_fit, content_width)) + _ansi_gray("│"))
    # workspace line (padded prefix)
    if line3_fit == line3:
        line3_rendered = _ansi_gray(prefix_workspace) + workspace_name
    else:
        line3_rendered = line3_fit
    print(_ansi_gray("│ ") + line3_rendered + _ansi_gray(_pad_startup_text(line3_fit, content_width)) + _ansi_gray("│"))
    # directory line (padded prefix)
    if line4_fit == line4:
        line4_rendered = _ansi_gray(prefix_directory) + workspace_dir
    else:
        line4_rendered = line4_fit
    print(_ansi_gray("│ ") + line4_rendered + _ansi_gray(_pad_startup_text(line4_fit, content_width)) + _ansi_gray("│"))
    print(_ansi_gray(bottom))
    startup_chat_warning = str(getattr(agent, "_startup_chat_state_warning", "") or "").strip()
    if startup_chat_warning:
        print(_ansi_yellow(startup_chat_warning))
    print("")
    tip_entry = get_random_startup_tip_entry(language=lang)
    tip_text = str(tip_entry.get("text") or "")
    highlights_raw = tip_entry.get("highlights", [])
    highlights = highlights_raw if isinstance(highlights_raw, list) else []
    rendered_tip = format_tip_with_highlights(
        text=tip_text,
        highlights=[str(h or "") for h in highlights],
        highlight_formatter=_ansi_cyan,
    )
    print("  " + _ansi_bold(t("common.tip")) + rendered_tip)
    print("")


def run_agent_loop(agent: Any):
    """Run the AI Agent main loop with multi-step tool execution until done."""
    self = agent
    from ..core.localization import get_display_language, translate

    lang = get_display_language(agent)
    t = lambda key, fallback=None, **kwargs: translate(key, lang, fallback, **kwargs)
    import sys
    import os
    os_name = os.name

    if self.skills:
        _sk_path = self.config_dir / "skills"

    _print_startup_overview(self)
    try:
        sys.stdout.flush()
    except Exception:
        pass

    import subprocess
    import re
    system_cmd_patterns = [
        r'^cd(\s+.+)?$',
        r'^(dir|ls|list)(\s+.+)?$',
        r'^(del|delete|rm)(\s+.+)?$',
        r'^(ping)(\s+.+)?$',
        r'^(ipconfig|ifconfig)(\s+.+)?$',
        r'^(type|cat)(\s+.+)?$',
        r'^(echo)(\s+.+)?$',
        r'^(whoami|hostname|date|time)(\s+.+)?$',
        r'^(wmic|net)(\s+.+)?$',
    ]
    system_cmd_re = re.compile('|'.join(system_cmd_patterns), re.IGNORECASE)


    while True:
        auto_exit_after_turn = False
        in_task_execution = False
        self._in_task_execution = False
        original_user_task = ""
        user_message_recorded = False
        pre_task_status_ticker: Optional[_WorkingStatusTicker] = None
        active_status_ticker: Optional[_WorkingStatusTicker] = None
        explore_ticker: Any = None
        try:
            self._refresh_input_handler_skill_completions()
            # Get user input (including replayed waiting-state input) and route everything through the main loop.
            if getattr(self, "_queued_user_input", None) is not None:
                user_input = str(self._queued_user_input or "")
                self._queued_user_input = None
                if bool(getattr(self, "_startup_exec_turn_pending", False)):
                    auto_exit_after_turn = True
                    self._startup_exec_turn_pending = False
            else:
                # Before prompting, re-offer the plan execute/modify chooser
                # when the last assistant message still carries an undismissed
                # proposed plan (e.g. after exiting and reloading the chat).
                # This restores the affordance the inline end-of-turn offer
                # can't show on reload, in both Plan and Agent mode.
                plan_followup = None
                try:
                    plan_followup = _maybe_offer_plan_execution_choice_on_prompt(self)
                except Exception:
                    plan_followup = None
                if plan_followup:
                    self._queued_user_input = plan_followup
                    continue
                user_input = self._get_user_input_with_history()
            # GUI composer input is sentinel-prefixed so it is always handled as
            # a model prompt; "/foo" and "!bar" text must not run directly.
            force_prompt = False
            gui_internal_command = False
            if user_input and str(user_input).startswith(GUI_FORCE_PROMPT_PREFIX):
                force_prompt = True
                user_input = str(user_input)[len(GUI_FORCE_PROMPT_PREFIX):]
            elif user_input and str(user_input).startswith(GUI_INTERNAL_COMMAND_PREFIX):
                # Command the GUI issued on the user's behalf: run it, but keep it
                # out of the user's input history (it isn't something they typed).
                gui_internal_command = True
                user_input = str(user_input)[len(GUI_INTERNAL_COMMAND_PREFIX):]
            user_input = _sanitize_prompt_pollution(user_input, self.work_directory)
            raw_user_input = str(user_input or "")
        
            # Save non-empty input to history (never for GUI-internal commands).
            if user_input.strip() and not gui_internal_command:
                _sync_command_input_history(self, user_input)

            stripped_in = user_input.strip()
            if not stripped_in:
                continue

            forced_mcp = self._extract_forced_mcp_reference(stripped_in)
            forced_mcp_entries: List[Dict[str, str]] = (
                list(forced_mcp.get("entries", [])) if forced_mcp else []
            )

            forced_skill: Optional[Dict[str, Any]] = self._extract_forced_skill_reference(stripped_in)
            forced_skills: List[Dict[str, str]] = (
                list(forced_skill.get("skills", [])) if forced_skill else []
            )
            # When slash references are present, route the remaining natural-language
            # part as the task text to avoid the model treating "/skills/<skill-name>" itself as work.
            task_user_input = stripped_in
            try:
                if forced_mcp and str(forced_mcp.get("rest") or "").strip():
                    task_user_input = str(forced_mcp.get("rest") or "").strip()
                if forced_skill and str(forced_skill.get("rest") or "").strip():
                    task_user_input = str(forced_skill.get("rest") or "").strip()
            except Exception:
                task_user_input = stripped_in

            # Built-in slash commands use "/" prefix; direct shell uses "!" prefix.
            builtin_line: Optional[str] = None
            if stripped_in.startswith("/") and not forced_skills and not forced_mcp_entries and not force_prompt:
                builtin_line = stripped_in[1:].lstrip()
                if not builtin_line:
                    print(
                        t(
                            "runtime.builtin_command_empty"
                        )
                    )
                    continue

            if builtin_line is not None:
                slash_command = f"/{builtin_line}"
                slash_out_buf = io.StringIO()
                self._rewrite_previous_prompt_as_user(slash_command)
                try:
                    slash_terminal_columns = max(
                        1, int(self._terminal_columns_for_prompt_separator(default=80) or 80)
                    )
                except Exception:
                    slash_terminal_columns = 80
                slash_stdout_primary = self._build_internal_slash_output_stream(
                    sys.stdout, terminal_columns=slash_terminal_columns
                )
                slash_stderr_primary = self._build_internal_slash_output_stream(
                    sys.stderr, terminal_columns=slash_terminal_columns
                )
                slash_stdout = _TeeTextStream(slash_stdout_primary, slash_out_buf)
                slash_stderr = _TeeTextStream(slash_stderr_primary, slash_out_buf)
                with redirect_stdout(slash_stdout), redirect_stderr(slash_stderr):
                    try:
                        handled, should_exit = dispatch_builtin_command(
                            self,
                            builtin_line,
                            os_name=os_name,
                            wait_for_supplement=False,
                            consume_unknown=False,
                        )
                        if handled:
                            if should_exit:
                                break
                            continue

                        bl = builtin_line.lower()
                        mcp_tool, mcp_args, mcp_err = self._parse_mcp_shortcut_command(builtin_line)
                        if mcp_tool:
                            if is_command(mcp_tool):
                                mcp_res = run_command(self, mcp_tool, mcp_args)
                            else:
                                mcp_res = self.execute_tool_call(mcp_tool, mcp_args)
                            self._print_mcp_shortcut_result(mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {})
                            continue
                        if bl == "mcp" or bl.startswith("mcp "):
                            print(f"❌ {mcp_err}")
                            continue
                        if bl in ('exit', 'quit'):
                            break
                        # clear screen
                        if bl == 'clear screen':
                            os.system('cls' if os_name == 'nt' else 'clear')
                            self._suppress_next_separator = True
                            continue
                        if bl == "clear":
                            print(t("builtin.clear_usage"))
                            continue
                        if bl == 'clear input history':
                            self.history_manager.clear_history()
                            if self.input_handler is not None and hasattr(
                                self.input_handler, "reset_command_history"
                            ):
                                self.input_handler.reset_command_history(
                                    self.history_manager.get_all_history()
                                )
                            print(t("builtin.history_cleared_check"))
                            continue
                        if bl == "clear context":
                            self._clear_active_chat_context_and_tasks()
                            print(t("builtin.context_cleared_full"))
                            try:
                                self._handle_chat_builtin_command("chat reload")
                            except Exception:
                                pass
                            continue
                        if bl == "memory":
                            print(
                                t("builtin.memory_usage")
                            )
                            continue
                        if bl == "memory enable":
                            self.memory_enabled = True
                            ok = self._save_memory_enabled_to_config()
                            print(
                                t(
                                    "memory_command.enabled_saved"
                                    if ok
                                    else "memory_command.enabled_session_only",
                                    config_file=CONFIG_JSONC_FILENAME,
                                )
                            )
                            continue
                        if bl == "memory disable":
                            self.memory_enabled = False
                            ok = self._save_memory_enabled_to_config()
                            print(
                                t(
                                    "memory_command.disabled_saved"
                                    if ok
                                    else "memory_command.disabled_session_only",
                                    config_file=CONFIG_JSONC_FILENAME,
                                )
                            )
                            continue
                        if bl == "memory status":
                            self._print_memory_status_details()
                            continue
                        if bl == "memory stats":
                            print(run_command(self, "memory_stats", {"verbose_print": True}))
                            continue
                        if bl == "memory list":
                            print(run_command(
                                self, "memory_list", {"limit": 20, "verbose_print": True}
                            ))
                            continue
                        if bl.startswith("memory search "):
                            q = builtin_line[len("memory search ") :].strip()
                            if q:
                                self.execute_tool_call(
                                    "memory_search", {"query": q, "verbose_print": True}
                                )
                            else:
                                print(t("memory_command.no_search_query"))
                            continue
                        if bl.startswith("memory remember "):
                            text = builtin_line[len("memory remember ") :].strip()
                            if not text:
                                print(t("memory_command.no_remember_text"))
                                continue
                            title = text[:80] + ("…" if len(text) > 80 else "")
                            self.execute_tool_call(
                                "memory_add",
                                {
                                    "title": title,
                                    "content": text,
                                    "tier": "episodic",
                                    "memory_type": "preference",
                                    "source": "user_request",
                                    "user_request": text,
                                    "verbose_print": True,
                                },
                            )
                            continue
                        if bl.startswith("memory delete "):
                            mid = builtin_line[len("memory delete ") :].strip()
                            if mid:
                                self.execute_tool_call(
                                    "memory_delete",
                                    {"memory_id": mid, "verbose_print": True},
                                )
                            else:
                                print(t("memory_command.no_memory_id"))
                            continue

                        if self._handle_chat_builtin_command(builtin_line):
                            continue

                        if self._handle_workspace_builtin_command(builtin_line):
                            continue

                        if bl.startswith("execution-policy "):
                            policy = ""
                            policy = bl.split(" ", 1)[1].strip().lower()
                            if policy == "show":
                                self._print_execution_policy_details()
                                continue
                            if not policy:
                                print(
                                    t("builtin.execution_policy_usage")
                                )
                            else:
                                self.execute_tool_call("execution_policy_set", {"policy": policy})
                            continue
                        if bl == "execution-policy":
                            print(
                                t("builtin.execution_policy_usage")
                            )
                            continue

                        if bl == "always_confirm-reset":
                            reset_result = self._reset_always_confirm_skip()
                            if isinstance(reset_result, dict) and reset_result.get("success"):
                                print(reset_result.get("message", "always-confirm skip list reset"))
                            continue

                        if bl == 'help':

                            self._print_main_help()

                            continue

                        print(
                            t(
                                "runtime.unrecognized_builtin_command_with_direct_shell_hint"
                            )
                        )
                        continue
                    finally:
                        recorder = getattr(self, "_record_internal_slash_execution_history", None)
                        if callable(recorder):
                            try:
                                recorder(
                                    raw_user_command=slash_command,
                                    output_text=slash_out_buf.getvalue(),
                                )
                            except Exception:
                                pass

            # Direct local execution without AI: requires leading "!" on all platforms.
            run_direct_shell: Optional[str] = None
            if stripped_in.startswith("!") and not force_prompt:
                run_direct_shell = stripped_in[1:].lstrip()
                if not run_direct_shell:
                    print(
                        t(
                            "runtime.direct_shell_prefix_required"
                        )
                    )
                    continue

            if run_direct_shell is not None:
                ui = run_direct_shell
                self._print_direct_shell_command_feedback(ui, failed=False)
                execution_cwd = self._shell_execution_cwd()
                raw_user_direct_cmd = f"!{ui}"
                try:
                    self._last_direct_shell_execution = None
                except Exception:
                    pass
                if self._is_executable_file(ui):
                    exec_ok = bool(self._execute_file_directly(ui))
                    last_direct = getattr(self, "_last_direct_shell_execution", None)
                    if (not exec_ok) and (not self._is_direct_shell_result_aborted(last_direct)):
                        rendered_lines = 0
                        cursor_at_line_start = True
                        if isinstance(last_direct, dict):
                            try:
                                rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                            except Exception:
                                rendered_lines = 0
                            cursor_at_line_start = bool(last_direct.get("cursor_at_line_start", True))
                        else:
                            rendered_lines = _estimate_visible_lines(
                                self, "❌ Failed to execute file\n"
                            )
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=rendered_lines,
                            cursor_at_line_start=cursor_at_line_start,
                            failed=True,
                        )
                    if isinstance(last_direct, dict):
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=str(last_direct.get("executed_command") or ui),
                            cwd=str(last_direct.get("cwd") or execution_cwd),
                            return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else (0 if exec_ok else 1)),
                            stdout_text=str(last_direct.get("stdout") or ""),
                            stderr_text=str(last_direct.get("stderr") or ""),
                            aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                        )
                    else:
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=ui,
                            cwd=str(execution_cwd),
                            return_code=0 if exec_ok else 1,
                            stdout_text="",
                            stderr_text="" if exec_ok else "Command execution failed (no detailed output captured)\n",
                        )
                    aborted_direct_result = self._is_direct_shell_result_aborted(last_direct)
                    self._show_separator_next_prompt = not aborted_direct_result
                    if aborted_direct_result:
                        _render_aborted_direct_shell_feedback(self, ui, last_direct)
                    continue

                user_input_cmd = ui
                if system_cmd_re.match(ui):
                    current_direct_result = None
                    if user_input_cmd.lower().startswith('ls') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[2:].strip()
                    elif user_input_cmd.lower().startswith('list') and os_name == 'nt':
                        user_input_cmd = 'dir ' + user_input_cmd[4:].strip()
                    elif user_input_cmd.lower().startswith('dir') and os_name != 'nt':
                        user_input_cmd = 'ls ' + user_input_cmd[3:].strip()

                    try:
                        if user_input_cmd.lower().startswith('cd '):
                            cd_stdout = ""
                            cd_stderr = ""
                            cd_return_code = 0
                            path = user_input_cmd[3:].strip()
                            try:
                                if path == "..":
                                    new_path = execution_cwd.parent
                                elif path == ".":
                                    new_path = execution_cwd
                                else:
                                    raw_path = Path(path)
                                    if raw_path.is_absolute():
                                        new_path = raw_path
                                    else:
                                        new_path = execution_cwd / raw_path
                                new_path = new_path.resolve()
                                if not new_path.exists():
                                    msg = f"❌ Directory '{path}' does not exist"
                                    print(msg)
                                    cd_stderr = f"{msg}\n"
                                    cd_return_code = 1
                                elif not new_path.is_dir():
                                    msg = f"❌ '{path}' is not a directory"
                                    print(msg)
                                    cd_stderr = f"{msg}\n"
                                    cd_return_code = 1
                                else:
                                    self.work_directory = new_path
                                    if self.input_handler:
                                        self.input_handler.update_work_directory(new_path)
                                    self._save_current_workspace_position()
                                    self._reset_work_directory_to_startup_initial()
                            except Exception as e:
                                msg = f"❌ Failed to change directory: {e}"
                                print(msg)
                                cd_stderr = f"{msg}\n"
                                cd_return_code = 1
                            if cd_return_code != 0:
                                self._repaint_direct_shell_command_feedback_if_failed(
                                    ui,
                                    rendered_output_lines=_estimate_visible_lines(
                                        self, cd_stderr
                                    ),
                                    cursor_at_line_start=True,
                                    failed=True,
                                )
                            self._record_direct_shell_execution_history(
                                raw_user_command=raw_user_direct_cmd,
                                executed_command=user_input_cmd,
                                cwd=str(execution_cwd),
                                return_code=cd_return_code,
                                stdout_text=cd_stdout,
                                stderr_text=cd_stderr,
                            )
                        else:
                            return_code: Optional[int] = None
                            try:
                                return_code = self._run_direct_shell_with_prefixed_output(
                                    user_input_cmd,
                                    execution_cwd,
                                )
                            except Exception as e:
                                msg = f"❌ Command execution error: {e}"
                                print(msg)
                                self._repaint_direct_shell_command_feedback_if_failed(
                                    ui,
                                    rendered_output_lines=_estimate_visible_lines(
                                        self, f"{msg}\n"
                                    ),
                                    cursor_at_line_start=True,
                                    failed=True,
                                )
                                self._record_direct_shell_execution_history(
                                    raw_user_command=raw_user_direct_cmd,
                                    executed_command=user_input_cmd,
                                    cwd=str(execution_cwd),
                                    return_code=1,
                                    stdout_text="",
                                    stderr_text=f"{msg}\n",
                                )
                            finally:
                                self._reset_work_directory_to_startup_initial()
                            if return_code is not None:
                                last_direct = getattr(self, "_last_direct_shell_execution", None)
                                current_direct_result = last_direct
                                if int(return_code) != 0 and not self._is_direct_shell_result_aborted(last_direct):
                                    rendered_lines = 0
                                    cursor_at_line_start = True
                                    if isinstance(last_direct, dict):
                                        try:
                                            rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                                        except Exception:
                                            rendered_lines = 0
                                        cursor_at_line_start = bool(
                                            last_direct.get("cursor_at_line_start", True)
                                        )
                                    self._repaint_direct_shell_command_feedback_if_failed(
                                        ui,
                                        rendered_output_lines=rendered_lines,
                                        cursor_at_line_start=cursor_at_line_start,
                                        failed=True,
                                    )
                                if isinstance(last_direct, dict):
                                    self._record_direct_shell_execution_history(
                                        raw_user_command=raw_user_direct_cmd,
                                        executed_command=str(last_direct.get("executed_command") or user_input_cmd),
                                        cwd=str(last_direct.get("cwd") or execution_cwd),
                                        return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else return_code),
                                        stdout_text=str(last_direct.get("stdout") or ""),
                                        stderr_text=str(last_direct.get("stderr") or ""),
                                        aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                                    )
                                else:
                                    self._record_direct_shell_execution_history(
                                        raw_user_command=raw_user_direct_cmd,
                                        executed_command=user_input_cmd,
                                        cwd=str(execution_cwd),
                                        return_code=int(return_code),
                                        stdout_text="",
                                        stderr_text="",
                                    )
                    except Exception as e:
                        msg = f"❌ System command execution error: {e}"
                        print(msg)
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=_estimate_visible_lines(
                                self, f"{msg}\n"
                            ),
                            cursor_at_line_start=True,
                            failed=True,
                        )
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=user_input_cmd,
                            cwd=str(execution_cwd),
                            return_code=1,
                            stdout_text="",
                            stderr_text=f"{msg}\n",
                        )
                    aborted_direct_result = self._is_direct_shell_result_aborted(
                        current_direct_result
                    )
                    self._show_separator_next_prompt = not aborted_direct_result
                    if aborted_direct_result:
                        _render_aborted_direct_shell_feedback(
                            self, ui, current_direct_result
                        )
                    continue

                # e.g. !git status — not in the small whitelist but still direct shell
                return_code: Optional[int] = None
                last_direct = None
                try:
                    return_code = self._run_direct_shell_with_prefixed_output(
                        ui,
                        execution_cwd,
                    )
                except Exception as e:
                    msg = f"❌ Command execution error: {e}"
                    print(msg)
                    self._repaint_direct_shell_command_feedback_if_failed(
                        ui,
                        rendered_output_lines=_estimate_visible_lines(
                            self, f"{msg}\n"
                        ),
                        cursor_at_line_start=True,
                        failed=True,
                    )
                    self._record_direct_shell_execution_history(
                        raw_user_command=raw_user_direct_cmd,
                        executed_command=ui,
                        cwd=str(execution_cwd),
                        return_code=1,
                        stdout_text="",
                        stderr_text=f"{msg}\n",
                    )
                finally:
                    self._reset_work_directory_to_startup_initial()
                if return_code is not None:
                    last_direct = getattr(self, "_last_direct_shell_execution", None)
                    if int(return_code) != 0 and not self._is_direct_shell_result_aborted(last_direct):
                        rendered_lines = 0
                        cursor_at_line_start = True
                        if isinstance(last_direct, dict):
                            try:
                                rendered_lines = int(last_direct.get("rendered_output_lines") or 0)
                            except Exception:
                                rendered_lines = 0
                            cursor_at_line_start = bool(last_direct.get("cursor_at_line_start", True))
                        self._repaint_direct_shell_command_feedback_if_failed(
                            ui,
                            rendered_output_lines=rendered_lines,
                            cursor_at_line_start=cursor_at_line_start,
                            failed=True,
                        )
                    if isinstance(last_direct, dict):
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=str(last_direct.get("executed_command") or ui),
                            cwd=str(last_direct.get("cwd") or execution_cwd),
                            return_code=int(last_direct.get("return_code") if last_direct.get("return_code") is not None else return_code),
                            stdout_text=str(last_direct.get("stdout") or ""),
                            stderr_text=str(last_direct.get("stderr") or ""),
                            aborted_by_user=bool(last_direct.get("aborted_by_user", False)),
                        )
                    else:
                        self._record_direct_shell_execution_history(
                            raw_user_command=raw_user_direct_cmd,
                            executed_command=ui,
                            cwd=str(execution_cwd),
                            return_code=int(return_code),
                            stdout_text="",
                            stderr_text="",
                        )
                aborted_direct_result = self._is_direct_shell_result_aborted(last_direct)
                self._show_separator_next_prompt = not aborted_direct_result
                if aborted_direct_result:
                    _render_aborted_direct_shell_feedback(self, ui, last_direct)
                continue

            # Natural-language turn: rewrite prompt line as chat-style user line.
            in_task_execution = True
            self._in_task_execution = True
            task_started_at = time.monotonic()
            worked_summary_emitted = False
            turn_send_started_at = time.perf_counter()
            # Auto-create a chat on first message when the workspace has none.
            _ensure_tui_active_chat(self)
            # Drop any prior ephemeral on-screen notices (e.g., multi-attempt
            # model-call errors). They are intentionally tied to the moment
            # they surfaced and should not bleed into the next user turn.
            try:
                clear_notices = getattr(self, "clear_ephemeral_screen_notices", None)
                if callable(clear_notices):
                    clear_notices()
            except Exception:
                pass
            _emit_flow_log(
                f"User submitted input: chars={len(task_user_input)}, active_chat={getattr(self, 'active_chat_id', '')}"
            )
            self._start_interrupt_monitor(cancel_task_on_interrupt=True)
            self._consume_task_interrupt_requested()
            try:
                self._conversation_interrupt_banner_recent = False
                self._conversation_interrupt_banner_recent_at = 0.0
            except Exception:
                pass
            self._rewrite_previous_prompt_as_user(raw_user_input.strip())
            # Plan-mode is a session-sticky flag toggled via ``/plan`` /
            # ``/agent`` commands. When on, the active mode's instructions ride
            # the system prompt's ``<collaboration_mode>`` section (see
            # CollaborationModePart), telling the model to outline a plan rather
            # than execute destructive tools. Outgoing messages and history are
            # unaffected, so chat history stores the user's verbatim text.
            original_user_task = task_user_input
            # ``task_user_input`` has had any skill / MCP reference markers
            # (``[skill: ...]``, ``[mcp tool|prompt: ...]``, ``/skills/...``)
            # stripped so the model treats only the natural-language remainder
            # as the task. For the *recorded* history entry, however, keep the
            # original markers so that reloading the chat (TUI or GUI) can
            # re-render the referenced skill / MCP as an inline pill instead of
            # silently dropping it.
            recorded_user_task = stripped_in
            maybe_auto_compact = getattr(
                getattr(self, "session_memory_service", None),
                "maybe_auto_compact_before_user_message",
                None,
            )
            if callable(maybe_auto_compact):
                try:
                    maybe_auto_compact(original_user_task)
                except Exception:
                    try:
                        _emit_flow_log("Automatic context compact failed; continuing with the current request")
                    except Exception:
                        pass
            pre_task_status_ticker = _new_working_status_ticker(self)
            pre_task_status_ticker.start()

            last_result = None
            self._last_auto_removed_ephemeral = None
            user_message_recorded = _try_record_user_task_message(
                self, recorded_user_task, already_recorded=user_message_recorded
            )
            in_task_execution = True
            self._active_skill_full_prompt = ""
            self._active_skill_id = None
            self._active_skill_source = None
            self._active_skill_section = 0
            self._active_skill_total_sections = 0
            self._active_skill_chunked = False
            forced_skill_prefix = ""
            forced_mcp_prefix = ""
            if forced_mcp_entries:
                pre_task_status_ticker = _stop_pre_task_status_ticker_for_console_output(
                    self,
                    pre_task_status_ticker,
                )
                mcp_items = []
                # Mirror the forced-skill path: inject each MCP prompt body as a
                # separate internal user message (not embedded in the task text)
                # and track it in ``_session_injected_mcp_prompts`` so the same
                # prompt is never injected twice — across turns, edits/reloads,
                # or a later ``mcp_get_prompt`` tool call.
                session_injected_mcp = getattr(self, "_session_injected_mcp_prompts", set())
                gui_mode = callable(getattr(self, "_confirm_choice_provider", None))
                for e in forced_mcp_entries:
                    srv = str(e.get("server", "")).strip()
                    name = str(e.get("name", "")).strip()
                    kind = str(e.get("kind", "")).strip() or "unknown"
                    mcp_items.append(f"`/mcp/{srv}/{name}`({kind})")
                    if not gui_mode:
                        print(t("runtime.mcp_reference_enabled", server=srv, name=name, kind=kind))
                    if kind != "prompt":
                        continue
                    prompt_key = f"{srv}/{name}"
                    if prompt_key in session_injected_mcp:
                        continue
                    pobj = None
                    try:
                        pobj = self.mcp_manager.get_prompt(srv, name, {}, timeout_s=20.0)
                    except Exception:
                        pobj = None
                    if not isinstance(pobj, dict):
                        continue
                    body_lines = []
                    desc = str(pobj.get("description", "")).strip()
                    if desc:
                        body_lines.append(f"prompt.description: {desc}")
                    msgs = pobj.get("messages", [])
                    if isinstance(msgs, list):
                        for msg in msgs:
                            if not isinstance(msg, dict):
                                continue
                            role = str(msg.get("role", "")).strip()
                            content = msg.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                text = str(content.get("text", "")).strip()
                                if text:
                                    body_lines.append(f"prompt.{role}: {text}")
                    if not body_lines:
                        continue
                    mcp_msg = (
                        f"----- BEGIN MCP PROMPT (server={srv}, name={name}) -----\n"
                        + "\n".join(body_lines)
                        + "\n----- END MCP PROMPT -----"
                    )
                    self._append_chat_message("user", mcp_msg, _internal=True)
                    session_injected_mcp.add(prompt_key)
                if mcp_items:
                    forced_mcp_prefix = self._build_forced_mcp_prefix(forced_mcp_entries)
            preloaded_skill_ids: Set[str] = set()
            if forced_skills:
                skill_items = []
                full_prompts: List[Tuple[str, str]] = []  # (skill_id, prompt_text)
                session_injected = getattr(self, "_session_injected_skills", set())
                for s in forced_skills:
                    sid = str(s.get("skill_id") or "").strip()
                    sname = str(s.get("name") or sid).strip()
                    if not sid:
                        continue
                    canon_sid = self._canonical_skill_id(sid)
                    skill_items.append(f"`{sname}`(skill_id=`{sid}`)")
                    if canon_sid and canon_sid in session_injected:
                        continue
                    full_prompt, meta = self._build_single_skill_prompt(sid)
                    if full_prompt:
                        pre_task_status_ticker = _stop_pre_task_status_ticker_for_console_output(
                            self,
                            pre_task_status_ticker,
                        )
                        if not callable(getattr(self, "_confirm_choice_provider", None)):
                            print(t("runtime.skill_enabled", skill=sname))
                        full_prompts.append((sid, full_prompt))
                        preloaded_skill_ids.add(canon_sid)
                        if canon_sid:
                            session_injected.add(canon_sid)
                        if not self._active_skill_id:
                            self._active_skill_id = sid
                            self._active_skill_source = "local" if self._is_local_skill_id(sid) else "mcp"
                            self._active_skill_section = int(meta.get("section") or 0)
                            self._active_skill_total_sections = int(meta.get("total") or 0)
                            self._active_skill_chunked = bool(meta.get("chunked", False))
                if skill_items:
                    forced_skill_prefix = (
                        f"[Forced skills] This turn must prioritize these skills in user-input order: {', '.join(skill_items)}. "
                        "Follow each corresponding SKILL.md. If they conflict with AGENTS.md or general system instructions, "
                        "the skill bodies take precedence except for safety, privilege, and destructive-action hard limits.\n\n"
                    )
                if full_prompts:
                    self._active_skill_full_prompt = "\n".join(fp for _, fp in full_prompts)
                    for sid, fp in full_prompts:
                        skill_msg = (
                            f"----- BEGIN SKILL PROMPT (skill_id={sid}) -----\n"
                            f"{fp}\n"
                            f"----- END SKILL PROMPT -----"
                        )
                        self._append_chat_message("user", skill_msg, _internal=True)
            memory_runtime_enabled = bool(getattr(self, "memory_enabled", True))
            base_rules: List[str] = [
                "For tasks that require two or more steps, briefly state what will be done, then list Step 1..N with status (pending/in_progress/completed/failed).",
                "If a tool is needed, the same assistant message must include both content=visible plan/status and tool_calls=standard API tool-call field. Do not split the plan and tool invocation into separate messages or turns.",
                "Never output or serialize `tool_calls`, `content/tool_calls` message objects, tool JSON/YAML, XML/tags, markdown tool-call code blocks, or any pseudo tool-call format in content.",
                "For multi-step tool tasks, the first turn must not contain only tool_calls without a visible task summary and step plan; it must also not contain only a plan without the required standard API tool_calls.",
                "When no further tool is needed and the user request can be answered, finish by replying in natural language with no tool_calls; the host returns to the command prompt automatically.",
            ]
            if memory_runtime_enabled:
                base_rules.append(
                    "If the task needs a natural-language reference resolved to a stable identifier or mapping, read experiential memory first; if still insufficient, use `memory_search` before search, shell, or request_skill_prompt. Do not guess identifiers before checking memory."
                )
            base_rules.append(
                "If any network search/fetch/online query/tool/script/skill was used, before finishing output a search-results summary with key facts, source highlights, and relevance to the user question. Do not finish directly after a search call."
            )
            numbered_rules = "".join(
                f"{idx + 1}) {rule}\n" for idx, rule in enumerate(base_rules)
            )
            memory_rules_block = ""
            if memory_runtime_enabled:
                memory_rules_block = (
                    "[Experiential memory `memory_*` rules]\n"
                    "- `memory_search`: if injected experiential memory already contains enough information, do not call it merely for process. If the task depends on an entity identifier or alias mapping that may exist only in memory and no reliable value is visible, call `memory_search` before downstream tools; do not invent identifiers.\n"
                    "- `memory_add`: use it when the user explicitly asks to remember something or use a preference in the future and it is personal experiential information rather than documentation. If you believe the user's statement is clearly wrong, you may record your judgment in `system_note` according to tool rules.\n"
                )
            task_uses_standard_openai_tools = bool(self._use_standard_openai_tools_call())
            project_context_task = (
                task_uses_standard_openai_tools
                and self._project_context_feature_enabled()
                and _should_prioritize_project_context_for_task(original_user_task)
            )
            first_round_evidence = ""
            if project_context_task:
                project_context_ready = False
                project_context_files_total = 0
                project_context_inflight = False
                project_context_skip_reason = "unknown"
                project_context_refreshed_now = False
                try:
                    project_context_inflight = bool(
                        getattr(self, "_project_context_refresh_inflight", False)
                    )
                    if project_context_inflight:
                        project_context_ready = False
                        project_context_skip_reason = "refresh_inflight"
                    else:
                        idx = getattr(self, "_project_context_index", None)
                        if idx is None:
                            project_context_skip_reason = "index_missing"
                        else:
                            files_map = getattr(idx, "files", None)
                            project_context_files_total = (
                                len(files_map) if isinstance(files_map, dict) else 0
                            )
                            if project_context_files_total <= 0:
                                project_context_skip_reason = "index_empty"
                                project_context_refreshed_now = False
                            else:
                                project_context_ready = True
                                project_context_skip_reason = "ready"
                except Exception as e:
                    project_context_ready = False
                    project_context_skip_reason = f"status_check_failed:{type(e).__name__}"

                if project_context_ready:
                    project_context_started_at = time.perf_counter()
                    _emit_flow_log(
                        "First-round project context retrieval preparation started: "
                        f"files_total={project_context_files_total}"
                    )
                    ev_args = {
                        "query": original_user_task,
                        "max_files": 8,
                        "refresh": False,
                        "refresh_async": not project_context_refreshed_now,
                    }
                    ev_res = self.execute_tool_call("project_context_search", ev_args)
                    self.operation_results.append(
                        {
                            "command": {"action": "project_context_search", "params": ev_args},
                            "result": ev_res,
                            "timestamp": datetime.now().isoformat(),
                        }
                    )
                    first_round_evidence = self._render_evidence_block_from_project_context_result(ev_res)
                    project_context_elapsed_ms = int((time.perf_counter() - project_context_started_at) * 1000)
                    _emit_flow_log(
                        "First-round project context retrieval preparation finished: "
                        f"success={bool(ev_res.get('success', False))}, "
                        f"matches={int(ev_res.get('total_matches', 0) or 0)}, "
                        f"elapsed_ms={project_context_elapsed_ms}"
                    )
                else:
                    if not project_context_inflight:
                        try:
                            self._schedule_project_context_refresh_background(
                                force=False,
                                reason="first-round-evidence-not-ready",
                            )
                        except Exception:
                            pass
                    _emit_flow_log(
                        "First-round project context retrieval preparation finished: "
                        f"skipped(not_ready:{project_context_skip_reason}), "
                        f"files_total={project_context_files_total}, "
                        f"refresh_inflight={project_context_inflight}"
                    )
            else:
                _emit_flow_log("First-round project context retrieval preparation finished: skipped(feature_disabled)")
            next_input = (
                f"{forced_mcp_prefix}{forced_skill_prefix}{original_user_task}"
                f"{(chr(10) + chr(10) + first_round_evidence) if first_round_evidence else ''}"
            )
            ready_to_send_elapsed_ms = int((time.perf_counter() - turn_send_started_at) * 1000)
            _emit_flow_log(f"First-round request preparation complete; sending actual request: elapsed_ms={ready_to_send_elapsed_ms}")
            self._memory_injected_this_task = False
            is_first_round = True
            last_announced_skill_key: Optional[str] = None
            raw_max_tool_rounds = getattr(self, "max_tool_rounds", None)
            try:
                parsed_max_tool_rounds = (
                    int(raw_max_tool_rounds) if raw_max_tool_rounds is not None else None
                )
            except Exception:
                parsed_max_tool_rounds = None
            max_tool_rounds = (
                parsed_max_tool_rounds if parsed_max_tool_rounds and parsed_max_tool_rounds > 0 else None
            )
            max_no_tool_rounds = 3
            no_tool_rounds = 0
            # Counts pseudo-tool-call retries within this turn. Only
            # the unrecoverable branch increments it, so a retry
            # caused by the model self-recovering on a later round
            # never escalates the prompt. Used to escalate the retry
            # prompt with a concrete JSON tool_calls example after
            # the first retry didn't fix the issue.
            pseudo_retry_attempts = 0
            tool_round = 0
            plan_finalize_nudged = False
            turn_used_request_user_input = False
            # Plan mode: set once the model emits a ``<proposed_plan>`` block this
            # turn — that, not ``update_plan`` (now blocked in Plan mode), is the
            # signal that planning is done and the execute chooser should appear.
            turn_emitted_proposed_plan = False
            while max_tool_rounds is None or tool_round < max_tool_rounds:
                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                tool_round += 1
                status_ticker = pre_task_status_ticker
                pre_task_status_ticker = None
                if status_ticker is None:
                    status_ticker = _new_working_status_ticker(self)
                    status_ticker.start()
                active_status_ticker = status_ticker
                status_ticker_stopped = False

                def _stop_status_ticker_before_first_output() -> None:
                    nonlocal status_ticker_stopped, active_status_ticker
                    if status_ticker_stopped:
                        return
                    status_ticker.stop()
                    self._clear_last_thinking_line()
                    status_ticker_stopped = True
                    active_status_ticker = None

                # Expose the ticker-stop hook to synchronous callbacks that
                # may need to write to the terminal *before* call_ai returns
                # (e.g. the ephemeral notice channel surfacing a multi-attempt
                # model-call error trail). Without this hook, those writes
                # would race with the still-running status ticker and leave
                # the spinner line interleaved with the error output.
                self._active_status_ticker_stopper = _stop_status_ticker_before_first_output
                try:
                    standard_tool_schemas = list(getattr(self, "tool_specs", []) or [])
                    if not bool(getattr(self, "memory_enabled", True)):
                        standard_tool_schemas = [
                            item
                            for item in standard_tool_schemas
                            if str(
                                ((item or {}).get("function", {}) or {}).get("name", "")
                            ).strip()
                            not in MEMORY_TOOLS
                        ]
                    if not self._multimodal_enabled_for_current_model():
                        standard_tool_schemas = [
                            item
                            for item in standard_tool_schemas
                            if str(
                                ((item or {}).get("function", {}) or {}).get("name", "")
                            ).strip()
                            not in IMAGE_INPUT_TOOLS
                        ]
                    # Collaboration-mode gating: in Plan mode hide mutating /
                    # checklist tools (update_plan) and expose the Plan-only
                    # ``request_user_input``; in Agent mode do the inverse.
                    plan_mode_active = bool(getattr(self, "_plan_mode_sticky", False))
                    drop_tools = (
                        PLAN_MODE_EXCLUDED_TOOLS if plan_mode_active else PLAN_MODE_ONLY_TOOLS
                    )
                    if drop_tools:
                        standard_tool_schemas = [
                            item
                            for item in standard_tool_schemas
                            if str(
                                ((item or {}).get("function", {}) or {}).get("name", "")
                            ).strip()
                            not in drop_tools
                        ]
                    # Open this round's wait timer for the GUI just before the
                    # model request goes out.
                    _gui_round_mark(self, True)
                    # Plan-mode instructions now ride the system prompt's
                    # ``<collaboration_mode>`` section (see CollaborationModePart),
                    # so outgoing messages are sent verbatim — no per-message
                    # directive suffix and nothing extra to strip from history.
                    model_input = next_input
                    _brief_ctx = ""
                    if last_result and isinstance(last_result, dict):
                        _tool_ctx = str(last_tool_name or "")
                        _succ_ctx = bool(last_result.get("success", True))
                        _msg_ctx = str(last_result.get("message", "") or "")
                        _err_ctx = str(last_result.get("error", "") or "")
                        if _tool_ctx and _succ_ctx:
                            _brief_ctx = f"Latest tool: {_tool_ctx}, success=True"
                            if _msg_ctx:
                                _brief_ctx += f", message: {_msg_ctx[:120]}"
                        elif _tool_ctx:
                            _brief_ctx = f"Latest tool: {_tool_ctx}, success=False"
                            if _err_ctx:
                                _brief_ctx += f", error: {_err_ctx[:120]}"
                    ai_result = self.call_ai(
                        model_input,
                        context=_brief_ctx,
                        stream=None,
                        return_message=task_uses_standard_openai_tools,
                        history_user_input=original_user_task if not user_message_recorded else None,
                        history_skip_user=user_message_recorded,
                        tool_schemas=standard_tool_schemas,
                        tool_choice="auto" if task_uses_standard_openai_tools else None,
                    )
                except Exception:
                    _stop_status_ticker_before_first_output()
                    self._active_status_ticker_stopper = None
                    raise
                try:
                    if self._consume_task_interrupt_requested():
                        raise KeyboardInterrupt
                    message_tool_plans: List[Tuple[str, Dict[str, Any]]] = []
                    pending_stream_history_reload = False
                    if isinstance(ai_result, dict):
                        if not status_ticker_stopped:
                            _stop_status_ticker_before_first_output()
                        msg_content = ai_result.get("content", "")
                        ai_response = msg_content if isinstance(msg_content, str) else str(msg_content or "")
                        # Forward thinking content to GUI for non-streaming responses.
                        _thinking_text = str(ai_result.get("_thinking", ai_result.get("thinking", "")) or "").strip()
                        if not _thinking_text:
                            _m = re.search(
                                r"<\|channel\>\s*thought\s*\n?(.*?)<channel\|>",
                                ai_response,
                                re.IGNORECASE | re.DOTALL,
                            )
                            if _m:
                                _thinking_text = _m.group(1).strip()
                        if _thinking_text:
                            _gui_thinking_hook = getattr(self, "_gui_thinking_chunk", None)
                            if callable(_gui_thinking_hook):
                                try:
                                    _gui_thinking_hook(_thinking_text)
                                except Exception:
                                    pass
                        streamed_assistant_output = False
                        message_tool_plans = (
                            _parse_tool_plans_from_model_message(ai_result)
                            if task_uses_standard_openai_tools
                            else []
                        )
                        if not task_uses_standard_openai_tools and not message_tool_plans:
                            message_tool_plans = _extract_nonstandard_tool_plans(
                                self,
                                ai_result,
                                ai_response,
                            )
                    else:
                        ai_response, streamed_assistant_output = _consume_streaming_ai_response(
                            self,
                            ai_result,
                            before_first_visible_output=_stop_status_ticker_before_first_output,
                        )
                        pending_stream_history_reload = _take_pending_stream_history_reload_request(self)
                        stream_final_message = getattr(ai_result, "final_message", None)
                        if isinstance(stream_final_message, dict):
                            if not ai_response:
                                msg_content = stream_final_message.get("content", "")
                                ai_response = msg_content if isinstance(msg_content, str) else str(msg_content or "")
                            message_tool_plans = (
                                _parse_tool_plans_from_model_message(stream_final_message)
                                if task_uses_standard_openai_tools
                                else []
                            )
                        if not task_uses_standard_openai_tools and not message_tool_plans:
                            # Some "basic chat" rounds still come back with real
                            # API-level tool_calls even though this turn opted
                            # out of ``return_message=True``. Others only persist
                            # the plan to history and return an empty visible
                            # string. Support both without dropping the tool
                            # call on the floor.
                            message_tool_plans = _extract_nonstandard_tool_plans(
                                self,
                                ai_result,
                                ai_response,
                            )
                        # Ensure thinking content from the stream result is stored in
                        # the latest assistant message in conversation history.
                        _thinking = getattr(ai_result, "thinking_text", "") or ""
                        _thinking_from_content = getattr(ai_result, "_thinking_from_content", False)
                        if _thinking:
                            _ensure_thinking_in_latest_assistant_message(self, _thinking, _thinking_from_content)
                finally:
                    self._active_status_ticker_stopper = None
                # The model has fully responded for this round; freeze its wait
                # timer before any tool output for the round streams out.
                _gui_round_mark(self, False)
                if not status_ticker_stopped:
                    _stop_status_ticker_before_first_output()
                if not isinstance(ai_response, str):
                    print(t("runtime.ai_invalid_response", value=ai_response))
                    _warn_loop_ended_with_pending_plan(
                        self,
                        plan_finalize_nudged=plan_finalize_nudged,
                        turn_used_request_user_input=turn_used_request_user_input,
                    )
                    break
                ai_response = _strip_leaked_internal_history_markers(ai_response)
                if not user_message_recorded:
                    user_message_recorded = True

                # Plan mode: capture the latest ``<proposed_plan>`` block (if any)
                # so the end-of-turn chooser can offer execute/modify and the
                # plan markdown is available for a clear-context implementation.
                if bool(getattr(self, "_plan_mode_sticky", False)):
                    try:
                        from ..core.proposed_plan import latest_proposed_plan

                        plan_md = latest_proposed_plan(ai_response)
                    except Exception:
                        plan_md = None
                    if plan_md:
                        turn_emitted_proposed_plan = True
                        try:
                            self._latest_proposed_plan_markdown = plan_md
                        except Exception:
                            pass

                visible_ai_response, pseudo_text_tool_plans, pseudo_tool_call_text = (
                    _split_trailing_pseudo_tool_calls_text_details(ai_response)
                    if task_uses_standard_openai_tools
                    else (ai_response, [], "")
                )
                if pseudo_text_tool_plans:
                    if not message_tool_plans:
                        message_tool_plans = pseudo_text_tool_plans
                    ai_response = visible_ai_response

                _update_latest_assistant_clean_content(self, ai_response)
                fallback_plans = list(message_tool_plans)
                if fallback_plans:
                    _raw_tool_plans = list(fallback_plans)
                    _seen = set()
                    _deduped = []
                    for _tn, _ta in _raw_tool_plans:
                        _key = f"{_tn}::{json.dumps(_ta, sort_keys=True, ensure_ascii=False) if isinstance(_ta, dict) else str(_ta)}"
                        if _key not in _seen:
                            _seen.add(_key)
                            _deduped.append((_tn, _ta))
                    if len(_deduped) != len(_raw_tool_plans):
                        get_logger(f"{get_app_logger_root()}.runtime.flow").debug(
                            "Deduplicated tool calls: original=%d -> kept=%d, plans=%s",
                            len(_raw_tool_plans),
                            len(_deduped),
                            json.dumps(
                                [{"tool": t, "args": a} for t, a in _raw_tool_plans],
                                ensure_ascii=False,
                            ),
                        )
                        fallback_plans = _deduped
                # Track which assistant message issued the current tool batch and
                # make sure its structured ``tool_calls`` field is populated even
                # when the model returned the call as raw JSON text. This keeps
                # tool results and ``_tool_rounds_raw`` correctly paired with the
                # issuing assistant message (instead of an earlier assistant that
                # also carried tool_calls, e.g. a prior ``request_skill_prompt``).
                _issuing = None
                _hist = getattr(self, "conversation_history", None) or []
                for _m in reversed(_hist):
                    if isinstance(_m, dict) and str(_m.get("role") or "").strip().lower() == "assistant":
                        _issuing = _m
                        break
                if _issuing is not None:
                    self._last_tool_issuing_assistant = _issuing
                    if fallback_plans and not _issuing.get("tool_calls"):
                        _rebuilt = _build_tool_calls_from_plans(_issuing, fallback_plans)
                        if _rebuilt:
                            _issuing["tool_calls"] = _rebuilt
                            try:
                                self._sync_active_chat_messages()
                            except Exception:
                                pass
                ai_response_looks_like_pseudo_tool = _looks_like_pseudo_tool_call_text(ai_response)
                if (
                    task_uses_standard_openai_tools
                    and not fallback_plans
                    and ai_response_looks_like_pseudo_tool
                ):
                    # The compatibility parser could not recover executable
                    # plans from the assistant text (e.g. the inner JSON was
                    # malformed by over-escaping). Strip the raw pseudo-tool-call
                    # JSON from the display text and save the cleaned copy.
                    cleaned_for_history = _stream_visible_text_with_json_pause(
                        ai_response, final=True
                    )
                    if cleaned_for_history and cleaned_for_history != ai_response:
                        ai_response = cleaned_for_history
                        _update_latest_assistant_clean_content(self, ai_response)
                    if pending_stream_history_reload:
                        _reload_chat_history_after_streamed_assistant_output(self)
                        pending_stream_history_reload = False
                    no_tool_rounds += 1
                    if no_tool_rounds >= max_no_tool_rounds:
                        print(
                            t(
                                "ERROR: The model repeatedly wrote tool calls as assistant text instead of standard tool_calls. Auto-execution has stopped for this round.",
                                "错误：模型反复将工具调用写成助手文本，而不是标准 tool_calls。本轮自动执行已停止。",
                            )
                        )
                        _warn_loop_ended_with_pending_plan(
                            self,
                            plan_finalize_nudged=plan_finalize_nudged,
                            turn_used_request_user_input=turn_used_request_user_input,
                        )
                        break

                    pseudo_retry_attempts += 1
                    next_input = _build_pseudo_tool_call_retry_prompt(
                        original_user_task=original_user_task,
                        attempt=pseudo_retry_attempts,
                    )
                    is_first_round = False
                    continue
                if pending_stream_history_reload:
                    _reload_chat_history_after_streamed_assistant_output(self)
                    pending_stream_history_reload = False
                if ai_response and not streamed_assistant_output and not ai_response_looks_like_pseudo_tool:
                    # When a model-call error trail was just printed via the
                    # ephemeral notice channel, the orchestrator returns a
                    # one-line summary purely as a fallback for non-UI
                    # callers. Suppress its terminal echo here so the user
                    # only sees the rich multi-attempt block.
                    suppress_assistant_render = False
                    try:
                        consume_flag = getattr(
                            self, "consume_ephemeral_notice_just_emitted", None
                        )
                        if callable(consume_flag) and consume_flag():
                            suppress_assistant_render = True
                    except Exception:
                        suppress_assistant_render = False
                    if not suppress_assistant_render:
                        try:
                            self._hide_previous_shell_output_if_needed()
                        except Exception:
                            pass
                        display_response = format_assistant_display_response(ai_response)
                        if display_response:
                            self._ensure_terminal_line_start()
                            rendered = self._format_assistant_chat_display_message(display_response)
                            sys.stdout.write(f"{rendered}\n")
                            sys.stdout.flush()
                            self._last_terminal_block_kind = "assistant"
                            self._terminal_cursor_at_line_start = True

                explore_ticker = None
                gui_prompt_printed_early: List[bool] = []
                if fallback_plans:
                    # Open a tool-execution round so the GUI shows
                    # "Working…" during long-running tools like
                    # run_subagent, instead of going dark after the
                    # model round ends.
                    _gui_round_mark(self, True)
                    explore_ticker = None
                    for tool_name, args in fallback_plans:
                        prompt_printed_early = False
                        if tool_name == "run_subagent" and str(args.get("subagent") or "").strip().lower() == "explore":
                            if bool(getattr(self, "_gui_plain_stream", False)):
                                explore_ticker = _NullStatusTicker()
                                self._print_tool_call_feedback(tool_name, args, failed=False)
                                prompt_printed_early = True
                            else:
                                ticker = _WorkingStatusTicker(
                                    sys.stdout,
                                    fps=_WORKING_STATUS_MARQUEE_FPS,
                                    language=getattr(self, "display_language", None),
                                )
                                def _explore_render(elapsed_seconds, frame, _t=ticker, _self=self, _args=args):
                                    lang = getattr(_self, "display_language", None)
                                    label_fn = getattr(_self, "_explore_running_label", None)
                                    if callable(label_fn):
                                        label = label_fn(_args if isinstance(_args, dict) else {})
                                    else:
                                        label = "Exploring..."
                                    line = _render_working_status_line(
                                        elapsed_seconds=elapsed_seconds, frame=frame,
                                        label=label, language=lang,
                                    )
                                    try:
                                        sys.stdout.write(f"\r\x1b[2K{line}")
                                        sys.stdout.flush()
                                    except Exception:
                                        pass
                                ticker._render_frame = _explore_render
                                ticker.start()
                                explore_ticker = ticker
                        else:
                            # In GUI streaming mode, print the prompt as soon as
                            # the tool starts so long-running tools show up
                            # immediately. The completion path appends only the
                            # output block to this same tool-execution round.
                            # apply_patch and request_skill_prompt are the
                            # exceptions: they emit their own prompt+payload
                            # block from their specialized paths.
                            _gui_stream = bool(getattr(self, "_gui_plain_stream", False))
                            _tool_defers_prompt = _gui_stream and tool_name in (
                                "apply_patch",
                                "request_skill_prompt",
                            )
                            if not _tool_defers_prompt:
                                self._print_tool_call_feedback(tool_name, args, failed=False)
                                prompt_printed_early = _gui_stream
                        gui_prompt_printed_early.append(prompt_printed_early)
                else:
                    tool_name, args = "", {}

                if ai_response and not fallback_plans and not streamed_assistant_output:
                    # Keep output spacing consistent when only narrative is shown.
                    if not ai_response.endswith("\n"):
                        sys.stdout.write("\n")
                    sys.stdout.flush()
                if not fallback_plans:
                    # No tool calls in the assistant message. Before we hand
                    # control back to the command prompt, give the model
                    # exactly one chance to flush a stale plan: if there is
                    # still an active plan with non-`completed` steps, ask
                    # the model to call `update_plan` once more. The nudge
                    # only fires when standard tool_calls are available so
                    # the model can actually invoke the tool, and at most
                    # once per turn to avoid loops with stubborn models.
                    # Skip the nudge entirely if the model used
                    # ``request_user_input`` earlier in this turn: that path is
                    # an explicit handoff to the user and the plan can
                    # legitimately stay open until the next user message
                    # is processed.
                    nudge_summary = _should_fire_plan_finalize_nudge(
                        self,
                        task_uses_standard_openai_tools=task_uses_standard_openai_tools,
                        plan_finalize_nudged=plan_finalize_nudged,
                        turn_used_request_user_input=turn_used_request_user_input,
                    )
                    if nudge_summary is not None:
                        plan_finalize_nudged = True
                        no_tool_rounds = 0
                        is_first_round = False
                        next_input = _build_plan_finalize_nudge_prompt(
                            original_user_task=original_user_task,
                            plan_summary=nudge_summary,
                        )
                        continue
                    # No tool calls in the assistant message: end the loop and
                    # return to the command prompt regardless of standard-tool
                    # mode. Visible content (when present) has already been
                    # rendered above.
                    _warn_loop_ended_with_pending_plan(
                        self,
                        plan_finalize_nudged=plan_finalize_nudged,
                        turn_used_request_user_input=turn_used_request_user_input,
                    )
                    _refresh_context_usage_after_task_boundary(
                        self,
                        user_input_hint=str(original_user_task or ""),
                        context_hint="assistant turn finished",
                    )
                    break

                executed_batch_results: List[Dict[str, Any]] = []
                last_tool_name = ""
                last_tool_args: Dict[str, Any] = {}
                last_tool_result: Dict[str, Any] = {}
                continue_after_batch = False
                break_after_batch = False

                for tool_index, (tool_name, args) in enumerate(fallback_plans):
                    if not tool_name:
                        print(t("runtime.tool_plan_missing_name"))
                        break_after_batch = True
                        break

                    if tool_name == "request_user_input":
                        # Remember that this turn paused for a clarifying
                        # question. The end-of-turn plan-finalization nudge
                        # below must not fire after the model used
                        # ``request_user_input``: the model is legitimately
                        # waiting on the user, and forcing one more round
                        # of ``update_plan`` here would either block the
                        # handoff (model has nothing more to do without
                        # the answer) or pressure the model to mark steps
                        # ``completed`` prematurely.
                        turn_used_request_user_input = True

                    if tool_name == "apply_patch":
                        patch_path = str(args.get("path") or "").strip() if isinstance(args, dict) else ""
                        patch_text = args.get("patch") if isinstance(args, dict) else None
                        if (not patch_path) or (not isinstance(patch_text, str)) or (not patch_text.strip()):
                            print(
                                t(
                                    "⚠️ apply_patch plan is missing required `path`/`patch`; requesting the model to resend a valid patch/git-apply unified diff call.",
                                    "⚠️ apply_patch 计划缺少必要的 `path`/`patch`；正在请求模型重新发送有效的 patch/git-apply unified diff 调用。",
                                )
                            )
                            next_input = (
                                "Your previous `apply_patch` tool plan was missing required arguments.\n"
                                "Retry with a valid standard API tool_calls entry for `apply_patch`; do not print JSON in visible text:\n"
                                "{\"tool\":\"apply_patch\",\"args\":{\"path\":\"<file>\",\"patch\":\"--- a/<file>\\n+++ b/<file>\\n@@ ... @@\\n- old\\n+ new\"}}\n"
                                "Required arguments: `path` and `patch`. Prefer a standard unified diff patch containing ---/+++ and at least one `@@ ... @@` hunk."
                            )
                            no_tool_rounds = 0
                            is_first_round = False
                            continue_after_batch = True
                            break

                    if tool_name == "request_skill_prompt":
                        sid = str(args.get("skill_id") or "").strip()
                        canon_sid = self._canonical_skill_id(sid)
                        active_sid = self._canonical_skill_id(self._active_skill_id or "")
                        requested_section_raw = args.get("section")
                        requested_section: Optional[int] = None
                        try:
                            if requested_section_raw is not None:
                                requested_section = int(requested_section_raw)
                        except Exception:
                            requested_section = None
                        force_full = bool(args.get("full", False))
                        request_is_expansion = force_full or (requested_section is not None and requested_section > 1)
                        # Decide whether the skill prompt must be (re-)injected into
                        # the model context. A normal first-time request injects it.
                        # A repeat of an already-active skill only re-injects when the
                        # post-compaction context lost the previous role:tool result
                        # (compaction-recovery), to avoid duplicate prompts.
                        inject_prompt = True
                        if canon_sid and canon_sid in preloaded_skill_ids and not request_is_expansion:
                            inject_prompt = False
                        if (
                            active_sid
                            and canon_sid
                            and active_sid == canon_sid
                            and str(self._active_skill_full_prompt or "").strip()
                            and not self._active_skill_chunked
                            and not request_is_expansion
                        ):
                            inject_prompt = self._skill_prompt_recovery_needed(canon_sid or sid)
                        if (
                            active_sid
                            and canon_sid
                            and active_sid == canon_sid
                            and self._active_skill_chunked
                            and requested_section is None
                            and not force_full
                            and self._active_skill_section > 0
                            and self._active_skill_section < self._active_skill_total_sections
                        ):
                            requested_section = self._active_skill_section + 1
                        if not inject_prompt:
                            next_input = (
                                f"skill_id=`{sid}` has already been injected in this session. "
                                "Do not call request_skill_prompt repeatedly. Continue directly with standard tools."
                            )
                            no_tool_rounds = 0
                            continue_after_batch = True
                            break
                        full_prompt, meta = self._build_single_skill_prompt(
                            sid,
                            requested_section=requested_section,
                            full=force_full,
                        )
                        if not full_prompt:
                            no_tool_rounds += 1
                            next_input = (
                                f"The requested skill_id=`{sid}` does not exist. "
                                "Retry based on the loaded skill index with a valid request_skill_prompt call, or continue directly with standard tools."
                            )
                            continue_after_batch = True
                            break
                        if active_sid != canon_sid and not bool(getattr(self, "_gui_plain_stream", False)):
                            print(t("runtime.skill_about_to_enable", skill=sid))
                        self._active_skill_full_prompt = full_prompt
                        self._active_skill_id = canon_sid or sid
                        self._active_skill_source = "local" if self._is_local_skill_id(canon_sid or sid) else "mcp"
                        self._active_skill_section = int(meta.get("section") or 0)
                        self._active_skill_total_sections = int(meta.get("total") or 0)
                        self._active_skill_chunked = bool(meta.get("chunked", False))
                        # Track in session-level set so future forced references
                        # don't re-inject the full body.
                        session_injected = getattr(self, "_session_injected_skills", set())
                        if canon_sid:
                            session_injected.add(canon_sid)
                        # Record the skill prompt as a real ``role: tool`` result
                        # paired with the model's ``request_skill_prompt`` tool call
                        # (matching ``tool_call_id``). This replaces the deprecated
                        # ``[MODEL_TOOL_RESULT]`` assistant message and the extra
                        # "injected above" user message.
                        skill_tool_call_id = "call_0"
                        _issuing = getattr(self, "_last_tool_issuing_assistant", None)
                        if isinstance(_issuing, dict):
                            for _tc in (_issuing.get("tool_calls") or []):
                                if isinstance(_tc, dict) and str(_tc.get("function", {}).get("name") or "").strip() == "request_skill_prompt":
                                    _cid = str(_tc.get("id") or "").strip()
                                    if _cid:
                                        skill_tool_call_id = _cid
                                    break
                        result_payload = {
                            "kind": "model_tool_result",
                            "tool": "request_skill_prompt",
                            "args": {"skill_id": sid},
                            "success": True,
                            "output": full_prompt,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        self.conversation_history.append({
                            "role": "tool",
                            "name": "request_skill_prompt",
                            "content": json.dumps(result_payload, ensure_ascii=False),
                            "tool_call_id": skill_tool_call_id,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        self._sync_active_chat_messages()
                        # Record a ``_tool_rounds_raw`` entry on the issuing
                        # assistant message (the ``request_skill_prompt`` tool
                        # call) so history reload can expand its output, exactly
                        # like every other tool call.
                        _skill_raw = {
                            "tool": "request_skill_prompt",
                            "args": {"skill_id": sid},
                            "failed": False,
                            "elapsed": None,
                            "output": full_prompt,
                        }
                        _issuing_msg = getattr(self, "_last_tool_issuing_assistant", None)
                        if not (isinstance(_issuing_msg, dict) and str(_issuing_msg.get("role") or "").strip().lower() == "assistant"):
                            for _m in reversed(getattr(self, "conversation_history", None) or []):
                                if isinstance(_m, dict) and str(_m.get("role") or "").strip().lower() == "assistant" and _m.get("tool_calls"):
                                    _issuing_msg = _m
                                    break
                        if isinstance(_issuing_msg, dict):
                            _issuing_msg.setdefault("_tool_rounds_raw", []).append(_skill_raw)
                            try:
                                self._sync_active_chat_messages()
                            except Exception:
                                pass
                        # In GUI streaming mode, print the request_skill_prompt
                        # tool round so the frontend renders it during live
                        # execution (not just on history reload). Without this,
                        # the frontend sees an empty round between round_start
                        # and round_end and skips it entirely.
                        _gui_stream = bool(getattr(self, "_gui_plain_stream", False))
                        if _gui_stream:
                            try:
                                _tool_round = self._format_tool_call_feedback_line(
                                    "request_skill_prompt",
                                    {"skill_id": sid},
                                    failed=False,
                                )
                                _tool_round = (
                                    f"{_tool_round}\n{GUI_CMD_OUTPUT_BEGIN}"
                                    f"{full_prompt}{GUI_CMD_OUTPUT_END}"
                                )
                                print(_tool_round)
                            except Exception:
                                pass
                        next_input = (
                            "Continue with standard tools when more tool work is needed; you may call one or more tools at once. "
                            "When no further tool action is required, reply in natural language with no tool_calls and the host will return to the command prompt."
                        )
                        no_tool_rounds = 0
                        continue_after_batch = True
                        break

                    pseudo_command = {"tool": tool_name, "args": args}
                    if self._is_repeated_tool_call_pattern(tool_name, args):
                        next_input = (
                            "Detected repeated calls to the same shell command with nearly identical arguments.\n"
                            "Stop repeating the search. Instead:\n"
                            "1) Provide an interim conclusion from existing results;\n"
                            "2) If evidence is insufficient, make only one more targeted tool call;\n"
                            "3) If evidence is sufficient, finish in the next assistant message with a natural-language reply only.\n"
                            "Continue with standard tools and avoid repetition; you may call one or more tools at once."
                        )
                        no_tool_rounds = 0
                        continue_after_batch = True
                        break
                    selected_skill = self._infer_selected_skill(pseudo_command, ai_response)
                    if selected_skill:
                        skill_key = f"{selected_skill.get('skill_id')}::{selected_skill.get('name')}"
                        if skill_key != last_announced_skill_key:
                            print(
                                t(
                                    "🧩 Use skill: {name} ({skill_id})",
                                    "🧩 使用 skill：{name}（{skill_id}）",
                                ).format(
                                    name=selected_skill.get("name"),
                                    skill_id=selected_skill.get("skill_id"),
                                )
                            )
                            last_announced_skill_key = skill_key

                    if self._consume_task_interrupt_requested():
                        raise KeyboardInterrupt
                    result = self.execute_tool_call(tool_name, args)
                    repaint_up_lines = 1
                    if tool_name == "shell":
                        try:
                            rendered_lines = int(result.get("display_rendered_lines", 0) or 0)
                        except Exception:
                            rendered_lines = 0
                        try:
                            interstitial_lines = int(
                                getattr(self, "_tool_call_feedback_interstitial_lines", 0) or 0
                            )
                        except Exception:
                            interstitial_lines = 0
                        repaint_up_lines = max(1, rendered_lines + max(0, interstitial_lines) + 1)
                    aborted_tool_result = _model_tool_result_was_aborted(tool_name, result)
                    self._repaint_tool_call_feedback_if_failed(
                        tool_name,
                        args,
                        failed=(not bool(result.get("success", True))) and (not aborted_tool_result),
                        up_lines=repaint_up_lines,
                    )
                    try:
                        self._tool_call_feedback_interstitial_lines = 0
                    except Exception:
                        pass
                    no_tool_rounds = 0
                    self.operation_results.append({
                        "command": pseudo_command,
                        "result": result,
                        "timestamp": datetime.now().isoformat()
                    })
                    executed_batch_results.append({
                        "tool": tool_name,
                        "args": args,
                        "result": result,
                    })
                    recorder = getattr(self, "_record_model_tool_execution_history", None)
                    if callable(recorder):
                        try:
                            recorder(tool_name, args, result if isinstance(result, dict) else {})
                        except Exception:
                            pass
                    # In GUI streaming mode, tools that deferred their prompt
                    # line now print the accumulated tool_round (prompt +
                    # output as a single SSE event). This guarantees the
                    # frontend receives a coherent CMD_PROMPT / CMD_OUTPUT
                    # pair that cannot be split across rounds by a race,
                    # and consecutive tool calls' prompts and outputs never
                    # interleave.
                    _gui_stream = bool(getattr(self, "_gui_plain_stream", False))
                    if _gui_stream:
                        _rounds = getattr(self, "_accumulated_tool_rounds", None) or []
                        if _rounds:
                            _last_round = _rounds[-1]
                            _prompt_printed_early = (
                                tool_index < len(gui_prompt_printed_early)
                                and bool(gui_prompt_printed_early[tool_index])
                            )
                            try:
                                if tool_name in ("run_subagent", "apply_patch"):
                                    # Sub-agent calls keep a separate transcript;
                                    # apply_patch already printed prompt+diff
                                    # via _emit_gui_diff_block in apply_patch.py.
                                    pass
                                elif _prompt_printed_early:
                                    _output_start = str(_last_round).find(GUI_CMD_OUTPUT_BEGIN)
                                    if _output_start >= 0:
                                        print(str(_last_round)[_output_start:])
                                else:
                                    print(_last_round)
                            except Exception:
                                pass
                    # Real-time context tracking: after each tool result is
                    # appended to history, refresh usage and auto-compact if
                    # the trigger threshold is exceeded.
                    try:
                        check_fn = getattr(
                            getattr(self, "session_memory_service", None),
                            "check_and_compact_if_needed",
                            None,
                        )
                        if callable(check_fn):
                            check_fn(
                                user_input_hint=str(original_user_task or ""),
                                context_hint=f"after tool: {tool_name}",
                            )
                    except Exception:
                        pass
                    if tool_name == "shell" and aborted_tool_result:
                        _reload_chat_history_after_aborted_command(self)
                    last_result = result
                    last_tool_name = tool_name
                    last_tool_args = args if isinstance(args, dict) else {}
                    last_tool_result = result if isinstance(result, dict) else {}
                    if not bool(getattr(self, "_gui_plain_stream", False)):
                        print("")
                    is_first_round = False
                    if tool_name == "apply_patch" and (not bool(result.get("success", False))):
                        err = str(result.get("error") or result.get("message") or "unknown error").strip()
                        print(t("runtime.apply_patch_failed", error=err))
                        print(t("🔎 apply_patch diagnostic hints:", "🔎 apply_patch 诊断提示："))
                        for hint in _build_apply_patch_failure_hints(
                            err,
                            args if isinstance(args, dict) else {},
                            t,
                        ):
                            print(f"  - {hint}")
                    if self._result_indicates_user_cancelled(result):
                        if explore_ticker is not None:
                            explore_ticker.stop()
                        self._force_current_input_as_requirement_once = True
                        self._last_cancelled_task = str(original_user_task or "").strip()
                        self._mark_cancelled_unanswered_user_message()
                        try:
                            self._record_conversation_interrupted_history(
                                interrupted_kind="task",
                                reason="user_cancelled",
                                detail=str(original_user_task or ""),
                            )
                        except Exception:
                            pass
                        _refresh_context_usage_after_task_boundary(
                            self,
                            user_input_hint=str(original_user_task or ""),
                            context_hint="task cancelled",
                        )
                        print(t("runtime.user_cancelled_task"))
                        break_after_batch = True
                        break

                    if bool(result.get("needs_user_input", False)) and str(result.get("input_type", "")).strip() == "supplement":
                        if (not worked_summary_emitted) and tool_name == "request_user_input":
                            _print_worked_for_summary_line(
                                self,
                                int(max(0.0, time.monotonic() - float(task_started_at))),
                            )
                            worked_summary_emitted = True
                        q = str(result.get("question") or "").strip() or t(
                            "runtime.request_user_input.default_question"
                        )
                        raw_options = result.get("options")
                        options_list = (
                            [str(o) for o in raw_options if str(o or "").strip()]
                            if isinstance(raw_options, list)
                            else []
                        )
                        multi_select_flag = bool(result.get("multi_select", False))
                        supplement_text, handoff_to_main_loop = _solicit_request_user_input_answer(
                            self, q, options_list, multi_select_flag
                        )
                        if handoff_to_main_loop:
                            _refresh_context_usage_after_task_boundary(
                                self,
                                user_input_hint=str(original_user_task or ""),
                                context_hint="request_user_input handoff",
                            )
                            break_after_batch = True
                            break
                        if not supplement_text:
                            _refresh_context_usage_after_task_boundary(
                                self,
                                user_input_hint=str(original_user_task or ""),
                                context_hint="request_user_input paused",
                            )
                            break_after_batch = True
                            break
                        # Record the user's clarifying answer as a left-side
                        # transcript bubble (TUI + GUI). It is a reply to the
                        # agent's question, not a user-initiated turn, so it
                        # sits on the left and is excluded from model context
                        # (the model already receives it via ``next_input``).
                        try:
                            recorder = getattr(
                                self, "_record_request_user_input_answer_history", None
                            )
                            if callable(recorder):
                                recorder(supplement_text)
                        except Exception:
                            pass
                        next_input = (
                            f"[User supplement]\n{supplement_text}\n\n"
                            "Continue handling the original request together with this supplement using standard tools; "
                            "you may call one or more tools at once. If information is still insufficient, call `request_user_input` again."
                        )
                        continue_after_batch = True
                        break
                    if (
                        (not result.get("success", True))
                        and bool(result.get("needs_user_input", False))
                        and (result.get("retryable", True) is False)
                    ):
                        hint = str(
                            result.get("error", "")
                            or t("runtime.auto_continue.default_hint")
                        )
                        print(t("runtime.auto_continue.paused", hint=hint))
                        _refresh_context_usage_after_task_boundary(
                            self,
                            user_input_hint=str(original_user_task or ""),
                            context_hint="task paused needs user input",
                        )
                        break_after_batch = True
                        break

                # Close the tool-execution round; the next model-call
                # iteration opens its own round.
                _gui_round_mark(self, False)

                if break_after_batch:
                    if explore_ticker is not None:
                        explore_ticker.stop()
                    _warn_loop_ended_with_pending_plan(
                        self,
                        plan_finalize_nudged=plan_finalize_nudged,
                        turn_used_request_user_input=turn_used_request_user_input,
                    )
                    break
                if continue_after_batch:
                    continue
                if not executed_batch_results:
                    print(t("runtime.no_executable_tool_call"))
                    _warn_loop_ended_with_pending_plan(
                        self,
                        plan_finalize_nudged=plan_finalize_nudged,
                        turn_used_request_user_input=turn_used_request_user_input,
                    )
                    break

                step_progress = self._build_step_progress_context()
                post_result_synthesis_rule = self._build_post_result_synthesis_rule(
                    tool_name=last_tool_name,
                    args=last_tool_args,
                    result=last_tool_result,
                )
                active_plan_summary = _summarize_active_plan(self)
                active_plan_block = (
                    f"{_format_active_plan_reminder(active_plan_summary)}\n\n"
                    if active_plan_summary
                    else ""
                )
                # Flush accumulated tool_rounds onto the preceding assistant message
                flusher = getattr(self, "_flush_tool_rounds", None)
                if callable(flusher):
                    flusher()
                # Re-print explore completion text, overwriting the "Exploring..." line
                if last_tool_name == "run_subagent" and str(last_tool_args.get("subagent") or "").strip().lower() == "explore":
                    if explore_ticker is not None:
                        explore_ticker.stop()
                    try:
                        _rerender = getattr(self, "_rerender_tool_rounds", None)
                        if callable(_rerender):
                            msgs = list(getattr(self, "conversation_history", None) or [])
                            for m in reversed(msgs):
                                if isinstance(m, dict) and m.get("_tool_rounds_raw"):
                                    rendered = _rerender(m["_tool_rounds_raw"])
                                    if rendered:
                                        if bool(getattr(self, "_gui_plain_stream", False)):
                                            print(str(rendered[0]).rstrip("\n"))
                                            break
                                        clean = rendered[0].split("\ue008")[0].rstrip("\n").replace("\ue004", "").replace("\ue005", "").replace("\ue002", "").replace("\ue003", "").replace("\ue000", "").replace("\ue001", "").replace("\ue006", "").replace("\ue007", "")
                                        if sys.stdout.isatty():
                                            sys.stdout.write("\033[1A\033[K")
                                            sys.stdout.flush()
                                        print(clean)
                                    break
                    except Exception:
                        pass
                next_input = (
                    "Continue with standard tools when more tool work is needed; you may call one or more tools at once. "
                    "When no further tool action is required, reply in natural language with no tool_calls and the host will return to the command prompt. "
                    "If the previous batch result already satisfies the original request, finish in the next assistant message with a natural-language reply only."
                    + (f"\n{post_result_synthesis_rule}" if post_result_synthesis_rule else "")
                )
            if max_tool_rounds is not None and tool_round >= max_tool_rounds:
                print(
                    t(
                        "⏹️ Reached the auto-execution limit for this round ({steps} steps). Task is paused. Ask again to continue, or narrow the task scope and retry.",
                        "⏹️ 已达到本轮自动执行上限（{steps} 步）。任务已暂停。请再次询问以继续，或缩小任务范围后重试。",
                    ).format(steps=max_tool_rounds)
                )
            in_task_execution = False
            self._in_task_execution = False
            # Broadcast file changes summary at task boundary
            try:
                from ..core.logging.app_logging import get_logger
                _fc_logger = get_logger("codewood.file_change")
                tracker = getattr(self, "file_change_tracker", None)
                if tracker is not None:
                    changes = tracker.get_changes()
                    _fc_logger.debug(f"[file_changes] task boundary: {len(changes)} changes recorded")
                    # Determine turn index for per-turn file-change association.
                    # Each task boundary marks the end of one turn; we count
                    # completed tasks per chat to assign stable turn indices.
                    # When the chat ID is unavailable, fall back to a global
                    # counter so turn indices still increase monotonically.
                    _cs = getattr(self, "_chat_state", None)
                    _chat_id_for_turn = str(_cs.get("active", "")) if isinstance(_cs, dict) else ""
                    if _chat_id_for_turn:
                        _task_turn_counts = getattr(self, "_task_turn_counts", {})
                        turn_index = _task_turn_counts.get(_chat_id_for_turn, 0)
                        _task_turn_counts[_chat_id_for_turn] = turn_index + 1
                        setattr(self, "_task_turn_counts", _task_turn_counts)
                    else:
                        _task_turn_counts = getattr(self, "_task_turn_counts", {})
                        turn_index = _task_turn_counts.get("__global__", 0)
                        _task_turn_counts["__global__"] = turn_index + 1
                        setattr(self, "_task_turn_counts", _task_turn_counts)
                    if changes:
                        summary = tracker.get_summary()
                        summary["turnIndex"] = turn_index
                        gui_handler = getattr(self, "_gui_file_changes", None)
                        _fc_logger.debug(f"[file_changes] gui_handler callable={callable(gui_handler)}, chat={_chat_id_for_turn or '?'}, turnIndex={turn_index}")
                        if callable(gui_handler):
                            gui_handler(summary)
                        tracker.clear()
                else:
                    _fc_logger.debug("[file_changes] tracker is None at task boundary")
            except Exception as _e:
                import traceback as _tb
                try:
                    from ..core.logging.app_logging import get_logger
                    get_logger("codewood.file_change").debug(f"[file_changes] ERROR: {_e}\n{_tb.format_exc()}")
                except Exception:
                    pass
                pass
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            if active_status_ticker is not None:
                try:
                    active_status_ticker.stop()
                except Exception:
                    pass
                active_status_ticker = None
                self._clear_last_thinking_line()
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            # Plan mode: once the agent has drafted a plan, offer an
            # interactive "execute now / modify plan" choice (mirrors the
            # GUI's Execute-now affordance). Choosing execute switches to
            # Agent mode and queues a proceed message; choosing modify (or
            # typing notes in the inline input) queues that text while
            # staying in Plan mode. Skipped when ``auto_exit_after_turn`` is
            # set (one-shot ``--exec`` runs must not block on a prompt).
            if not auto_exit_after_turn:
                try:
                    plan_followup = _maybe_offer_plan_execution_choice(
                        self,
                        turn_used_request_user_input=turn_used_request_user_input,
                        plan_ready=turn_emitted_proposed_plan,
                    )
                except Exception:
                    plan_followup = None
                if plan_followup:
                    self._queued_user_input = plan_followup
                    continue
            if auto_exit_after_turn:
                break

        except KeyboardInterrupt:
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            if active_status_ticker is not None:
                try:
                    active_status_ticker.stop()
                except Exception:
                    pass
                active_status_ticker = None
                self._clear_last_thinking_line()
            if explore_ticker is not None:
                try:
                    explore_ticker.stop()
                except Exception:
                    pass
                explore_ticker = None
            if in_task_execution:
                in_task_execution = False
                self._in_task_execution = False
                self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
                self._consume_task_interrupt_requested()
                pending_user_task = str(original_user_task or "").strip()
                if not pending_user_task:
                    try:
                        pending_user_task = str(locals().get("task_user_input", "") or "").strip()
                    except Exception:
                        pending_user_task = ""
                if not pending_user_task:
                    try:
                        pending_user_task = str(locals().get("raw_user_input", "") or "").strip()
                    except Exception:
                        pending_user_task = ""
                user_message_recorded = _try_record_user_task_message(
                    self, pending_user_task, already_recorded=user_message_recorded
                )
                self._force_current_input_as_requirement_once = True
                self._mark_cancelled_unanswered_user_message()
                _refresh_context_usage_after_task_boundary(
                    self,
                    user_input_hint=str(pending_user_task or ""),
                    context_hint="task cancelled by interrupt",
                )
                try:
                    self._last_cancelled_task = str(original_user_task or "").strip()
                except Exception:
                    self._last_cancelled_task = str(getattr(self, "_last_cancelled_task", "") or "")
                try:
                    self._record_conversation_interrupted_history(
                        interrupted_kind="task",
                        reason="user_interrupt",
                        detail=str(self._last_cancelled_task or ""),
                    )
                except Exception:
                    pass
                self._active_skill_full_prompt = ""
                self._active_skill_id = None
                self._active_skill_source = None
                self._active_skill_section = 0
                self._active_skill_total_sections = 0
                self._active_skill_chunked = False
                self._last_auto_removed_ephemeral = None
                if not self._consume_conversation_interrupted_banner_recent():
                    self._print_conversation_interrupted_banner()
                continue

            self._in_task_execution = False
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            if self._consume_task_interrupt_requested():
                self._record_conversation_interrupted_history(
                    interrupted_kind="task",
                    reason="user_interrupt",
                )
                if not self._consume_conversation_interrupted_banner_recent():
                    self._print_conversation_interrupted_banner()
                continue
            print("")
            try:
                should_exit = input(
                    t("runtime.exit_prompt", app_name=get_app_name())
                ).strip().lower() == "y"
            except KeyboardInterrupt:
                should_exit = False

            if should_exit:
                print(t("runtime.exit_goodbye", app_name=get_app_name()))
                break
            continue
        except Exception as e:
            if pre_task_status_ticker is not None:
                pre_task_status_ticker.stop()
                pre_task_status_ticker = None
                self._clear_last_thinking_line()
            if active_status_ticker is not None:
                try:
                    active_status_ticker.stop()
                except Exception:
                    pass
                active_status_ticker = None
                self._clear_last_thinking_line()
            if explore_ticker is not None:
                try:
                    explore_ticker.stop()
                except Exception:
                    pass
                explore_ticker = None
            self._in_task_execution = False
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            print(t("runtime.error_occurred", error=str(e)))
