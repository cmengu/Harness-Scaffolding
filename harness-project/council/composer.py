"""council/composer.py — the C2 input cockpit: a compact composer zone at the bottom
of the terminal (separator bar → multiline input → status line), content scrolling
natively above. NO alternate screen, ever.

↔ ADAPTED from omnigent-ui-sdk terminal/_host.py (Apache-2.0; see NOTICE) — the patched-
PromptSession technique, trimmed from 3,762 lines to this file by dropping streaming,
approvals, overlays, sub-agent menus, and themes. The warts kept are the load-bearing ones:
  · root HSplit JUSTIFY→TOP (else the buffer absorbs the screen and pins the bar to the
    terminal bottom with dead space in between)
  · buffer height = visual lines incl. soft-wrap (Document.line_count misses wraps)
  · +8 rows while the completion menu is open (else the Float has no room and the popup
    squeezes to 1-2 lines) with reserve_space_for_menu=0
  · Enter submits / Esc+Enter · Ctrl+J · Shift+Enter(CSI-u→F20) · trailing-\\ insert newline
  · a separate Enter binding while reverse-i-search is open (else eager Enter submits the
    raw search text instead of accepting the match)
  · 'noreverse' on the status style (else accent fragments render as accent BACKGROUND)
"""
from __future__ import annotations

import os
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import is_searching
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings import search as _search_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import HSplit, VerticalAlign, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.shortcuts import set_title
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from rich.console import Console
from rich.markup import escape

_MAX_INPUT_ROWS = 8
_MENU_ROWS = 8


class Composer:
    """The bottom strip, owned; everything else is the terminal's. Pure widget — all
    council-specific content (marker, status text, command table) arrives as callables,
    so this file stays brand-free and reusable."""

    def __init__(self, console: Console, *, accent: str, title: str,
                 marker: Callable[[], str], status: Callable[[], str],
                 commands: list[tuple[str, str, str]], history_path) -> None:
        self.console = console
        self._accent = accent
        self._marker = marker
        self._status = status
        _install_shift_enter()
        history_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        history_path.touch(mode=0o600, exist_ok=True)
        os.chmod(history_path, 0o600)        # typed questions are as private as the ledger
        self._session = PromptSession(
            multiline=True,                  # buffer ACCEPTS \n; Enter is re-bound to submit
            erase_when_done=True,            # composer vanishes on submit; read() re-echoes
            history=FileHistory(str(history_path)),
            completer=_SlashCompleter(commands),
            key_bindings=self._bindings(),
            reserve_space_for_menu=0,        # menu space comes from _input_height instead
            prompt_continuation=lambda width, line_no, soft: [("class:prompt", "… ")] if not soft else "",
            style=Style.from_dict({
                "prompt": f"bold {accent}",
                "bar": accent,
                "bottom-toolbar": f"noreverse {accent}",
            }),
        )
        self._patch_layout()
        set_title(title)

    def read(self) -> str:
        """One trip through the composer. After submit the live widget is erased, so the
        input is re-printed as a plain scrollback line — the conversation stays readable."""
        text = self._session.prompt([("class:prompt", f"{self._marker()} ")])
        if text.strip():
            first, *rest = text.split("\n")
            self.console.print(f"[bold {self._accent}]{self._marker()}[/] {escape(first)}")
            for line in rest:
                self.console.print(f"[bold {self._accent}]…[/] {escape(line)}")
        return text

    # ── internals ────────────────────────────────────────────────────────────

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter", eager=True, filter=is_searching)
        def _accept_search(event) -> None:
            # Ctrl+R search owns Enter: accept the matched history item, don't submit.
            handler = getattr(_search_bindings.accept_search, "handler", _search_bindings.accept_search)
            handler(event)

        @kb.add("enter", eager=True, filter=~is_searching)
        def _submit(event) -> None:
            buf = event.current_buffer
            if buf.text.endswith("\\"):      # trailing backslash: shell-style continuation
                buf.text = buf.text[:-1]
                buf.cursor_position = len(buf.text)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @kb.add("escape", "enter")           # works everywhere (Esc then Enter)
        @kb.add("c-j")                       # works everywhere (Ctrl+J IS line-feed)
        @kb.add("f20")                       # Shift+Enter on CSI-u terminals (see installer)
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        return kb

    def _patch_layout(self) -> None:
        """The omnigent layout surgery. Verified against prompt_toolkit 3.0.52: the root
        HSplit's LAST child is the bottom_toolbar ConditionalContainer — swap it for a
        separator bar + status line that flow right under the input (no dead space)."""
        root = self._session.layout.container
        if not isinstance(root, HSplit):     # unexpected pt internals: keep stock layout
            return
        root.align = VerticalAlign.TOP       # remaining height → invisible padding, not the buffer
        buf_win = _find_buffer_window(root)
        if buf_win is not None:
            buf_win.height = self._input_height
        children = list(root.children)
        if children:
            children.pop()                   # the (unused) pinned bottom_toolbar container
        children.insert(0, Window(           # ── separator bar ABOVE the input: fences the
            FormattedTextControl(lambda: FormattedText([("class:bar", "─" * _term_width())])),
            height=1, dont_extend_height=True))          # composer zone off from content
        children.append(Window(              # — status line under it
            FormattedTextControl(lambda: FormattedText([("class:bottom-toolbar", self._status())])),
            height=1, dont_extend_height=True))
        root.children = children

    def _input_height(self) -> Dimension:
        rows = _visual_lines(self._session.default_buffer.text,
                             columns=_term_width(), marker=self._marker())
        rows = min(_MAX_INPUT_ROWS, rows)
        menu = _MENU_ROWS if self._session.default_buffer.complete_state else 0
        return Dimension(min=max(1, rows + menu), max=max(1, rows + menu), preferred=rows + menu)


class _SlashCompleter(Completer):
    """Popup only while typing the command word itself — never over arguments."""

    def __init__(self, commands: list[tuple[str, str, str]]) -> None:
        self._commands = commands

    def get_completions(self, doc, _event):
        t = doc.text_before_cursor
        if t.startswith("/") and " " not in t and "\n" not in t:
            for cmd, _args, desc in self._commands:
                if cmd.startswith(t):
                    yield Completion(cmd, start_position=-len(t), display_meta=desc)


def _find_buffer_window(container) -> Window | None:
    if isinstance(container, Window):
        return container if isinstance(getattr(container, "content", None), BufferControl) else None
    for child in container.get_children():
        hit = _find_buffer_window(child)
        if hit:
            return hit
    return None


def _visual_lines(text: str, *, columns: int, marker: str) -> int:
    """Rows the buffer needs, counting soft-wraps — Document.line_count only counts \\n."""
    prefix = sum(get_cwidth(c) for c in f"{marker} ")
    width = max(1, columns - prefix)
    rows = 0
    for line in text.split("\n"):
        w = sum(get_cwidth(c) for c in line)
        rows += max(1, (w + width - 1) // width)
    return max(1, rows)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (ValueError, OSError):
        return 80


_SHIFT_ENTER_INSTALLED = False


def _install_shift_enter() -> None:
    """Map the Kitty-protocol Shift+Enter sequence (CSI 13;2u) to F20 so the newline
    binding can catch it. Kitty/WezTerm/Ghostty/iTerm2-with-CSI-u emit it; terminals
    without CSI-u simply never send it and Esc+Enter / Ctrl+J remain the paths."""
    global _SHIFT_ENTER_INSTALLED
    if _SHIFT_ENTER_INSTALLED:
        return
    try:
        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        ANSI_SEQUENCES.setdefault("\x1b[13;2u", Keys.F20)
        _SHIFT_ENTER_INSTALLED = True
    except Exception:                        # parser internals moved: lose Shift+Enter only
        pass
