"""council/config.py — thin config.
↔ omnigent cli.py (_load_effective_config) + onboarding model_catalog/*.json,
   trimmed from a multi-vendor registry down to three roles."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_LEDGER = Path.home() / ".council" / "ledger.jsonl"
CONFIG_TOML = Path.home() / ".council" / "config.toml"


@dataclass
class Heads:
    proposer: str = "claude"      # runs as `claude -p`   (THINK)
    adversary: str = "codex"      # runs as `codex exec`  (THINK + REVIEW)
    judge: str | None = None      # which FAMILY runs the judge (who); the STYLE = Config.judge_style


@dataclass
class Config:
    ledger_path: Path = DEFAULT_LEDGER
    claude_command: str = "claude"   # the REAL binary CODE wraps
    codex_command: str = "codex"
    rounds: int = 1                  # debate default               ↔ Debby SKILL.md:14-18
    head_timeout: int = 300          # per-head subprocess timeout, s (120 starved codex/extended thinking)
    turn_timeout: int = 600          # H1: max wait for a code-mode turn before the stall check
    submit_timeout: int = 10         # H2: max wait to confirm an inject submitted before failing loud
    # D4: inject timing knobs (seconds) — tune on slow machines instead of editing bridge.py
    tmux_ready_timeout: float = 30.0  # tmux target advertised + input box mounted
    paste_settle: float = 0.1         # gap between the paste committing and the submit Enter
    draft_watch_timeout: float = 5.0  # advisory-only: how long to watch for the draft to render
    submit_retry_interval: float = 1.0  # re-send Enter this often while the H2 receipt is missing
    boot_probe: bool = False         # D3: spend one turn at launch proving the H2 receipt loop works
    history_turns: int = 6           # ask-mode memory: past turns carried in the ledger preamble
    judge_style: str | None = None   # interactive-loop judge STYLE: None | 'moderator' | 'reasoning'
    heads: Heads = field(default_factory=Heads)


def load_config() -> Config:
    """Defaults ← ~/.council/config.toml ← COUNCIL_* env overrides.
    (Omnigent merges global+local+effective across ~250 lines; council keeps one file.)"""
    cfg = Config()
    if CONFIG_TOML.exists():
        try:
            data = tomllib.loads(CONFIG_TOML.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        _apply(cfg, data)
        for name, value in data.get("heads", {}).items():
            if name in {f.name for f in fields(Heads)}:
                setattr(cfg.heads, name, value)
    _apply(cfg, {
        k.removeprefix("COUNCIL_").lower(): v
        for k, v in os.environ.items() if k.startswith("COUNCIL_")
    })
    return cfg


def _apply(cfg: Config, data: dict) -> None:
    """Overlay known scalar keys onto cfg, coercing to the field's declared type."""
    for f in fields(Config):
        if f.name == "heads" or f.name not in data:
            continue
        raw = data[f.name]
        try:
            if f.name == "ledger_path":
                setattr(cfg, f.name, Path(raw).expanduser())
            elif f.type == "int":
                setattr(cfg, f.name, int(raw))
            elif f.type == "float":
                setattr(cfg, f.name, float(raw))
            elif f.type == "bool":
                setattr(cfg, f.name, raw if isinstance(raw, bool)
                        else str(raw).strip().lower() in ("1", "true", "yes", "on"))
            else:
                setattr(cfg, f.name, raw if raw != "" else None)
        except (TypeError, ValueError):
            continue     # a bad knob falls back to the default, never crashes startup
