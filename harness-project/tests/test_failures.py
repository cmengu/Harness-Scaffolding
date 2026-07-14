"""11 Jul live-failure fixes: dead round 0 aborts the debate, mid-debate death keeps the
last good answers, timeouts speak human, huge pastes stay cheap to render."""
from __future__ import annotations

import io
import subprocess

from rich.console import Console

from council import debate, preamble
from council.config import load_config
from council.ledger import trace

from conftest import STUBS


def taped():
    buf = io.StringIO()
    return Console(file=buf, width=100), buf


def test_dead_round_zero_aborts_debate(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    r = debate.QuietRenderer()                             # engine test: assert events, not paint
    result = debate.run("q", rounds=2, judge="reasoning", cfg=load_config(),
                        console=Console(quiet=True), renderer=r)
    assert result.escalated is True                        # both dead = loudly failed
    assert any("turn abandoned" in e.get("text", "") for e in r.events)   # emitted as a notice
    rows = [r for r in trace(role="debate") if r.get("round") is not None and "proposer" in r]
    assert {r["round"] for r in rows} == {0}               # NO critique of corpses
    assert trace(role="debate", event="round0_failed")[0]["dead"] == ["claude", "codex"]
    assert not trace(role="judge")                         # nothing to weigh, no judge call


def test_one_dead_head_goes_single_voiced_without_rounds(monkeypatch):
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    r = debate.QuietRenderer()
    result = debate.run("q", rounds=2, judge=None, cfg=load_config(),
                        console=Console(quiet=True), renderer=r)
    assert result.escalated is False
    assert "STUB CLAUDE" in result.proposer_final          # the healthy answer survives
    assert any("single-voiced" in e.get("text", "") for e in r.events)
    rounds = {r["round"] for r in trace(role="debate") if r.get("round") is not None and "proposer" in r}
    assert rounds == {0}                                   # no debate against a corpse


def test_timeout_reads_as_human_words():
    cfg = load_config()
    msg = debate._err_text(subprocess.TimeoutExpired(["codex", "exec"], cfg.head_timeout), cfg)
    assert "no output for" in msg and "killed as hung" in msg
    assert "Command" not in msg                            # never the raw repr


def test_safe_marker_uses_friendly_timeout(monkeypatch):
    def hung(msg, cfg):
        raise subprocess.TimeoutExpired(["claude"], cfg.head_timeout)
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    cfg = load_config()
    out = debate._safe(hung, "q", cfg, "claude")
    assert "killed as hung" in out and "Command" not in out
    assert "killed as hung" in trace(role="head_error", head="claude")[0]["error"]


def test_is_dead_recognises_only_markers():
    assert preamble.is_dead("_(claude unavailable: boom)_")
    assert preamble.is_dead("_(codex cancelled)_")
    assert not preamble.is_dead("a normal answer")
    assert not preamble.is_dead("_(this trails off")


def test_visual_lines_short_circuits_on_huge_paste():
    from council.composer import _MAX_INPUT_ROWS, _visual_lines
    import time
    huge = ("x" * 200 + "\n") * 50_000                     # ~10MB paste
    t0 = time.monotonic()
    rows = _visual_lines(huge, columns=80, marker="›")
    assert rows == _MAX_INPUT_ROWS
    assert time.monotonic() - t0 < 0.1                     # a render tick, not a freeze
