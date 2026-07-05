"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent chat.py:3805 (_run_repl), MINUS the SDK-event adapter — council swaps it for a Renderer.
Input strip ↔ omnigent-ui-sdk terminal/_host.py (TerminalHost) — the SAME primitive
(prompt_toolkit PromptSession: history + completer + toolbar), minus its layout surgery."""
from __future__ import annotations

import sys
from typing import Callable, Protocol

from rich.console import Console
from rich.table import Table

from .config import Config
from .ledger import RUN_ID, record, trace

# One table drives /help AND the completion popup — they can never drift apart.
_COMMANDS: list[tuple[str, str, str]] = [
    ("/duel", "[on|off]", "toggle the codex adversary (bare /duel flips it)"),
    ("/rounds", "N", "debate depth when duelling"),
    ("/judge", "<style>", "off · moderator · reasoning"),
    ("/status", "", "adversary · rounds · judge · heads · session cost"),
    ("/cost", "", "what this session has spent so far"),
    ("/last", "", "reprint the previous answer / debate"),
    ("/report", "[days]", "runs · cost · latency · failures from the ledger"),
    ("/show", "<run-id>", "replay one run (IDs listed by /report)"),
    ("/new", "", "fresh memory boundary (history preamble resets)"),
    ("/help", "", "all commands"),
    ("/exit", "", "leave"),
]


class Renderer(Protocol):
    """One method. THINK → debate columns (G2) · REVIEW → codex output (G6).
       (CODE is the exception — it owns its own live loop; see G1 note.)"""
    def handle(self, user_input: str) -> None: ...


def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit."""
    record({"role": "session_start"})        # memory boundary: the history preamble (G2) only reads
    read_input = _make_prompt(renderer, cfg, console)
    try:                                     # ledger rows AFTER the latest session_start
        while True:
            try:
                text = read_input().strip()
            except KeyboardInterrupt:
                continue                     # Ctrl+C abandons the draft, not the session
            except EOFError:                 # Ctrl+D (or exhausted piped stdin) leaves
                break
            if text in ("/exit", "/quit", "exit", "quit"):
                break
            if text.startswith("/"):
                _slash(text, renderer, console)  # slash commands may mutate renderer state (/duel)
                continue
            if not text:
                continue
            record({"role": "user", "text": text})
            renderer.handle(text)
    finally:
        _clear_title()


def _make_prompt(renderer, cfg: Config, console: Console) -> Callable[[], str]:
    """The input cockpit (C2): compact composer zone — separator bar, multiline input,
    status line — owned at the bottom while content scrolls natively above; never the
    alternate screen (the omnigent/Claude-Code principle). See composer.py.
    Falls back to plain console.input when stdin isn't a TTY (pipes, scripted runs) or
    prompt_toolkit is missing (stale editable install) — the loop can't tell the difference."""
    def marker() -> str:
        # Mode you can SEE: ⚔ › while the adversary is live, › solo, both in the theme accent.
        return "⚔ ›" if getattr(renderer, "adversarial", False) else "›"

    def status() -> str:
        # Re-read per render tick, so /duel · /rounds · /judge flips show without a redraw.
        # _spent() re-reads the ledger — cheap at solo scale, revisit if the file grows huge.
        duel = "⚔ duel" if getattr(renderer, "adversarial", False) else "solo"
        return (f" — {cfg.banner_title.lower()} · {duel} · rounds {cfg.rounds}"
                f" · judge {cfg.judge_style or 'off'} · ${_spent():.2f} · ^O report · /help ")

    fallback = lambda: console.input(f"[bold {cfg.accent_color}]{marker()}[/] ")
    if not sys.stdin.isatty():
        return fallback
    try:
        from .composer import Composer
    except ImportError:
        return fallback
    return Composer(console, accent=cfg.accent_color, title=cfg.banner_title.lower(),
                    marker=marker, status=status, commands=_COMMANDS,
                    history_path=cfg.ledger_path.parent / "history").read


def _clear_title() -> None:
    """Leave the tab bar as we found it (only matters on the TTY/prompt_toolkit path)."""
    if not sys.stdin.isatty():
        return
    try:
        from prompt_toolkit.shortcuts import clear_title
        clear_title()
    except ImportError:
        pass


def _slash(text: str, renderer, console: Console) -> None:
    """Slash commands. Two rules keep them safe and learnable: (1) every knob flips state BETWEEN
    turns only — handle() blocks, so a toggle can never interrupt a turn in flight (the 'fake
    toggle' trick Claude Code's own toggles use); (2) every state change echoes the NEW state
    and teaches the way back."""
    cfg = renderer.cfg
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    if cmd == "/help":
        _help(cfg, console)
    elif cmd == "/new":
        record({"role": "session_start"})    # fresh memory boundary, same process
        console.print("— new session — history preamble reset —")
    elif cmd == "/duel":
        import shutil
        renderer.adversarial = {"on": True, "off": False}.get(arg, not renderer.adversarial)
        if renderer.adversarial and not shutil.which(cfg.codex_command):
            renderer.adversarial = False     # fail loud, not a one-voiced "debate"
            console.print("[red]✗ codex not found — install @openai/codex first; staying solo[/]")
        else:
            console.print("⚔ adversary ON — codex will cross-examine every answer"
                          "  [dim](/duel again to turn off)[/]"
                          if renderer.adversarial else
                          "adversary off — plain claude chat  [dim](/duel to re-arm)[/]")
    elif cmd == "/rounds":
        if not arg.isdigit() or not 0 <= int(arg) <= 6:
            console.print(f"[red]usage: /rounds N (0–6)[/] — now {cfg.rounds}")
            return
        cfg.rounds = int(arg)                # renderer reads cfg each turn → live next question
        console.print(f"rounds = {cfg.rounds}" + ("  [dim](0 = answers only, no cross-critique)[/]"
                                                  if cfg.rounds == 0 else ""))
    elif cmd == "/judge":
        styles = {"off": None, "moderator": "moderator", "reasoning": "reasoning"}
        if arg not in styles:
            console.print(f"[red]usage: /judge off|moderator|reasoning[/] — now {cfg.judge_style or 'off'}")
            return
        cfg.judge_style = styles[arg]
        who = cfg.heads.judge or "claude"
        console.print(f"judge = {cfg.judge_style or 'off'}"
                      + (f"  [dim]({who} merges/weighs after each duel)[/]" if cfg.judge_style else ""))
    elif cmd == "/status":
        _status(renderer, console)
    elif cmd == "/cost":
        _cost(console)
    elif cmd == "/last":
        _last(console)
    elif cmd == "/report":
        from .report import summary
        days = int(arg) if arg.isdigit() else 7
        _view(console, cfg, f"report · last {days} day(s)",
              lambda: console.print(summary(days)))
    elif cmd == "/show":
        from .report import replay
        if not arg:
            console.print("[red]usage: /show <run-id>[/] — IDs listed by /report")
            return
        _view(console, cfg, f"run {arg}", lambda: replay(arg, console))
    else:
        console.print(f"[dim]unknown command {text!r} — try /help[/]")


def _view(console: Console, cfg: Config, title: str, render: Callable[[], None]) -> None:
    """Long output goes to a scrollable overlay on a TTY, plain print otherwise. The
    renderable is captured WITH its ANSI styling and replayed inside the overlay."""
    if not sys.stdin.isatty():
        render()
        return
    try:
        from .composer import show_overlay
    except ImportError:
        render()
        return
    with console.capture() as cap:
        render()
    show_overlay(title, cap.get(), accent=cfg.accent_color)


def _help(cfg: Config, console: Console) -> None:
    from rich.markup import escape          # "[on|off]" / "[days]" are literal, not style tags
    now = {"/rounds": f" · now {cfg.rounds}", "/judge": f" · now {cfg.judge_style or 'off'}"}
    t = Table(show_header=False, box=None, padding=(0, 2))
    for cmd, args, desc in _COMMANDS:
        if cmd == "/help":
            continue
        t.add_row(f"[bold]{escape((cmd + ' ' + args).strip())}[/]", f"[dim]{desc}{now.get(cmd, '')}[/]")
    console.print(t)


def _status(renderer, console: Console) -> None:
    """The in-REPL twin of the launch banner: every live knob on one card."""
    cfg = renderer.cfg
    duel = getattr(renderer, "adversarial", False)
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("adversary", "[bold]⚔ ON[/] — codex cross-examines every answer" if duel
              else "off — plain claude chat [dim](/duel to arm)[/]")
    t.add_row("rounds", str(cfg.rounds))
    t.add_row("judge", (cfg.judge_style or "off")
              + (f" · run by {cfg.heads.judge or 'claude'}" if cfg.judge_style else ""))
    t.add_row("heads", f"{cfg.claude_command} · {cfg.codex_command}")
    t.add_row("memory", f"last {cfg.history_turns} turns from the ledger")
    t.add_row("session", f"{RUN_ID} · {_spent():.2f} USD so far")
    console.rule("[bold]status[/]", style=cfg.accent_color, align="left")
    console.print(t)


def _cost(console: Console) -> None:
    calls = trace(run_id=RUN_ID, role="head_call")
    console.print(f"session {RUN_ID}: [bold]${_spent():.2f}[/] across {len(calls)} head call(s)"
                  "  [dim](claude head only — codex's CLI exposes no per-call cost)[/]")


def _spent() -> float:
    return sum(r.get("usd") or 0.0 for r in trace(run_id=RUN_ID, role="head_cost"))


def _last(console: Console) -> None:
    """Reprint the previous turn — columns for a duel, one voice for solo, verdict if judged."""
    turns = [r for r in trace(run_id=RUN_ID, role="debate") if r.get("round") is not None]
    if not turns:
        console.print("[dim]nothing yet this session[/]")
        return
    last = turns[-1]
    if last.get("adversary"):
        from .debate import _present
        _present(console, str(last.get("proposer", "")), str(last["adversary"]))
    else:
        console.print(f"[orange1]## 🟠 Claude[/]\n{last.get('proposer', '')}")
    judges = trace(run_id=RUN_ID, role="judge")
    if judges and judges[-1].get("ts", 0) > last.get("ts", 0):
        console.print(f"\n[bold]## ⚖ Synthesis[/] ({judges[-1].get('style')})\n{judges[-1].get('text', '')}")
