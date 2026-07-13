"""Depth pack (step 3, 11 Jul): duel = max thinking + research tools, solo = fast by
default but configurable. Stub hooks: COUNCIL_STUB_ARGV (argv), COUNCIL_STUB_ENV
(claude stub echoes MAX_THINKING_TOKENS)."""
from __future__ import annotations

from rich.console import Console

from council import chat, debate
from council.backends import HeadSessions, adversary, proposer
from council.config import load_config


def quiet():
    return Console(quiet=True)


def test_duel_arms_thinking_and_tools(tmp_path, monkeypatch):
    argv, env = tmp_path / "argv", tmp_path / "env"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    monkeypatch.setenv("COUNCIL_STUB_ENV", str(env))
    debate.run("q", rounds=0, judge=None, cfg=load_config(), console=quiet())
    # codex stub wrote argv last only if it ran second — capture per-head via two asserts
    # on whichever survived is flaky; claude's env capture is unambiguous:
    assert "MAX_THINKING_TOKENS=31999" in env.read_text()


def test_duel_claude_argv_carries_tool_allowlist(tmp_path, monkeypatch):
    argv = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    proposer("q", load_config(), thinking=31999, tools=True)
    text = argv.read_text()
    assert "Read Grep Glob WebSearch WebFetch" in text
    assert "Bash" not in text                       # no shell in v1, ever


def test_duel_codex_argv_carries_effort_and_search(tmp_path, monkeypatch):
    argv = tmp_path / "argv"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    adversary("q", load_config(), session=HeadSessions(), effort="high", tools=True)
    lines = argv.read_text().splitlines()
    assert "model_reasoning_effort=high" in lines
    assert "tools.web_search=true" in lines


def test_solo_defaults_stay_fast(tmp_path, monkeypatch):
    argv, env = tmp_path / "argv", tmp_path / "env"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    monkeypatch.setenv("COUNCIL_STUB_ENV", str(env))
    debate.DebateRenderer(load_config(), quiet(), adversarial=False).handle("hi")
    assert "Read Grep" not in argv.read_text()              # tools off
    assert env.read_text().strip() == "MAX_THINKING_TOKENS="  # thinking env not set


def test_solo_can_be_armed_by_config(tmp_path, monkeypatch):
    argv, env = tmp_path / "argv", tmp_path / "env"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv))
    monkeypatch.setenv("COUNCIL_STUB_ENV", str(env))
    monkeypatch.setenv("COUNCIL_SOLO_TOOLS", "true")
    monkeypatch.setenv("COUNCIL_SOLO_THINKING_TOKENS", "8000")
    debate.DebateRenderer(load_config(), quiet(), adversarial=False).handle("hi")
    assert "Read Grep Glob WebSearch WebFetch" in argv.read_text()
    assert "MAX_THINKING_TOKENS=8000" in env.read_text()


def test_head_prompt_matches_tool_state():
    from council.backends import _head_prompt
    assert "NO tools" in _head_prompt(False)
    assert "read-only research tools" in _head_prompt(True)


def test_think_and_tools_slash_commands():
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=False)
    chat._slash("/think duel off", r, quiet())
    assert cfg.duel_thinking_tokens == 0
    chat._slash("/think solo max", r, quiet())
    assert cfg.solo_thinking_tokens == 31999
    chat._slash("/think duel 90000", r, quiet())
    assert cfg.duel_thinking_tokens == 31999        # clamped to the ultrathink cap
    chat._slash("/tools duel off", r, quiet())
    assert cfg.duel_tools is False
    chat._slash("/tools solo on", r, quiet())
    assert cfg.solo_tools is True
    chat._slash("/think nonsense max", r, quiet())  # bad mode → usage, no crash, no change
    assert cfg.duel_thinking_tokens == 31999


def test_model_aliases_and_head_guessing():
    """12 Jul: /model claude opus expands the alias; /model opus infers the head;
    unknown names still ship verbatim (warn-never-block survives the sugar)."""
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=False)
    chat._slash("/model claude opus", r, quiet())
    assert cfg.claude_model == "claude-opus-4-8"
    chat._slash("/model fable", r, quiet())               # bare alias → claude
    assert cfg.claude_model == "claude-fable-5"
    chat._slash("/model gpt-5.5", r, quiet())             # gpt-* → codex
    assert cfg.codex_model == "gpt-5.5"
    chat._slash("/model claude made-up-name", r, quiet())  # verbatim passthrough
    assert cfg.claude_model == "made-up-name"
    chat._slash("/model claude reset", r, quiet())        # per-head reset
    assert cfg.claude_model is None and cfg.codex_model == "gpt-5.5"
    chat._slash("/model reset", r, quiet())
    assert cfg.codex_model is None


def test_think_named_levels_and_effort_xhigh():
    cfg = load_config()
    r = debate.DebateRenderer(cfg, quiet(), adversarial=False)
    chat._slash("/think duel high", r, quiet())
    assert cfg.duel_thinking_tokens == 16000
    chat._slash("/think solo low", r, quiet())
    assert cfg.solo_thinking_tokens == 4000
    chat._slash("/effort xhigh", r, quiet())
    assert cfg.codex_effort == "xhigh"


def test_slash_arg_words_vocab():
    """The popup vocabulary: finite knobs complete, free-text stays silent."""
    assert "claude" in chat._slash_arg_words("/model", 0, [])
    assert "opus" in chat._slash_arg_words("/model", 1, ["claude"])
    assert "gpt-5.5" in chat._slash_arg_words("/model", 1, ["codex"])
    assert "xhigh" in chat._slash_arg_words("/effort", 0, [])
    assert "high" in chat._slash_arg_words("/think", 1, ["duel"])
    assert chat._slash_arg_words("/note", 0, []) == ()    # free text — no popup
