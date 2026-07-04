"""council/cli.py — the front door: 2 commands, thin.  (`review` CUT from v1 — see G6, 3 Jul 2026.)
↔ omnigent cli.py:1161 (group), :1241 (main), the `claude` command (→ code), the `run` command (→ ask).
Dropped: 22 commands, ~28k lines of plumbing."""
from __future__ import annotations

import sys

import click
from rich.console import Console

from . import __version__
from .banner import render_banner
from .config import load_config

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="council")
def cli() -> None:
    """council — think and code with a cross-family second opinion."""


@cli.command()
@click.argument("question", required=False)
@click.option("-p", "--prompt", default=None, help="One-shot question (answer once, exit).")
@click.option("--rounds", default=None, type=int, help="Debate rounds when duelling (default: config).")
@click.option("--judge", default=None, type=click.Choice(["moderator", "reasoning"]),
              help="Judge style — explicit value REQUIRED (a bare --judge used to eat the question).")
@click.option("--duel/--no-duel", default=False, help="Start with the codex adversary ON (toggle live with /duel).")
def ask(question, prompt, rounds, judge, duel):
    """THINK — chat with Claude; summon the codex adversary per-question with /duel."""
    cfg = load_config()
    if rounds is not None:
        cfg.rounds = rounds        # CLI flags must reach EVERY turn, not just
    if judge is not None:
        cfg.judge_style = judge    # the first — the renderer reads cfg each turn
    render_banner(console, cfg, "ask")
    from .chat import run_loop                        # G1 loop
    from .debate import DebateRenderer                # G2 seam
    renderer = DebateRenderer(cfg, console, adversarial=duel)
    q = prompt or question
    if q:                                             # one-shot: answer once (solo or duel) and exit
        from .ledger import record
        record({"role": "session_start"})             # else _history_preamble inherits the PREVIOUS
        record({"role": "user", "text": q})           # session's tail as stale "memory"
        renderer.handle(q)
        return
    run_loop(renderer, cfg, console)                  # DEFAULT: interactive chat; /duel toggles codex


@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--resume", default=None, help="Resume the last coding session.")
@click.option("--command", "claude_command", default=None, help="Claude binary to wrap.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def code(resume, claude_command, claude_args):
    """CODE — branded front over the REAL Claude Code (the-harness gate live)."""
    if sys.platform == "win32":
        raise click.ClickException("council code needs a PTY (macOS/Linux).")
    cfg = load_config()
    render_banner(console, cfg, "code")
    from .wrap.session import run_claude_session     # G3 seam
    run_claude_session(
        claude_args=claude_args,
        use_claude_config=True,        # HARDWIRED → loads ~/.claude, so the-harness hooks fire
        command=claude_command or cfg.claude_command,
        resume=resume,
        cfg=cfg,
    )


# `review` command: CUT from v1 (3 Jul 2026). It imported a `review.py` that never existed
# (G6 defines launch.py + call-reviewer.sh instead) → guaranteed ImportError. Two-mode goal
# = ask + code; the G6 section stays as documented future work.


def main() -> None:
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
