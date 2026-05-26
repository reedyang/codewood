from __future__ import annotations

import json
import re
from typing import Callable, List, Pattern, Tuple

from .console_utils import _ansi_bright_blue, _ansi_cyan, _ansi_gray, _ansi_green, _ansi_yellow


def strip_tool_json_blocks_for_display(text: str) -> str:
    """Hide trailing tool-call JSON blocks from assistant display text."""
    if not isinstance(text, str) or not text:
        return ""

    def _replace_fence(match: re.Match) -> str:
        body = (match.group(1) or "").strip()
        if body.startswith("`") and body.endswith("`") and len(body) >= 2:
            body = body[1:-1].strip()
        try:
            obj = json.loads(body)
        except Exception:
            return match.group(0)
        if isinstance(obj, dict) and isinstance((obj.get("tool") or obj.get("action")), str):
            return ""
        return match.group(0)

    out = re.sub(
        r"```(?:json)?\s*(.*?)\s*```",
        _replace_fence,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = out.strip()
    if not stripped:
        return ""

    unclosed = re.search(r"```(?:json)?\s*(.*)\Z", stripped, flags=re.IGNORECASE | re.DOTALL)
    if unclosed:
        body = (unclosed.group(1) or "").strip()
        if body.startswith("`") and body.endswith("`") and len(body) >= 2:
            body = body[1:-1].strip()
        try:
            obj = json.loads(body)
        except Exception:
            obj = None
        if isinstance(obj, dict) and isinstance((obj.get("tool") or obj.get("action")), str):
            return stripped[: unclosed.start()].strip()

    def _find_trailing_tool_json_span(s: str) -> Tuple[int, int] | None:
        s = s.rstrip()
        if not s:
            return None
        n = len(s)
        for m_obj in re.finditer(r"\{", s):
            start = m_obj.start()
            depth = 0
            in_str = False
            esc = False
            end = -1
            i = start
            while i < n:
                ch = s[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    i += 1
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
                i += 1
            if end == -1 or end != n:
                continue
            chunk = s[start:end].strip()
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if isinstance((obj.get("tool") or obj.get("action")), str):
                return (start, end)
        return None

    trailing_span = _find_trailing_tool_json_span(stripped)
    if trailing_span:
        start, _ = trailing_span
        prefix = stripped[:start]
        prefix = re.sub(r"```(?:json)?\s*$", "", prefix, flags=re.IGNORECASE)
        return prefix.strip()
    return stripped


def normalize_display_text(text: str) -> str:
    """Normalize assistant display text for consistent terminal rendering."""
    if not isinstance(text, str) or not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    if not s.strip():
        return ""
    lines = s.split("\n")
    out: List[str] = []
    prev_blank = False
    for ln in lines:
        blank = ln.strip() == ""
        if blank:
            if prev_blank:
                continue
            out.append("")
            prev_blank = True
        else:
            out.append(ln.rstrip())
            prev_blank = False
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def format_assistant_display_response(text: str) -> str:
    """Prepare assistant text for terminal display (clean + normalize + highlight)."""
    normalized = normalize_display_text(strip_tool_json_blocks_for_display(text))
    if not normalized:
        return ""
    return highlight_assistant_display_text(normalized)


def highlight_assistant_display_text(text: str) -> str:
    """Colorize important tokens in assistant narrative output."""
    if not isinstance(text, str) or not text:
        return ""
    lines = text.split("\n")
    return "\n".join(highlight_assistant_display_line(line) for line in lines)


def highlight_assistant_display_line(line: str) -> str:
    if not line:
        return line
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return _ansi_gray(line)

    comment_idx = line.find(" #")
    if comment_idx >= 0:
        main = line[:comment_idx]
        comment = line[comment_idx:]
    else:
        main = line
        comment = ""

    marker = ""
    body = main
    marker_match = re.match(r"^(\s*(?:[-*]|\d+\.)\s+)(.*)$", main)
    if marker_match:
        marker = _ansi_bright_blue(marker_match.group(1))
        body = marker_match.group(2)

    if _looks_like_shell_command_line(body):
        highlighted_body = _highlight_shell_command_line(body)
    else:
        highlighted_body = _highlight_assistant_inline_tokens(body)
    highlighted = marker + highlighted_body
    if comment:
        return highlighted + _ansi_gray(comment)
    return highlighted


def _looks_like_shell_command_line(text: str) -> bool:
    s = str(text or "").lstrip()
    if not s or s.startswith("#"):
        return False
    first = re.match(r'(?:\"[^\"]*\"|\'[^\']*\'|\S+)', s)
    if not first:
        return False
    token = first.group(0).strip("\"'")
    lower = token.lower()
    if _contains_cjk(token):
        return False
    command_names = (
        "powershell",
        "pwsh",
        "python",
        "python3",
        "pip",
        "pip3",
        "cmd",
        "bash",
        "sh",
        "git",
        "npm",
        "node",
        "npx",
        "docker",
        "kubectl",
        "curl",
        "wget",
        "make",
        "uv",
        "poetry",
        "conda",
        "rsync",
        "scp",
        "ssh",
        "dir",
        "ls",
        "cat",
        "type",
        "echo",
        "start",
        "stop",
    )
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
        return True
    if lower.startswith((".", "/", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", token):
        return True
    if lower.endswith((".ps1", ".cmd", ".bat", ".sh", ".py", ".exe")):
        return True
    if lower in command_names:
        return True
    return False


def _contains_cjk(text: str) -> bool:
    for ch in str(text or ""):
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
        ):
            return True
    return False


def _highlight_shell_command_line(text: str) -> str:
    if not text:
        return text

    parts = re.split(r"(\s+)", text)
    out: List[str] = []
    first_token_seen = False
    prev_plain = ""
    for part in parts:
        if not part or part.isspace():
            out.append(part)
            continue
        token = part
        plain = token.strip("\"'")
        lower = plain.lower()
        if not first_token_seen:
            out.append(_ansi_bright_blue(token))
            first_token_seen = True
            prev_plain = lower
            continue
        if token.startswith(("--", "-")) and not token.startswith(("http://", "https://")):
            out.append(_ansi_yellow(token))
            prev_plain = lower
            continue
        if prev_plain == "-m":
            out.append(_ansi_bright_blue(token))
            prev_plain = lower
            continue
        if lower in {"install", "run", "start", "stop", "check", "list", "show", "create", "delete", "remove", "update", "switch", "clone", "pull", "push", "build", "test", "verify"}:
            out.append(_ansi_bright_blue(token))
            prev_plain = lower
            continue
        if token.startswith(('"', "'")) and token.endswith(('"', "'")) and len(token) >= 2:
            inner = token[1:-1]
            if _looks_like_path_or_url(inner):
                out.append(token[0] + _ansi_cyan(inner) + token[-1])
            else:
                out.append(_ansi_green(token))
            prev_plain = lower
            continue
        if _looks_like_path_or_url(plain) or _looks_like_env_var(plain):
            out.append(_ansi_cyan(token))
            prev_plain = lower
            continue
        out.append(token)
        prev_plain = lower
    return "".join(out)


def _looks_like_path_or_url(text: str) -> bool:
    s = str(text or "")
    if not s:
        return False
    if re.match(r"https?://", s, flags=re.IGNORECASE):
        return True
    if any(ch in s for ch in ("[", "]", "=", "*", "?")) and not re.search(r"[\\/]", s):
        return False
    if s.startswith((".", "/", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    if re.search(r"[\\/]", s):
        return True
    if re.search(r"\.[A-Za-z0-9]{1,8}$", s):
        return True
    return False


def _looks_like_env_var(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", str(text or "")))


def _highlight_assistant_inline_tokens(text: str) -> str:
    if not text:
        return text

    rules: List[Tuple[Pattern[str], Callable[[str], str]]] = [
        (re.compile(r"`[^`\n]+`"), _ansi_cyan),
        (re.compile(r"https?://[^\s`<>)\]}]+", re.IGNORECASE), _ansi_cyan),
        (
            re.compile(
                r"(?<![A-Za-z0-9_])(?:~[\\/][^\s`\"'<>|]+|"
                r"(?:\.{1,2}[\\/][^\s`\"'<>|]+)|"
                r"(?:[A-Za-z]:[\\/][^\s`\"'<>|]+)|"
                r"(?:/[A-Za-z0-9_.~\-\/]+)|"
                r"(?:[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.\\/-]*\.[A-Za-z0-9]{1,8})|"
                r"(?:[A-Za-z0-9_.-]+\.(?:ps1|cmd|bat|sh|py|exe|json|ya?ml|toml|md|txt|env))|"
                r"(?:[A-Za-z0-9_.-]+[\\/]))"
            ),
            _ansi_cyan,
        ),
        (re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b"), _ansi_cyan),
    ]

    occupied = [False] * len(text)
    spans: List[Tuple[int, int, Callable[[str], str]]] = []
    for pattern, painter in rules:
        for match in pattern.finditer(text):
            start, end = match.span()
            if start >= end:
                continue
            if any(occupied[start:end]):
                continue
            for i in range(start, end):
                occupied[i] = True
            spans.append((start, end, painter))

    if not spans:
        return text

    spans.sort(key=lambda it: it[0])
    out: List[str] = []
    cursor = 0
    for start, end, painter in spans:
        if cursor < start:
            out.append(text[cursor:start])
        out.append(painter(text[start:end]))
        cursor = end
    if cursor < len(text):
        out.append(text[cursor:])
    return "".join(out)
