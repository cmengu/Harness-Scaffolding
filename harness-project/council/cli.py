"""council/cli.py — the front door: ask/code write, report/show read.  (`review` CUT from v1 — G6.)
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
    from .ledger import record
    record({"role": "run_start", "mode": "ask"})      # the run's header row — report/show thread on it
    render_banner(console, cfg, "ask")
    from .chat import run_loop                        # G1 loop
    from .debate import DebateRenderer                # G2 seam
    renderer = DebateRenderer(cfg, console, adversarial=duel)
    q = prompt or question
    if q:                                             # one-shot: answer once (solo or duel) and exit
        from .ledger import start_session
        start_session()                               # else _history_preamble inherits the PREVIOUS
        record({"role": "user", "text": q})           # session's tail as stale "memory"
        renderer.handle(q)
    else:
        run_loop(renderer, cfg, console)              # DEFAULT: interactive chat; /duel toggles codex
    from .ledger import RUN_ID                        # exit hint → the run stays addressable
    console.print(f"[dim]run {RUN_ID} — `council show {RUN_ID}` to replay[/]")


@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--resume", default=None, help="Resume the last coding session.")
@click.option("--command", "claude_command", default=None, help="Claude binary to wrap.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def code(resume, claude_command, claude_args):
    """CODE — branded front over the REAL Claude Code (the-harness gate live)."""
    if sys.platform == "win32":
        raise click.ClickException("council code needs a PTY (macOS/Linux).")
    cfg = load_config()
    from .ledger import record
    record({"role": "run_start", "mode": "code"})
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


@cli.command()
@click.argument("bridge_id", required=False)
def attach(bridge_id):
    """ATTACH — reconnect to a live code session (after /detach or a crashed wrapper)."""
    if sys.platform == "win32":
        raise click.ClickException("council attach needs a PTY (macOS/Linux).")
    cfg = load_config()
    from .wrap.bridge import list_bridges
    from .wrap.state import read_launch_state
    live = list_bridges()                       # dead bridge dirs are pruned as a side effect
    if not live:
        console.print("[dim]no running code sessions — start one with `council code`, "
                      "leave it with /detach[/]")
        return
    if bridge_id:
        hits = [b for b in live if b.name.startswith(bridge_id)]
        if len(hits) != 1:
            console.print(f"[red]{'ambiguous' if hits else 'unknown'} session {bridge_id!r}[/]"
                          " — bare `council attach` lists them")
            return
        bridge = hits[0]
    elif len(live) == 1:
        bridge = live[0]
    else:
        import time as _time
        from rich.table import Table
        t = Table(title="live code sessions", padding=(0, 2))
        for col in ("id", "started", "cwd"):
            t.add_column(col, style="dim" if col == "started" else "")
        for b in live:
            launch = read_launch_state(b)
            t.add_row(b.name,
                      _time.strftime("%d %b %H:%M", _time.localtime(launch.get("launched_at", 0))),
                      launch.get("cwd", "?"))
        console.print(t)
        console.print("[dim]council attach <id> to pick one[/]")
        return
    from .ledger import record as _record
    _record({"role": "run_start", "mode": "code"})
    render_banner(console, cfg, "attach")
    from .wrap.session import attach_claude_session
    attach_claude_session(bridge, cfg)


@cli.command()
@click.option("-p", "--prompt", "prompt", required=True, help="The question both arms answer.")
@click.option("--set", "overrides", multiple=True, metavar="KEY=VALUE",
              help="Config override for arm B (repeatable), e.g. --set rounds=2 --set judge_style=reasoning.")
def shadow(prompt, overrides):
    """SHADOW — one question under config A (current) and B (A + overrides), side by side."""
    from .shadow import run_shadow
    try:
        run_shadow(prompt, overrides, console)
    except ValueError as e:                  # malformed --set → a usage error, not a stack trace
        raise click.ClickException(str(e))
    from .ledger import RUN_ID
    console.print(f"[dim]run {RUN_ID} — `council show {RUN_ID}` to replay both arms[/]")


@cli.command()
@click.option("--days", default=7, show_default=True, help="Aggregation window.")
def report(days):
    """REPORT — runs · cost · latency · failure rate from the ledger (read-only)."""
    from .report import summary
    console.print(summary(days))


@cli.command()
@click.argument("run_id")
def show(run_id):
    """SHOW — replay one run from the ledger (IDs: `council report`)."""
    from .report import replay
    replay(run_id, console)


def main() -> None:
    cli(standalone_mode=True)


if __name__ == "__main__":
    main()
