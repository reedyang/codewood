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
from ..core.assistant_output_highlighter import (
    format_assistant_display_response,
)
from ..core.logging.app_logging import get_logger
from ..controllers.builtin_command_router import dispatch_builtin_command
from ..tooling.handlers.mcp_handlers import MCP_MANAGEMENT_GATED_TOOLS
from ..tooling.handlers.memory_handlers import MEMORY_TOOLS
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
    _ansi_yellow,
)

_WORKING_STATUS_MARQUEE_FPS = 10.0
_STREAM_ATTR_TERMINAL_COLUMNS = get_app_runtime_attr_name("terminal_columns")
_STREAM_ATTR_OUTPUT_INDENT_WIDTH = get_app_runtime_attr_name("output_indent_width")
_MODEL_TOOL_RESULT_HISTORY_PREFIX = "[MODEL_TOOL_RESULT]"


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
    if re.search(r"""(?is)```[^\n]*\n.*(["']?\b(?:tool|tool_calls|args|arguments)\b["']?)\s*[:=]""", text):
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
            "call `ask_more_info` (the host will pause for the user's reply); "
            "do not mark pending steps as `completed` just to end the turn."
        )
    else:
        lines.append(
            "All plan steps are `completed`. You may now finish with a natural-"
            "language reply and no further tool_calls."
        )
    return "\n".join(lines)


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

    toolish_key_pattern = r"""(?is)(["']?\b(?:tool|tool_calls|args|arguments)\b["']?)\s*[:=]"""
    for m in re.finditer(r"(?im)^[ \t]*```[^\n]*\n", s):
        fence_header = m.group(0)
        block = s[m.start() :]
        fence_body = block[len(fence_header) :]
        fence_closed = bool(re.search(r"(?m)^```", fence_body))
        jsonish_fence = bool(re.search(r"(?i)```\s*(?:json|javascript|js)?\s*\n", fence_header))
        jsonish_body = bool(re.match(r"\s*(?:\{|\[)", fence_body))
        if re.search(toolish_key_pattern, block):
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


def _consume_streaming_ai_response(
    agent: Any,
    ai_result: Any,
    before_first_visible_output: Optional[Callable[[], None]] = None,
) -> Tuple[Optional[str], bool]:
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
    streamed_any = False
    first_visible_output_ready = False
    last_rendered_block = ""
    last_rendered_lines = 0
    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
    can_format_render = callable(getattr(agent, "_format_assistant_chat_display_message", None))
    append_stream_builder = getattr(agent, "_build_internal_slash_output_stream", None)
    can_append_stream = bool(is_tty and callable(append_stream_builder))
    append_stream = None

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
        try:
            if term_cols and term_cols > 0:
                append_stream = builder(sys.stdout, terminal_columns=term_cols)
            else:
                append_stream = builder(sys.stdout)
        except TypeError:
            try:
                append_stream = builder(sys.stdout)
            except Exception:
                append_stream = None
        except Exception:
            append_stream = None
        if append_stream is None:
            return None
        try:
            sys.stdout.write(f"{_ansi_gray('•')} ")
            sys.stdout.flush()
        except Exception:
            try:
                sys.stdout.write("• ")
                sys.stdout.flush()
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
                delta = visible_now[len(shown_visible) :] if visible_now.startswith(shown_visible) else visible_now
                if delta:
                    target_stream = _ensure_append_stream()
                    if target_stream is not None:
                        try:
                            target_stream.write(delta)
                            target_stream.flush()
                            streamed_any = True
                        except Exception:
                            sys.stdout.write(delta)
                            sys.stdout.flush()
                            streamed_any = True
                    else:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        streamed_any = True
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
            tail = (
                visible_final[len(shown_visible) :]
                if visible_final.startswith(shown_visible)
                else visible_final
            )
            if tail:
                target_stream = _ensure_append_stream()
                if target_stream is not None:
                    try:
                        target_stream.write(tail)
                        target_stream.flush()
                        streamed_any = True
                    except Exception:
                        sys.stdout.write(tail)
                        sys.stdout.flush()
                        streamed_any = True
                else:
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                    streamed_any = True
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
    if streamed_any:
        if not shown_visible.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
        try:
            agent._last_terminal_block_kind = "assistant"
            agent._terminal_cursor_at_line_start = True
        except Exception:
            pass
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


def _model_tool_result_was_aborted(tool_name: str, result: Any) -> bool:
    if str(tool_name or "").strip() != "shell":
        return False
    if not isinstance(result, dict):
        return False
    if bool(result.get("aborted_by_user", False)):
        return True
    return "command aborted by user" in str(result.get("output") or "").lower()


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
    (turn finished/cancelled/ask_more_info pause), so the status bar
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
            return True
    except Exception:
        return False
    return False


def _print_startup_overview(agent: Any) -> None:
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
                user_input = self._get_user_input_with_history()
            user_input = _sanitize_prompt_pollution(user_input, self.work_directory)
            raw_user_input = str(user_input or "")
        
            # Save non-empty input to history.
            if user_input.strip():
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
            if stripped_in.startswith("/") and not forced_skills and not forced_mcp_entries:
                builtin_line = stripped_in[1:].lstrip()
                if not builtin_line:
                    print(
                        t(
                            "ℹ️ Built-in commands must start with /. For example: /exit, /help, /clear screen, /memory status; a standalone / is invalid. For local commands/scripts executed without AI, use ! prefix, e.g. !ls, !git status.",
                            "ℹ️ 内置命令必须以 / 开头。例如：/exit、/help、/clear screen、/memory status；单独的 / 无效。对于无需 AI 的本地命令/脚本，请使用 ! 前缀，例如 !ls、!git status。",
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
                            mcp_res = self.execute_tool_call(mcp_tool, mcp_args)
                            self._print_mcp_shortcut_result(mcp_tool, mcp_args, mcp_res if isinstance(mcp_res, dict) else {})
                            continue
                        if bl == "mcp" or bl.startswith("mcp "):
                            print(f"❌ {mcp_err}")
                            continue
                        if bl in ('exit', 'quit'):
                            self._save_current_workspace_position()
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
                            self.execute_tool_call("memory_stats", {"verbose_print": True})
                            continue
                        if bl == "memory list":
                            self.execute_tool_call(
                                "memory_list", {"limit": 20, "verbose_print": True}
                            )
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
                            self.execute_tool_call("always_confirm_reset", {})
                            continue

                        if bl == 'help':

                            self._print_main_help()

                            continue

                        print(
                            t(
                                "❌ Unrecognized built-in command. Use /help to view the list. For direct local shell/script execution, use ! prefix, e.g. !git status, !dir.",
                                "❌ 无法识别的内置命令。请使用 /help 查看列表。对于直接执行本地 shell/脚本命令，请使用 ! 前缀，例如 !git status、!dir。",
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
            if stripped_in.startswith("!"):
                run_direct_shell = stripped_in[1:].lstrip()
                if not run_direct_shell:
                    print(
                        t(
                            "ℹ️ System commands/executables executed directly (without AI) must start with !, for example !ls, !dir, !ping 127.0.0.1, !git status; a standalone ! is invalid.",
                            "ℹ️ 直接执行的系统命令/可执行文件（不经过 AI）必须以 ! 开头，例如 !ls、!dir、!ping 127.0.0.1、!git status；单独的 ! 无效。",
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
            original_user_task = task_user_input
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
            pre_task_status_ticker = _WorkingStatusTicker(
                sys.stdout,
                fps=_WORKING_STATUS_MARQUEE_FPS,
                language=getattr(self, "display_language", None),
            )
            pre_task_status_ticker.start()

            last_result = None
            self._last_auto_removed_ephemeral = None
            user_message_recorded = _try_record_user_task_message(
                self, original_user_task, already_recorded=user_message_recorded
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
                for e in forced_mcp_entries:
                    srv = str(e.get("server", "")).strip()
                    name = str(e.get("name", "")).strip()
                    kind = str(e.get("kind", "")).strip() or "unknown"
                    print(t("runtime.mcp_reference_enabled", server=srv, name=name, kind=kind))
                forced_mcp_prefix = self._build_forced_mcp_prefix(forced_mcp_entries)
            preloaded_skill_ids: Set[str] = set()
            if forced_skills:
                skill_items = []
                full_prompts: List[str] = []
                for s in forced_skills:
                    sid = str(s.get("skill_id") or "").strip()
                    sname = str(s.get("name") or sid).strip()
                    if not sid:
                        continue
                    skill_items.append(f"`{sname}`(skill_id=`{sid}`)")
                    full_prompt, meta = self._build_single_skill_prompt(sid)
                    if full_prompt:
                        pre_task_status_ticker = _stop_pre_task_status_ticker_for_console_output(
                            self,
                            pre_task_status_ticker,
                        )
                        print(t("runtime.skill_enabled", skill=sname))
                        full_prompts.append(full_prompt)
                        preloaded_skill_ids.add(self._canonical_skill_id(sid))
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
                    self._active_skill_full_prompt = "\n".join(full_prompts)
            mcp_tool_selection_constraint = ""
            if bool(getattr(self, "mcp_tools_enabled", False)):
                mcp_tool_selection_constraint = (
                    "[Additional MCP tool selection constraints]\n"
                    "- If the user asks for information/details about one specific MCP server, the first query tool must be `mcp_server_info`.\n"
                    "- `mcp_status` / `mcp_status_refresh` are only for global MCP status overview and must not replace details for a specific server.\n\n"
                )
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
            first_round_contract = (
                "\n\n[First-turn hard requirements]\n"
                + numbered_rules
                + "\n"
                + mcp_tool_selection_constraint
                + memory_rules_block
            )
            if not task_uses_standard_openai_tools:
                first_round_contract = (
                    "\n\n[Basic chat mode]\n"
                    "The current model context window is under 64k. This turn must not use standard API tool_calls and must not simulate, write, or serialize any tool call in visible text.\n"
                    "Answer only in natural language. If the task requires reading files, running commands, loading skills, calling MCP, or other tool capabilities, explain that this small-context model supports only basic chat and suggest switching to a 64k+ context model before executing it.\n"
                )
            project_context_task = (
                task_uses_standard_openai_tools
                and self._project_context_feature_enabled()
                and _should_prioritize_project_context_for_task(original_user_task)
            )
            project_context_contract = ""
            if project_context_task:
                project_context_contract = (
                    "\n\n[Project context retrieval policy]\n"
                    "- This is a software development task. Use `project_context_search` as the first retrieval step before shell search or file reads.\n"
                    "- If the index is empty or stale, refresh it once and retry the search before falling back to broader search.\n"
                )
            first_round_contract = first_round_contract + project_context_contract

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
                                try:
                                    refresh_result = idx.refresh_index(force=False, timeout_ms=2000)
                                    project_context_refreshed_now = bool(refresh_result.get("success", False))
                                except Exception as e:
                                    refresh_result = {"success": False, "error": f"{type(e).__name__}: {e}"}
                                files_map = getattr(idx, "files", None)
                                project_context_files_total = (
                                    len(files_map) if isinstance(files_map, dict) else 0
                                )
                                if project_context_files_total <= 0:
                                    project_context_skip_reason = (
                                        "refresh_failed"
                                        if not bool(refresh_result.get("success", False))
                                        else "index_empty_after_refresh"
                                    )
                                else:
                                    project_context_ready = True
                                    project_context_skip_reason = "ready_after_refresh"
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
                f"{first_round_contract}"
                f"{(chr(10) + chr(10) + first_round_evidence) if first_round_evidence else ''}"
            )
            ready_to_send_elapsed_ms = int((time.perf_counter() - turn_send_started_at) * 1000)
            _emit_flow_log(f"First-round request preparation complete; sending actual request: elapsed_ms={ready_to_send_elapsed_ms}")
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
            tool_round = 0
            plan_finalize_nudged = False
            turn_used_ask_more_info = False
            while max_tool_rounds is None or tool_round < max_tool_rounds:
                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                tool_round += 1
                status_ticker = pre_task_status_ticker
                pre_task_status_ticker = None
                if status_ticker is None:
                    status_ticker = _WorkingStatusTicker(
                        sys.stdout,
                        fps=_WORKING_STATUS_MARQUEE_FPS,
                        language=getattr(self, "display_language", None),
                    )
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
                try:
                    standard_tool_schemas = []
                    if task_uses_standard_openai_tools:
                        standard_tool_schemas = list(getattr(self, "tool_specs", []) or [])
                        if not bool(getattr(self, "mcp_tools_enabled", False)):
                            standard_tool_schemas = [
                                item
                                for item in standard_tool_schemas
                                if str(
                                    ((item or {}).get("function", {}) or {}).get("name", "")
                                ).strip()
                                not in MCP_MANAGEMENT_GATED_TOOLS
                            ]
                        if not bool(getattr(self, "memory_enabled", True)):
                            standard_tool_schemas = [
                                item
                                for item in standard_tool_schemas
                                if str(
                                    ((item or {}).get("function", {}) or {}).get("name", "")
                                ).strip()
                                not in MEMORY_TOOLS
                            ]
                    ai_result = self.call_ai(
                        next_input,
                        context=json.dumps(last_result, ensure_ascii=False) if last_result else "",
                        stream=None,
                        return_message=task_uses_standard_openai_tools,
                        history_user_input=original_user_task if not user_message_recorded else None,
                        history_skip_user=user_message_recorded,
                        tool_schemas=standard_tool_schemas,
                        tool_choice="auto" if task_uses_standard_openai_tools else None,
                    )
                except Exception:
                    _stop_status_ticker_before_first_output()
                    raise
                if self._consume_task_interrupt_requested():
                    raise KeyboardInterrupt
                message_tool_plans: List[Tuple[str, Dict[str, Any]]] = []
                if isinstance(ai_result, dict):
                    if not status_ticker_stopped:
                        _stop_status_ticker_before_first_output()
                    msg_content = ai_result.get("content", "")
                    ai_response = msg_content if isinstance(msg_content, str) else str(msg_content or "")
                    streamed_assistant_output = False
                    message_tool_plans = (
                        _parse_tool_plans_from_model_message(ai_result)
                        if task_uses_standard_openai_tools
                        else []
                    )
                else:
                    ai_response, streamed_assistant_output = _consume_streaming_ai_response(
                        self,
                        ai_result,
                        before_first_visible_output=_stop_status_ticker_before_first_output,
                    )
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
                if not status_ticker_stopped:
                    _stop_status_ticker_before_first_output()
                if not isinstance(ai_response, str):
                    print(t("runtime.ai_invalid_response", value=ai_response))
                    break
                cleaned_internal_ai_response = _strip_leaked_internal_history_markers(ai_response)
                if cleaned_internal_ai_response != ai_response:
                    _replace_latest_assistant_history_content(
                        self,
                        ai_response,
                        cleaned_internal_ai_response,
                    )
                    ai_response = cleaned_internal_ai_response
                if not user_message_recorded:
                    user_message_recorded = True

                visible_ai_response, pseudo_text_tool_plans, pseudo_tool_call_text = (
                    _split_trailing_pseudo_tool_calls_text_details(ai_response)
                    if task_uses_standard_openai_tools
                    else (ai_response, [], "")
                )
                if pseudo_text_tool_plans:
                    if not message_tool_plans:
                        message_tool_plans = pseudo_text_tool_plans
                    _replace_latest_assistant_history_content(
                        self,
                        ai_response,
                        visible_ai_response,
                        pseudo_tool_call_text=pseudo_tool_call_text,
                        pseudo_tool_call_tools=[
                            tool_name
                            for tool_name, _args in pseudo_text_tool_plans
                        ],
                    )
                    ai_response = visible_ai_response

                fallback_plans = list(message_tool_plans)
                ai_response_looks_like_pseudo_tool = _looks_like_pseudo_tool_call_text(ai_response)
                if (
                    task_uses_standard_openai_tools
                    and not fallback_plans
                    and ai_response_looks_like_pseudo_tool
                ):
                    # The compatibility parser could not recover executable
                    # plans from the assistant text (e.g. the inner JSON was
                    # malformed by over-escaping). The streaming sanitizer
                    # already withheld the JSON envelope from the live
                    # terminal, but the persisted ``content`` still carries
                    # the raw pseudo-tool-call JSON. Run the same final-mode
                    # cutter over the recorded content and rewrite history
                    # so ``/chat reload`` shows only the natural-language
                    # prose, matching what the user saw live.
                    cleaned_for_history = _stream_visible_text_with_json_pause(
                        ai_response, final=True
                    )
                    if cleaned_for_history and cleaned_for_history != ai_response:
                        pseudo_tail = ai_response[len(cleaned_for_history):].strip()
                        _replace_latest_assistant_history_content(
                            self,
                            ai_response,
                            cleaned_for_history,
                            pseudo_tool_call_text=pseudo_tail,
                        )
                        ai_response = cleaned_for_history
                    no_tool_rounds += 1
                    if no_tool_rounds >= max_no_tool_rounds:
                        print(
                            t(
                                "ERROR: The model repeatedly wrote tool calls as assistant text instead of standard tool_calls. Auto-execution has stopped for this round.",
                                "错误：模型反复将工具调用写成助手文本，而不是标准 tool_calls。本轮自动执行已停止。",
                            )
                        )
                        break

                    next_input = (
                        f"[Original user request]\n{original_user_task}\n\n"
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
                    is_first_round = False
                    continue
                if ai_response and not streamed_assistant_output and not ai_response_looks_like_pseudo_tool:
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

                if fallback_plans:
                    for tool_name, args in fallback_plans:
                        self._print_tool_call_feedback(tool_name, args, failed=False)
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
                    # ``ask_more_info`` earlier in this turn: that path is
                    # an explicit handoff to the user and the plan can
                    # legitimately stay open until the next user message
                    # is processed.
                    if (
                        task_uses_standard_openai_tools
                        and not plan_finalize_nudged
                        and not turn_used_ask_more_info
                    ):
                        plan_summary_for_nudge = _summarize_active_plan(self)
                        if plan_summary_for_nudge and plan_summary_for_nudge.get("has_pending"):
                            plan_finalize_nudged = True
                            no_tool_rounds = 0
                            is_first_round = False
                            plan_block = _format_active_plan_reminder(plan_summary_for_nudge)
                            next_input = (
                                f"[Original user request]\n{original_user_task}\n\n"
                                f"{plan_block}\n\n"
                                "Before finishing, reconcile the active plan with the work that "
                                "has actually been done in this turn. Call `update_plan` to mark "
                                "every step that is finished as `completed` (and remove or "
                                "rewrite steps that no longer apply, providing an `explanation`). "
                                "After the plan is up to date, reply with a short natural-"
                                "language summary and no further tool_calls; the host will then "
                                "return to the command prompt."
                            )
                            continue
                    # No tool calls in the assistant message: end the loop and
                    # return to the command prompt regardless of standard-tool
                    # mode. Visible content (when present) has already been
                    # rendered above.
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

                for tool_name, args in fallback_plans:
                    if not tool_name:
                        print(t("runtime.tool_plan_missing_name"))
                        break_after_batch = True
                        break

                    if tool_name == "ask_more_info":
                        # Remember that this turn paused for a clarifying
                        # question. The end-of-turn plan-finalization nudge
                        # below must not fire after the model used
                        # ``ask_more_info``: the model is legitimately
                        # waiting on the user, and forcing one more round
                        # of ``update_plan`` here would either block the
                        # handoff (model has nothing more to do without
                        # the answer) or pressure the model to mark steps
                        # ``completed`` prematurely.
                        turn_used_ask_more_info = True

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
                                f"[Original user request]\n{original_user_task}\n\n"
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
                        if canon_sid and canon_sid in preloaded_skill_ids and not request_is_expansion:
                            next_input = (
                                f"[Original user request]\n{original_user_task}\n\n"
                                f"skill_id=`{sid}` was explicitly pre-injected this turn through `/skills/<skill-name>`. "
                                "Do not call request_skill_prompt again. Continue directly with standard tools."
                            )
                            no_tool_rounds = 0
                            continue_after_batch = True
                            break
                        if (
                            active_sid
                            and canon_sid
                            and active_sid == canon_sid
                            and str(self._active_skill_full_prompt or "").strip()
                            and not self._active_skill_chunked
                            and not request_is_expansion
                        ):
                            next_input = (
                                f"[Original user request]\n{original_user_task}\n\n"
                                f"skill_id=`{sid}` has already been injected in this session. "
                                "Do not call request_skill_prompt repeatedly. Continue directly with standard tools."
                            )
                            no_tool_rounds = 0
                            continue_after_batch = True
                            break
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
                        full_prompt, meta = self._build_single_skill_prompt(
                            sid,
                            requested_section=requested_section,
                            full=force_full,
                        )
                        if not full_prompt:
                            no_tool_rounds += 1
                            next_input = (
                                f"[Original user request]\n{original_user_task}\n\n"
                                f"The requested skill_id=`{sid}` does not exist. "
                                "Retry based on the loaded skill index with a valid request_skill_prompt call, or continue directly with standard tools."
                            )
                            continue_after_batch = True
                            break
                        if active_sid != canon_sid:
                            print(t("runtime.skill_about_to_enable", skill=sid))
                        self._active_skill_full_prompt = full_prompt
                        self._active_skill_id = canon_sid or sid
                        self._active_skill_source = "local" if self._is_local_skill_id(canon_sid or sid) else "mcp"
                        self._active_skill_section = int(meta.get("section") or 0)
                        self._active_skill_total_sections = int(meta.get("total") or 0)
                        self._active_skill_chunked = bool(meta.get("chunked", False))
                        next_input = (
                            f"[Original user request]\n{original_user_task}\n\n"
                            f"Injected the skill prompt for skill_id=`{sid}` . "
                            f"Current section progress: {self._active_skill_section}/{self._active_skill_total_sections if self._active_skill_total_sections else 1}。"
                            "Continue with standard tools; you may call one or more tools at once."
                        )
                        no_tool_rounds = 0
                        continue_after_batch = True
                        break

                    pseudo_command = {"tool": tool_name, "args": args}
                    if self._is_repeated_tool_call_pattern(tool_name, args):
                        next_input = (
                            f"[Original user request]\n{original_user_task}\n\n"
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
                    if tool_name == "shell" and aborted_tool_result:
                        _reload_chat_history_after_aborted_command(self)
                    last_result = result
                    last_tool_name = tool_name
                    last_tool_args = args if isinstance(args, dict) else {}
                    last_tool_result = result if isinstance(result, dict) else {}
                    is_first_round = False
                    if tool_name == "apply_patch" and (not bool(result.get("success", False))):
                        err = str(result.get("error") or result.get("message") or "unknown error").strip()
                        print(t("runtime.apply_patch_failed", error=err))
                    if self._result_indicates_user_cancelled(result):
                        self._force_current_input_as_requirement_once = True
                        self._last_cancelled_task = str(original_user_task or "").strip()
                        self._mark_cancelled_unanswered_user_message()
                        _refresh_context_usage_after_task_boundary(
                            self,
                            user_input_hint=str(original_user_task or ""),
                            context_hint="task cancelled",
                        )
                        print(t("runtime.user_cancelled_task"))
                        break_after_batch = True
                        break

                    if bool(result.get("needs_user_input", False)) and str(result.get("input_type", "")).strip() == "supplement":
                        if (not worked_summary_emitted) and tool_name == "ask_more_info":
                            _print_worked_for_summary_line(
                                self,
                                int(max(0.0, time.monotonic() - float(task_started_at))),
                            )
                            worked_summary_emitted = True
                        q = str(result.get("question") or "").strip() or t(
                            "runtime.ask_more_info.default_question"
                        )
                        print(t("runtime.ask_more_info.required"))
                        print(t("runtime.ask_more_info.question", question=q))
                        supplement_text = ""
                        handoff_to_main_loop = False
                        while True:
                            try:
                                supplement_text = self._get_user_input_with_history().strip()
                            except KeyboardInterrupt:
                                print(t("runtime.ask_more_info.supplement_cancelled"))
                                supplement_text = ""
                                break
                            if not supplement_text:
                                print(t("runtime.ask_more_info.no_supplement"))
                                break
                            if supplement_text.startswith("/") or supplement_text.startswith("!"):
                                # Route prefixed input back to the main loop so it shares
                                # the exact same parsing/execution path as a normal turn.
                                self._queued_user_input = supplement_text
                                handoff_to_main_loop = True
                                break
                            break
                        if handoff_to_main_loop:
                            _refresh_context_usage_after_task_boundary(
                                self,
                                user_input_hint=str(original_user_task or ""),
                                context_hint="ask_more_info handoff",
                            )
                            break_after_batch = True
                            break
                        if not supplement_text:
                            _refresh_context_usage_after_task_boundary(
                                self,
                                user_input_hint=str(original_user_task or ""),
                                context_hint="ask_more_info paused",
                            )
                            break_after_batch = True
                            break
                        next_input = (
                            f"[Original user request]\n{original_user_task}\n\n"
                            f"[User supplement]\n{supplement_text}\n\n"
                            "Continue handling the original request together with this supplement using standard tools; "
                            "you may call one or more tools at once. If information is still insufficient, call `ask_more_info` again."
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

                if break_after_batch:
                    break
                if continue_after_batch:
                    continue
                if not executed_batch_results:
                    print(t("runtime.no_executable_tool_call"))
                    break

                result_for_next_input: Dict[str, Any]
                if len(executed_batch_results) == 1:
                    result_for_next_input = last_tool_result
                else:
                    batch_success = True
                    for entry in executed_batch_results:
                        entry_result = entry.get("result")
                        if isinstance(entry_result, dict) and (entry_result.get("success", True) is False):
                            batch_success = False
                            break
                    result_for_next_input = {
                        "success": batch_success,
                        "batch_results": executed_batch_results,
                    }
                last_result = result_for_next_input
                step_progress = self._build_step_progress_context()
                post_status_rule = ""
                if last_tool_name in ("mcp_status", "mcp_status_refresh"):
                    post_status_rule = (
                        "You just ran an MCP status query tool. The next assistant message must render the complete status report from the previous tool result's `status` fields "
                        "using the fixed template, and then finish without further tool calls."
                    )
                elif last_tool_name == "mcp_server_info":
                    post_status_rule = (
                        "You just ran `mcp_server_info`. The next assistant message must render the server details report from the previous tool result's `info`/`status` fields "
                        "using the fixed template. "
                        "After the report is visible, decide from [Original user request]: "
                        "if it only asks to query/show that MCP server, finish without further tool calls; "
                        "if it contains other unfinished goals, continue with the relevant next tool call. "
                        "Query/show requests should not create files or run shell by default; "
                        "create files only when the user explicitly asks to export/save/write a file. "
                        "Do not call unrelated tools such as mcp_status/mcp_status_refresh or shell just to pad steps."
                    )
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
                next_input = (
                    f"[Original user request]\n{original_user_task}\n\n"
                    f"{step_progress}\n\n"
                    f"{active_plan_block}"
                    f"[Previous batch tool results (compact)]\n{self._compact_result_for_next_input(result_for_next_input)}\n\n"
                    + "Continue with standard tools when more tool work is needed; you may call one or more tools at once. "
                    "When no further tool action is required, reply in natural language with no tool_calls and the host will return to the command prompt. "
                    "If the previous batch result already satisfies the original request, finish in the next assistant message with a natural-language reply only."
                    + (f"\n{post_status_rule}" if post_status_rule else "")
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
            self._schedule_auto_memory_reflect()
            if auto_exit_after_turn:
                self._save_current_workspace_position()
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
            print("")
            try:
                should_exit = input(
                    t("runtime.exit_prompt", app_name=get_app_name())
                ).strip().lower() == "y"
            except KeyboardInterrupt:
                should_exit = False

            if should_exit:
                self._save_current_workspace_position()
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
            self._in_task_execution = False
            self._stop_interrupt_monitor(cancel_task_on_interrupt=True)
            print(t("runtime.error_occurred", error=str(e)))
