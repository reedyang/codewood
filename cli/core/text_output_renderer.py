from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable, Dict, List, Pattern, Tuple

from .console_utils import (
    _ansi_bold,
    _ansi_bright_blue,
    _ansi_cyan,
    _ansi_gray,
    _ansi_green,
    _ansi_italic,
    _ansi_rgb,
    _ansi_yellow,
)


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
    ``request_user_input`` inside one assistant turn).
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
    followed by an ``request_user_input`` prompt in the same turn) is fully
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


# Curated LaTeX-command -> Unicode map for inline math the model commonly
# emits in plain narrative (e.g. ``$\rightarrow$``). Neither the TUI nor the
# GUI runs a TeX engine, so these would otherwise render literally. Kept
# intentionally small and arrow/operator focused; longest keys are matched
# first so e.g. ``\leftrightarrow`` wins over ``\leftarrow``. The GUI mirrors
# this same table in ``desktop/frontend/src/components/Markdown.tsx``.
_LATEX_MATH_SYMBOLS: Dict[str, str] = {
    "leftrightarrow": "\u2194",
    "Leftrightarrow": "\u21d4",
    "rightarrow": "\u2192",
    "Rightarrow": "\u21d2",
    "leftarrow": "\u2190",
    "Leftarrow": "\u21d0",
    "longrightarrow": "\u27f6",
    "longleftarrow": "\u27f5",
    "uparrow": "\u2191",
    "downarrow": "\u2193",
    "mapsto": "\u21a6",
    "to": "\u2192",
    "gets": "\u2190",
    "times": "\u00d7",
    "div": "\u00f7",
    "cdot": "\u00b7",
    "pm": "\u00b1",
    "mp": "\u2213",
    "leq": "\u2264",
    "le": "\u2264",
    "geq": "\u2265",
    "ge": "\u2265",
    "neq": "\u2260",
    "ne": "\u2260",
    "approx": "\u2248",
    "equiv": "\u2261",
    "infty": "\u221e",
    "ldots": "\u2026",
    "cdots": "\u22ef",
}

# Match a `\command` (letters only) so the replacement is whole-token.
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+)")
# Inline math spans: ``$...$`` (not ``$$``) and ``\(...\)``.
_INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)|\\\(([^\n]+?)\\\)")


def _convert_latex_command(match: "re.Match[str]") -> str:
    name = match.group(1)
    repl = _LATEX_MATH_SYMBOLS.get(name)
    return repl if repl is not None else match.group(0)


def _render_inline_math_span(body: str) -> str:
    """Render the inside of an inline-math span to Unicode-ish plain text.

    Replaces known LaTeX commands with Unicode, drops a few formatting-only
    wrappers, and collapses spacing macros — enough to make simple symbolic
    expressions (especially arrows) readable without a TeX engine.
    """
    s = body
    # Spacing macros and braces carry no meaning in plain text.
    s = s.replace("\\,", "\u202f").replace("\\;", " ").replace("\\!", "")
    s = _LATEX_CMD_RE.sub(_convert_latex_command, s)
    s = s.replace("{", "").replace("}", "")
    return s.strip()


def convert_inline_latex_math(text: str) -> str:
    """Convert common inline LaTeX math (``$...$`` / ``\\(...\\)``) to Unicode.

    Only spans whose body, after conversion, no longer contains a stray
    backslash command are unwrapped; anything we don't recognize is left
    verbatim so we never mangle real prose or genuine ``$`` usage (prices,
    shell vars). Display-time only — never mutates stored history.
    """
    if not isinstance(text, str) or "$" not in text and "\\(" not in text:
        return text

    def _repl(m: "re.Match[str]") -> str:
        body = m.group(1) if m.group(1) is not None else m.group(2)
        if body is None:
            return m.group(0)
        # Require an actual LaTeX command in the span before treating it as
        # math. This keeps plain ``$`` usage (prices like ``$5``, shell vars)
        # untouched — we only rewrite spans the model clearly meant as TeX.
        if "\\" not in body:
            return m.group(0)
        converted = _render_inline_math_span(body)
        # Bail out if conversion left unhandled TeX (a backslash command):
        # rendering a half-converted span is worse than leaving it as-is.
        if "\\" in converted:
            return m.group(0)
        return converted

    return _INLINE_MATH_RE.sub(_repl, text)


def normalize_display_text(text: str) -> str:
    """Normalize assistant display text for consistent terminal rendering."""
    if not isinstance(text, str) or not text:
        return ""
    s = convert_inline_latex_math(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
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


def _reframe_proposed_plan_blocks(text: str) -> str:
    """Replace ``<proposed_plan>`` tags with a visible "Proposed Plan" banner.

    Plan mode wraps the final plan in ``<proposed_plan>...</proposed_plan>``; the
    raw tags are an internal protocol marker, not something the user should read.
    We keep the plan body (so the TUI still shows the plan) but swap the literal
    tags for a clear header/footer so it reads as a dedicated section.
    """
    from .proposed_plan import PROPOSED_PLAN_OPEN_TAG, _PROPOSED_PLAN_RE

    if not isinstance(text, str) or PROPOSED_PLAN_OPEN_TAG not in text:
        return text

    def _repl(m: "re.Match[str]") -> str:
        body = (m.group(1) or "").strip()
        if not body:
            return ""
        return f"\n\n{'─' * 8} Proposed Plan {'─' * 8}\n\n{body}\n\n{'─' * 31}\n"

    return _PROPOSED_PLAN_RE.sub(_repl, text)


def format_assistant_display_response(text: str) -> str:
    """Prepare assistant text for terminal display (clean + normalize + highlight)."""
    reframed = _reframe_proposed_plan_blocks(strip_tool_json_blocks_for_display(text))
    normalized = normalize_display_text(reframed)
    if not normalized:
        return ""
    return highlight_assistant_display_text(normalized)


def format_assistant_display_response_plain(text: str) -> str:
    """Prepare assistant text for the GUI's persisted history.

    Unlike :func:`format_assistant_display_response` (terminal-oriented), this
    keeps the raw ``<proposed_plan>...</proposed_plan>`` block intact and applies
    no ANSI highlighting. The GUI's Markdown renderer turns the block into a
    "Proposed Plan" card and the chooser keys off the literal tags, so they MUST
    survive into the reloaded history — otherwise, after an app restart, the plan
    card and the "Implement this plan?" options disappear. Tool-call JSON is
    still stripped so the GUI never shows a serialized tool envelope.
    """
    cleaned = strip_tool_json_blocks_for_display(text)
    return normalize_display_text(cleaned)


# --- GitHub-style Markdown tables -----------------------------------------
# A delimiter cell is dashes with optional alignment colons (``:--``, ``--:``,
# ``:-:``). Mirrors the GUI table parser in
# ``desktop/frontend/src/components/Markdown.tsx`` — keep the two in sync.
_MD_TABLE_DELIM_CELL_RE = re.compile(r"^:?-+:?$")
_MD_TABLE_DELIM_LINE_RE = re.compile(r"^[\s|:-]+$")


def _split_md_table_row(line: str) -> List[str]:
    """Split a table row into trimmed cells, honoring escaped pipes (``\\|``)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: List[str] = []
    buf: List[str] = []
    i = 0
    length = len(s)
    while i < length:
        ch = s[i]
        if ch == "\\" and i + 1 < length and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _is_md_table_delimiter_row(line: str) -> bool:
    """True when ``line`` is a header/body separator like ``| --- | :--: |``."""
    s = line.strip()
    if "-" not in s or not _MD_TABLE_DELIM_LINE_RE.fullmatch(s):
        return False
    cells = _split_md_table_row(s)
    return bool(cells) and all(
        _MD_TABLE_DELIM_CELL_RE.fullmatch(c) for c in cells
    )


def _md_table_aligns(delim_line: str) -> List[str]:
    """Per-column alignment ("left"/"center"/"right"/"") from the delimiter row."""
    aligns: List[str] = []
    for c in _split_md_table_row(delim_line):
        left = c.startswith(":")
        right = c.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        elif left:
            aligns.append("left")
        else:
            aligns.append("")
    return aligns


def _md_cell_display_width(s: str) -> int:
    """Terminal display width of plain (un-ANSI) text, counting CJK as 2."""
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _render_markdown_table(
    header: List[str], aligns: List[str], body: List[List[str]]
) -> List[str]:
    """Render a parsed Markdown table as box-drawing lines for the terminal.

    Column widths are computed from the plain cell text (so CJK double-width
    characters align), the header row is bold, body cells reuse the inline
    token highlighter, and the borders are dimmed.
    """
    ncols = max([len(header)] + [len(r) for r in body]) if (header or body) else 0
    if ncols == 0:
        return []

    def _norm(row: List[str]) -> List[str]:
        return [row[i] if i < len(row) else "" for i in range(ncols)]

    header = _norm(header)
    body = [_norm(r) for r in body]
    aligns = [aligns[i] if i < len(aligns) else "" for i in range(ncols)]

    widths = [_md_cell_display_width(header[i]) for i in range(ncols)]
    for row in body:
        for i in range(ncols):
            widths[i] = max(widths[i], _md_cell_display_width(row[i]))

    def _border(left: str, mid: str, right: str) -> str:
        segments = mid.join("─" * (widths[i] + 2) for i in range(ncols))
        return _ansi_gray(f"{left}{segments}{right}")

    def _render_row(cells: List[str], is_header: bool) -> str:
        bar = _ansi_gray("│")
        rendered: List[str] = []
        for i in range(ncols):
            plain = cells[i]
            pad = max(0, widths[i] - _md_cell_display_width(plain))
            align = aligns[i]
            if align == "right":
                left_pad, right_pad = pad, 0
            elif align == "center":
                left_pad = pad // 2
                right_pad = pad - left_pad
            else:
                left_pad, right_pad = 0, pad
            if not plain:
                content = ""
            elif is_header:
                content = _ansi_bold(plain)
            else:
                content = _highlight_assistant_inline_tokens(plain)
            rendered.append(f" {' ' * left_pad}{content}{' ' * right_pad} ")
        return bar + bar.join(rendered) + bar

    out: List[str] = [_border("┌", "┬", "┐"), _render_row(header, True)]
    out.append(_border("├", "┼", "┤"))
    for row in body:
        out.append(_render_row(row, False))
    out.append(_border("└", "┴", "┘"))
    return out


# Fenced code block delimiter (``` or ~~~), optionally with a language tag.
_CODE_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+\-]*)\s*$")
# Markdown heading: 1-6 leading '#'. Captures level + text.
_HEADING_RE = re.compile(r"^(\s*)(#{1,6})\s+(.*?)\s*#*\s*$")
# Horizontal rule: a line of only ---, ***, or ___ (3+).
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")


def highlight_assistant_display_text(text: str) -> str:
    """Colorize important tokens in assistant narrative output.

    Lightweight Markdown rendering for the terminal: fenced code blocks are
    dimmed and left literal (no inline token painting inside them), ATX
    headings render bold, horizontal rules become a thin separator, and inline
    spans (``**bold**``, ``*italic*``, `` `code` ``) are styled per line. We do
    NOT pull in a Markdown engine — this keeps the existing width-aware wrap and
    bullet-indent pipeline (and its ANSI-aware width math) intact.
    """
    if not isinstance(text, str) or not text:
        return ""
    lines = text.split("\n")
    out: List[str] = []
    in_fence = False
    fence_marker = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        fence_match = _CODE_FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(2)
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
                indent = fence_match.group(1)
                lang = fence_match.group(3)
                label = f"{indent}┌─ {lang}" if lang else f"{indent}┌─"
                out.append(_ansi_gray(label))
            elif marker[0] == fence_marker:
                in_fence = False
                fence_marker = ""
                out.append(_ansi_gray(f"{fence_match.group(1)}└─"))
            else:
                out.append(_ansi_green(line))
            i += 1
            continue
        if in_fence:
            # Inside a code block: keep the source verbatim, just dim-color it
            # so it reads as code without the inline token painter mangling it.
            out.append(_ansi_green(line))
            i += 1
            continue
        # GitHub-style table: a header row followed by a delimiter row, then
        # zero or more body rows. Rendered as an aligned box-drawing table so
        # the columns line up in the terminal instead of leaking raw pipes.
        if "|" in line and i + 1 < n and _is_md_table_delimiter_row(lines[i + 1]):
            header = _split_md_table_row(line)
            aligns = _md_table_aligns(lines[i + 1])
            i += 2
            body: List[List[str]] = []
            while i < n and lines[i].strip() != "" and "|" in lines[i]:
                body.append(_split_md_table_row(lines[i]))
                i += 1
            out.extend(_render_markdown_table(header, aligns, body))
            continue
        out.append(highlight_assistant_display_line(line))
        i += 1
    return "\n".join(out)


def highlight_assistant_display_line(line: str) -> str:
    if not line:
        return line

    heading_match = _HEADING_RE.match(line)
    if heading_match:
        indent = heading_match.group(1)
        body = heading_match.group(3)
        if body:
            return f"{indent}{_ansi_bold(_highlight_assistant_inline_tokens(body))}"
        return _ansi_bold(line)

    if _HR_RE.match(line):
        return _ansi_gray("─" * 24)

    stripped = line.lstrip()
    if stripped.startswith(">"):
        indent = line[: len(line) - len(stripped)]
        quoted = stripped[1:].lstrip()
        return f"{indent}{_ansi_gray('│ ')}{_ansi_italic(_highlight_assistant_inline_tokens(quoted))}"

    comment_idx = line.find(" #")
    if comment_idx >= 0:
        main = line[:comment_idx]
        comment = line[comment_idx:]
    else:
        main = line
        comment = ""

    marker = ""
    body = main
    # Unordered list: render -, * or + as a • bullet (indent preserved so nested
    # levels keep their depth). Ordered list: keep the "N." marker verbatim.
    bullet_match = re.match(r"^(\s*)([-*+])(\s+)(.*)$", main)
    ordered_match = re.match(r"^(\s*\d+\.\s+)(.*)$", main)
    if bullet_match:
        marker = _ansi_bright_blue(f"{bullet_match.group(1)}•{bullet_match.group(3)}")
        body = bullet_match.group(4)
    elif ordered_match:
        marker = _ansi_bright_blue(ordered_match.group(1))
        body = ordered_match.group(2)

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


_ANSI_RESET = "\033[0m"
# Sentinel used to split a painter's output into (open, close) around content.
_STYLE_SENTINEL = "\x00\x00"


def _style_open_close(painter: Callable[[str], str]) -> Tuple[str, str]:
    """Recover the (open, close) sequences a painter wraps content with.

    Works for both the real ANSI helpers (``\x1b[1m`` … ``\x1b[0m``) and the
    ``<B>``/``</B>`` test doubles, and yields ``("", "")`` when color is
    disabled (the painter returns its argument unchanged).
    """
    wrapped = painter(_STYLE_SENTINEL)
    open_seq, sep, close_seq = wrapped.partition(_STYLE_SENTINEL)
    if not sep:
        return "", ""
    return open_seq, close_seq


def _wrap_style_over_inner(open_seq: str, close_seq: str, inner: str) -> str:
    """Wrap ``inner`` in a style, surviving nested resets.

    Inner painters (e.g. inline code) emit their own close/reset which would
    otherwise cancel an outer bold/italic for the remainder of the span. We
    re-open ``open_seq`` after every embedded close so the outer emphasis stays
    applied around nested spans (e.g. ``**bold `code` more**``). No-op when color
    is disabled (the open sequence is empty).
    """
    if not open_seq:
        return inner
    patched = inner.replace(close_seq, close_seq + open_seq)
    return f"{open_seq}{patched}{close_seq}"


def _highlight_assistant_inline_tokens(text: str) -> str:
    if not text:
        return text

    _bold_open, _bold_close = _style_open_close(_ansi_bold)
    _italic_open, _italic_close = _style_open_close(_ansi_italic)

    def _paint_bold(s: str) -> str:
        # Recurse so nested inline code / italics inside the bold span are also
        # styled, then keep bold applied across their resets.
        inner = _highlight_assistant_inline_tokens(s[2:-2])
        return _wrap_style_over_inner(_bold_open, _bold_close, inner)

    def _paint_italic(s: str) -> str:
        inner = _highlight_assistant_inline_tokens(s[1:-1])
        return _wrap_style_over_inner(_italic_open, _italic_close, inner)

    rules: List[Tuple[Pattern[str], Callable[[str], str]]] = [
        # Emphasis first so a bold/italic span that *contains* inline code is
        # recognised as a whole (its body is highlighted recursively); otherwise
        # the inner code span would be painted and the surrounding ** markers
        # would be left raw because their region was already "occupied".
        (re.compile(r"\*\*(?=\S)(?:[^*\n]|\*(?!\*))+?(?<=\S)\*\*"), _paint_bold),
        (re.compile(r"(?<![A-Za-z0-9_])__(?=\S)[^_\n]+?(?<=\S)__(?![A-Za-z0-9_])"), _paint_bold),
        # ``*italic*`` / ``_italic_`` (avoid bare ``*`` bullets and snake_case).
        (re.compile(r"\*(?=\S)(?:[^*\n])+?(?<=\S)\*"), _paint_italic),
        (re.compile(r"(?<![A-Za-z0-9_])_(?=\S)[^_\n]+?(?<=\S)_(?![A-Za-z0-9_])"), _paint_italic),
        # Standalone inline code (outside any emphasis span). Backticks kept.
        (re.compile(r"``[^`\n]+``"), _ansi_cyan),
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
