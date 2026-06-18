from __future__ import annotations

import json
import re
from typing import Any, Callable, List, Pattern, Tuple

from .console_utils import _ansi_bright_blue, _ansi_cyan, _ansi_gray, _ansi_green, _ansi_rgb, _ansi_yellow


def _payload_looks_like_tool_call(payload: Any) -> bool:
    """Return True iff a parsed JSON blob looks like a model tool-call payload.

    Recognized shapes (whichever the model emitted):
      * ``{"tool_calls": [...]}`` — OpenAI-style envelope
      * ``{"tool": "...", "args": {...}}`` — short pseudo-tool form
      * ``{"name"/"function": ...}`` — bare single call
      * a top-level list whose first element is one of the above shapes

    Used by ``strip_tool_json_blocks_for_display`` to recognise (and remove)
    a trailing JSON tool-call block that the model accidentally streamed
    into its visible reply. We only need to *detect* tool-callness for the
    display strip; the actual tool execution path uses the strict parser in
    ``runtime_loop`` so we deliberately err on the side of "skip" here
    (better to drop the JSON than to surface it as text).
    """
    if isinstance(payload, dict):
        if "tool_calls" in payload:
            return True
        if "tool" in payload and ("args" in payload or "arguments" in payload):
            return True
        if "name" in payload and ("args" in payload or "arguments" in payload):
            return True
        if "function" in payload:
            return True
        return False
    if isinstance(payload, list) and payload:
        return _payload_looks_like_tool_call(payload[0])
    return False


# Trailing pseudo tool-call JSON sometimes arrives inside a ```json fence;
# match the opener so we can locate the body's starting offset.
_TRAILING_FENCE_RE = re.compile(r"(?im)^[ \t]*```(?:json|javascript|js)?[ \t]*\n")
_TRAILING_OBJECT_RE = re.compile(r"(?m)^[ \t]*(?:\{|\[)")


_POWERSHELL_OPERATOR_TOKENS = {
    "-eq",
    "-ne",
    "-gt",
    "-ge",
    "-lt",
    "-le",
    "-like",
    "-notlike",
    "-match",
    "-notmatch",
    "-replace",
    "-contains",
    "-notcontains",
    "-in",
    "-notin",
    "-and",
    "-or",
    "-xor",
    "-not",
    "-is",
    "-isnot",
    "-as",
}
def _ansi_ps_command(text: str) -> str:
    return _ansi_rgb(text, 97, 175, 239)


def _ansi_ps_parameter(text: str) -> str:
    return _ansi_rgb(text, 198, 120, 221)


def _ansi_ps_operator(text: str) -> str:
    return _ansi_rgb(text, 224, 108, 117)


def _ansi_ps_pipe(text: str) -> str:
    return _ansi_rgb(text, 97, 175, 239)


def _strip_trailing_tool_call_json_once(text: str) -> Tuple[str, bool]:
    """Strip a single trailing tool-call JSON block from ``text``.

    Returns ``(new_text, stripped)``. ``stripped`` is True iff exactly one
    trailing block was recognized and removed. The caller loops to handle
    models that emit several JSON blocks in succession (typical when the
    model both plans a step via ``update_plan`` and then prompts via
    ``ask_more_info`` inside one assistant turn).
    """
    rstripped = text.rstrip()
    if not rstripped:
        return text, False

    candidates: List[Tuple[int, str]] = []
    if rstripped.endswith("```"):
        fence_matches = list(_TRAILING_FENCE_RE.finditer(rstripped))
        if fence_matches:
            fence = fence_matches[-1]
            body_start = fence.end()
            body_end = rstripped.rfind("```")
            if body_end > body_start:
                candidates.append((fence.start(), rstripped[body_start:body_end].strip()))

    # Try the latest possible top-of-line ``{`` / ``[`` opener first so the
    # tail-most JSON is detected even when the model concatenated multiple
    # blocks with ``,`` separators (each loop iteration consumes one block).
    for m in reversed(list(_TRAILING_OBJECT_RE.finditer(rstripped))):
        start = m.start()
        tail = rstripped[start:].strip()
        # Strip a trailing JSON-list comma the model uses to chain blocks,
        # otherwise ``json.loads`` would reject the candidate as malformed.
        if tail.endswith(","):
            tail = tail[:-1].rstrip()
        candidates.append((start, tail))

    for start, candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if not _payload_looks_like_tool_call(payload):
            continue
        stripped = rstripped[:start].rstrip()
        # Drop a dangling comma that the model used to chain this block onto
        # an earlier one (``…},\n{…}`` patterns).
        if stripped.endswith(","):
            stripped = stripped[:-1].rstrip()
        return stripped, True
    return text, False


def strip_tool_json_blocks_for_display(text: str) -> str:
    """Strip trailing pseudo tool-call JSON block(s) from assistant text.

    Some models occasionally emit a tool call as a JSON object inside the
    assistant's natural-language reply (instead of as a real ``tool_calls``
    entry on the message). The runtime still recognises and executes the
    embedded call, but the JSON itself must not leak into either the TUI
    history replay or the GUI's ``chat_history`` payload — both renderers
    funnel through ``format_assistant_display_response`` so this helper is
    the single chokepoint for hiding it.

    Loops until no more trailing tool-call JSON can be peeled off so a
    model that concatenated several blocks (e.g. an ``update_plan`` step
    followed by an ``ask_more_info`` prompt in the same turn) is fully
    cleaned, not just the last block. Returns the input (whitespace-
    trimmed) when no tool-call shaped trailing JSON is found.
    """
    if not isinstance(text, str) or not text:
        return ""
    current = text
    if not current.strip():
        return ""
    for _ in range(8):  # safety cap; pathological inputs can't loop forever
        next_text, stripped = _strip_trailing_tool_call_json_once(current)
        if not stripped:
            break
        current = next_text
        if not current.strip():
            return ""
    return current.strip()


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
    token = re.sub(r"^[\(\[\{]+", "", token)
    token = re.sub(r"[\)\]\},;]+$", "", token)
    if token.startswith("!") and len(token) > 1:
        token = token[1:]
    if not token:
        return False
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
        "rg",
        "ripgrep",
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
    if _looks_like_powershell_cmdlet(token):
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


def _looks_like_powershell_cmdlet(token: str) -> bool:
    t = str(token or "").strip()
    if not re.fullmatch(r"[A-Za-z]+-[A-Za-z][A-Za-z0-9]*", t):
        return False
    verb, noun = t.split("-", 1)
    approved_verbs = {
        "add",
        "clear",
        "close",
        "compare",
        "connect",
        "convert",
        "copy",
        "disable",
        "disconnect",
        "enable",
        "enter",
        "exit",
        "export",
        "find",
        "format",
        "get",
        "import",
        "invoke",
        "join",
        "measure",
        "move",
        "new",
        "open",
        "out",
        "pop",
        "push",
        "read",
        "receive",
        "remove",
        "rename",
        "reset",
        "resize",
        "search",
        "select",
        "send",
        "set",
        "show",
        "sort",
        "split",
        "start",
        "stop",
        "switch",
        "test",
        "trace",
        "update",
        "use",
        "wait",
        "where",
        "write",
    }
    return verb.lower() in approved_verbs and noun.isalnum()


def _highlight_shell_command_line(text: str) -> str:
    if not text:
        return text

    is_powershell = _looks_like_powershell_command_line(text)
    parts = re.split(r"(\s+)", text)
    out: List[str] = []
    first_token_seen = False
    prev_plain = ""
    in_command_string = False
    command_quote = ""
    command_first_token_seen = False
    command_prev_plain = ""
    in_quoted_string = False
    quoted_char = ""
    for part in parts:
        if not part or part.isspace():
            out.append(part)
            continue
        token = part
        plain = token.strip("\"'")
        lower = plain.lower()
        if in_command_string:
            ended = token.endswith(command_quote)
            inner = token[:-1] if ended else token
            if inner:
                colored_inner, command_first_token_seen, command_prev_plain = _highlight_shell_token(
                    inner,
                    command_first_token_seen,
                    command_prev_plain,
                    is_powershell=True,
                )
                out.append(colored_inner)
            if ended:
                out.append(command_quote)
                in_command_string = False
                command_quote = ""
            prev_plain = lower
            continue
        if in_quoted_string:
            end_idx = _find_unescaped_quote_index(token, quoted_char)
            if end_idx >= 0:
                inner = token[:end_idx]
                tail = token[end_idx + 1 :]
                if inner:
                    out.append(_ansi_green(inner))
                out.append(quoted_char)
                if tail:
                    out.append(tail)
                in_quoted_string = False
                quoted_char = ""
            else:
                out.append(_ansi_green(token))
            prev_plain = lower
            continue
        if prev_plain == "-command":
            if token[:1] in {'"', "'"}:
                quote = token[0]
                body = token[1:]
                ended = body.endswith(quote)
                inner = body[:-1] if ended else body
                out.append(quote)
                command_first_token_seen = False
                command_prev_plain = ""
                if inner:
                    colored_inner, command_first_token_seen, command_prev_plain = _highlight_shell_token(
                        inner,
                        command_first_token_seen,
                        command_prev_plain,
                        is_powershell=True,
                    )
                    out.append(colored_inner)
                if ended:
                    out.append(quote)
                else:
                    in_command_string = True
                    command_quote = quote
                prev_plain = lower
                continue
        if token[:1] in {'"', "'"} and len(token) > 1:
            quoted_char_candidate = token[0]
            body = token[1:]
            end_idx = _find_unescaped_quote_index(body, quoted_char_candidate)
            if end_idx >= 0:
                inner = body[:end_idx]
                tail = body[end_idx + 1 :]
                out.append(quoted_char_candidate + _ansi_green(inner) + quoted_char_candidate)
                if tail:
                    out.append(tail)
                first_token_seen = True
                prev_plain = lower
                continue
            quoted_char = quoted_char_candidate
            out.append(quoted_char + _ansi_green(body))
            in_quoted_string = True
            first_token_seen = True
            prev_plain = lower
            continue
        colored, first_token_seen, prev_plain = _highlight_shell_token(
            token,
            first_token_seen,
            prev_plain,
            is_powershell=is_powershell,
        )
        out.append(colored)
    return "".join(out)


def _highlight_shell_token(
    token: str,
    first_token_seen: bool,
    prev_plain: str,
    is_powershell: bool = False,
) -> Tuple[str, bool, str]:
    plain = str(token or "").strip("\"'")
    lower = plain.lower()
    if token in {"|", "||", "&&", ";"}:
        if is_powershell and token == "|":
            return _ansi_ps_pipe(token), False, lower
        return _ansi_yellow(token), False, lower
    if not first_token_seen:
        if token.startswith("!") and len(token) > 1:
            colored_first = _ansi_ps_command(token[1:]) if is_powershell else _ansi_bright_blue(token[1:])
            return "!" + colored_first, True, lower
        if is_powershell:
            return _ansi_ps_command(token), True, lower
        return _ansi_bright_blue(token), True, lower
    if is_powershell and lower in _POWERSHELL_OPERATOR_TOKENS:
        return _ansi_ps_operator(token), first_token_seen, lower
    if is_powershell and re.fullmatch(r"-[A-Za-z][A-Za-z0-9_-]*", plain):
        return _ansi_ps_parameter(token), first_token_seen, lower
    if token.startswith(("--", "-")) and not token.startswith(("http://", "https://")):
        return _ansi_yellow(token), first_token_seen, lower
    if prev_plain == "-m":
        return _ansi_bright_blue(token), first_token_seen, lower
    if _looks_like_powershell_cmdlet(plain):
        if is_powershell:
            return _ansi_ps_command(token), first_token_seen, lower
        return _ansi_bright_blue(token), first_token_seen, lower
    if lower in {
        "install",
        "run",
        "start",
        "stop",
        "check",
        "list",
        "show",
        "create",
        "delete",
        "remove",
        "update",
        "switch",
        "clone",
        "pull",
        "push",
        "build",
        "test",
        "verify",
    }:
        return _ansi_bright_blue(token), first_token_seen, lower
    if token.startswith(('"', "'")) and token.endswith(('"', "'")) and len(token) >= 2:
        inner = token[1:-1]
        if _looks_like_path_or_url(inner):
            return token[0] + _ansi_cyan(inner) + token[-1], first_token_seen, lower
        return _ansi_green(token), first_token_seen, lower
    if _looks_like_path_or_url(plain) or _looks_like_env_var(plain):
        return _ansi_cyan(token), first_token_seen, lower
    return token, first_token_seen, lower


def _looks_like_powershell_command_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if re.search(r"(?i)\b(?:powershell|pwsh)(?:\.exe)?\b", s):
        return True
    for raw in re.findall(r'(?:\"[^\"]*\"|\'[^\']*\'|\S+)', s):
        tok = raw.strip("\"'")
        tok = re.sub(r"^[\(\[\{]+", "", tok)
        tok = re.sub(r"[\)\]\},;]+$", "", tok)
        if _looks_like_powershell_cmdlet(tok):
            return True
    return False


def _find_unescaped_quote_index(text: str, quote_char: str) -> int:
    if not text or quote_char not in {'"', "'"}:
        return -1
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == quote_char:
            return i
    return -1


def _looks_like_path_or_url(text: str) -> bool:
    s = str(text or "")
    if not s:
        return False
    s_check = s.lstrip("([{").rstrip(")]},;")
    if not s_check:
        s_check = s
    if re.match(r"https?://", s_check, flags=re.IGNORECASE):
        return True
    if any(ch in s_check for ch in ("[", "]", "=", "*", "?")) and not re.search(r"[\\/]", s_check):
        return False
    if s_check.startswith((".", "/", "~")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", s_check):
        return True
    if re.search(r"[\\/]", s_check):
        return True
    if re.search(r"\.[A-Za-z0-9]{1,8}$", s_check):
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
