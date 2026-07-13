"""The head contracts: stdin-vs-argv prompt delivery, JSON cost capture + raw fallback,
timeout, nonzero-exit stderr surfacing, and error classification."""
from __future__ import annotations

import subprocess

import pytest

from council.backends import HEAD_PROMPT, _classify, adversary, codex_usd, proposer
from council.config import load_config
from council.ledger import cost_usd, trace

from conftest import STUBS


def test_proposer_json_path_records_cost():
    out = proposer("what is the moon made of?", load_config())
    assert out == "STUB CLAUDE: the moon is made of rock"
    costs = trace(role="head_cost", head="claude")
    assert len(costs) == 1 and costs[0]["usd"] == 0.0042


def test_proposer_raw_text_fallback(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-plain"))
    out = proposer("q", load_config())
    assert out == "STUB CLAUDE PLAIN: not json at all"
    assert trace(role="head_cost") == []                 # no cost row without JSON


def test_prompt_travels_via_stdin(tmp_path, monkeypatch):
    capture = tmp_path / "stdin.txt"
    monkeypatch.setenv("COUNCIL_STUB_CAPTURE", str(capture))
    proposer("what is love", load_config())
    text = capture.read_text()
    assert "two voices in a council" in text             # HEAD_PROMPT leads
    assert "what is love" in text
    assert HEAD_PROMPT.splitlines()[0] in text


def test_claude_model_override_reaches_argv(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv_file))
    monkeypatch.setenv("COUNCIL_CLAUDE_MODEL", "claude-opus-4-8")
    proposer("q", load_config())
    assert "--model claude-opus-4-8" in argv_file.read_text()


def test_adversary_prompt_travels_via_argv(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv_file))
    out = adversary("what is love", load_config())
    assert out.startswith("STUB CODEX")
    lines = argv_file.read_text().splitlines()
    assert lines[0] == "exec"
    assert "read-only" in lines                          # sandboxed by construction
    assert any("what is love" in l for l in lines)


def test_codex_effort_override_reaches_argv(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("COUNCIL_STUB_ARGV", str(argv_file))
    monkeypatch.setenv("COUNCIL_CODEX_EFFORT", "high")
    adversary("q", load_config())
    assert "model_reasoning_effort=high" in argv_file.read_text()


def test_nonzero_exit_raises_with_stderr_tail(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-flaky-quota"))
    with pytest.raises(RuntimeError, match="429 rate limit"):
        proposer("q", load_config())


def test_hung_head_times_out(monkeypatch):
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude-slow"))
    monkeypatch.setenv("COUNCIL_STUB_SLEEP", "5")
    monkeypatch.setenv("COUNCIL_HEAD_TIMEOUT", "1")
    with pytest.raises(subprocess.TimeoutExpired):
        proposer("q", load_config())


def test_codex_usd_prices_tokens_at_list_rates():
    cfg = load_config()                                  # defaults: $1.25 / $0.125 / $10 per 1M
    # 1M non-cached input + 1M output = input rate + output rate.
    assert codex_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000}, cfg) == 11.25
    # cached input bills at the discounted rate; input_tokens is the full prompt incl. cached.
    assert codex_usd({"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000,
                      "output_tokens": 0}, cfg) == 0.125
    assert codex_usd({}, cfg) == 0.0                     # no usage → no charge


def test_codex_price_knobs_can_zero_out_to_token_only(monkeypatch):
    monkeypatch.setenv("COUNCIL_CODEX_PRICE_INPUT", "0")
    monkeypatch.setenv("COUNCIL_CODEX_PRICE_CACHED", "0")
    monkeypatch.setenv("COUNCIL_CODEX_PRICE_OUTPUT", "0")
    cfg = load_config()
    assert codex_usd({"input_tokens": 9_999, "output_tokens": 9_999}, cfg) == 0.0


def test_codex_session_path_records_dollar_cost():
    from council.backends import HeadSessions
    out = adversary("what is love", load_config(), session=HeadSessions())
    assert out.startswith("STUB CODEX")
    row = trace(role="head_cost", head="codex")[-1]
    assert row["tokens"]["output_tokens"] == 5           # raw tokens still recorded
    assert cost_usd(row) > 0                              # …and now priced, so codex spend shows


def test_classify_transient_vs_permanent():
    assert _classify(subprocess.TimeoutExpired(cmd="claude", timeout=1)) == "transient"
    assert _classify(RuntimeError("claude exited 1: 429 rate limit")) == "transient"
    assert _classify(RuntimeError("codex exited 1: quota exceeded")) == "transient"
    assert _classify(RuntimeError("Connection refused")) == "transient"
    assert _classify(RuntimeError("error: unexpected argument '--frobnicate'")) == "permanent"
    assert _classify(ValueError("empty response")) == "permanent"
