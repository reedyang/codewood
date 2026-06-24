import difflib
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


class ChangePreviewFormatter:
    ANSI_RESET = "\x1b[0m"
    ANSI_GRAY = "\x1b[90m"
    ANSI_BG_RED = "\x1b[41m"
    ANSI_BG_GREEN = "\x1b[42m"
    ANSI_ITALIC_GRAY = "\x1b[3;90m"
    # Subtle changed-line background tints (256-color: dark red / dark green).
    # Unlike the bright ANSI_BG_RED/GREEN, these stay dark enough that the
    # syntax-highlighted foreground colors painted on top remain clearly
    # readable, satisfying the "text vs background must be distinguishable"
    # requirement for changed lines.
    ANSI_BG_DEL = "\x1b[48;5;52m"
    ANSI_BG_ADD = "\x1b[48;5;22m"

    # Map common file extensions to a syntax-highlighter language id. Used to
    # color the diff code text; falls back to the generic profile when unknown.
    _EXT_LANG = {
        "py": "python", "pyi": "python",
        "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
        "ts": "typescript", "tsx": "typescript",
        "c": "c", "h": "c",
        "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "hxx": "cpp",
        "cs": "csharp", "java": "java", "go": "go", "rs": "rust",
        "rb": "ruby", "php": "php", "swift": "swift", "kt": "kotlin", "kts": "kotlin",
        "sql": "sql", "sh": "bash", "bash": "bash", "zsh": "bash",
        "ps1": "powershell", "psm1": "powershell",
        "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml",
        "ini": "ini", "cfg": "ini", "conf": "ini",
        "html": "html", "htm": "html", "xml": "html", "svg": "html", "vue": "html",
        "css": "css", "scss": "css", "less": "css",
        "dockerfile": "dockerfile",
    }

    @staticmethod
    def language_from_path(path: Any) -> Optional[str]:
        """Best-effort syntax-highlighter language id from a file path."""
        try:
            name = str(path or "")
        except Exception:
            return None
        if not name:
            return None
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if base.lower() == "dockerfile":
            return "dockerfile"
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        return ChangePreviewFormatter._EXT_LANG.get(ext)

    @staticmethod
    def _highlight_code(text: str, code_language: Optional[str]) -> Optional[str]:
        """Return ``text`` syntax-highlighted with ANSI color, or None when no
        language is given or highlighting is unavailable/disabled."""
        if not code_language or not text:
            return None
        try:
            from .syntax_highlighter import highlight_code
            from .console_utils import _stdout_color_enabled

            if not _stdout_color_enabled():
                return None
            rendered = highlight_code(text, code_language)
            return rendered if rendered != text else rendered
        except Exception:
            return None

    @staticmethod
    def _colored_chunks(
        plain_chunks: List[str], full_text: str, code_language: Optional[str], max_width: int
    ) -> List[str]:
        """Return per-chunk colored text aligned 1:1 with ``plain_chunks``.

        The full line is highlighted once and then sliced ANSI-aware at the same
        visible-width boundaries, so a token (string/comment) that wraps keeps
        its color on the continuation chunk. Falls back to the plain chunks when
        highlighting is unavailable or yields a different chunk count."""
        highlighted = ChangePreviewFormatter._highlight_code(full_text, code_language)
        if highlighted is None or highlighted == full_text:
            return list(plain_chunks)
        colored = ChangePreviewFormatter._slice_ansi_by_display_width(highlighted, max_width)
        if len(colored) != len(plain_chunks):
            # Boundary mismatch (rare; e.g. tab/width edge cases) — stay safe.
            return list(plain_chunks)
        return colored

    @staticmethod
    def _apply_bg(highlighted: str, bg: str) -> str:
        """Overlay background ``bg`` on already-highlighted ANSI ``text`` while
        preserving its syntax foreground colors. Each embedded reset (\\x1b[0m)
        is re-armed with the background so the tint spans the whole cell."""
        if not bg:
            return highlighted
        reset = ChangePreviewFormatter.ANSI_RESET
        re_armed = highlighted.replace(reset, reset + bg)
        return f"{bg}{re_armed}{reset}"

    @staticmethod
    def format_side_by_side(
        old_lines: List[str],
        new_lines: List[str],
        old_start_line: int = 1,
        new_start_line: int = 1,
        preview_text_max_width: int = 72,
    ) -> List[str]:
        raw_rows = ChangePreviewFormatter._build_raw_rows(
            old_lines=old_lines,
            new_lines=new_lines,
            old_start_line=old_start_line,
            new_start_line=new_start_line,
        )
        return ChangePreviewFormatter._render_raw_rows(raw_rows, preview_text_max_width)

    @staticmethod
    def format_side_by_side_segments(
        segments: List[Dict[str, object]],
        preview_text_max_width: int = 72,
        language: Any = None,
        code_language: Optional[str] = None,
    ) -> List[str]:
        from .localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text as _text

        lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]] = []
        prev_old_end: Optional[int] = None
        prev_new_end: Optional[int] = None

        for seg in segments:
            old_lines = list(seg.get("old_lines") or [])
            new_lines = list(seg.get("new_lines") or [])
            old_start_line = int(seg.get("old_start_line") or 1)
            new_start_line = int(seg.get("new_start_line") or 1)
            if raw_rows and (prev_old_end is not None or prev_new_end is not None):
                old_gap = 0
                new_gap = 0
                if prev_old_end is not None and old_start_line > (prev_old_end + 1):
                    old_gap = old_start_line - prev_old_end - 1
                if prev_new_end is not None and new_start_line > (prev_new_end + 1):
                    new_gap = new_start_line - prev_new_end - 1
                omitted = max(old_gap, new_gap)
                if omitted > 0:
                    marker = _text(
                        "output.omitted_lines",
                        lang,
                        fallback="... omitted {count} lines ...",
                        count=omitted,
                    )
                    raw_rows.append((" ", None, marker, " ", None, marker))

            seg_rows = ChangePreviewFormatter._build_raw_rows(
                old_lines=old_lines,
                new_lines=new_lines,
                old_start_line=old_start_line,
                new_start_line=new_start_line,
            )
            raw_rows.extend(seg_rows)
            for _lm, old_no, _ot, _rm, new_no, _nt in seg_rows:
                if old_no is not None:
                    prev_old_end = old_no
                if new_no is not None:
                    prev_new_end = new_no

        return ChangePreviewFormatter._render_raw_rows(
            raw_rows, preview_text_max_width, code_language=code_language
        )

    # Minimum terminal width (columns) needed for the two-column side-by-side
    # layout to stay readable. Below this we fall back to the inline (unified)
    # layout. Two columns each show ~SIDE_BY_SIDE_COL_TEXT_WIDTH chars of text
    # plus a "M NNNN│ " prefix (~8) and a " ││ " separator (4):
    #   2 * (8 + 36) + 4  ≈ 92  -> round up for breathing room.
    SIDE_BY_SIDE_MIN_TERMINAL_WIDTH = 100
    SIDE_BY_SIDE_COL_TEXT_WIDTH = 72
    INLINE_MIN_TEXT_WIDTH = 24

    # prompt_toolkit style class names for the fragment renderers. They mirror
    # the ANSI palette above and are registered in the selector's Style.
    PT_STYLE_GRAY = "class:diff.gray"
    PT_STYLE_DEL = "class:diff.del"
    PT_STYLE_ADD = "class:diff.add"
    PT_STYLE_OMITTED = "class:diff.omitted"
    PT_STYLE_SEP = "class:diff.sep"

    @staticmethod
    def _segments_to_raw_rows(
        segments: List[Dict[str, object]],
        language: Any = None,
    ) -> List[Tuple[str, Optional[int], str, str, Optional[int], str]]:
        """Build the unified raw-row model from multi-hunk segments.

        Shared by every renderer (ANSI side-by-side / inline and their
        prompt_toolkit fragment variants). Inserts an omitted-lines marker row
        between non-adjacent hunks.
        """
        from .localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text as _text

        lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]] = []
        prev_old_end: Optional[int] = None
        prev_new_end: Optional[int] = None
        for seg in segments:
            old_lines = list(seg.get("old_lines") or [])
            new_lines = list(seg.get("new_lines") or [])
            old_start_line = int(seg.get("old_start_line") or 1)
            new_start_line = int(seg.get("new_start_line") or 1)
            if raw_rows and (prev_old_end is not None or prev_new_end is not None):
                old_gap = 0
                new_gap = 0
                if prev_old_end is not None and old_start_line > (prev_old_end + 1):
                    old_gap = old_start_line - prev_old_end - 1
                if prev_new_end is not None and new_start_line > (prev_new_end + 1):
                    new_gap = new_start_line - prev_new_end - 1
                omitted = max(old_gap, new_gap)
                if omitted > 0:
                    marker = _text(
                        "output.omitted_lines",
                        lang,
                        fallback="... omitted {count} lines ...",
                        count=omitted,
                    )
                    raw_rows.append((" ", None, marker, " ", None, marker))
            seg_rows = ChangePreviewFormatter._build_raw_rows(
                old_lines=old_lines,
                new_lines=new_lines,
                old_start_line=old_start_line,
                new_start_line=new_start_line,
            )
            raw_rows.extend(seg_rows)
            for _lm, old_no, _ot, _rm, new_no, _nt in seg_rows:
                if old_no is not None:
                    prev_old_end = old_no
                if new_no is not None:
                    prev_new_end = new_no
        return raw_rows

    @staticmethod
    def format_segments_structured(
        segments: List[Dict[str, object]],
        language: Any = None,
    ) -> List[Dict[str, object]]:
        """Convert multi-hunk segments into JSON-serializable diff rows.

        Each row is ``{"type", "oldNo", "newNo", "oldText", "newText"}`` where
        ``type`` is one of ``"context" | "del" | "add" | "omitted"``. The GUI
        consumes this to render the change preview natively (responsive
        side-by-side / inline) with syntax highlighting, instead of the
        pre-rendered ANSI text. No truncation/wrapping is applied here; layout
        is the frontend's responsibility.
        """
        raw_rows = ChangePreviewFormatter._segments_to_raw_rows(segments, language=language)
        rows: List[Dict[str, object]] = []
        for left_mark, old_no, old_text, right_mark, new_no, new_text in raw_rows:
            is_omitted = (
                old_no is None
                and new_no is None
                and old_text == new_text
                and str(old_text).startswith("... omitted ")
            )
            if is_omitted:
                rows.append(
                    {
                        "type": "omitted",
                        "oldNo": None,
                        "newNo": None,
                        "oldText": ChangePreviewFormatter._norm(old_text),
                        "newText": ChangePreviewFormatter._norm(new_text),
                    }
                )
                continue
            if left_mark == "-" and right_mark == "+":
                row_type = "change"
            elif left_mark == "-":
                row_type = "del"
            elif right_mark == "+":
                row_type = "add"
            else:
                row_type = "context"
            rows.append(
                {
                    "type": row_type,
                    "oldNo": old_no,
                    "newNo": new_no,
                    "oldText": ChangePreviewFormatter._norm(old_text),
                    "newText": ChangePreviewFormatter._norm(new_text),
                }
            )
        return rows

    @staticmethod
    def format_segments_responsive_fragments(
        segments: List[Dict[str, object]],
        terminal_width: Optional[int] = None,
        language: Any = None,
        code_language: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """Like :meth:`format_segments_responsive` but emit prompt_toolkit
        ``(style, text)`` fragments instead of an ANSI string list.

        Used by the TUI confirmation selector so the embedded diff preview
        re-lays-out automatically on terminal resize (prompt_toolkit re-invokes
        the fragment provider on every repaint). ``code_language`` (when given)
        syntax-highlights the diff code text via ANSI->fragment conversion.
        """
        try:
            width = int(terminal_width or 0)
        except Exception:
            width = 0
        raw_rows = ChangePreviewFormatter._segments_to_raw_rows(segments, language=language)
        if width <= 0 or width >= ChangePreviewFormatter.SIDE_BY_SIDE_MIN_TERMINAL_WIDTH:
            col_text_width = ChangePreviewFormatter.SIDE_BY_SIDE_COL_TEXT_WIDTH
            if width > 0:
                usable = max(0, width - (2 * 8) - 4)
                col_text_width = max(
                    ChangePreviewFormatter.INLINE_MIN_TEXT_WIDTH,
                    min(ChangePreviewFormatter.SIDE_BY_SIDE_COL_TEXT_WIDTH, usable // 2),
                )
            return ChangePreviewFormatter._render_raw_rows_fragments(
                raw_rows, col_text_width, code_language=code_language
            )
        inline_text_width = max(
            ChangePreviewFormatter.INLINE_MIN_TEXT_WIDTH,
            width - 9,
        )
        return ChangePreviewFormatter._render_inline_rows_fragments(
            raw_rows, inline_text_width, code_language=code_language
        )

    @staticmethod
    def _ansi_to_fragments(text: str) -> List[Tuple[str, str]]:
        """Convert an ANSI-colored string to prompt_toolkit ``(style, text)``
        fragments. Falls back to a single plain fragment if conversion fails."""
        try:
            from prompt_toolkit.formatted_text import ANSI, to_formatted_text

            return list(to_formatted_text(ANSI(text)))
        except Exception:
            return [("", text)]

    # prompt_toolkit background styles for changed lines, mirroring the
    # subtle dark tints registered in the selector Style (diff.del / diff.add).
    PT_BG_DEL = "bg:#5a1f1f"
    PT_BG_ADD = "bg:#1f5a1f"

    @staticmethod
    def _chunk_fragments(
        plain_chunk: str,
        colored_chunk: str,
        fallback_style: str,
        bg_style: str = "",
    ) -> List[Tuple[str, str]]:
        """Return PT fragments for one cell chunk. ``colored_chunk`` is the
        already-highlighted ANSI text (sliced ANSI-aware so wrapped tokens keep
        color); when it differs from the plain text it is converted to
        fragments, else a single ``(fallback_style, plain_chunk)`` is used.
        ``bg_style`` (when set) is composed onto every fragment so a changed
        line keeps its background tint with syntax foreground colors."""
        if colored_chunk and colored_chunk != plain_chunk:
            frags = ChangePreviewFormatter._ansi_to_fragments(colored_chunk)
            if bg_style:
                frags = [
                    (f"{bg_style} {style}".strip(), txt) for style, txt in frags
                ]
            return frags
        if bg_style:
            return [(f"{bg_style} {fallback_style}".strip(), plain_chunk)]
        return [(fallback_style, plain_chunk)]

    @staticmethod
    def _render_raw_rows_fragments(
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]],
        preview_text_max_width: int,
        code_language: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        max_old_no = 0
        max_new_no = 0
        for _lm, old_no, _ot, _rm, new_no, _nt in raw_rows:
            if old_no is not None:
                max_old_no = max(max_old_no, old_no)
            if new_no is not None:
                max_new_no = max(max_new_no, new_no)
        old_no_w = max(4, len(str(max_old_no or 0)))
        new_no_w = max(4, len(str(max_new_no or 0)))

        # Pre-compute wrapped rows and the left column width for alignment.
        wrapped: List[Tuple[str, str, str, str, str, bool]] = []
        left_col_width = 0
        for left_mark, old_no, old_text, right_mark, new_no, new_text in raw_rows:
            old_no_s = (" " * old_no_w) if old_no is None else f"{old_no:>{old_no_w}}"
            new_no_s = (" " * new_no_w) if new_no is None else f"{new_no:>{new_no_w}}"
            left_prefix = f"{left_mark} {old_no_s}│ "
            right_prefix = f"{right_mark} {new_no_s}│ "
            left_cont = f"{' ' * (2 + old_no_w)}│ "
            right_cont = f"{' ' * (2 + new_no_w)}│ "
            old_norm = ChangePreviewFormatter._norm(old_text)
            new_norm = ChangePreviewFormatter._norm(new_text)
            left_chunks = ChangePreviewFormatter._slice_by_display_width(
                old_norm, preview_text_max_width
            )
            right_chunks = ChangePreviewFormatter._slice_by_display_width(
                new_norm, preview_text_max_width
            )
            is_omitted = (
                old_no is None
                and new_no is None
                and old_text == new_text
                and str(old_text).startswith("... omitted ")
            )
            if is_omitted:
                left_colored = list(left_chunks)
                right_colored = list(right_chunks)
            else:
                left_colored = ChangePreviewFormatter._colored_chunks(
                    left_chunks, old_norm, code_language, preview_text_max_width
                )
                right_colored = ChangePreviewFormatter._colored_chunks(
                    right_chunks, new_norm, code_language, preview_text_max_width
                )
            for idx in range(max(len(left_chunks), len(right_chunks))):
                lc = left_chunks[idx] if idx < len(left_chunks) else ""
                rc = right_chunks[idx] if idx < len(right_chunks) else ""
                lcc = left_colored[idx] if idx < len(left_colored) else ""
                rcc = right_colored[idx] if idx < len(right_colored) else ""
                lp = left_prefix if idx == 0 else left_cont
                rp = right_prefix if idx == 0 else right_cont
                left_col_width = max(
                    left_col_width, ChangePreviewFormatter._display_width(f"{lp}{lc}")
                )
                wrapped.append((f"{left_mark}{right_mark}", lp, lc, lcc, rp, rc, rcc, is_omitted))

        frags: List[Tuple[str, str]] = []
        for mark_pair, lp, lc, lcc, rp, rc, rcc, is_omitted in wrapped:
            pad = max(0, left_col_width - ChangePreviewFormatter._display_width(f"{lp}{lc}"))
            pad_spaces = " " * pad
            frags.append((ChangePreviewFormatter.PT_STYLE_GRAY, lp))
            if is_omitted:
                frags.append((ChangePreviewFormatter.PT_STYLE_OMITTED, lc + pad_spaces))
            elif "-" in mark_pair:
                # Fill the whole padded cell with the del tint, even on wrapped
                # continuation rows where this side is empty, so the background
                # block and the vertical separators stay continuous.
                frags.extend(
                    ChangePreviewFormatter._chunk_fragments(
                        lc + pad_spaces,
                        (lcc + pad_spaces) if lcc else "",
                        ChangePreviewFormatter.PT_STYLE_DEL,
                        bg_style=ChangePreviewFormatter.PT_BG_DEL,
                    )
                )
            else:
                frags.extend(
                    ChangePreviewFormatter._chunk_fragments(lc + pad_spaces, lcc, "")
                )
            frags.append((ChangePreviewFormatter.PT_STYLE_SEP, " ││ "))
            frags.append((ChangePreviewFormatter.PT_STYLE_GRAY, rp))
            if is_omitted:
                frags.append((ChangePreviewFormatter.PT_STYLE_OMITTED, rc))
            elif "+" in mark_pair:
                frags.extend(
                    ChangePreviewFormatter._chunk_fragments(
                        rc,
                        rcc,
                        ChangePreviewFormatter.PT_STYLE_ADD,
                        bg_style=ChangePreviewFormatter.PT_BG_ADD,
                    )
                )
            else:
                frags.extend(
                    ChangePreviewFormatter._chunk_fragments(rc, rcc, "")
                )
            frags.append(("", "\n"))
        return frags

    @staticmethod
    def _render_inline_rows_fragments(
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]],
        preview_text_max_width: int,
        code_language: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        max_no = 0
        for _lm, old_no, _ot, _rm, new_no, _nt in raw_rows:
            for n in (old_no, new_no):
                if n is not None:
                    max_no = max(max_no, n)
        no_w = max(4, len(str(max_no or 0)))
        frags: List[Tuple[str, str]] = []

        def _emit(mark: str, no: Optional[int], textval: str, style: str, omitted: bool, bg: str) -> None:
            no_s = (" " * no_w) if no is None else f"{no:>{no_w}}"
            prefix = f"{mark} {no_s}│ "
            cont = f"{' ' * (2 + no_w)}│ "
            normalized = ChangePreviewFormatter._norm(textval)
            chunks = ChangePreviewFormatter._slice_by_display_width(
                normalized, preview_text_max_width
            )
            if omitted:
                colored = list(chunks)
            else:
                colored = ChangePreviewFormatter._colored_chunks(
                    chunks, normalized, code_language, preview_text_max_width
                )
            for idx, chunk in enumerate(chunks):
                pfx = prefix if idx == 0 else cont
                chunk_c = colored[idx] if idx < len(colored) else chunk
                frags.append((ChangePreviewFormatter.PT_STYLE_GRAY, pfx))
                if omitted:
                    frags.append((ChangePreviewFormatter.PT_STYLE_OMITTED, chunk))
                elif style and chunk:
                    frags.extend(
                        ChangePreviewFormatter._chunk_fragments(
                            chunk, chunk_c, style, bg_style=bg
                        )
                    )
                else:
                    frags.extend(
                        ChangePreviewFormatter._chunk_fragments(chunk, chunk_c, "")
                    )
                frags.append(("", "\n"))

        for left_mark, old_no, old_text, right_mark, new_no, new_text in raw_rows:
            omitted = (
                old_no is None
                and new_no is None
                and str(old_text).startswith("... omitted ")
            )
            if omitted:
                _emit("~", None, old_text, "", True, "")
                continue
            if left_mark == "=" and right_mark == "=":
                _emit(" ", new_no, new_text, "", False, "")
                continue
            if left_mark == "-":
                _emit("-", old_no, old_text, ChangePreviewFormatter.PT_STYLE_DEL, False, ChangePreviewFormatter.PT_BG_DEL)
            if right_mark == "+":
                _emit("+", new_no, new_text, ChangePreviewFormatter.PT_STYLE_ADD, False, ChangePreviewFormatter.PT_BG_ADD)
        return frags

    @staticmethod
    def format_segments_responsive(
        segments: List[Dict[str, object]],
        terminal_width: Optional[int] = None,
        language: Any = None,
        code_language: Optional[str] = None,
    ) -> List[str]:
        """Render change-preview segments, choosing the layout by terminal width.

        Wide terminals get the two-column side-by-side diff; narrow terminals
        fall back to an inline (unified) diff so neither column is squeezed to
        an unreadable width. ``terminal_width`` defaults to a wide value so
        callers that can't measure the terminal keep the original behavior.
        ``code_language`` (when given) syntax-highlights the diff code text.
        """
        try:
            width = int(terminal_width or 0)
        except Exception:
            width = 0
        if width <= 0 or width >= ChangePreviewFormatter.SIDE_BY_SIDE_MIN_TERMINAL_WIDTH:
            # Size each column to the available width when known, capped at the
            # comfortable default so very wide terminals don't stretch forever.
            col_text_width = ChangePreviewFormatter.SIDE_BY_SIDE_COL_TEXT_WIDTH
            if width > 0:
                # Two prefixes (~8 each) + separator (4) overhead between cols.
                usable = max(0, width - (2 * 8) - 4)
                col_text_width = max(
                    ChangePreviewFormatter.INLINE_MIN_TEXT_WIDTH,
                    min(ChangePreviewFormatter.SIDE_BY_SIDE_COL_TEXT_WIDTH, usable // 2),
                )
            return ChangePreviewFormatter.format_side_by_side_segments(
                segments,
                preview_text_max_width=col_text_width,
                language=language,
                code_language=code_language,
            )
        # Narrow terminal: inline unified layout.
        inline_text_width = max(
            ChangePreviewFormatter.INLINE_MIN_TEXT_WIDTH,
            width - 9,  # "M NNNN│ " prefix is ~8 cols; leave 1 for safety.
        )
        return ChangePreviewFormatter.format_inline_segments(
            segments,
            preview_text_max_width=inline_text_width,
            language=language,
            code_language=code_language,
        )

    @staticmethod
    def format_inline_segments(
        segments: List[Dict[str, object]],
        preview_text_max_width: int = 80,
        language: Any = None,
        code_language: Optional[str] = None,
    ) -> List[str]:
        """Render segments as a single-column unified diff (for narrow widths).

        Deleted lines (carrying the old line number) precede inserted lines
        (carrying the new line number); unchanged context lines show both via
        the new number. Omitted-line markers separate non-adjacent hunks, the
        same way the side-by-side renderer does.
        """
        from .localization import DEFAULT_DISPLAY_LANGUAGE, normalize_display_language, text as _text

        lang = normalize_display_language(language) or DEFAULT_DISPLAY_LANGUAGE
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]] = []
        prev_old_end: Optional[int] = None
        prev_new_end: Optional[int] = None

        for seg in segments:
            old_lines = list(seg.get("old_lines") or [])
            new_lines = list(seg.get("new_lines") or [])
            old_start_line = int(seg.get("old_start_line") or 1)
            new_start_line = int(seg.get("new_start_line") or 1)
            if raw_rows and (prev_old_end is not None or prev_new_end is not None):
                old_gap = 0
                new_gap = 0
                if prev_old_end is not None and old_start_line > (prev_old_end + 1):
                    old_gap = old_start_line - prev_old_end - 1
                if prev_new_end is not None and new_start_line > (prev_new_end + 1):
                    new_gap = new_start_line - prev_new_end - 1
                omitted = max(old_gap, new_gap)
                if omitted > 0:
                    marker = _text(
                        "output.omitted_lines",
                        lang,
                        fallback="... omitted {count} lines ...",
                        count=omitted,
                    )
                    raw_rows.append(("~", None, marker, "~", None, marker))

            seg_rows = ChangePreviewFormatter._build_raw_rows(
                old_lines=old_lines,
                new_lines=new_lines,
                old_start_line=old_start_line,
                new_start_line=new_start_line,
            )
            raw_rows.extend(seg_rows)
            for _lm, old_no, _ot, _rm, new_no, _nt in seg_rows:
                if old_no is not None:
                    prev_old_end = old_no
                if new_no is not None:
                    prev_new_end = new_no

        return ChangePreviewFormatter._render_inline_rows(
            raw_rows, preview_text_max_width, code_language=code_language
        )

    @staticmethod
    def _render_inline_rows(
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]],
        preview_text_max_width: int,
        code_language: Optional[str] = None,
    ) -> List[str]:
        max_no = 0
        for _lm, old_no, _ot, _rm, new_no, _nt in raw_rows:
            for n in (old_no, new_no):
                if n is not None:
                    max_no = max(max_no, n)
        no_w = max(4, len(str(max_no or 0)))

        out: List[str] = []

        def _emit(mark: str, no: Optional[int], textval: str, bg: str, omitted: bool) -> None:
            no_s = (" " * no_w) if no is None else f"{no:>{no_w}}"
            prefix = f"{mark} {no_s}│ "
            cont_prefix = f"{' ' * (2 + no_w)}│ "
            normalized = ChangePreviewFormatter._norm(textval)
            chunks = ChangePreviewFormatter._slice_by_display_width(
                normalized, preview_text_max_width
            )
            if omitted:
                colored = list(chunks)
            else:
                colored = ChangePreviewFormatter._colored_chunks(
                    chunks, normalized, code_language, preview_text_max_width
                )
            for idx, chunk in enumerate(chunks):
                pfx = prefix if idx == 0 else cont_prefix
                pfx_colored = f"{ChangePreviewFormatter.ANSI_GRAY}{pfx}{ChangePreviewFormatter.ANSI_RESET}"
                chunk_c = colored[idx] if idx < len(colored) else chunk
                if omitted:
                    body = f"{ChangePreviewFormatter.ANSI_ITALIC_GRAY}{chunk}{ChangePreviewFormatter.ANSI_RESET}"
                elif bg and chunk:
                    body = ChangePreviewFormatter._apply_bg(chunk_c, bg)
                else:
                    body = chunk_c
                out.append(f"{pfx_colored}{body}")

        for left_mark, old_no, old_text, right_mark, new_no, new_text in raw_rows:
            omitted = (
                old_no is None
                and new_no is None
                and str(old_text).startswith("... omitted ")
            )
            if omitted:
                _emit("~", None, old_text, "", True)
                continue
            if left_mark == "=" and right_mark == "=":
                # Unchanged context line: show once with the new line number.
                _emit(" ", new_no, new_text, "", False)
                continue
            # Replace/delete/insert: deleted line(s) first, then inserted.
            if left_mark == "-":
                _emit("-", old_no, old_text, ChangePreviewFormatter.ANSI_BG_DEL, False)
            if right_mark == "+":
                _emit("+", new_no, new_text, ChangePreviewFormatter.ANSI_BG_ADD, False)
        return out

    @staticmethod
    def _build_raw_rows(
        old_lines: List[str],
        new_lines: List[str],
        old_start_line: int,
        new_start_line: int,
    ) -> List[Tuple[str, Optional[int], str, str, Optional[int], str]]:
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]] = []
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    raw_rows.append(
                        ("=", old_start_line + oi, old_lines[oi], "=", new_start_line + nj, new_lines[nj])
                    )
            elif tag == "delete":
                for oi in range(i1, i2):
                    raw_rows.append(("-", old_start_line + oi, old_lines[oi], " ", None, ""))
            elif tag == "insert":
                for nj in range(j1, j2):
                    raw_rows.append((" ", None, "", "+", new_start_line + nj, new_lines[nj]))
            else:  # replace
                old_count = i2 - i1
                new_count = j2 - j1
                row_count = max(old_count, new_count)
                for idx in range(row_count):
                    has_old = idx < old_count
                    has_new = idx < new_count
                    left_mark = "-" if has_old else " "
                    right_mark = "+" if has_new else " "
                    old_no = (old_start_line + i1 + idx) if has_old else None
                    new_no = (new_start_line + j1 + idx) if has_new else None
                    old_text = old_lines[i1 + idx] if has_old else ""
                    new_text = new_lines[j1 + idx] if has_new else ""
                    raw_rows.append((left_mark, old_no, old_text, right_mark, new_no, new_text))
        return raw_rows

    @staticmethod
    def _render_raw_rows(
        raw_rows: List[Tuple[str, Optional[int], str, str, Optional[int], str]],
        preview_text_max_width: int,
        code_language: Optional[str] = None,
    ) -> List[str]:
        max_old_no = 0
        max_new_no = 0
        for _lm, old_no, _ot, _rm, new_no, _nt in raw_rows:
            if old_no is not None:
                max_old_no = max(max_old_no, old_no)
            if new_no is not None:
                max_new_no = max(max_new_no, new_no)
        old_no_w = max(4, len(str(max_old_no or 0)))
        new_no_w = max(4, len(str(max_new_no or 0)))

        wrapped_rows: List[Tuple[str, str, str, str, str, bool]] = []
        left_col_width = 0

        for left_mark, old_no, old_text, right_mark, new_no, new_text in raw_rows:
            old_no_s = (" " * old_no_w) if old_no is None else f"{old_no:>{old_no_w}}"
            new_no_s = (" " * new_no_w) if new_no is None else f"{new_no:>{new_no_w}}"
            left_prefix = f"{left_mark} {old_no_s}│ "
            right_prefix = f"{right_mark} {new_no_s}│ "
            left_cont_prefix = f"{' ' * (2 + old_no_w)}│ "
            right_cont_prefix = f"{' ' * (2 + new_no_w)}│ "
            old_norm = ChangePreviewFormatter._norm(old_text)
            new_norm = ChangePreviewFormatter._norm(new_text)
            left_chunks = ChangePreviewFormatter._slice_by_display_width(
                old_norm, preview_text_max_width
            )
            right_chunks = ChangePreviewFormatter._slice_by_display_width(
                new_norm, preview_text_max_width
            )
            is_omitted_row = (
                old_no is None
                and new_no is None
                and old_text == new_text
                and str(old_text).startswith("... omitted ")
            )
            # Highlight each side's FULL line once, then slice the colored text
            # at the same width boundaries so a wrapped token keeps its color.
            if is_omitted_row:
                left_colored = list(left_chunks)
                right_colored = list(right_chunks)
            else:
                left_colored = ChangePreviewFormatter._colored_chunks(
                    left_chunks, old_norm, code_language, preview_text_max_width
                )
                right_colored = ChangePreviewFormatter._colored_chunks(
                    right_chunks, new_norm, code_language, preview_text_max_width
                )
            row_count = max(len(left_chunks), len(right_chunks))
            for idx in range(row_count):
                left_chunk = left_chunks[idx] if idx < len(left_chunks) else ""
                right_chunk = right_chunks[idx] if idx < len(right_chunks) else ""
                left_chunk_c = left_colored[idx] if idx < len(left_colored) else ""
                right_chunk_c = right_colored[idx] if idx < len(right_colored) else ""
                left_prefix_part = left_prefix if idx == 0 else left_cont_prefix
                right_prefix_part = right_prefix if idx == 0 else right_cont_prefix
                left_segment = f"{left_prefix_part}{left_chunk}"
                left_col_width = max(
                    left_col_width, ChangePreviewFormatter._display_width(left_segment)
                )
                wrapped_rows.append(
                    (
                        f"{left_mark}{right_mark}",
                        left_prefix_part,
                        left_chunk,
                        left_chunk_c,
                        right_prefix_part,
                        right_chunk,
                        right_chunk_c,
                        is_omitted_row,
                    )
                )

        rows: List[str] = []
        gray_sep = f"{ChangePreviewFormatter.ANSI_GRAY} ││ {ChangePreviewFormatter.ANSI_RESET}"
        for (
            mark_pair,
            left_prefix_part,
            left_chunk,
            left_chunk_c,
            right_prefix_part,
            right_chunk,
            right_chunk_c,
            is_omitted_row,
        ) in wrapped_rows:
            left_prefix_plain = left_prefix_part
            # Pad BOTH columns to the same content width so every changed-line
            # background fills a solid, uniform rectangle. Ragged-width fills
            # are what make the vertical separators look "broken".
            left_plain_full = f"{left_prefix_part}{left_chunk}"
            left_pad = max(0, left_col_width - ChangePreviewFormatter._display_width(left_plain_full))
            left_chunk_c_padded = left_chunk_c + (" " * left_pad)

            left_prefix_colored = f"{ChangePreviewFormatter.ANSI_GRAY}{left_prefix_plain}{ChangePreviewFormatter.ANSI_RESET}"
            right_prefix_colored = f"{ChangePreviewFormatter.ANSI_GRAY}{right_prefix_part}{ChangePreviewFormatter.ANSI_RESET}"
            if is_omitted_row:
                left_chunk_colored = f"{ChangePreviewFormatter.ANSI_ITALIC_GRAY}{left_chunk_c_padded}{ChangePreviewFormatter.ANSI_RESET}"
                right_chunk_colored = f"{ChangePreviewFormatter.ANSI_ITALIC_GRAY}{right_chunk_c}{ChangePreviewFormatter.ANSI_RESET}"
            else:
                left_chunk_colored = left_chunk_c_padded
                right_chunk_colored = right_chunk_c
                # Paint the changed-line background across the whole content
                # cell (code + trailing pad), even on wrapped continuation rows
                # where this side's chunk is empty, so the tint stays a solid
                # block and the gutter/separator columns read as continuous.
                if "-" in mark_pair:
                    left_chunk_colored = ChangePreviewFormatter._apply_bg(
                        left_chunk_colored, ChangePreviewFormatter.ANSI_BG_DEL
                    )
                if "+" in mark_pair:
                    right_chunk_colored = ChangePreviewFormatter._apply_bg(
                        right_chunk_colored, ChangePreviewFormatter.ANSI_BG_ADD
                    )

            left_rendered = f"{left_prefix_colored}{left_chunk_colored}"
            right_rendered = f"{right_prefix_colored}{right_chunk_colored}"
            rows.append(f"{left_rendered}{gray_sep}{right_rendered}")
        return rows

    @staticmethod
    def _norm(s: str) -> str:
        return str(s).expandtabs(4)

    @staticmethod
    def _display_width(s: str) -> int:
        width = 0
        for ch in s:
            if unicodedata.combining(ch):
                continue
            width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return width

    @staticmethod
    def _pad_to_width(s: str, target: int) -> str:
        pad = max(0, target - ChangePreviewFormatter._display_width(s))
        return s + (" " * pad)

    # Matches a single ANSI SGR escape (e.g. ``\x1b[38;2;1;2;3m`` or ``\x1b[0m``).
    _SGR_RE = None  # lazily compiled in _slice_ansi_by_display_width

    @staticmethod
    def _slice_ansi_by_display_width(ansi_text: str, max_width: int) -> List[str]:
        """Slice ANSI-colored ``ansi_text`` into chunks no wider than
        ``max_width`` visible columns, preserving color across wrap boundaries.

        ANSI SGR escapes do not count toward width. Each emitted chunk is
        self-contained: it re-arms the active SGR state at its start (so a long
        highlighted token keeps its color after a wrap) and appends a reset at
        its end. Returns ``[""]`` for empty input."""
        import re as _re

        if ChangePreviewFormatter._SGR_RE is None:
            ChangePreviewFormatter._SGR_RE = _re.compile(r"\x1b\[[0-9;]*m")
        sgr_re = ChangePreviewFormatter._SGR_RE
        reset = ChangePreviewFormatter.ANSI_RESET
        if max_width <= 0:
            return [ansi_text]
        if not ansi_text:
            return [""]

        chunks: List[str] = []
        current: List[str] = []
        current_w = 0
        active: List[str] = []  # currently-active SGR codes (in order)
        i = 0
        n = len(ansi_text)

        def _start_chunk() -> None:
            current.clear()
            if active:
                current.append("".join(active))

        def _flush_chunk() -> None:
            if not current:
                return
            text = "".join(current)
            if active:
                text += reset
            chunks.append(text)

        _start_chunk()
        while i < n:
            m = sgr_re.match(ansi_text, i)
            if m:
                code = m.group(0)
                if code in (reset, "\x1b[m"):
                    active = []
                else:
                    active.append(code)
                current.append(code)
                i = m.end()
                continue
            ch = ansi_text[i]
            ch_w = (
                0
                if unicodedata.combining(ch)
                else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
            )
            # Only wrap on a visible char that would overflow (skip when the
            # accumulated visible width is already 0 to avoid empty chunks).
            if current_w > 0 and current_w + ch_w > max_width:
                _flush_chunk()
                current_w = 0
                _start_chunk()
            current.append(ch)
            current_w += ch_w
            i += 1
        _flush_chunk()
        return chunks or [""]

    @staticmethod
    def _slice_by_display_width(s: str, max_width: int) -> List[str]:
        if max_width <= 0:
            return [s]
        if not s:
            return [""]
        chunks: List[str] = []
        current: List[str] = []
        current_w = 0
        for ch in s:
            ch_w = (
                0
                if unicodedata.combining(ch)
                else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
            )
            if current and current_w + ch_w > max_width:
                chunks.append("".join(current))
                current = [ch]
                current_w = ch_w
                continue
            current.append(ch)
            current_w += ch_w
        if current:
            chunks.append("".join(current))
        return chunks or [""]
