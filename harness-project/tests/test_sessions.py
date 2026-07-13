"""Head sessions (11 Jul): mint on the first armed message, resume after, drop on boundary.
Stub contract: claude echoes session_id "stub-claude-sid"; codex under --json emits the
exec --json JSONL schema with thread_id "stub-thread-1"."""
from __future__ import annotations

from rich.console import Console

from council import debate
from council.backends import HeadSessions, adversary, proposer
from council.config import load_config
from council.ledger import record, start_session, trace

from conftest import STUBS


def quiet():
    return Console(quiet=True)


def test_proposer_mints_then_resumes(tmp_path, monkeypatch):
    capture = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(capture))
    s = HeadSessions()
    proposer("q", load_config(), session=s)
    assert "--session-id" in capture.read_text()          # first call mints
    assert s.claude == "stub-claude-sid"                  # id captured from the payload
    proposer("q2", load_config(), session=s)
    assert "--resume stub-claude-sid" in capture.read_text()


def test_proposer_sessionless_argv_unchanged(tmp_path, monkeypatch):
    capture = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(capture))
    proposer("q", load_config())
    argv = capture.read_text()
    assert "--session-id" not in argv and "--resume" not in argv


def test_adversary_mints_then_resumes(tmp_path, monkeypatch):
    capture = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(capture))
    s = HeadSessions()
    out = adversary("q", load_config(), session=s)
    assert "cheese" in out                                # answer text out of the JSONL
    assert s.codex == "stub-thread-1"                     # thread_id captured
    argv = capture.read_text().splitlines()
    assert "--json" in argv and "--skip-git-repo-check" in argv
    tokens = trace(role="head_cost", head="codex")
    assert tokens and tokens[-1]["tokens"]["output_tokens"] == 5
    adversary("q2", load_config(), session=s)
    argv = capture.read_text().splitlines()
    assert argv[:3] == ["exec", "resume", "stub-thread-1"] or argv[1:4] == ["exec", "resume", "stub-thread-1"]


def test_adversary_sessionless_stays_plain(tmp_path, monkeypatch):
    capture = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(capture))
    out = adversary("q", load_config())
    assert out == "STUB CODEX: disagree - the moon is made of cheese"
    assert "--json" not in capture.read_text().splitlines()


def test_debate_seeds_round_zero_only(tmp_path, monkeypatch):
    stdin_capture = tmp_path / "stdin"
    monkeypatch.setenv("COUNCIL_STUB_CAPTURE", str(stdin_capture))
    debate.run("the question", rounds=1, judge=None, cfg=load_config(),
               console=quiet(), sessions=HeadSessions(), seed="SEEDMARK\n\n")
    last = stdin_capture.read_text()                      # capture holds the LAST claude call
    assert "SEEDMARK" not in last                         # round 1 sends only the delta…
    assert "The other voice said" in last                 # …the critique message


def test_renderer_first_armed_message_records_session_row():
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=True)
    r.handle("q")
    assert r.sessions is not None and r.sessions.claude == "stub-claude-sid"
    rows = trace(role="head_session")
    assert rows and rows[-1]["claude"] == "stub-claude-sid" \
        and rows[-1]["codex"] == "stub-thread-1"
    r.reset_sessions()
    assert r.sessions is None


def test_renderer_head_sessions_off_keeps_stateless_path(tmp_path, monkeypatch):
    capture = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(capture))
    monkeypatch.setenv("COUNCIL_HEAD_SESSIONS", "false")
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=True)
    r.handle("q")
    assert r.sessions is None                             # knob off = old behavior exactly
    assert not trace(role="head_session")


def test_permanent_failure_clears_that_heads_session(monkeypatch):
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "claude-bad-flag"))
    monkeypatch.setenv("COUNCIL_HEAD_RETRIES", "0")
    s = HeadSessions(claude="keep-me", codex="stale-thread")
    debate._safe(adversary, "q", load_config(), "codex", s)
    assert s.codex is None                                # next duel reseeds codex…
    assert s.claude == "keep-me"                          # …claude's memory survives


def test_half_minted_pair_reseeds_only_the_dead_head(monkeypatch):
    """12 Jul regression: claude died on turn 1 (_safe cleared its session) while codex
    minted. Turn 2 must hand the seed to the unminted head ONLY — before the fix the
    seed rode all-or-nothing on _fresh(), so the dead head resumed nothing AND got no
    preamble: a permanent cold start ("I don't have the earlier analysis")."""
    monkeypatch.setenv("COUNCIL_STREAM_TAPE", "false")
    calls = []

    def fake_both(msg_a, msg_b, cfg, console, sessions=None, depth=None,
                  round_no=0, live=True, con_a="", con_b=""):
        calls.append((msg_a, msg_b))
        return "A-ans", "B-ans"

    monkeypatch.setattr(debate, "_both", fake_both)
    s = HeadSessions(codex="stub-thread-1")               # post-failure: claude unminted
    debate.run("the question", rounds=0, judge=None, cfg=load_config(),
               console=quiet(), sessions=s, seed="SEEDMARK\n\n")
    msg_a, msg_b = calls[0]
    assert msg_a.startswith("SEEDMARK")                   # recovering claude gets the back-story…
    assert msg_b == "the question"                        # …the live codex head never doubles it


def test_renderer_rebriefs_head_cleared_by_failure(tmp_path, monkeypatch):
    """Renderer-level twin of the above: after a turn where one head's session was
    cleared, the next armed turn re-briefs that head with the ledger preamble and
    it re-mints — instead of cold-starting with the bare new message."""
    monkeypatch.setenv("COUNCIL_ROUNDS", "0")             # capture holds the LAST claude call
    start_session()                                       # chain_rows scopes to the active session
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=True)
    record({"role": "user", "text": "first question"})
    r.handle("first question")                            # healthy turn: both heads mint
    assert r.sessions.claude == "stub-claude-sid"
    r.sessions.clear("claude")                            # what _safe does on permanent failure
    record({"role": "user", "text": "redo it"})
    capture = tmp_path / "stdin"
    monkeypatch.setenv("COUNCIL_STUB_CAPTURE", str(capture))
    r.handle("redo it")
    text = capture.read_text()
    assert "Conversation so far" in text                  # the preamble reached the recovering head
    assert "first question" in text                       # …carrying the turn it missed
    assert r.sessions.claude == "stub-claude-sid"         # and it re-minted
