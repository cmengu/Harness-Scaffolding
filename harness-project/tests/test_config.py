"""Config precedence (default < toml < env), coercion, and the bad-knob guarantee."""
from __future__ import annotations

from council.config import load_config


def test_defaults_hold_without_toml_or_env():
    cfg = load_config()
    assert cfg.rounds == 1
    assert cfg.head_timeout == 300
    assert cfg.head_retries == 2
    assert cfg.judge_style is None
    assert cfg.boot_probe is False
    assert cfg.heads.proposer == "claude"


def test_toml_overrides_defaults(tmp_path, monkeypatch):
    from council import config as config_mod
    toml = tmp_path / "config.toml"
    toml.write_text('rounds = 4\n[heads]\nadversary = "claude"\n')
    monkeypatch.setattr(config_mod, "CONFIG_TOML", toml)
    cfg = load_config()
    assert cfg.rounds == 4
    assert cfg.heads.adversary == "claude"


def test_env_beats_toml(tmp_path, monkeypatch):
    from council import config as config_mod
    toml = tmp_path / "config.toml"
    toml.write_text("rounds = 4\n")
    monkeypatch.setattr(config_mod, "CONFIG_TOML", toml)
    monkeypatch.setenv("COUNCIL_ROUNDS", "7")
    assert load_config().rounds == 7


def test_bad_knob_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("COUNCIL_HEAD_TIMEOUT", "banana")
    assert load_config().head_timeout == 300     # never crashes startup


def test_bool_and_float_coercion(monkeypatch):
    monkeypatch.setenv("COUNCIL_BOOT_PROBE", "yes")
    monkeypatch.setenv("COUNCIL_RETRY_BASE_DELAY", "0.5")
    cfg = load_config()
    assert cfg.boot_probe is True
    assert cfg.retry_base_delay == 0.5


def test_empty_string_means_none_for_optional_knobs(monkeypatch):
    monkeypatch.setenv("COUNCIL_JUDGE_STYLE", "")
    assert load_config().judge_style is None
