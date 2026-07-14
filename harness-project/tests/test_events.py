"""Event seam (step 5 / issue #10): the duel engine emits renderer-neutral events, so engine
behaviour is asserted through the QuietRenderer's event list — not by scraping printed text —
and the REPL input loop runs without a TTY."""
from __future__ import annotations

import io

from rich.console import Console

from council import chat, debate
from council.config import load_config

from conftest import STUBS


def quiet():
    return Console(quiet=True)


def test_duel_emits_round_and_final_events():
    r = debate.QuietRenderer()
    debate.run("moon?", rounds=1, judge=None, cfg=load_config(), console=quiet(), renderer=r)
    # round boundaries are events, not printed rules
    assert [e["round"] for e in r.events if e.get("kind") == "round_start"] == [0, 1]
    # both heads streamed to a final through the same fan-out
    assert "final" in r.kinds("claude") and "final" in r.kinds("codex")
    # the renderer only recorded — a Quiet renderer paints nothing (no notice unless the engine emits one)


def test_one_dead_head_emits_a_single_voiced_notice(monkeypatch):
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    r = debate.QuietRenderer()
    debate.run("q", rounds=1, judge=None, cfg=load_config(), console=quiet(), renderer=r)
    notices = [e.get("text", "") for e in r.events if e.get("kind") == "notice"]
    assert any("single-voiced" in t for t in notices)     # the engine EMITTED it; no painting scraped
    assert "final" in r.kinds("claude")                   # the healthy head still produced its answer


def test_run_loop_processes_a_piped_turn_without_a_tty(monkeypatch):
    # the input loop, TTY-free: piped stdin drives the classic synchronous path to /exit.
    seen = []

    class FakeRenderer:
        live_status = True

        def handle(self, text):
            seen.append(text)

    monkeypatch.setattr("sys.stdin", io.StringIO("hello there\n/exit\n"))
    chat.run_loop(FakeRenderer(), load_config(), quiet())
    assert seen == ["hello there"]                        # the turn dispatched; /exit ended the loop
