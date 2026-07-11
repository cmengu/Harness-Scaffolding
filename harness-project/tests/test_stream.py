"""The streaming pump (step 4): renderer-neutral events out of both heads, with _safe's
retry/quarantine contract wrapped around the iterator."""
from __future__ import annotations

import json

from council import debate
from council.backends import HeadSessions, adversary_stream, proposer_stream
from council.config import load_config
from council.ledger import trace

from conftest import STUBS


def kinds(events):
    return [e["kind"] for e in events]


def test_proposer_stream_event_shapes():
    s = HeadSessions()
    events = list(proposer_stream("q", load_config(), session=s, thinking=1000, tools=True))
    assert kinds(events) == ["text", "tool", "text", "thinking", "cost", "final"]
    assert events[1]["payload"]["name"] == "WebSearch"
    assert events[3]["payload"]["tokens"] == 42          # the pulse, not the text (redacted)
    assert events[-1]["payload"] == "STUB CLAUDE: the moon is made of rock"
    assert "".join(e["payload"] for e in events if e["kind"] == "text") \
        == "STUB CLAUDE: the moon is made of rock"        # deltas reassemble to the final
    assert s.claude == "stub-claude-sid"                  # session captured from the stream
    assert all(set(e) == {"head", "kind", "payload", "ts"} for e in events)  # JSONL-able, Rich-free
    json.dumps(events)                                    # …literally


def test_adversary_stream_reasoning_is_readable():
    s = HeadSessions()
    events = list(adversary_stream("q", load_config(), session=s, effort="high", tools=True))
    assert kinds(events) == ["tool", "thinking", "final", "cost"]
    assert "cheese hypothesis" in events[1]["payload"]["text"]   # codex thinking = real text
    assert events[0]["payload"]["name"] == "web_search"
    assert s.codex == "stub-thread-1"
    assert trace(role="head_cost", head="codex")


def test_adversary_stream_works_sessionless():
    events = list(adversary_stream("q", load_config()))
    assert kinds(events)[-2:] == ["final", "cost"]


def test_safe_stream_retries_then_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-stream-flaky"))
    monkeypatch.setenv("COUNCIL_STUB_STATE", str(tmp_path / "state"))
    cfg = load_config()
    events = list(debate._safe_stream(
        lambda m, c: proposer_stream(m, c), "q", cfg, "claude"))
    assert "retry" in kinds(events)                       # the retry announced itself
    assert kinds(events)[-1] == "final"                   # …and the restart finished the job
    assert trace(role="head_retry", head="claude")
    calls = trace(role="head_call", head="claude")
    assert calls[-1]["ok"] and calls[-1]["attempts"] == 2 and calls[-1]["stream"] is True


def test_safe_stream_terminal_failure_is_an_event_not_a_crash(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    s = HeadSessions(claude="stale")
    events = list(debate._safe_stream(
        lambda m, c: proposer_stream(m, c, session=s), "q", load_config(), "claude", s))
    assert kinds(events) == ["error"]
    assert s.claude is None                               # failed head's session cleared
    assert trace(role="head_error", head="claude")
    assert trace(role="quarantined", head="claude")


def test_block_paths_still_green_after_stub_rewrite():
    """The stubs grew streaming branches — the classic block contract must be untouched."""
    from council.backends import adversary, proposer
    assert "moon is made of rock" in proposer("q", load_config())
    assert adversary("q", load_config()) == "STUB CODEX: disagree - the moon is made of cheese"
