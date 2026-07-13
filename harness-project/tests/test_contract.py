"""The output contract, step 3 core (issue #7): injection template + per-head injection, trailer
slice/validate, one schema-flag retry then graceful degrade, opponent confidence in round N.

The contract is default-ON in production but OFF under test (conftest) so the pre-contract suite
stays the baseline; every test here opts in with COUNCIL_CONTRACT=1. Retry/degrade tests run the
block path (COUNCIL_STREAM_TAPE=0) for deterministic single-line stub output; injection and the
valid round-trip run the default streaming path — the production one.
"""
from __future__ import annotations

from pathlib import Path

from rich.console import Console

from council import contract, debate
from council.backends import HeadSessions
from council.config import Config, load_config
from council.ledger import trace

from conftest import STUBS


def quiet():
    return Console(quiet=True)


def armed(monkeypatch, **env):
    monkeypatch.setenv("COUNCIL_CONTRACT", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# ── template units (pure, no subprocess) ───────────────────────────────────────────────────
def test_injection_round0_is_opening_shape():
    t = contract.injection(0)
    assert "=== POSITION ===" in t and "=== ANSWER ===" in t and "=== TRAILER ===" in t
    assert "DELIBERATION" not in t                     # no critique process in the opening
    assert "SUPPORT|REFUTE|UNCERTAIN" not in t         # no stances at round 0


def test_injection_roundN_carries_deliberation_pack_and_confidence():
    t = contract.injection(1, opponent_confidence=0.5, final_round=True)
    assert "=== DELIBERATION ===" in t
    assert "REFUTE BY REPRODUCTION" in t               # the community prompt-line pack is woven in
    assert "do not simply defer" in t.lower() or "not simply defer" in t.lower()
    assert "SUPPORT|REFUTE|UNCERTAIN" in t             # stances committed in the trailer
    assert "0.50" in t                                 # the opponent's stated confidence
    assert contract.injection(1, opponent_confidence=None).find("stated confidence") == -1


def test_injection_nonfinal_round_pins_artifact_none():
    assert "keep ARTIFACT as `none`" in contract.injection(1, final_round=False)
    assert "keep ARTIFACT as `none`" not in contract.injection(1, final_round=True)


def test_split_and_validate_trailer():
    body, raw = contract.split_trailer(
        "=== ANSWER ===\nrock\n=== TRAILER ===\n{\"position\": \"rock\", \"confidence\": 0.8}")
    assert "=== TRAILER ===" not in body and raw.startswith("{")
    parsed = contract.parse_trailer(raw, 1)
    assert parsed["position"] == "rock" and parsed["confidence"] == 0.8


def test_validate_rejects_missing_broken_and_out_of_range():
    assert contract.parse_trailer(None, 0) is None                       # no trailer
    assert contract.parse_trailer("{ not json", 0) is None               # unparseable
    assert contract.parse_trailer('{"position": "x"}', 0) is None        # confidence missing
    assert contract.parse_trailer('{"position": "x", "confidence": 2}', 0) is None  # out of range
    assert contract.parse_trailer('{"confidence": 0.5}', 0) is None      # position missing
    assert contract.parse_trailer('{"position": "x", "confidence": 0.5}', 0) is not None


def test_fenced_trailer_tolerated():
    parsed = contract.parse_trailer('```json\n{"position": "x", "confidence": 0.3}\n```', 0)
    assert parsed and parsed["confidence"] == 0.3


def test_config_contract_defaults_on():
    assert Config().contract is True                   # production default: on when armed


# ── injection reaches each head, fresh each round, absent from the recap ─────────────────────
def test_contract_injected_verbatim_fresh_each_round(tmp_path, monkeypatch):
    armed(monkeypatch)
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-contract"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-contract"))
    sysprompt = tmp_path / "sysprompt"        # claude's --append-system-prompt-file content
    codex_argv = tmp_path / "codex-argv"
    monkeypatch.setenv("COUNCIL_STUB_SYSPROMPT", str(sysprompt))
    monkeypatch.setenv("COUNCIL_CODEX_ARGV", str(codex_argv))
    debate.run("moon?", rounds=1, judge=None, cfg=load_config(),
               console=quiet(), sessions=HeadSessions())
    sp = sysprompt.read_text()
    assert "Answer in the sections below" in sp        # claude got the contract, verbatim
    assert "=== DELIBERATION ===" in sp                # …and the round-N variant on the 2nd call
    assert sp.count("=== TRAILER ===") >= 2            # fresh injection every round, not once
    assert "council duel" in codex_argv.read_text()    # codex got it too (message prefix)
    # absent from the recap: the injection text is never written to any ledger row
    assert all("Answer in the sections below" not in str(r) for r in trace())


def test_valid_duel_round_trips_trailers_from_both_heads(monkeypatch):
    armed(monkeypatch)
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-contract"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-contract"))
    result = debate.run("moon?", rounds=1, judge=None, cfg=load_config(),
                        console=quiet(), sessions=HeadSessions())
    parsed = [t for t in trace(role="trailer") if t.get("contract") == "parsed"]
    assert {t["head"] for t in parsed} == {"claude", "codex"}
    assert all("confidence" in t and "position" in t for t in parsed)
    # the trailer is sliced off the deliverable; the prose answer survives
    assert "The moon is made of rock." in result.proposer_final
    assert "=== TRAILER ===" not in result.proposer_final


# ── retry recovers · degrade never dies ─────────────────────────────────────────────────────
def test_malformed_trailer_recovers_on_schema_flag_retry(tmp_path, monkeypatch):
    armed(monkeypatch, COUNCIL_STREAM_TAPE="0")
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-contract-malformed"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-contract"))
    argv = tmp_path / "claude-argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    debate.run("moon?", rounds=0, judge=None, cfg=load_config(),
               console=quiet(), sessions=HeadSessions())
    claude_trailers = trace(role="trailer", head="claude")
    assert claude_trailers[-1]["contract"] == "parsed"       # the retry recovered it
    fired = [r for r in trace(role="head_retry", head="claude") if r.get("kind") == "trailer"]
    assert len(fired) == 1                                    # exactly one corrective retry
    text = argv.read_text()
    assert "--append-system-prompt-file" in text             # main call carried the contract
    assert "--json-schema" in text                           # the retry attached the schema flag


def test_malformed_trailer_twice_degrades_but_keeps_prose(tmp_path, monkeypatch):
    armed(monkeypatch, COUNCIL_STREAM_TAPE="0")
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-contract-degrade"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-contract"))
    result = debate.run("moon?", rounds=0, judge=None, cfg=load_config(),
                        console=quiet(), sessions=HeadSessions())
    ct = trace(role="trailer", head="claude")[-1]
    assert ct["contract"] == "unparsed" and ct.get("raw")    # stored raw, marked unparsed
    fired = [r for r in trace(role="head_retry", head="claude") if r.get("kind") == "trailer"]
    assert len(fired) == 1                                    # the one retry was still tried
    assert "The moon is rock." in result.proposer_final      # prose kept — formatting never kills
    assert "=== TRAILER ===" not in result.proposer_final


# ── opponent confidence in the round-N message ──────────────────────────────────────────────
def test_roundN_message_carries_opponent_confidence(tmp_path, monkeypatch):
    armed(monkeypatch)
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-contract"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex-contract"))
    capture = tmp_path / "claude-stdin"                       # overwritten per call → last = round 1
    monkeypatch.setenv("COUNCIL_STUB_CAPTURE", str(capture))
    debate.run("moon?", rounds=1, judge=None, cfg=load_config(),
               console=quiet(), sessions=HeadSessions())
    assert "stated confidence 0.70" in capture.read_text()   # codex's round-0 confidence, shown to claude


# ── unarmed / knob off: no contract text anywhere ───────────────────────────────────────────
def test_unarmed_solo_turn_never_injects(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CONTRACT", "1")              # on, but solo must ignore it
    argv = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    renderer = debate.DebateRenderer(load_config(), quiet(), adversarial=False)
    renderer.handle("hello")
    assert "--append-system-prompt-file" not in argv.read_text()
    assert trace(role="trailer") == []


def test_contract_knob_off_skips_injection_and_trailers(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_CONTRACT", "0")              # explicit off
    monkeypatch.setenv("COUNCIL_STREAM_TAPE", "0")
    argv = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    debate.run("q", rounds=0, judge=None, cfg=load_config(),
               console=quiet(), sessions=HeadSessions())
    assert "--append-system-prompt-file" not in argv.read_text()
    assert trace(role="trailer") == []
