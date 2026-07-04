"""council/banner.py — branded startup banner.
↔ omnigent repl/_repl.py:248 (_StartupHeader) + _display_cwd."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config


def render_banner(console: Console, cfg: Config, mode: str) -> None:
    """Paint council's skin once, at launch. Same skin for all 3 modes — only the subtitle
    changes — so the user never sees a different UI when CODE swaps in the hidden Claude Code."""
    subtitle = {
        "ask":    f"think · {cfg.heads.proposer} · /duel summons {cfg.heads.adversary}"
                  + (f" · judge:{cfg.heads.judge}" if cfg.heads.judge else ""),
        "code":   "code · Claude Code + the-harness  (hidden engine)",
    }[mode]   # ("review" cut from v1 — G6)
    body = Text.assemble(
        (subtitle + "\n", "cyan"),
        (f"cwd  {_display_cwd()}\n", "dim"),
        (f"log  {cfg.ledger_path}", "dim"),
    )
    console.print(Panel(body, title=Text("⚖  COUNCIL", style="bold"), border_style="blue"))


def _display_cwd() -> str:
    p = Path.cwd()
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)
