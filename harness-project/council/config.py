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
    head_sessions: bool = True       # duel memory via native resume (claude --resume / codex exec
                                     # resume; probes 11 Jul). False = every round reseeds from the
                                     # ledger preamble (the old stateless behavior, kept as fallback)
    # Depth pack (step 3; decisions 10-11 Jul: duel = max depth, cost accepted; solo = fast
    # by default but the owner may arm it). claude thinks via MAX_THINKING_TOKENS (0 = off;
    # 31999 = the interactive "ultrathink" cap); codex depth = /effort (codex_effort below).
    duel_thinking_tokens: int = 31999
    duel_tools: bool = True          # duel heads may research: claude claude_tools, codex web search
    solo_thinking_tokens: int = 0
    solo_tools: bool = False
    claude_tools: str = "Read Grep Glob WebSearch WebFetch"   # the allowlist when tools are on (no Bash in v1)
    # The tape (step 5): duels stream both heads interleaved into one scroll column.
    stream_tape: bool = True         # false = the classic block-then-present duel
    claude_glyph: str = "✳"          # gutter mark per head (terminals can't render logos;
    codex_glyph: str = "⬡"           # ✳ = Anthropic starburst, ⬡ = nearest to the OpenAI knot)
    head_retries: int = 2            # attempts AFTER the first try — spent on TRANSIENT failures only
    retry_base_delay: float = 1.0    # backoff between attempts: 1s → 2s → 4s
    ask_budget_usd: float = 0.0      # ask-mode budget; > 0 = red nag in the turn receipt once crossed
    judge_style: str | None = None   # interactive-loop judge STYLE: None | 'moderator' | 'reasoning'
    # Ask-mode head overrides (/model · /effort flip these live). None = the CLI's own default.
    claude_model: str | None = None
    codex_model: str | None = None
    codex_effort: str | None = None  # codex -c model_reasoning_effort: minimal·low·medium·high
    code_budget_usd: float = 0.0     # code-mode session budget; 0 = off. The PreToolUse gate
                                     # ASKs at each crossed multiple (checkpoint ladder).
    # Theme — the banner/prompt skin. GENERIC defaults on purpose: the public repo stays
    # unbranded; a private skin (name, accent, mascot) lives in ~/.council/config.toml only.
    banner_title: str = "COUNCIL"
    banner_tagline: str = ""         # one-liner under the title (omnigent's "Multi-agent coding …" slot)
    accent_color: str = "blue"       # rich color name or hex; border + mascot + prompt = ONE accent
    banner_art: str = ""             # multi-line outline mascot (polly-style); "" = classic banner
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
