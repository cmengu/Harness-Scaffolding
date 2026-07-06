"""Bonus 5c: shadow mode — same question, two configs, one addressable comparison."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from council.cli import cli
from council.config import load_config
from council.ledger import trace
from council.shadow import apply_overrides, parse_overrides


def test_parse_overrides_wants_key_value():
    assert parse_overrides(("rounds=2", "judge_style=reasoning")) == {
        "rounds": "2", "judge_style": "reasoning"}
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_overrides(("rounds",))


def test_apply_overrides_reuses_config_coercion():
    cfg = apply_overrides(load_config(), {"rounds": "3", "heads.adversary": "claude",
                                          "no_such_knob": "x"})
    assert cfg.rounds == 3                               # coerced like a toml knob
    assert cfg.heads.adversary == "claude"               # dotted keys reach the Heads table
    assert not hasattr(cfg, "no_such_knob")              # unknown keys ignored, not fatal


def test_shadow_runs_both_arms_and_records_them():
    result = CliRunner().invoke(cli, ["shadow", "-p", "moon?", "--set", "rounds=0"])
    assert result.exit_code == 0, result.output
    assert "arm A" in result.output and "arm B" in result.output
    arms = {r["arm"]: r for r in trace(role="shadow_arm")}
    assert set(arms) == {"A", "B"}
    assert arms["B"]["overrides"] == ["rounds=0"]
    assert "STUB CLAUDE" in arms["A"]["answer"]
    assert arms["A"]["run_id"] == arms["B"]["run_id"]    # one run id = one addressable comparison


def test_shadow_rejects_malformed_set():
    result = CliRunner().invoke(cli, ["shadow", "-p", "q", "--set", "rounds"])
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output
