import difflib
import unicodedata
from typing import Dict, List, Optional, Tuple


class ChangePreviewFormatter:
    ANSI_RESET = "\x1b[0m"
    ANSI_GRAY = "\x1b[90m"
    ANSI_BG_RED = "\x1b[41m"
    ANSI_BG_GREEN = "\x1b[42m"
    ANSI_ITALIC_GRAY = "\x1b[3;90m"

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
    ) -> List[str]:
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
                    marker = f"... omitted {omitted} lines ..."
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

        return ChangePreviewFormatter._render_raw_rows(raw_rows, preview_text_max_width)

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
            left_chunks = ChangePreviewFormatter._slice_by_display_width(
                ChangePreviewFormatter._norm(old_text), preview_text_max_width
            )
            right_chunks = ChangePreviewFormatter._slice_by_display_width(
                ChangePreviewFormatter._norm(new_text), preview_text_max_width
            )
            is_omitted_row = (
                old_no is None
                and new_no is None
                and old_text == new_text
                and str(old_text).startswith("... omitted ")
            )
            row_count = max(len(left_chunks), len(right_chunks))
            for idx in range(row_count):
                left_chunk = left_chunks[idx] if idx < len(left_chunks) else ""
                right_chunk = right_chunks[idx] if idx < len(right_chunks) else ""
                left_prefix_part = left_prefix if idx == 0 else left_cont_prefix
                right_prefix_part = right_prefix if idx == 0 else right_cont_prefix
                left_segment = f"{left_prefix_part}{left_chunk}"
                right_segment = f"{right_prefix_part}{right_chunk}"
                left_col_width = max(
                    left_col_width, ChangePreviewFormatter._display_width(left_segment)
                )
                wrapped_rows.append(
                    (
                        f"{left_mark}{right_mark}",
                        left_prefix_part,
                        left_chunk,
                        right_prefix_part,
                        right_chunk,
                        is_omitted_row,
                    )
                )

        rows: List[str] = []
        gray_sep = f"{ChangePreviewFormatter.ANSI_GRAY} ││ {ChangePreviewFormatter.ANSI_RESET}"
        for mark_pair, left_prefix_part, left_chunk, right_prefix_part, right_chunk, is_omitted_row in wrapped_rows:
            left_plain = f"{left_prefix_part}{left_chunk}"
            left_padded = ChangePreviewFormatter._pad_to_width(left_plain, left_col_width)
            left_prefix_len = len(left_prefix_part)
            left_prefix_plain = left_padded[:left_prefix_len]
            left_chunk_plain = left_padded[left_prefix_len:]

            left_prefix_colored = f"{ChangePreviewFormatter.ANSI_GRAY}{left_prefix_plain}{ChangePreviewFormatter.ANSI_RESET}"
            right_prefix_colored = f"{ChangePreviewFormatter.ANSI_GRAY}{right_prefix_part}{ChangePreviewFormatter.ANSI_RESET}"
            left_chunk_colored = left_chunk_plain
            right_chunk_colored = right_chunk
            if is_omitted_row:
                left_chunk_colored = f"{ChangePreviewFormatter.ANSI_ITALIC_GRAY}{left_chunk_plain}{ChangePreviewFormatter.ANSI_RESET}"
                right_chunk_colored = f"{ChangePreviewFormatter.ANSI_ITALIC_GRAY}{right_chunk}{ChangePreviewFormatter.ANSI_RESET}"
            else:
                if "-" in mark_pair and left_chunk_plain:
                    left_chunk_colored = f"{ChangePreviewFormatter.ANSI_BG_RED}{left_chunk_plain}{ChangePreviewFormatter.ANSI_RESET}"
                if "+" in mark_pair and right_chunk:
                    right_chunk_colored = f"{ChangePreviewFormatter.ANSI_BG_GREEN}{right_chunk}{ChangePreviewFormatter.ANSI_RESET}"

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
