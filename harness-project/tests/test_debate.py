"""The whole debate loop, end to end, no API key: fan-out, degrade, judge, memory."""
from __future__ import annotations

from rich.console import Console

from council import debate
from council.config import load_config
from council.ledger import record, start_session, trace

from conftest import STUBS


def quiet():
    return Console(quiet=True)


def test_full_debate_with_stubs():
    result = debate.run("moon?", rounds=1, judge=None, cfg=load_config(), console=quiet())
    assert "STUB CLAUDE" in result.proposer_final
    assert "STUB CODEX" in result.adversary_final
    rows = [r for r in trace(role="debate") if r.get("round") is not None and "proposer" in r]
    assert {r["round"] for r in rows} <= {0, 1}
    assert all(r["run_id"] == rows[0]["run_id"] for r in rows)   # step 1 pays off here
    # identical answers every round → the deterministic early-stop must fire
    assert any(r.get("event") == "converged" for r in trace(role="debate"))


def test_dead_head_degrades_not_dies(monkeypatch):
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-flaky-quota"))
    result = debate.run("q", rounds=0, judge=None, cfg=load_config(), console=quiet())
    assert "STUB CLAUDE" in result.proposer_final        # the healthy head still answered
    assert "unavailable" in result.adversary_final       # _safe's contract, enforced forever
    assert trace(role="head_error", head="codex")


def test_moderator_judge_synthesizes():
    result = debate.run("q", rounds=0, judge="moderator", cfg=load_config(), console=quiet())
    assert result.synthesis and not result.escalated
    judge_rows = trace(role="judge")
    assert judge_rows[0]["style"] == "moderator"         # the verdict survives the session
    assert trace(role="judge_keymap")                    # blind-grading map is recorded


def test_reasoning_judge_may_escalate(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-escalate"))
    result = debate.run("q", rounds=0, judge="reasoning", cfg=load_config(), console=quiet())
    assert result.escalated is True


def test_solo_renderer_records_single_voice():
    cfg = load_config()
    debate.DebateRenderer(cfg, quiet(), adversarial=False).handle("hello")
    rows = [r for r in trace(role="debate") if "proposer" in r]
    assert len(rows) == 1 and rows[0].get("adversary") is None   # bare row: null adversary dropped
    assert trace(role="head_call", head="claude")
    assert not trace(role="head_call", head="codex")     # solo = one subprocess, cheap turns


def test_history_preamble_scopes_to_answered_turns():
    cfg = load_config()
    start_session()
    record({"role": "user", "text": "earlier question"})
    record({"role": "debate", "round": 0, "proposer": "earlier answer", "adversary": None})
    record({"role": "user", "text": "current unanswered question"})   # recorded before handle()
    pre = debate._history_preamble(cfg)
    assert "earlier question" in pre and "earlier answer" in pre
    assert "current unanswered question" not in pre      # never echoed back as fake memory
