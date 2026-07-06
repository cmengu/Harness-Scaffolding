"""council/shadow.py — the understudy pattern (further_steps 5c): one question through
two configs, diff before switching. An eval harness in miniature — arm A = the config
you trust today, arm B = A plus the overrides you're auditioning. Both arms land in the
ledger as shadow_arm rows under one run_id, so a comparison stays addressable forever."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .config import Config, Heads, _apply, load_config
from .ledger import record


def parse_overrides(overrides: tuple[str, ...]) -> dict:
    """`--set key=value` pairs → a dict shaped like a config.toml load, so arm B reuses
    config._apply (same coercion, same bad-knob-falls-back behavior). Dotted `heads.*`
    keys address the Heads table (`--set heads.adversary=claude`)."""
    data: dict = {}
    for ov in overrides:
        key, sep, val = ov.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set wants KEY=VALUE, got {ov!r}")
        data[key.strip()] = val.strip()
    return data


def apply_overrides(cfg: Config, data: dict) -> Config:
    """Overlay overrides onto a fresh config. Unknown keys are ignored the same way a
    stale config.toml knob is — shadow compares configs, it doesn't validate them."""
    _apply(cfg, {k: v for k, v in data.items() if not k.startswith("heads.")})
    head_fields = {f for f in Heads.__dataclass_fields__}
    for key, val in data.items():
        if key.startswith("heads."):
            name = key.split(".", 1)[1]
            if name in head_fields:
                setattr(cfg.heads, name, val or None)
    return cfg


def run_shadow(prompt: str, overrides: tuple[str, ...], console: Console) -> None:
    """Run both arms (sequentially — a duel is already two subprocesses; four at once
    doubles the blast radius for no insight), record each, then present side by side."""
    from .debate import run as debate_run   # lazy: keeps `council --help` fast

    data = parse_overrides(overrides)
    cfg_a = load_config()
    cfg_b = apply_overrides(load_config(), data)
    record({"role": "run_start", "mode": "shadow", "overrides": list(overrides)})
    quiet = Console(quiet=True)              # arms render nothing; the comparison is the output
    arms: list[tuple[str, str, str]] = []
    for arm, cfg, label in (("A", cfg_a, "current config"),
                            ("B", cfg_b, "+ " + " ".join(overrides) if overrides else "no overrides")):
        with console.status(f"[dim]arm {arm} ({label}) thinking…[/]", spinner="dots"):
            r = debate_run(prompt, rounds=cfg.rounds, judge=cfg.judge_style, cfg=cfg, console=quiet)
        answer = r.synthesis or r.proposer_final
        record({"role": "shadow_arm", "arm": arm, "answer": answer,
                "overrides": list(overrides) if arm == "B" else []})
        arms.append((arm, label, answer))
    _present_arms(console, arms)


def _present_arms(console: Console, arms: list[tuple[str, str, str]]) -> None:
    """Same width-adaptive stance as debate._present: columns only when each arm gets
    readable prose width; narrow terminals get full-width blocks under rule headers."""
    if console.width >= 110:
        cols = Table.grid(padding=(0, 2))
        cols.add_column()
        cols.add_column()
        cols.add_row(*(f"[bold]## arm {arm}[/] [dim]({label})[/]\n{answer}"
                       for arm, label, answer in arms))
        console.print(cols)
    else:
        for arm, label, answer in arms:
            console.rule(f"arm {arm} [dim]({label})[/]", align="left")
            console.print(answer)
