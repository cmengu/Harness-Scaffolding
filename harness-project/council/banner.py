"""council/banner.py — branded startup banner.
↔ omnigent repl/_repl.py:248 (_StartupHeader) + _display_cwd; the mascot layout mirrors
  omnigent's _render_startup_banner_ansi (mascot + box read as ONE accent — #F43BA6 there,
  cfg.accent_color here). The art itself is a config knob, so the repo ships no brand."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config


def render_banner(console: Console, cfg: Config, mode: str) -> None:
    """Paint council's skin once, at launch. Same skin for all 3 modes — only the subtitle
    changes — so the user never sees a different UI when CODE swaps in the hidden Claude Code."""
    subtitle = {
        "ask":    f"think · {cfg.heads.proposer} · /duel summons {cfg.heads.adversary}"
                  + (f" · judge:{cfg.heads.judge}" if cfg.heads.judge else ""),
        "code":   "code · Claude Code + the-harness  (hidden engine)",
        "attach": "code · reattached to a running hidden engine",
    }[mode]   # ("review" cut from v1 — G6)
    info = Text.assemble(
        (subtitle + "\n", "cyan"),
        (f"cwd  {_display_cwd()}\n", "dim"),
        (f"log  {cfg.ledger_path}", "dim"),
    )
    if cfg.banner_art:      # omnigent-style: outline mascot left, name + info right, one accent.
        # Name is bold DEFAULT (white on dark) like omnigent's header — only art/border carry
        # the accent, so the text column stays readable on any accent color.
        head = Text.assemble((cfg.banner_title + "\n", "bold"))
        if cfg.banner_tagline:
            head.append(cfg.banner_tagline + "\n")
        head.append("\n")
        head.append_text(info)
        grid = Table.grid(padding=(0, 3))
        grid.add_column(vertical="middle")
        grid.add_column(vertical="middle")
        grid.add_row(Text(cfg.banner_art.strip("\n"), style=cfg.accent_color), head)
        console.print(Panel(grid, border_style=cfg.accent_color))
    else:                   # classic: title in the border, no mascot (the repo default)
        console.print(Panel(info, title=Text(f"⚖  {cfg.banner_title}", style="bold"),
                            border_style=cfg.accent_color))


def _display_cwd() -> str:
    p = Path.cwd()
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)
