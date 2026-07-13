"""Composer contracts that don't need a real TTY: the crash logger (an event-loop
exception must land in crashes.log and NEVER re-raise) and status-fragment coercion."""
from __future__ import annotations

import pytest

pt = pytest.importorskip("prompt_toolkit")

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console


def _composer(tmp_path, **kw):
    from council.composer import Composer
    return Composer(Console(), accent="blue", title="t", marker=lambda: ">",
                    status=kw.pop("status", lambda: " ok "),
                    commands=[("/help", "", "help")],
                    history_path=tmp_path / "history", **kw)


def test_crash_logger_writes_and_swallows(tmp_path, capsys):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        c = _composer(tmp_path)
        handler = c._session.app._handle_exception
        try:
            raise ValueError("boom from the loop")
        except ValueError as e:
            handler(None, {"exception": e})          # must not raise
    log = (tmp_path / "crashes.log").read_text()
    assert "ValueError: boom from the loop" in log
    assert "composer event-loop exception" in log
    assert "recovered" in capsys.readouterr().out    # the one-line notice

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        c2 = _composer(tmp_path)
        c2._session.app._handle_exception(None, {"message": "no exception object"})
    assert "no exception object" in (tmp_path / "crashes.log").read_text()


def test_show_picker_arrows_accelerators_escape():
    import threading
    import time
    from council.composer import show_picker
    opts = [("A", "briefing"), ("B", "last turns"), ("C", "full transcript")]

    def run(keys):
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            def feed():
                for k in keys:
                    time.sleep(0.05)
                    pipe.send_text(k)
            t = threading.Thread(target=feed, daemon=True)
            t.start()
            got = show_picker(opts)
            t.join()
            return got

    assert run(["\x1b[B", "\r"]) == 1        # ↓ then Enter
    assert run(["\x1b[A", "\r"]) == 2        # ↑ wraps to the last option
    assert run(["c"]) == 2                   # letter accelerator, case-insensitive
    assert run(["\x1b"]) is None             # Esc cancels (caller maps to the default)


def test_status_fragments_accept_str_and_fragments(tmp_path):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        c = _composer(tmp_path)
        assert list(c._status_fragments()) == [("class:bottom-toolbar", " ok ")]
        c._status = lambda: [("class:bottom-toolbar", "a"), ("fg:ansiyellow", "b")]
        assert list(c._status_fragments()) == [("class:bottom-toolbar", "a"),
                                               ("fg:ansiyellow", "b")]


def test_slash_completer_completes_arguments():
    """12 Jul: finite-vocabulary arguments popup (via the host's arg_words callable);
    free-text arguments and unknown commands stay popup-free."""
    from prompt_toolkit.document import Document

    from council.chat import _slash_arg_words
    from council.composer import _SlashCompleter

    c = _SlashCompleter([("/model", "", "per-head model")], _slash_arg_words)

    def words(text):
        return [x.text for x in c.get_completions(Document(text, len(text)), None)]

    assert words("/mod") == ["/model"]                    # command word still completes
    assert "claude" in words("/model ")                   # first arg vocabulary
    assert words("/model cl") == ["claude"]               # prefix-filtered
    assert "opus" in words("/model claude ")              # second arg, head-aware
    assert words("/note ") == []                          # free text — silent
    completer_without_vocab = _SlashCompleter([("/model", "", "d")])
    assert [x.text for x in completer_without_vocab.get_completions(
        Document("/model ", 7), None)] == []              # no callable → old behavior
