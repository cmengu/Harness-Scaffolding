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

from prompt_toolkit import Application, PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import is_searching
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings import search as _search_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
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
                 commands: list[tuple[str, str, str]], history_path,
                 on_toggle: Callable[[], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 hotkeys: dict[str, Callable[[], None]] | None = None,
                 arg_words: Callable[[str, int, list[str]], tuple[str, ...]] | None = None) -> None:
        self.console = console
        self._accent = accent
        self._marker = marker
        self._status = status                # str OR prompt_toolkit fragments — see _status_fragments
        self._on_toggle = on_toggle          # Shift+Tab: arm/disarm the duel (step 8)
        self._on_cancel = on_cancel          # bare Esc on an EMPTY box (the turn-cancel path)
        self._hotkeys = hotkeys or {}        # extra one-shot keys → callbacks (e.g. c-t)
        self._pending_draft = ""             # survives a Ctrl+O overlay trip (see _bindings)
        _install_shift_enter()
        history_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        history_path.touch(mode=0o600, exist_ok=True)
        os.chmod(history_path, 0o600)        # typed questions are as private as the ledger
        self._session = PromptSession(
            multiline=True,                  # buffer ACCEPTS \n; Enter is re-bound to submit
            erase_when_done=True,            # composer vanishes on submit; read() re-echoes
            refresh_interval=0.5,            # status line ticks while a parked turn runs (step 7)
            history=FileHistory(str(history_path)),
            completer=_SlashCompleter(commands, arg_words),
            key_bindings=self._bindings(),
            reserve_space_for_menu=0,        # menu space comes from _input_height instead
            prompt_continuation=lambda width, line_no, soft: [("class:prompt", "… ")] if not soft else "",
            style=Style.from_dict({
                "prompt": f"bold {accent}",
                "bar": accent,
                "bottom-toolbar": f"noreverse {accent}",
            }),
        )
        # Bare Esc must feel instant, not "swallowed": pt holds a lone ESC byte for
        # ttimeoutlen before deciding it isn't the start of a chord (Esc+Enter, Alt+…).
        # 0.25s keeps the chords working and halves the stock 0.5s hesitation.
        self._session.app.ttimeoutlen = 0.25
        self._install_crash_logger(history_path.parent / "crashes.log")
        self._patch_layout()
        set_title(title)

    def read(self) -> str:
        """One trip through the composer. After submit the live widget is erased, so the
        input is re-printed as a plain scrollback line — the conversation stays readable."""
        default, self._pending_draft = self._pending_draft, ""   # Ctrl+O stashed a draft?
        text = self._session.prompt([("class:prompt", f"{self._marker()} ")], default=default)
        if text.strip():
            first, *rest = text.split("\n")
            self.console.print(f"[bold {self._accent}]{self._marker()}[/] {escape(first)}")
            for line in rest[:6]:            # a huge paste re-echoes as a stub, not a flood
                self.console.print(f"[bold {self._accent}]…[/] {escape(line)}")
            if len(rest) > 6:
                self.console.print(f"[bold {self._accent}]…[/] [dim](+{len(rest) - 6} more lines)[/]")
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
                clean = _scrub_surrogates(buf.text)      # BEFORE accept: the history file
                if clean is not buf.text:                # encodes strict (crash 11 Jul)
                    buf.text = clean
                    buf.cursor_position = len(clean)
                buf.validate_and_handle()

        @kb.add("escape", "enter")           # works everywhere (Esc then Enter)
        @kb.add("c-j")                       # works everywhere (Ctrl+J IS line-feed)
        @kb.add("f20")                       # Shift+Enter on CSI-u terminals (see installer)
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        # Ctrl+O: open the report overlay — by SUBMITTING "/report" through the normal
        # dispatch path (no nested Application while the prompt runs; overlays run between
        # prompts where a fresh app is trivially safe). Any half-typed draft is stashed and
        # restored as the next prompt's default, so the keystroke never loses work.
        # (Why not F1/Ctrl+H: terminals intercept F1, and Ctrl+H IS Backspace at the byte
        # level — both verified unbindable upstream.)
        @kb.add("c-o", filter=~is_searching)
        def _overlay(event) -> None:
            buf = event.current_buffer
            self._pending_draft = buf.text
            buf.text = "/report"
            buf.validate_and_handle()

        # Shift+Tab (BackTab): the duel toggle (step 8) — the friendlier twin of typing
        # /duel. The callback PRINTS, and printing from inside a binding while the app owns
        # the screen corrupts the composer buffer (live-observed 11 Jul: the "⚔ adversary
        # ON" confirmation leaked INTO the user's next message). run_in_terminal is the
        # sanctioned escape hatch: suspend the app, run the callback, repaint.
        @kb.add("s-tab", eager=True)
        def _toggle(event) -> None:
            if self._on_toggle is not None:
                from prompt_toolkit.application import run_in_terminal
                run_in_terminal(self._on_toggle)
                event.app.invalidate()

        # Bare Esc, layered (backlog item 7): close the slash menu → clear the draft →
        # (empty box) the turn-cancel path, whose arm/confirm brain lives in the on_cancel
        # callback so this widget stays council-free. NOT eager: Esc+Enter and the default
        # Alt/meta chords must still win — pt disambiguates via ttimeoutlen (set in __init__).
        @kb.add("escape", filter=~is_searching)
        def _esc(event) -> None:
            buf = event.current_buffer
            if buf.complete_state:
                buf.cancel_completion()
            elif buf.text:
                buf.text = ""
                buf.cursor_position = 0
            elif self._on_cancel is not None:
                from prompt_toolkit.application import run_in_terminal
                run_in_terminal(self._on_cancel)
                event.app.invalidate()

        for key, fn in self._hotkeys.items():           # e.g. c-t: the tape toggle
            @kb.add(key, eager=True)
            def _hot(event, fn=fn) -> None:
                from prompt_toolkit.application import run_in_terminal
                run_in_terminal(fn)
                event.app.invalidate()

        return kb

    def _install_crash_logger(self, path) -> None:
        """pt's stock answer to an exception on the app's event loop is a MODAL
        'Press ENTER to continue...' prompt — which itself crashes when more input is
        queued behind it (live-observed 11 Jul: spam Shift+Tab + huge paste → double
        'Application is not running' cascade). Council's contract: the composer never
        holds the session hostage. Replace the handler — full traceback to a file, one
        dim line to the screen, render loop recovers. Instance-attribute shadowing is
        the seam: run_async re-reads self._handle_exception each run (verified 3.0.52)."""
        import time
        import traceback

        def _log(loop, context) -> None:
            exc = context.get("exception")
            try:
                with open(path, "a") as f:
                    f.write(f"\n── {time.ctime()} · composer event-loop exception ──\n")
                    if exc is not None:
                        f.write("".join(traceback.format_exception(type(exc), exc,
                                                                   exc.__traceback__)))
                    else:
                        f.write(str(context.get("message", context)) + "\n")
            except OSError:
                pass                         # logging must never become the second crash
            try:                             # patch_stdout routes this above the prompt
                print(f"⚠ composer hiccup — details in {path}; recovered, keep typing")
            except Exception:
                pass

        self._session.app._handle_exception = _log

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
            FormattedTextControl(self._status_fragments),
            height=1, dont_extend_height=True))
        root.children = children

    def _status_fragments(self) -> FormattedText:
        """The status callable may return a plain str (one toolbar-styled run, the classic
        shape) or a fragments list [(style, text), …] — the flight panel colors its ⚠s."""
        s = self._status()
        if isinstance(s, str):
            return FormattedText([("class:bottom-toolbar", s)])
        return FormattedText(s)

    def _input_height(self) -> Dimension:
        rows = _visual_lines(self._session.default_buffer.text,
                             columns=_term_width(), marker=self._marker())
        rows = min(_MAX_INPUT_ROWS, rows)
        menu = _MENU_ROWS if self._session.default_buffer.complete_state else 0
        return Dimension(min=max(1, rows + menu), max=max(1, rows + menu), preferred=rows + menu)


def show_overlay(title: str, body_ansi: str, *, accent: str = "cyan") -> None:
    """A scrollable full-screen viewer for long output (reports, replays). This is the ONE
    place council borrows the alternate screen — a nested temporary Application, omnigent's
    overlay dodge for 'inline floats can't exceed the prompt region' — and it gives the
    screen back on close, scrollback untouched. Runs BETWEEN prompts (plain sync context),
    so no nested-event-loop machinery is needed.
    Keys: ↑/↓/PgUp/PgDn/Home/End scroll · q/Esc/Ctrl+C close."""
    lines = body_ansi.splitlines() or [""]
    cursor = [0]                             # scrolling = moving an invisible cursor: the
    body = Window(                           # window's scroll-follow does the rest (mutating
        FormattedTextControl(                # vertical_scroll directly gets snapped back to
            ANSI(body_ansi),                 # keep the cursor visible when wrap_lines is on)
            get_cursor_position=lambda: Point(x=0, y=cursor[0]),
            show_cursor=False),
        wrap_lines=True)

    def _page(event) -> int:
        return max(1, event.app.output.get_size().rows - 2)

    def _scroll(event, delta: int) -> None:
        cursor[0] = max(0, min(cursor[0] + delta, len(lines) - 1))

    kb = KeyBindings()
    kb.add("up")(lambda e: _scroll(e, -1))
    kb.add("down")(lambda e: _scroll(e, +1))
    kb.add("pageup")(lambda e: _scroll(e, -_page(e)))
    kb.add("pagedown")(lambda e: _scroll(e, +_page(e)))
    kb.add("home")(lambda e: _scroll(e, -len(lines)))
    kb.add("end")(lambda e: _scroll(e, +len(lines)))
    # Ctrl+C must be bound explicitly: with custom KeyBindings the overlay app has no
    # default fallback, so an unbound Ctrl+C would appear to do nothing (upstream wart).
    # Here it CLOSES (council's Ctrl+C means "abandon", not "kill") — as do q and Esc.
    for key in ("q", "escape", "c-c"):
        kb.add(key)(lambda e: e.app.exit())

    header = Window(
        FormattedTextControl(FormattedText([
            ("class:bar bold", f" {title} "),
            ("class:bar", "· ↑↓ PgUp PgDn scroll · q close"),
        ])),
        height=1, dont_extend_height=True)
    Application(
        layout=Layout(HSplit([header, body])),
        key_bindings=kb,
        style=Style.from_dict({"bar": f"reverse {accent}"}),
        full_screen=True,
        mouse_support=False,
    ).run()


def show_picker(options: list[tuple[str, str]], *, accent: str = "cyan",
                title: str = "") -> int | None:
    """Inline arrow-key picker, for BETWEEN prompts (main thread, plain sync context —
    show_overlay's contract minus the alternate screen): a few lines render in place,
    ↑/↓ move, Enter picks, an option's letter jump-picks, Esc/^C cancels (None).
    erase_when_done leaves the scrollback clean. `options` = [(accelerator, label), …]."""
    idx = [0]

    def move(event, delta: int) -> None:
        idx[0] = (idx[0] + delta) % len(options)
        event.app.invalidate()

    kb = KeyBindings()
    kb.add("up")(lambda e: move(e, -1))
    kb.add("down")(lambda e: move(e, +1))
    kb.add("enter")(lambda e: e.app.exit(result=idx[0]))
    for key in ("escape", "c-c"):
        kb.add(key)(lambda e: e.app.exit(result=None))
    for i, (accel, _label) in enumerate(options):
        if len(accel) == 1 and accel.lower() != accel.upper():   # letters only — glyph
            kb.add(accel.lower())(lambda e, i=i: e.app.exit(result=i))   # accelerators
            kb.add(accel.upper())(lambda e, i=i: e.app.exit(result=i))   # stay display-only

    def frags() -> FormattedText:
        out = []
        if title:
            out.append(("class:dim", f"{title}\n"))
        for i, (accel, label) in enumerate(options):
            cur = i == idx[0]
            out.append(("class:sel" if cur else "", f"{'❯' if cur else ' '} {accel}  {label}\n"))
        out.append(("class:dim", "  ↑↓ move · Enter picks · Esc = first option"))
        return FormattedText(out)

    app = Application(
        layout=Layout(Window(FormattedTextControl(frags, show_cursor=False), wrap_lines=True)),
        key_bindings=kb,
        erase_when_done=True,
        mouse_support=False,
        style=Style.from_dict({"sel": f"bold {accent}", "dim": "ansibrightblack"}),
    )
    app.ttimeoutlen = 0.25                   # bare Esc decides fast (no chords registered)
    return app.run()


class _SlashCompleter(Completer):
    """Popup while typing the command word — and, when the host supplies `arg_words`,
    over FINITE-vocabulary arguments too (/model claude opus, /effort high). Free-text
    arguments (/note, /fork titles) stay popup-free: an empty vocabulary yields nothing."""

    def __init__(self, commands: list[tuple[str, str, str]],
                 arg_words=None) -> None:
        self._commands = commands
        self._arg_words = arg_words

    def get_completions(self, doc, _event):
        t = doc.text_before_cursor
        if not t.startswith("/") or "\n" in t:
            return
        if " " not in t:
            for cmd, _args, desc in self._commands:
                if cmd.startswith(t):
                    yield Completion(cmd, start_position=-len(t), display_meta=desc)
            return
        if self._arg_words is None:
            return
        words = t.split()
        partial = "" if t.endswith(" ") else words[-1]
        prior = words[1:-1] if partial else words[1:]
        for word in self._arg_words(words[0], len(prior), prior):
            if word.startswith(partial):
                yield Completion(word, start_position=-len(partial))


def _find_buffer_window(container) -> Window | None:
    if isinstance(container, Window):
        return container if isinstance(getattr(container, "content", None), BufferControl) else None
    for child in container.get_children():
        hit = _find_buffer_window(child)
        if hit:
            return hit
    return None


def _visual_lines(text: str, *, columns: int, marker: str) -> int:
    """Rows the buffer needs, counting soft-wraps — Document.line_count only counts \\n.
    SHORT-CIRCUITS at the display cap: this runs on EVERY render tick, and a huge paste
    (hundreds of KB) must not turn each repaint into an O(paste) character walk — that
    freeze was half of the 11 Jul paste crash."""
    prefix = sum(get_cwidth(c) for c in f"{marker} ")
    width = max(1, columns - prefix)
    rows = 0
    for line in text.split("\n"):
        w = sum(get_cwidth(c) for c in line)
        rows += max(1, (w + width - 1) // width)
        if rows >= _MAX_INPUT_ROWS:          # already at the cap — counting further is waste
            return _MAX_INPUT_ROWS
    return max(1, rows)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (ValueError, OSError):
        return 80


def _scrub_surrogates(text: str) -> str:
    """A huge paste split mid-character across pt's input reads (or its detach/attach
    cycles — spam Shift+Tab widens the odds) decodes into lone \\udcXX surrogates. They
    are the ORIGINAL BYTES in disguise (surrogateescape), so re-encoding them usually
    reassembles the real character (— … ⚔); anything genuinely invalid becomes �.
    Without this, every strict utf-8 encoder downstream throws — the history file blew
    up the event loop and the head's stdin killed the call (live crash 11 Jul)."""
    try:
        text.encode("utf-8")                 # fast path: clean text costs one scan
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


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
