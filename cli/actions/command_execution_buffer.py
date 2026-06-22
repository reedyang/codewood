from __future__ import annotations

from typing import List


class CommandExecutionBuffer:
    """Hold captured command output and truncate it for the model context.

    When the captured output exceeds ``max_chars`` characters, the middle
    portion is elided on line boundaries so that roughly ``target_chars``
    characters remain (head + tail). The elision is reported inline so the
    model knows output was dropped.
    """

    MAX_CHARS = 12000
    TARGET_CHARS = 10000

    def __init__(
        self,
        text: str,
        max_chars: int = MAX_CHARS,
        target_chars: int = TARGET_CHARS,
    ) -> None:
        self._text = str(text or "")
        self._max_chars = max(1, int(max_chars))
        # target must not exceed the max threshold
        self._target_chars = max(1, min(int(target_chars), self._max_chars))

    @property
    def raw(self) -> str:
        return self._text

    def render(self) -> str:
        text = self._text
        if len(text) <= self._max_chars:
            return text

        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        # Reserve space for the header and the omission marker so the final
        # rendered string stays close to the target length.
        header = f"Total output lines: {total_lines}\n"
        budget = max(1, self._target_chars - len(header))

        head_budget = budget // 2
        tail_budget = budget - head_budget

        head_lines: List[str] = []
        head_len = 0
        head_count = 0
        for line in lines:
            if head_len + len(line) > head_budget and head_count > 0:
                break
            head_lines.append(line)
            head_len += len(line)
            head_count += 1

        tail_lines: List[str] = []
        tail_len = 0
        tail_count = 0
        for line in reversed(lines):
            if head_count + tail_count >= total_lines:
                break
            if tail_len + len(line) > tail_budget and tail_count > 0:
                break
            tail_lines.append(line)
            tail_len += len(line)
            tail_count += 1
        tail_lines.reverse()

        omitted_start = head_count + 1
        omitted_end = total_lines - tail_count
        if omitted_end < omitted_start:
            # Nothing left to omit after line accounting; return original.
            return text

        head_text = "".join(head_lines)
        if head_text and not head_text.endswith("\n"):
            head_text += "\n"
        marker = (
            f"... omitted lines {omitted_start} to {omitted_end} "
            f"({omitted_end - omitted_start + 1} lines) ...\n"
        )
        return header + head_text + marker + "".join(tail_lines)
