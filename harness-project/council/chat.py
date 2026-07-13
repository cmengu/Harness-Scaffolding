"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent chat.py:3805 (_run_repl), MINUS the SDK-event adapter — council swaps it for a Renderer.
Input strip ↔ omnigent-ui-sdk terminal/_host.py (TerminalHost) — the SAME primitive
(prompt_toolkit PromptSession: history + completer + toolbar), minus its layout surgery."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Protocol

from rich.console import Console
from rich.table import Table

from . import flight
from .config import Config
from .ledger import (RUN_ID, chain_rows, cost_usd, record, sessions,
                     start_session, trace)

# One table drives /help AND the completion popup — they can never drift apart.
_COMMANDS: list[tuple[str, str, str]] = [
    ("/duel", "[on|off]", "toggle the codex adversary (or Shift+Tab; bare /duel flips it)"),
    ("/note", "<fact>", "hand the heads a fact — no models fire, rides into the next turn"),
    ("/rounds", "N", "debate depth when duelling"),
    ("/judge", "<style>", "off · moderator · reasoning"),
    ("/model", "[head name]", "per-head model — /model claude opus · /model codex gpt-5.5 · reset"),
    ("/effort", "[level]", "codex reasoning effort: minimal·low·medium·high·xhigh · reset"),
    ("/think", "<duel|solo> <level|n>", "claude thinking: low·medium·high·max·off or a token count"),
    ("/tools", "<duel|solo> <on|off>", "may the heads research? (read-only files + web)"),
    ("/tape", "", "show/hide the dim thinking/tool lines while heads stream (or Ctrl+T)"),
    ("/status", "", "adversary · rounds · judge · heads · session cost"),
    ("/cost", "", "what this session has spent so far"),
    ("/last", "", "reprint the previous answer / debate"),
    ("/history", "", "everything the heads currently remember"),
    ("/context", "", "memory size vs the cap (chars · turns · summary)"),
    ("/compact", "", "squash memory into a summary, keep going"),
    ("/switch", "[#|id]", "list past conversations · resume one"),
    ("/fork", "[title]", "branch this conversation: same memory, new tangent"),
    ("/new", "", "fresh memory boundary (history preamble resets)"),
    ("/report", "[days]", "runs · cost · latency · failures from the ledger"),
    ("/show", "<run-id>", "replay one run (IDs listed by /report)"),
    ("/help", "", "all commands"),
    ("/exit", "", "leave"),
]


class Renderer(Protocol):
    """One method. THINK → debate columns (G2) · REVIEW → codex output (G6).
       (CODE is the exception — it owns its own live loop; see G1 note.)"""
    def handle(self, user_input: str) -> None: ...


_SAFE_MID_TURN = {"/note", "/status", "/cost", "/tape", "/help"}   # can't corrupt a turn in flight


def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit.
    On a TTY the composer is PARKED, never blocked (step 7, omnigent's park-and-wake
    collapsed to one process): the turn runs on a worker thread, the tape prints through
    prompt_toolkit's patch_stdout (lines land above the prompt), and you keep typing —
    /note lands mid-duel, a second question queues with a visible chip, one turn in
    flight at a time. ^C at the prompt abandons the draft; ^C while a turn runs CANCELS
    it (kills the heads, clears the queue) and the unanswered question stays out of
    memory (_chain_turns holds a question back until a debate row answers it).
    Piped/non-TTY runs keep the classic synchronous loop — same inputs, same outputs."""
    import threading
    from collections import deque

    start_session()                          # memory boundary: the history preamble (G2) only
    turn: list = [None]                      # the in-flight worker thread, if any
    pending: deque[str] = deque()

    def turn_alive() -> bool:
        return turn[0] is not None and turn[0].is_alive()

    esc_armed = [0.0]                        # monotonic deadline while the Esc confirm is armed

    def cancel_via_esc() -> None:
        """Bare Esc on an empty box (backlog item 7). Esc is a twitch key and a duel is
        minutes of paid work, so the first press only ARMS: the status bar shows '⚠ Esc
        again' for 2s and the second press kills the heads. Unlike ^C, the queue is KEPT —
        the worker rolls straight into the next question (^C stays the cancel-everything)."""
        if not turn_alive():
            esc_armed[0] = 0.0
            return
        if time.monotonic() < esc_armed[0]:
            esc_armed[0] = 0.0
            from .backends import kill_inflight
            kill_inflight()
            console.print("[yellow]✗ turn cancelled — queued questions kept (^C clears them too)[/]")
        else:
            esc_armed[0] = time.monotonic() + 2.0

    read_input = _make_prompt(renderer, cfg, console, busy=turn_alive,
                              queued=lambda: len(pending), esc_armed=esc_armed,
                              on_cancel=cancel_via_esc)
    interactive = sys.stdin.isatty()
    if interactive:
        renderer.live_status = False         # composer owns the bottom; no Rich Live spinners
    patch = _patch_stdout() if interactive else None

    def do_turn(text: str) -> None:          # one question, synchronously (whatever thread)
        record({"role": "user", "text": text})
        calls_before = len(trace(run_id=RUN_ID, role="head_call"))
        spent_before = _spent()
        try:
            renderer.handle(text)
        except KeyboardInterrupt:            # only reachable on the synchronous path
            record({"role": "debate", "event": "cancelled"})
            console.print("\n[yellow]✗ cancelled — answer discarded, question kept out of memory[/]")
        else:
            _turn_line(console, calls_before, spent_before, cfg)
        finally:
            flight.turn_over()               # the panel describes ONE turn; stale chips lie

    def worker(text: str) -> None:           # the parked turn: run, then drain the queue
        while True:
            do_turn(text)
            if not pending:
                return
            text = pending.popleft()
            console.print(f"[dim]▶ next: {text[:60]}[/]")

    try:
        with patch or _nullctx():
            while True:
                try:
                    text = read_input().strip()
                except KeyboardInterrupt:
                    if turn_alive():         # ^C with a turn running = cancel the turn
                        from .backends import kill_inflight
                        kill_inflight()
                        pending.clear()
                        console.print("[yellow]✗ cancelling the turn — queue cleared[/]")
                    continue                 # otherwise: abandons the draft, not the session
                except EOFError:             # Ctrl+D (or exhausted piped stdin) leaves
                    break
                if text in ("/exit", "/quit", "exit", "quit"):
                    break
                if text.startswith("/"):
                    if turn_alive() and text.split()[0] not in _SAFE_MID_TURN:
                        console.print("[dim]turn in flight — /note /status /cost /tape /help work "
                                      "now; Esc Esc or ^C cancels the turn[/]")
                        continue
                    _slash(text, renderer, console)  # may mutate renderer state (/duel)
                    continue
                if not text:
                    continue
                if turn_alive():             # one duel in flight; the next question queues
                    pending.append(text)
                    console.print(f"[dim]⧗ queued ({len(pending)}): {text[:60]}[/]")
                    continue
                if interactive and hasattr(renderer, "prepare_briefing"):
                    renderer.prepare_briefing(text)   # the popup runs BETWEEN reads, main thread
                if interactive:
                    turn[0] = threading.Thread(target=worker, args=(text,), daemon=True)
                    turn[0].start()
                else:
                    do_turn(text)            # piped: the classic synchronous loop
    finally:
        if turn_alive():
            from .backends import kill_inflight
            kill_inflight()
            turn[0].join(timeout=5)
        _clear_title()


def _patch_stdout():
    """Prints from the worker thread must land ABOVE the live prompt, not through it.
    raw=True keeps Rich's ANSI styling intact. None (plain loop) if pt is missing."""
    try:
        from prompt_toolkit.patch_stdout import patch_stdout
        return patch_stdout(raw=True)
    except ImportError:
        return None


def _nullctx():
    from contextlib import nullcontext
    return nullcontext()


def _turn_line(console: Console, calls_before: int, spent_before: float, cfg: Config) -> None:
    """One dim receipt after each turn: per-head seconds (✗ = that head failed) + the
    turn's cost delta. Data is already in the ledger (head_call/head_cost) — this just
    surfaces what /report aggregates, while the turn is still on screen. When an
    ask_budget_usd is set and the run has crossed it, the receipt carries a red nag
    (the code-mode counterpart is the PreToolUse checkpoint ladder)."""
    calls = trace(run_id=RUN_ID, role="head_call")[calls_before:]
    if not calls:
        return
    icon = {"claude": cfg.claude_glyph, "codex": cfg.codex_glyph, "judge": "⚖", "compact": "⧉"}
    parts = [f"{icon.get(c.get('head'), '·')} {c.get('secs', 0):.1f}s" + ("" if c.get("ok") else " ✗")
             for c in calls]
    spent = _spent()
    delta = spent - spent_before
    over = (f"  ·  [red]⚠ over budget (${spent:.2f} > ${cfg.ask_budget_usd:.2f})[/red]"
            if cfg.ask_budget_usd and spent > cfg.ask_budget_usd else "")
    console.print(f"[dim]{'  ·  '.join(parts)}{f'  ·  ~${delta:.2f}' if delta >= 0.005 else ''}{over}[/]")


def _make_prompt(renderer, cfg: Config, console: Console,
                 busy: Callable[[], bool] = lambda: False,
                 queued: Callable[[], int] = lambda: 0,
                 esc_armed: list | None = None,
                 on_cancel: Callable[[], None] | None = None) -> Callable[[], str]:
    """The input cockpit (C2): compact composer zone — separator bar, multiline input,
    status line — owned at the bottom while content scrolls natively above; never the
    alternate screen (the omnigent/Claude-Code principle). See composer.py.
    Falls back to plain console.input when stdin isn't a TTY (pipes, scripted runs) or
    prompt_toolkit is missing (stale editable install) — the loop can't tell the difference."""
    def marker() -> str:
        # Mode you can SEE: ⚔ › while the adversary is live, › solo, both in the theme accent.
        return "⚔ ›" if getattr(renderer, "adversarial", False) else "›"

    def status() -> list:
        # THE FLIGHT PANEL (backlog 6+7+9): fragments, re-read per 0.5s render tick, so
        # /duel flips, head phases, the Esc confirm and queue depth all show without a
        # redraw. While a turn runs: per-head glyph + phase + elapsed, ⚠ quiet once a
        # head's SILENCE crosses half of head_timeout (the same idle clock the watchdog
        # kills on — you get to judge "stuck" before the machine does), ~$ as each head's
        # receipt lands. Dollar figures are ~estimates at API list prices (a subscription
        # is billed by plan limits, not these numbers).
        # _spent() re-reads the ledger — cheap at solo scale, revisit if the file grows huge.
        tb = "class:bottom-toolbar"
        warn = f"{tb} fg:ansiyellow bold"
        duel = "⚔ duel" if getattr(renderer, "adversarial", False) else "solo"
        frags: list[tuple[str, str]] = [(tb, " — ")]
        if esc_armed and esc_armed[0] > time.monotonic():
            frags.append((warn, "⚠ Esc again to cancel the turn"))
            frags.append((tb, " · "))
        if busy():
            heads, _ = flight.snapshot()
            now = time.monotonic()
            glyphs = {"claude": cfg.claude_glyph, "codex": cfg.codex_glyph, "judge": "⚖"}
            for head, info in heads:
                g = glyphs.get(head, "·")
                if info["done"]:
                    frags.append((tb, f"{g} ✓"))
                else:
                    frags.append((tb, f"{g} {info['phase']} {now - info['t0']:.0f}s"))
                    idle = now - info["beat"]
                    if idle >= cfg.head_timeout / 2:
                        frags.append((warn, f" ⚠ quiet {idle:.0f}s"))
                if info["usd"] is not None:
                    frags.append((tb, f" ~${info['usd']:.2f}"))
                frags.append((tb, " · "))
            if not heads:
                frags.append((tb, "⏳ turn in flight · "))
            if queued():
                frags.append((tb, f"⧗ queue {queued()} · "))
            frags.append((tb, "Esc cancels · "))
        frags.append((tb, f"{cfg.banner_title.lower()} · {duel} · rounds {cfg.rounds}"
                          f" · judge {cfg.judge_style or 'off'} · ~${_spent():.2f}"))
        ctx = _context_frac(cfg)
        if ctx is not None:
            frags.append((tb, " · "))
            frags.append((warn if ctx >= 0.7 else tb, f"⛁ {ctx:.0%}"))
        if not cfg.tape_verbose:
            frags.append((tb, " · tape off (^T)"))
        frags.append((tb, " · ^O report · /help "))
        return frags

    fallback = lambda: console.input(f"[bold {cfg.accent_color}]{marker()}[/] ")
    if not sys.stdin.isatty():
        return fallback
    try:
        from .composer import Composer
    except ImportError:
        return fallback
    return Composer(console, accent=cfg.accent_color, title=cfg.banner_title.lower(),
                    marker=marker, status=status, commands=_COMMANDS,
                    history_path=cfg.ledger_path.parent / "history",
                    on_toggle=lambda: _toggle_duel(renderer, console),
                    on_cancel=on_cancel,
                    hotkeys={"c-t": lambda: _toggle_tape(cfg, console)},
                    arg_words=_slash_arg_words).read


def _context_frac(cfg: Config) -> float | None:
    """How full the fuller head's context window is, from each head's LAST call's prompt
    tokens (flight.context_tokens). Approximate twice over — the window sizes are config
    knobs, not CLI facts — so it renders as a meter, never a promise."""
    _, tokens = flight.snapshot()
    fracs = [tokens[h] / max(1, getattr(cfg, f"{h}_context_window"))
             for h in ("claude", "codex") if h in tokens]
    return max(fracs) if fracs else None


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
        start_session()                      # fresh memory boundary, same process
        was_armed = _disarm(renderer)        # 11 Jul: a new chat starts duel-off
        console.print("— new session — history reset —"
                      + ("  ⚔ duel off" if was_armed else "")
                      + "  [dim](/switch brings the old one back)[/]")
    elif cmd == "/duel":
        _toggle_duel(renderer, console, arg or None)
    elif cmd == "/note":
        if not arg:
            console.print("[red]usage: /note <fact>[/] — it rides into the next turn as a constraint")
            return
        record({"role": "note", "text": arg})
        console.print("[dim]✎ noted — the heads treat it as a fact next turn[/]")
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
    elif cmd == "/think":
        mode, _, val = arg.partition(" ")
        tokens = _THINK_LEVELS.get(val.strip(), None)
        if tokens is None and val.strip().isdigit():
            tokens = min(int(val.strip()), 31999)
        if mode not in ("duel", "solo") or tokens is None:
            console.print("[red]usage: /think <duel|solo> <low|medium|high|max|off|tokens>[/] — "
                          f"now duel {cfg.duel_thinking_tokens} · solo {cfg.solo_thinking_tokens}")
            return
        setattr(cfg, f"{mode}_thinking_tokens", tokens)
        console.print(f"{mode} thinking = {tokens or 'off'}"
                      + ("  [dim](claude's trace stays hidden headless — you'll see the count)[/]"
                         if tokens else ""))
    elif cmd == "/tools":
        mode, _, val = arg.partition(" ")
        if mode not in ("duel", "solo") or val.strip() not in ("on", "off"):
            console.print("[red]usage: /tools <duel|solo> <on|off>[/] — "
                          f"now duel {'on' if cfg.duel_tools else 'off'}"
                          f" · solo {'on' if cfg.solo_tools else 'off'}")
            return
        setattr(cfg, f"{mode}_tools", val.strip() == "on")
        console.print(f"{mode} tools = {val.strip()}"
                      + ("  [dim](read-only: files + web search, no shell)[/]"
                         if val.strip() == "on" else ""))
    elif cmd == "/tape":
        _toggle_tape(cfg, console)
    elif cmd == "/status":
        _status(renderer, console)
    elif cmd == "/cost":
        _cost(cfg, console)
    elif cmd == "/last":
        _last(cfg, console)
    elif cmd == "/switch":
        if _switch(arg, console) and _disarm(renderer):
            console.print("[dim]⚔ duel off — new conversation starts solo (/duel re-arms)[/]")
    elif cmd == "/fork":
        if _fork(arg, console) and _disarm(renderer):
            console.print("[dim]⚔ duel off — new branch starts solo (/duel re-arms)[/]")
    elif cmd == "/history":
        _history(cfg, console)
    elif cmd == "/model":
        _model(arg, cfg, console)
    elif cmd == "/effort":
        _effort(arg, cfg, console)
    elif cmd == "/context":
        _context(cfg, console)
    elif cmd == "/compact":
        _compact(cfg, console)
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


def _toggle_duel(renderer, console: Console, arg: str | None = None) -> None:
    """ONE arming path for /duel and Shift+Tab (step 8): loud codex-missing refusal,
    session drop on disarm, state echoed with the way back."""
    import shutil
    cfg = renderer.cfg
    renderer.adversarial = {"on": True, "off": False}.get(arg, not renderer.adversarial)
    if renderer.adversarial and not shutil.which(cfg.codex_command):
        renderer.adversarial = False         # fail loud, not a one-voiced "debate"
        console.print("[red]✗ codex not found — install @openai/codex first; staying solo[/]")
        return
    if not renderer.adversarial and hasattr(renderer, "reset_sessions"):
        renderer.reset_sessions()            # disarm drops head memory; re-arm reseeds fresh
    console.print("⚔ adversary ON — codex will cross-examine every answer"
                  "  [dim](Shift+Tab or /duel turns it off)[/]"
                  if renderer.adversarial else
                  "adversary off — plain claude chat  [dim](Shift+Tab or /duel re-arms)[/]")


def _toggle_tape(cfg: Config, console: Console) -> None:
    """Ctrl+T / /tape (backlog item: the Ctrl+O-verbose pattern, ^O being taken by /report):
    flip the dim thinking/tool/retry lines live, MID-TURN included — it only gates prints,
    so the running pump honors it on its next event. Phases stay on the status line either
    way: hiding the tape loses the text, never the liveness."""
    cfg.tape_verbose = not cfg.tape_verbose
    console.print("tape on — thinking/tool lines stream dim  [dim](Ctrl+T or /tape hides)[/]"
                  if cfg.tape_verbose else
                  "tape off — answers only; the status line still shows phases  "
                  "[dim](Ctrl+T or /tape brings it back)[/]")


def _disarm(renderer) -> bool:
    """Conversation boundary crossed (/new · /switch · /fork): the duel turns off and the
    heads' native sessions drop — they belong to the OLD conversation's memory (11 Jul).
    Returns whether the duel was actually armed, so callers can mention it or stay quiet."""
    was = getattr(renderer, "adversarial", False)
    renderer.adversarial = False
    if hasattr(renderer, "reset_sessions"):
        renderer.reset_sessions()
    return was


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
    t.add_row("heads", f"{cfg.claude_command}{f' ({cfg.claude_model})' if cfg.claude_model else ''}"
              f" · {cfg.codex_command}{f' ({cfg.codex_model})' if cfg.codex_model else ''}"
              + (f" · effort {cfg.codex_effort}" if cfg.codex_effort else ""))
    think = lambda n: "off" if not n else ("max" if n >= 31999 else str(n))
    t.add_row("depth", f"duel: think {think(cfg.duel_thinking_tokens)}"
              f" · tools {'✓' if cfg.duel_tools else '✗'} · codex {cfg.codex_effort or 'high'}"
              f"   solo: think {think(cfg.solo_thinking_tokens)}"
              f" · tools {'✓' if cfg.solo_tools else '✗'}")
    t.add_row("memory", f"last {cfg.history_turns} turns from the ledger")
    if duel:
        s = getattr(renderer, "sessions", None)
        live = s is not None and (s.claude or s.codex)
        t.add_row("head sessions",
                  f"live — claude {s.claude or '—'} · codex {s.codex or '—'}" if live
                  else ("mint on the next message" if cfg.head_sessions
                        else "off — preamble replay each round"))
    t.add_row("session", f"{RUN_ID} · {_spent():.2f} USD so far")
    console.rule("[bold]status[/]", style=cfg.accent_color, align="left")
    console.print(t)


def _cost(cfg: Config, console: Console) -> None:
    from .pricing import codex_rate
    calls = trace(run_id=RUN_ID, role="head_call")
    console.print(f"session {RUN_ID}: [bold]${_spent():.2f}[/] across {len(calls)} head call(s)")
    if not cfg.codex_pricing:
        note = "codex priced OFF (token-only)"
    else:
        (p_in, _c, p_out), model, exact = codex_rate(cfg.codex_model)
        assumed = "" if exact else " ⚠ assumed — unknown model, rate may be stale"
        note = (f"codex @ {model} list ${p_in:g}/${p_out:g} per 1M in/out{assumed}")
    console.print(f"  [dim]claude billed direct · {note}[/]")


def _spent() -> float:
    return sum(cost_usd(r) for r in trace(run_id=RUN_ID, role="head_cost"))


def _last(cfg: Config, console: Console) -> None:
    """Reprint the previous turn — columns for a duel, one voice for solo, verdict if judged.
    The `"proposer" in r` guard skips event rows (converged/cancelled share role=debate)."""
    turns = [r for r in trace(run_id=RUN_ID, role="debate")
             if r.get("round") is not None and "proposer" in r]
    if not turns:
        console.print("[dim]nothing yet this session[/]")
        return
    last = turns[-1]
    if last.get("adversary"):
        from .debate import _present
        _present(console, str(last.get("proposer", "")), str(last["adversary"]), cfg)
    else:
        console.print(f"[orange1]## {cfg.claude_glyph} Claude[/]\n{last.get('proposer', '')}")
    judges = trace(run_id=RUN_ID, role="judge")
    if judges and judges[-1].get("ts", 0) > last.get("ts", 0):
        console.print(f"\n[bold]## ⚖ Synthesis[/] ({judges[-1].get('style')})\n{judges[-1].get('text', '')}")


# ── conversation continuity: /switch · /fork · /history · /compact · /context ──────
# The mechanism is ONE move: append a session_start row that `resumes` an older session
# (ledger.start_session). /switch and /fork are the same row with different messaging;
# /compact is the same row carrying a summary instead of a pointer. Append-only storage
# never rewrites history — "moving around" it = appending one pointer row.

def _session_index() -> list[dict]:
    """Conversations worth listing: sessions with an ANSWERED turn (or a /compact summary —
    that IS a conversation, condensed). Same answered-only rule as _chain_turns, so the
    table never promises memory that resuming won't deliver (a lone cancelled question,
    abandoned /new markers, bare launches — all hidden). Newest first, capped at 20."""
    out = []
    for seg in sessions():
        answered, pending = [], None
        for r in seg["rows"]:
            if r.get("role") == "user":
                pending = r["text"]
            elif r.get("role") == "debate" and r.get("round") == 0 and "proposer" in r:
                if pending is not None:
                    answered.append(pending)
                    pending = None
        if not answered and not seg["start"].get("summary"):
            continue
        title = seg["start"].get("title") or (answered[0] if answered else "(compacted)")
        out.append({"sid": seg["sid"], "ts": seg["start"].get("ts", 0),
                    "turns": len(answered), "title": str(title)[:60]})
    return out[::-1][:20]


def _switch(arg: str, console: Console) -> bool:
    """No arg: the conversations table (↔ omnigent _repl.py:5195 — #·id·title·when shape).
    With #/id: splice that conversation's history in front of a fresh session. Works across
    processes for free — the ledger is one file, so a fresh `council ask` can /switch into
    last week's thread. Returns True only when a switch actually happened (listing and
    misses are not a conversation boundary — the caller disarms the duel on True)."""
    index = _session_index()
    if not arg:
        if not index:
            console.print("[dim]no past conversations yet[/]")
            return False
        t = Table(title="switch to…", padding=(0, 2))
        for col in ("#", "id", "started", "turns", "title"):
            t.add_column(col, style="dim" if col in ("id", "started") else "")
        for i, s in enumerate(index, 1):
            t.add_row(str(i), s["sid"], time.strftime("%d %b %H:%M", time.localtime(s["ts"])),
                      str(s["turns"]), s["title"])
        console.print(t)
        console.print("[dim]/switch <#> or <id> to resume[/]")
        return False
    if arg.isdigit():
        i = int(arg) - 1
        if not 0 <= i < len(index):
            console.print(f"[red]no conversation #{arg}[/] — bare /switch lists {len(index)}")
            return False
        target = index[i]
    else:
        hits = [s for s in index if s["sid"].startswith(arg)]
        if len(hits) != 1:
            console.print(f"[red]{'ambiguous' if hits else 'unknown'} id {arg!r}[/] — bare /switch lists them")
            return False
        target = hits[0]
    start_session(resumes=target["sid"])
    console.print(f"↺ resumed [bold]{target['title']}[/]  [dim]({target['sid']} · {target['turns']} turn(s))[/]")
    _recap(console)
    return True


def _recap(console: Console) -> None:
    """After a /switch: reprint the tail of what memory now holds, so the screen and the
    heads agree on where the conversation stands (council's cheap _attach_to_conversation)."""
    summary, rows = chain_rows()
    if summary:
        console.print(f"[dim]memory opens from a compact summary ({len(summary)} chars)[/]")
    last_u = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("role") == "user"), None)
    if last_u is None:
        return
    answers = [r for r in rows[last_u + 1:]
               if r.get("role") == "debate" and r.get("round") is not None and "proposer" in r]
    from .report import render_rows
    render_rows([rows[last_u]] + answers[-1:], console)   # the question + its FINAL round


def _fork(arg: str, console: Console) -> bool:
    """Branch: a new session resuming the CURRENT one — both share history up to here, new
    turns diverge. Return-path message ↔ omnigent _repl.py:5455 (fork switches you in-place;
    the old id is your way back). Returns True when a fork happened (see _switch)."""
    segs = sessions()
    if not segs:
        console.print("[dim]nothing to fork yet — this is already a fresh conversation[/]")
        return False
    old = segs[-1]["sid"]
    title = arg.strip() or None
    start_session(resumes=old, title=title)
    console.print(f"⑂ forked{f' as [bold]{title}[/]' if title else ''} — same memory, new branch"
                  f"  [dim](/switch {old} returns to the original)[/]")
    return True


def _history(cfg: Config, console: Console) -> None:
    """The full active chain, untruncated, in the overlay — truth-in-advertising for what
    /switch·/fork·/compact left in memory. (The preamble additionally clips: last
    history_turns×2 rows, 800 chars/voice, 8k total — /context shows those numbers.)"""
    summary, rows = chain_rows()
    if not rows and not summary:
        console.print("[dim]nothing yet this conversation[/]")
        return
    from .report import render_rows

    def body() -> None:
        if summary:
            console.rule("[bold]compact summary[/]", style="dim", align="left")
            console.print(summary.strip())
        render_rows(rows, console)
    _view(console, cfg, "history — what the heads remember", body)


def _slash_arg_words(cmd: str, idx: int, prior: list[str]) -> tuple[str, ...]:
    """Finite vocabulary for the idx-th argument of `cmd` — feeds the composer's
    completion popup so switching models/effort/thinking is a Tab away instead of
    memorized syntax (12 Jul ask). Empty tuple = free-text argument, no popup."""
    if cmd == "/model":
        if idx == 0:
            return ("claude", "codex", "reset")
        if idx == 1 and prior[:1] == ["claude"]:
            return tuple(_CLAUDE_ALIASES) + ("reset",)
        if idx == 1 and prior[:1] == ["codex"]:
            return _CODEX_SUGGEST + ("reset",)
    elif cmd == "/effort" and idx == 0:
        return _EFFORTS + ("reset",)
    elif cmd in ("/think", "/tools") and idx == 0:
        return ("duel", "solo")
    elif cmd == "/think" and idx == 1:
        return tuple(_THINK_LEVELS)
    elif cmd == "/tools" and idx == 1:
        return ("on", "off")
    elif cmd == "/judge" and idx == 0:
        return ("off", "moderator", "reasoning")
    elif cmd == "/duel" and idx == 0:
        return ("on", "off")
    return ()


# Friendly shorthands → real IDs. Anything NOT here still ships verbatim, so new models
# work the day they exist; the table only saves typing for the ones people reach for.
_CLAUDE_ALIASES = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5",
                   "haiku": "claude-haiku-4-5", "fable": "claude-fable-5"}
_CODEX_SUGGEST = ("gpt-5.5",)      # completion bait only — codex names always ship verbatim


def _cli_default(head: str) -> str | None:
    """What 'CLI default' actually resolves to, best-effort — /model must answer
    'default of WHAT?' (12 Jul ask). Display-only: any missing/odd file → None,
    because peeking at the vendors' config files must never break the REPL."""
    try:
        if head == "claude":
            return json.loads((Path.home() / ".claude" / "settings.json")
                              .read_text()).get("model")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"',
                      (Path.home() / ".codex" / "config.toml").read_text(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def _guess_head(name: str) -> str | None:
    """`/model opus` shouldn't demand a head word when the name already says it."""
    if name in _CLAUDE_ALIASES or name.startswith("claude"):
        return "claude"
    if name.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "codex"
    return None


def _model(arg: str, cfg: Config, console: Console) -> None:
    """Per-head model override, next turn onward. Aliases (opus·sonnet·haiku·fable) expand;
    everything else ships VERBATIM — no catalog to validate against, so warn-never-block
    (↔ omnigent _repl.py:4990): a wrong name fails loud on the next turn and lands in the
    ledger as a head_call error. `/model opus` infers the head from the name."""
    words = arg.split()
    if not words:
        for head in ("claude", "codex"):
            override = getattr(cfg, f"{head}_model")
            fallback = _cli_default(head)
            console.print(f"{head}: [bold]{override or 'CLI default'}[/]"
                          + (f"  [dim](→ {fallback})[/]" if not override and fallback else ""))
        console.print(f"[dim]/model claude {'·'.join(_CLAUDE_ALIASES)}|<id> · "
                      f"/model codex <id> · /model reset[/]")
        return
    if words[0] in ("reset", "off", "default"):
        cfg.claude_model = cfg.codex_model = None
        console.print("models reset — each CLI picks its own default again")
        return
    if len(words) == 1:                       # `/model opus` — the name names the head
        head = _guess_head(words[0])
        if head is None:
            console.print("[red]usage: /model · /model \\[claude|codex] <name> · /model reset[/]"
                          "  [dim](couldn't tell which head that name belongs to)[/]")
            return
        words = [head, words[0]]
    if len(words) == 2 and words[0] in ("claude", "codex"):
        head, name = words
        if name in ("reset", "off", "default"):
            setattr(cfg, f"{head}_model", None)
            console.print(f"{head} model reset — its CLI picks the default again"
                          + (f"  [dim](→ {_cli_default(head)})[/]" if _cli_default(head) else ""))
            return
        resolved = _CLAUDE_ALIASES.get(name, name) if head == "claude" else name
        setattr(cfg, f"{head}_model", resolved)
        console.print(f"{head} model = [bold]{resolved}[/]"
                      + (f"  [dim]({name} →)[/]" if resolved != name else "")
                      + "  [dim](verbatim — a bad name fails on the next turn; /model reset undoes)[/]")
        return
    console.print("[red]usage: /model · /model \\[claude|codex] <name> · /model reset[/]")


_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# Claude thinking shorthands: low/medium mirror Claude Code's think/megathink tiers,
# max is the ultrathink cap, high sits between as a budget (the knob is continuous).
_THINK_LEVELS = {"off": 0, "low": 4000, "medium": 10000, "high": 16000, "max": 31999}


def _effort(arg: str, cfg: Config, console: Console) -> None:
    """Codex reasoning effort (`-c model_reasoning_effort=…`). Codex-only and says so —
    claude -p has no effort knob (thinking budgets ride /think instead)."""
    if not arg:
        console.print(f"codex effort: [bold]{cfg.codex_effort or 'CLI default'}[/]"
                      f"  [dim](/effort {'·'.join(_EFFORTS)} · reset — codex only;"
                      " claude: /think)[/]")
    elif arg in ("reset", "off", "default"):
        cfg.codex_effort = None
        console.print("codex effort reset to the CLI default")
    elif arg in _EFFORTS:
        cfg.codex_effort = arg
        console.print(f"codex effort = [bold]{arg}[/]  [dim](claude unaffected — its depth is /think)[/]")
    else:
        console.print(f"[red]usage: /effort {'|'.join(_EFFORTS)}|reset[/]")


def _context(cfg: Config, console: Console) -> None:
    """How full ask-mode memory is — measured off the REAL preamble the next turn ships,
    not an estimate of one. Coin bar ↔ omnigent _repl.py:5516 (10 slots, block glyphs)."""
    from .debate import _chain_turns, _history_preamble
    summary, turns = _chain_turns()
    if not turns and not summary:
        console.print("[dim]memory is empty — nothing said yet this conversation[/]")
        return
    kept = turns[-cfg.history_turns * 2:]
    turn_text = "\n\n".join(kept)[-8000:]
    frac = len(turn_text) / 8000
    bar = "█" * round(frac * 10) + "░" * (10 - round(frac * 10))
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("turn window", f"{bar}  {len(turn_text)}/8000 chars ({frac:.0%})")
    t.add_row("turns", f"{len(kept)} of {len(turns)} rows in the window"
              f"  [dim](last {cfg.history_turns * 2}; answers clipped at 800 chars/voice)[/]")
    t.add_row("summary", f"{len(summary)} chars from a /compact" if summary else "[dim]none[/]")
    t.add_row("next turn ships", f"{len(_history_preamble(cfg))} chars of preamble to each head")
    if frac >= 0.8 or len(turns) > len(kept):
        t.add_row("", "[dim]older turns are falling off — /compact folds them into a summary[/]")
    console.print(t)


def _compact(cfg: Config, console: Console) -> None:
    """Squash the WHOLE chain (not just the preamble window) into a summary the claude head
    writes, then open a fresh session carrying it — the chain ends at a summary, so memory
    shrinks to one block and the conversation keeps going. Nothing is lost: the old session
    stays in the ledger, /switch brings it back verbatim."""
    from .backends import proposer
    from .debate import _chain_turns, _safe
    summary, turns = _chain_turns()
    if not turns and not summary:
        console.print("[dim]nothing to compact yet[/]")
        return
    corpus = ((f"Earlier summary:\n{summary}\n\n" if summary else "") + "\n\n".join(turns))[-24000:]
    prompt = ("Compress this conversation into a summary that lets it continue seamlessly. "
              "Keep: decisions made, the position each voice holds, open questions, hard "
              "constraints, and exact names/numbers. Drop pleasantries and dead ends. "
              "Write terse notes, not prose.\n\n" + corpus)
    with console.status(f"[dim]⧉ compacting {len(turns)} turn(s)…[/]", spinner="dots"):
        text = _safe(proposer, prompt, cfg, "compact")
    if text.startswith("_("):    # _safe's failure placeholder — never bake an error into memory
        console.print("[red]✗ compact failed — memory unchanged[/]")
        return
    start_session(summary=text)
    console.print(f"⧉ compacted — {len(turns)} turn(s) → {len(text)} chars of summary; "
                  f"[dim]the full transcript stays in the ledger (/switch lists it)[/]")
