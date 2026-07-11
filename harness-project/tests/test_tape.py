"""The tape (step 5): duels stream interleaved into one column; block path stays reachable."""
from __future__ import annotations

import io

from rich.console import Console

from council import debate
from council.config import load_config
from council.ledger import trace


def taped():
    buf = io.StringIO()
    return Console(file=buf, width=100), buf


def test_default_duel_streams_and_renders_the_tape(monkeypatch):
    console, buf = taped()
    result = debate.run("moon?", rounds=1, judge=None, cfg=load_config(), console=console)
    # the pump, not the block path (argv capture races between the two heads — the
    # ledger's stream flag is the unambiguous witness)
    assert all(c.get("stream") for c in trace(role="head_call"))
    out = buf.getvalue()
    assert "✳ Claude" in out and "⬡ Codex" in out        # brand-glyph gutters
    assert "challenge each other" in out                 # honest critique-round label
    assert "thought for 42 tokens" in out                # claude's pulse
    assert "cheese hypothesis" in out                    # codex's readable reasoning, dim
    assert "🔍" in out                                   # tool lines on the tape
    assert "STUB CLAUDE" in result.proposer_final and "STUB CODEX" in result.adversary_final
    rows = [r for r in trace(role="debate") if r.get("round") is not None and "proposer" in r]
    assert {r["round"] for r in rows} == {0, 1}          # ledger rows unchanged by the tape


def test_stream_tape_off_restores_block_path(tmp_path, monkeypatch):
    argv = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    monkeypatch.setenv("COUNCIL_STREAM_TAPE", "false")
    console, buf = taped()
    result = debate.run("moon?", rounds=0, judge=None, cfg=load_config(), console=console)
    assert "stream-json" not in argv.read_text()
    assert "STUB CLAUDE" in result.proposer_final
    assert "Claude" in buf.getvalue()                    # _present still presents


def test_glyphs_are_theme_configurable(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_GLYPH", "A")
    monkeypatch.setenv("COUNCIL_CODEX_GLYPH", "B")
    console, buf = taped()
    debate.run("q", rounds=0, judge=None, cfg=load_config(), console=console)
    out = buf.getvalue()
    assert "A Claude" in out and "B Codex" in out


def test_dead_head_on_the_tape_degrades_single_voiced(monkeypatch):
    from conftest import STUBS
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-flaky-quota"))
    console, buf = taped()
    result = debate.run("q", rounds=0, judge=None, cfg=load_config(), console=console)
    assert "STUB CLAUDE" in result.proposer_final
    assert "unavailable" in result.adversary_final       # _safe's contract holds on the tape
    assert "unavailable" in buf.getvalue()               # …and the user saw it happen
