"""Bonus 5a: prove the fan-out is genuinely parallel — two 1-second heads must finish
in ~1 second, not ~2. The README's 'duels run ~2x faster than serial' sentence rests here."""
from __future__ import annotations

import time

from rich.console import Console

from council import debate
from council.config import load_config

from conftest import STUBS


def test_both_heads_think_concurrently(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-slow"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-slow"))
    monkeypatch.setenv("COUNCIL_STUB_SLEEP", "1")
    t0 = time.monotonic()
    result = debate.run("q", rounds=0, judge=None, cfg=load_config(), console=Console(quiet=True))
    wall = time.monotonic() - t0
    assert "slow but steady" in result.proposer_final    # both heads actually answered
    assert "slow but steady" in result.adversary_final
    assert wall < 1.9, f"fan-out looks serial: {wall:.2f}s for two 1s heads"
