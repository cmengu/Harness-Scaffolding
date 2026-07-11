"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent chat.py:3805 (_run_repl), MINUS the SDK-event adapter — council swaps it for a Renderer.
Input strip ↔ omnigent-ui-sdk terminal/_host.py (TerminalHost) — the SAME primitive
(prompt_toolkit PromptSession: history + completer + toolbar), minus its layout surgery."""
from __future__ import annotations

import sys
import time
from typing import Callable, Protocol

from rich.console import Console
from rich.table import Table

from .config import Config
from .ledger import RUN_ID, chain_rows, record, sessions, start_session, trace

# One table drives /help AND the completion popup — they can never drift apart.
_COMMANDS: list[tuple[str, str, str]] = [
    ("/duel", "[on|off]", "toggle the codex adversary (bare /duel flips it)"),
    ("/rounds", "N", "debate depth when duelling"),
    ("/judge", "<style>", "off · moderator · reasoning"),
    ("/model", "[head name]", "per-head model override — /model claude opus · /model reset"),
    ("/effort", "[level]", "codex reasoning effort: minimal·low·medium·high · reset"),
    ("/think", "<duel|solo> <n|max|off>", "claude thinking budget per mode (codex: /effort)"),
    ("/tools", "<duel|solo> <on|off>", "may the heads research? (read-only files + web)"),
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


def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit. Two ^C behaviors, by where you are:
    at the prompt it abandons the draft; mid-turn it CANCELS the turn (kills the head
    subprocesses) and the unanswered question stays out of memory (_chain_turns holds a
    question back until a debate row answers it)."""
    start_session()                          # memory boundary: the history preamble (G2) only
    read_input = _make_prompt(renderer, cfg, console)   # reads the chain from this row on
    try:
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
            calls_before = len(trace(run_id=RUN_ID, role="head_call"))
            spent_before = _spent()
            try:
                renderer.handle(text)
            except KeyboardInterrupt:
                record({"role": "debate", "event": "cancelled"})
                console.print("\n[yellow]✗ cancelled — answer discarded, question kept out of memory[/]")
            else:
                _turn_line(console, calls_before, spent_before, cfg)
    finally:
        _clear_title()


def _turn_line(console: Console, calls_before: int, spent_before: float, cfg: Config) -> None:
    """One dim receipt after each turn: per-head seconds (✗ = that head failed) + the
    turn's cost delta. Data is already in the ledger (head_call/head_cost) — this just
    surfaces what /report aggregates, while the turn is still on screen. When an
    ask_budget_usd is set and the run has crossed it, the receipt carries a red nag
    (the code-mode counterpart is the PreToolUse checkpoint ladder)."""
    calls = trace(run_id=RUN_ID, role="head_call")[calls_before:]
    if not calls:
        return
    icon = {"claude": "🟠", "codex": "🔵", "judge": "⚖", "compact": "⧉"}
    parts = [f"{icon.get(c.get('head'), '·')} {c.get('secs', 0):.1f}s" + ("" if c.get("ok") else " ✗")
             for c in calls]
    spent = _spent()
    delta = spent - spent_before
    over = (f"  ·  [red]⚠ over budget (${spent:.2f} > ${cfg.ask_budget_usd:.2f})[/red]"
            if cfg.ask_budget_usd and spent > cfg.ask_budget_usd else "")
    console.print(f"[dim]{'  ·  '.join(parts)}{f'  ·  ${delta:.2f}' if delta >= 0.005 else ''}{over}[/]")


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
        start_session()                      # fresh memory boundary, same process
        was_armed = _disarm(renderer)        # 11 Jul: a new chat starts duel-off
        console.print("— new session — history reset —"
                      + ("  ⚔ duel off" if was_armed else "")
                      + "  [dim](/switch brings the old one back)[/]")
    elif cmd == "/duel":
        import shutil
        renderer.adversarial = {"on": True, "off": False}.get(arg, not renderer.adversarial)
        if renderer.adversarial and not shutil.which(cfg.codex_command):
            renderer.adversarial = False     # fail loud, not a one-voiced "debate"
            console.print("[red]✗ codex not found — install @openai/codex first; staying solo[/]")
        else:
            if not renderer.adversarial and hasattr(renderer, "reset_sessions"):
                renderer.reset_sessions()    # disarm drops head memory; re-arm reseeds fresh
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
    elif cmd == "/think":
        mode, _, val = arg.partition(" ")
        tokens = {"off": 0, "max": 31999}.get(val.strip(), None)
        if tokens is None and val.strip().isdigit():
            tokens = min(int(val.strip()), 31999)
        if mode not in ("duel", "solo") or tokens is None:
            console.print("[red]usage: /think <duel|solo> <tokens|max|off>[/] — "
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
    elif cmd == "/status":
        _status(renderer, console)
    elif cmd == "/cost":
        _cost(console)
    elif cmd == "/last":
        _last(console)
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


def _cost(console: Console) -> None:
    calls = trace(run_id=RUN_ID, role="head_call")
    console.print(f"session {RUN_ID}: [bold]${_spent():.2f}[/] across {len(calls)} head call(s)"
                  "  [dim](claude head only — codex's CLI exposes no per-call cost)[/]")


def _spent() -> float:
    return sum(r.get("usd") or 0.0 for r in trace(run_id=RUN_ID, role="head_cost"))


def _last(console: Console) -> None:
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
        _present(console, str(last.get("proposer", "")), str(last["adversary"]))
    else:
        console.print(f"[orange1]## 🟠 Claude[/]\n{last.get('proposer', '')}")
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


def _model(arg: str, cfg: Config, console: Console) -> None:
    """Per-head model override, next turn onward. Values ship VERBATIM — no catalog to
    validate against, so warn-never-block (↔ omnigent _repl.py:4990): a wrong name fails
    loud on the next turn and lands in the ledger as a head_call error."""
    words = arg.split()
    if not words:
        console.print(f"claude: [bold]{cfg.claude_model or 'CLI default'}[/] · "
                      f"codex: [bold]{cfg.codex_model or 'CLI default'}[/]"
                      "  [dim](/model claude|codex <name> · /model reset)[/]")
        return
    if words[0] in ("reset", "off", "default"):
        cfg.claude_model = cfg.codex_model = None
        console.print("models reset — each CLI picks its own default again")
        return
    if len(words) == 2 and words[0] in ("claude", "codex"):
        setattr(cfg, f"{words[0]}_model", words[1])
        console.print(f"{words[0]} model = [bold]{words[1]}[/]  [dim](verbatim — a bad name fails"
                      " on the next turn; /model reset undoes)[/]")
        return
    console.print("[red]usage: /model · /model claude|codex <name> · /model reset[/]")


_EFFORTS = ("minimal", "low", "medium", "high")


def _effort(arg: str, cfg: Config, console: Console) -> None:
    """Codex reasoning effort (`-c model_reasoning_effort=…`). Codex-only and says so —
    claude -p has no effort knob (extended thinking is prompt-level, not a flag)."""
    if not arg:
        console.print(f"codex effort: [bold]{cfg.codex_effort or 'CLI default'}[/]"
                      f"  [dim](/effort {'·'.join(_EFFORTS)} · reset — codex only)[/]")
    elif arg in ("reset", "off", "default"):
        cfg.codex_effort = None
        console.print("codex effort reset to the CLI default")
    elif arg in _EFFORTS:
        cfg.codex_effort = arg
        console.print(f"codex effort = [bold]{arg}[/]  [dim](claude unaffected — no such knob on claude -p)[/]")
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
