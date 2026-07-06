"""Step 3's contract: transients retry with backoff, permanents fail fast, and a head
that stays dead leaves a quarantine postmortem — never a silent gap."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from council import debate
from council.config import load_config
from council.ledger import trace

from conftest import STUBS


def quiet():
    return Console(quiet=True)


def test_flaky_once_recovers_on_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-flaky-once"))
    monkeypatch.setenv("COUNCIL_STUB_MARKER", str(tmp_path / "flaked"))
    result = debate.run("q", rounds=0, judge=None, cfg=load_config(), console=quiet())
    assert result.proposer_final == "STUB CLAUDE: recovered on retry"
    retries = trace(role="head_retry", head="claude")
    assert len(retries) == 1 and retries[0]["kind"] == "transient"
    call = trace(role="head_call", head="claude")[0]
    assert call["ok"] is True and call["attempts"] == 2
    assert trace(role="quarantined") == []               # recovery leaves no corpse


def test_quota_death_exhausts_retries_then_quarantines(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-flaky-quota"))
    count = tmp_path / "calls"
    monkeypatch.setenv("COUNCIL_STUB_COUNT", str(count))
    cfg = load_config()
    result = debate.run("why?", rounds=0, judge=None, cfg=cfg, console=quiet())
    assert "unavailable" in result.proposer_final        # degraded single-voice, exit 0
    assert "STUB CODEX" in result.adversary_final
    assert len(count.read_text().splitlines()) == cfg.head_retries + 1   # every attempt taken
    assert len(trace(role="head_retry", head="claude")) == cfg.head_retries
    q = trace(role="quarantined", head="claude")
    assert len(q) == 1 and q[0]["kind"] == "transient"
    postmortem = Path(q[0]["path"]).read_text()          # a readable corpse, not a gap
    assert "head failure: claude" in postmortem
    assert "429 rate limit" in postmortem
    assert "why?" in postmortem


def test_permanent_failure_takes_exactly_one_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-bad-flag"))
    count = tmp_path / "calls"
    monkeypatch.setenv("COUNCIL_STUB_COUNT", str(count))
    debate.run("q", rounds=0, judge=None, cfg=load_config(), console=quiet())
    assert len(count.read_text().splitlines()) == 1      # no slow-motion failing
    assert trace(role="head_retry") == []
    assert trace(role="quarantined", head="claude")[0]["kind"] == "permanent"


def test_retries_off_means_single_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-flaky-quota"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    count = tmp_path / "calls"
    monkeypatch.setenv("COUNCIL_STUB_COUNT", str(count))
    debate.run("q", rounds=0, judge=None, cfg=load_config(), console=quiet())
    assert len(count.read_text().splitlines()) == 1
    assert trace(role="head_retry") == []
