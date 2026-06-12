"""Full-screen transcript viewer for the chat REPL.

The transcript view is a read-only, single-screen overlay (rendered on the
alternate screen buffer) that lets the user scroll through the whole chat
history, jump between their own messages, and re-open one of them for editing.

It is intentionally self contained: callers pass a list of pre-rendered
blocks and receive back either ``None`` (the user quit) or an action dict such
as ``{"action": "edit", "user_index": 3}``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

try:  # prompt_toolkit is always available where this view is used.
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.formatted_text import (
        ANSI,
        fragment_list_to_text,
        to_formatted_text,
    )
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import ColorDepth
    from prompt_toolkit.styles import Style

    PROMPT_TOOLKIT_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    PROMPT_TOOLKIT_AVAILABLE = False
    ColorDepth = None  # type: ignore[assignment]


_TITLE = "/ T R A N S C R I P T "

_DEFAULT_HELP = {
    "title": "TRANSCRIPT",
    "scroll": "↑/↓ to scroll",
    "page": "pgup/pgdn to page",
    "jump": "home/end to jump",
    "quit": "q to quit",
    "edit_prev_hint": "esc to edit prev",
    "edit_prev": "esc/← to edit prev",
    "edit_next": "→ to edit next",
    "edit_msg": "enter to edit message",
}


class TranscriptView:
    """A read-only, single-screen transcript browser."""

    def __init__(
        self,
        blocks: List[Dict[str, Any]],
        *,
        labels: Optional[Dict[str, str]] = None,
        width_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        self._labels = dict(_DEFAULT_HELP)
        if labels:
            for key, value in labels.items():
                if value:
                    self._labels[str(key)] = str(value)
        self._width_provider = width_provider

        # Flatten blocks into individual display lines while tracking which
        # block (and which navigable user message) each line belongs to.
        self.lines: List[str] = []
        self.line_block: List[int] = []
        self.block_first_line: Dict[int, int] = {}
        # Ordered block ids that map to navigable user messages.
        self.nav_block_ids: List[int] = []
        # user_index (1-based) -> block id
        self.nav_user_index: Dict[int, int] = {}

        for block_id, block in enumerate(blocks or []):
            raw_lines = list(block.get("lines") or [])
            if not raw_lines:
                raw_lines = [""]
            self.block_first_line[block_id] = len(self.lines)
            if bool(block.get("nav")):
                self.nav_block_ids.append(block_id)
                user_index = block.get("user_index")
                if isinstance(user_index, int):
                    self.nav_user_index[user_index] = block_id
            for line in raw_lines:
                self.lines.append(str(line))
                self.line_block.append(block_id)
            # Blank spacer line between blocks for readability.
            if block_id != len(blocks) - 1:
                self.lines.append("")
                self.line_block.append(-1)

        self.total_lines = len(self.lines)
        # Top visible line index. Default to the latest content (bottom).
        self.top = 0
        # Currently highlighted navigable block id (or None).
        self.selected_block_id: Optional[int] = None
        self._result: Optional[Dict[str, Any]] = None
        self._last_height = 1
        # Stick to the latest message until the user scrolls. The true terminal
        # size is only known once the application is running, so the bottom is
        # re-pinned on the first real render to avoid clipping the last lines.
        self._follow_bottom = True
        self._body_window = None

    # ----------------------------------------------------------------- helpers
    def _term_width(self) -> int:
        width = 0
        try:
            width = int(get_app().output.get_size().columns or 0)
        except Exception:
            width = 0
        if width <= 0 and callable(self._width_provider):
            try:
                width = int(self._width_provider() or 0)
            except Exception:
                width = 0
        return max(8, width if width > 0 else 80)

    def _term_rows(self) -> int:
        try:
            rows = int(get_app().output.get_size().rows or 0)
        except Exception:
            rows = 0
        return max(4, rows if rows > 0 else 24)

    def _body_height(self) -> int:
        # Prefer the body window's real rendered height (known after the first
        # render); fall back to the terminal size minus the fixed chrome rows
        # (1 title + 1 scroll indicator + 2 help footer).
        height = 0
        window = getattr(self, "_body_window", None)
        info = getattr(window, "render_info", None) if window is not None else None
        if info is not None:
            try:
                height = int(info.window_height)
            except Exception:
                height = 0
        if height <= 0:
            height = self._term_rows() - 4
        height = max(1, height)
        self._last_height = height
        return height

    def _max_top(self) -> int:
        return max(0, self.total_lines - self._body_height())

    def _clamp_top(self) -> None:
        self.top = max(0, min(self.top, self._max_top()))

    def _scroll_percent(self) -> int:
        max_top = self._max_top()
        if max_top <= 0:
            return 100
        return int(round(100 * self.top / max_top))

    def _selected_user_index(self) -> Optional[int]:
        if self.selected_block_id is None:
            return None
        for user_index, block_id in self.nav_user_index.items():
            if block_id == self.selected_block_id:
                return user_index
        return None

    def _scroll_block_into_view(self, block_id: int) -> None:
        first = self.block_first_line.get(block_id)
        if first is None:
            return
        # Position the selected message near the top of the viewport so its
        # reply stays visible below it.
        self.top = min(first, self._max_top())
        self._clamp_top()

    def _select_nav(self, position: int) -> None:
        """Select the navigable block at ``position`` in ``nav_block_ids``."""
        if not self.nav_block_ids:
            return
        position = max(0, min(position, len(self.nav_block_ids) - 1))
        self.selected_block_id = self.nav_block_ids[position]
        self._scroll_block_into_view(self.selected_block_id)

    def _current_nav_position(self) -> Optional[int]:
        if self.selected_block_id is None:
            return None
        try:
            return self.nav_block_ids.index(self.selected_block_id)
        except ValueError:
            return None

    # ----------------------------------------------------------------- content
    def _title_fragments(self):
        width = self._term_width()
        title = _TITLE
        if len(title) >= width:
            text = title[:width]
        else:
            # Fill the rest of the row with space-separated slashes ("/ / / /").
            remaining = width - len(title)
            fill = ("/ " * (remaining // 2 + 1))[:remaining]
            text = title + fill
        return [("class:transcript.title", text)]

    def _status_fragments(self):
        width = self._term_width()
        tail = f" {self._scroll_percent()}% "
        if len(tail) >= width:
            return [("class:transcript.rule", tail[:width])]
        bar = "─" * (width - len(tail))
        return [("class:transcript.rule", bar + tail)]

    def _help_fragments(self):
        labels = self._labels
        sep = "   "
        line1 = sep.join([labels["scroll"], labels["page"], labels["jump"]])
        if self.selected_block_id is not None:
            line2 = sep.join(
                [
                    labels["quit"],
                    labels["edit_prev"],
                    labels["edit_next"],
                    labels["edit_msg"],
                ]
            )
        else:
            line2 = sep.join([labels["quit"], labels["edit_prev_hint"]])
        return [
            ("class:transcript.help", line1),
            ("", "\n"),
            ("class:transcript.help", line2),
        ]

    def _body_fragments(self):
        if self._follow_bottom:
            self.top = self._max_top()
        self._clamp_top()
        width = self._term_width()
        height = self._body_height()
        start = self.top
        end = min(start + height, self.total_lines)

        fragments: List[Any] = []
        for offset, line_index in enumerate(range(start, end)):
            if offset > 0:
                fragments.append(("", "\n"))
            block_id = self.line_block[line_index]
            raw = self.lines[line_index]
            if (
                self.selected_block_id is not None
                and block_id == self.selected_block_id
            ):
                plain = fragment_list_to_text(to_formatted_text(ANSI(raw)))
                plain = plain[:width].ljust(width)
                fragments.append(("class:transcript.selected", plain))
            else:
                fragments.extend(to_formatted_text(ANSI(raw)))
        return fragments

    # ----------------------------------------------------------------- bindings
    def _build_key_bindings(self) -> "KeyBindings":
        kb = KeyBindings()

        @kb.add("q")
        @kb.add("c-c")
        def _quit(event) -> None:
            self._result = None
            event.app.exit()

        @kb.add("up")
        def _up(event) -> None:
            self._follow_bottom = False
            self.top = max(0, self.top - 1)

        @kb.add("down")
        def _down(event) -> None:
            self._follow_bottom = False
            self.top = min(self._max_top(), self.top + 1)

        @kb.add("pageup")
        def _pageup(event) -> None:
            self._follow_bottom = False
            self.top = max(0, self.top - self._body_height())

        @kb.add("pagedown")
        def _pagedown(event) -> None:
            self._follow_bottom = False
            self.top = min(self._max_top(), self.top + self._body_height())

        @kb.add("home")
        def _home(event) -> None:
            self._follow_bottom = False
            self.top = 0

        @kb.add("end")
        def _end(event) -> None:
            self._follow_bottom = True
            self.top = self._max_top()

        @kb.add("escape")
        def _select_last(event) -> None:
            # ESC highlights the last (most recent) user message.
            if not self.nav_block_ids:
                return
            self._follow_bottom = False
            self._select_nav(len(self.nav_block_ids) - 1)

        @kb.add("left")
        def _select_prev(event) -> None:
            if not self.nav_block_ids:
                return
            self._follow_bottom = False
            pos = self._current_nav_position()
            if pos is None:
                self._select_nav(len(self.nav_block_ids) - 1)
            else:
                self._select_nav(pos - 1)

        @kb.add("right")
        def _select_next(event) -> None:
            if not self.nav_block_ids:
                return
            self._follow_bottom = False
            pos = self._current_nav_position()
            if pos is None:
                self._select_nav(len(self.nav_block_ids) - 1)
            else:
                self._select_nav(pos + 1)

        @kb.add("enter")
        def _edit(event) -> None:
            user_index = self._selected_user_index()
            if user_index is None:
                return
            self._result = {"action": "edit", "user_index": user_index}
            event.app.exit()

        return kb

    # --------------------------------------------------------------------- run
    def run(self) -> Optional[Dict[str, Any]]:
        if not PROMPT_TOOLKIT_AVAILABLE or self.total_lines == 0:
            return None

        # Start at the latest message (bottom of the transcript).
        self.top = self._max_top()

        body_window = Window(
            content=FormattedTextControl(self._body_fragments, focusable=True),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self._body_window = body_window
        layout = Layout(
            HSplit(
                [
                    Window(
                        height=1,
                        content=FormattedTextControl(self._title_fragments),
                    ),
                    body_window,
                    Window(
                        height=1,
                        content=FormattedTextControl(self._status_fragments),
                    ),
                    Window(
                        height=2,
                        content=FormattedTextControl(self._help_fragments),
                    ),
                ]
            ),
            focused_element=body_window,
        )

        style = Style.from_dict(
            {
                "transcript.title": "#808080",
                "transcript.rule": "#808080",
                "transcript.help": "#808080",
                "transcript.selected": "reverse",
            }
        )

        app_kwargs: Dict[str, Any] = dict(
            layout=layout,
            key_bindings=self._build_key_bindings(),
            style=style,
            full_screen=True,
            mouse_support=False,
        )
        # Render captured ANSI at full fidelity (avoid downsampling the model's
        # truecolor highlighting to 4/8-bit) so colors match normal mode.
        if ColorDepth is not None:
            try:
                app_kwargs["color_depth"] = ColorDepth.TRUE_COLOR
            except Exception:
                pass
        app = Application(**app_kwargs)
        app.run()
        return self._result


def run_transcript_view(
    blocks: List[Dict[str, Any]],
    *,
    labels: Optional[Dict[str, str]] = None,
    width_provider: Optional[Callable[[], int]] = None,
) -> Optional[Dict[str, Any]]:
    """Run the transcript view and return the chosen action (or ``None``)."""
    try:
        view = TranscriptView(
            blocks, labels=labels, width_provider=width_provider
        )
        return view.run()
    except Exception:
        return None
