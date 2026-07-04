# council — the build manuscript

A single readable manuscript of the council-harness, organized by **group** (G1, G2, …). Each
`### FILE: council/<x>` block is a *file-to-be* (a sketch), not yet real code. The `↔` notes cite the
**omnigent** source each block is derived from — and those files are now vendored under
**`reference/omnigent/`** so you can open them:

- omnigent core → `reference/omnigent/omnigent/<file>.py`
- Debby (G2)    → `reference/omnigent/examples/debby/…`
- the matching map → `reference/omnigent/MAPPING.md`

Line numbers track that vendored snapshot. (G1's are a few lines stale — corrected anchors in MAPPING.md.)

## Group index

| group | what it is | files | status |
|---|---|---|---|
| **G1 — FRONT** | the branded door + turn-based loop | `config.py` · `banner.py` · `cli.py` · `chat.py` | done |
| **G2 — DEBATE** | cross-family debate (Claude vs Codex) | `backends.py` · `debate.py` · `skills/debate/SKILL.md` | done |
| **G3 — WRAP** | run the REAL claude hidden, render it in council's skin | `wrap/{session,bridge,events,render,state,harness_status}.py` | **done — mapped in full below** |
| **G4 — PERSISTENCE** | the scaling seam | `ledger.py` (record/trace) | done (shown in G1) |
| **G5 — POLICY** | blast_radius ALLOW/DENY/ASK | `policy.py` | **done — mapped below** |
| **G6 — HARNESS BRIDGE** | the the-harness hand-off | `launch.py` · `scripts/call-reviewer.sh` | **cut from v1** — kept as future work |

---

## The 3 commands — which files each one uses

council is **one door (`cli.py`) → three separate engines**, sharing only the ledger (G4). The files
below belong to *one* engine each — they are **not** all used at once:

| command | what it's for | files it uses |
|---|---|---|
| **`council ask`** | chat with Claude; `/duel` summons the Codex adversary per-question | `cli.py` · `chat.py` (`run_loop`) · `debate.py` · `backends.py` |
| **`council code`** | write code via the REAL Claude Code, hidden, with the-harness live | `cli.py` · `wrap/{session,bridge,events,render,state,harness_status}.py` |
| ~~`council review`~~ | **CUT from v1** (3 Jul 2026) — cross-family review stays a G6 future-work section | — |

```
   council ask    ──► run_loop(DebateRenderer)   (G2)  /duel OFF → 🟠 solo · /duel ON → 🟠 vs 🔵
   council code   ──► run_claude_session  (G3: wrap/*  — hides a real Claude Code)
   council review ──► (cut from v1 — G6 future work)
          └──────────────── both modes → record() → ledger.jsonl (G4) ────────────┘
```

> So `chat.py` / `debate.py` / `backends.py` appear **only** in `ask`; `wrap/*` appears **only** in
> `code`. Each group below opens with its own step-by-step flow.

---

# G1 — FRONT

The branded door and the turn-based loop. ~200 lines you write, against ~29k of omnigent front-end.

### FILE: `council/config.py`
Which model plays each role, and where the log lives. That's it.

```python
"""council/config.py — thin config.
↔ omnigent cli.py (_load_effective_config) + onboarding model_catalog/*.json,
   trimmed from a multi-vendor registry down to three roles."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LEDGER = Path.home() / ".council" / "ledger.jsonl"

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
    history_turns: int = 6           # ask-mode memory: past turns carried in the ledger preamble
    judge_style: str | None = None   # interactive-loop judge STYLE: None | 'moderator' | 'reasoning'
    heads: Heads = field(default_factory=Heads)

def load_config() -> Config:
    """Defaults ← ~/.council/config.toml ← env overrides.
    (Omnigent merges global+local+effective across ~250 lines; council keeps one file.)"""
    cfg = Config()
    # ... read toml if present; apply COUNCIL_* env overrides (like the-harness's HARNESS_* knobs) ...
    return cfg
```

> **Reading it — this block is *pure definition* (the settings schema); no logic runs here.**
> `@dataclass` auto-writes the constructor from the listed fields + defaults.
> `field(default_factory=Heads)` gives each `Config` its own *fresh* `Heads` — writing `= Heads()`
> would make every config secretly share one object (a classic Python trap). The payoff: every other
> file takes one `cfg` and reads `cfg.heads.proposer` / `cfg.ledger_path` instead of hard-coding values
> in twenty places. The only thing that *runs* is `load_config()` — build the defaults, then (real
> version) overlay a `~/.council/config.toml` + `COUNCIL_*` env vars on top.

### FILE: `council/banner.py`
The only face the user ever sees. Under CODE, Claude Code + the-harness run behind this.

```python
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
```

> **Reading it** — `render_banner` paints council's panel once at launch; the `subtitle` dict picks
> one line per mode. `_display_cwd` is cosmetic: it turns the absolute current folder (`Path.cwd()`,
> e.g. `/Users/you/Projects/demo`) into the short `~/Projects/demo` form — `relative_to(home)` does
> the shortening, and the `except ValueError` handles a folder *outside* home by showing the full path.
> (Same trick your shell uses to show `~/Documents`.) "cwd" = current working directory.

### FILE: `council/ledger.py`  *(a G4 file, shown here because all of G1 leans on it)*
The scaling seam: two functions. Nothing else in council ever touches the ledger file. Swap these
two bodies (local jsonl → shared server) and the rest of the codebase doesn't change.

```python
"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30)."""
from __future__ import annotations
import json, threading, time
from functools import lru_cache
from .config import load_config

_LOCK = threading.Lock()   # record() has concurrent callers: code-mode pumps + ask-mode failure path

@lru_cache(maxsize=1)
def _cfg():
    """Config once per process, NOT once per event — code mode records per token chunk."""
    return load_config()

def record(event: dict) -> None:
    """The only writer. Append one event. (Local jsonl now; POST to a shared server the day
    you get a second user — callers never change.)"""
    row = {"ts": time.time(), **event}
    path = _cfg().ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a") as f:    # the lock: two simultaneous appends must never interleave
        f.write(json.dumps(row) + "\n")

def trace(**filters) -> list[dict]:
    """The only reader (history-replay + the live viewer tail this). NB: this 'resume' means
    council re-reading its OWN ledger — NOT 'code --resume', which is Claude Code's session resume."""
    path = _cfg().ledger_path
    if not path.exists():
        return []
    rows = (json.loads(l) for l in path.read_text().splitlines() if l.strip())
    return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
```

> **Reading it — council's whole "log" is two functions over one text file (`~/.council/ledger.jsonl`).**
> - **`record(event)` = the writer.** `{"ts": time.time(), **event}` adds a timestamp, then `**event`
>   unpacks the caller's dict in. `load_config().ledger_path` is the *one* place that decides *where* it
>   lands — that's the "routing" / scaling seam: swap this body for a server POST later and every caller
>   is unchanged. `open("a")` = **append** (never overwrite → an ever-growing history); it writes
>   `json.dumps(row)` + a newline, i.e. one JSON object per line (the `.jsonl` format). Returns `None` —
>   the effect is a line on disk. Callers: `run_loop`, `debate.py`, the wrap layer.
> - **`trace(**filters)` = the reader.** `**filters` collects keyword args into a dict, so
>   `trace(role="debate")` keeps only matching rows and `trace()` returns everything. `json.loads` turns
>   each line back into a dict; `all(r.get(k) == v …)` keeps a row only if *every* filter matches
>   (`.get` returns `None` instead of crashing on a missing key).

#### Who writes to the ledger (and why it's the spine)

Nothing in council remembers anything on its own — every group shouts events at this one file through
`record()`, and `trace()` reads them back. That's why **every group imports `record`**: pull the ledger
out and nothing can resume or be watched live.

| writer | group | when it records | row it writes |
|---|---|---|---|
| `run_loop` (`chat.py`) | G1 | each thing *you* type | `{"role": "user", "text": …}` |
| `debate.run` (`debate.py`) | G2 | each debate round / early-stop | `{"role": "debate", "round": n, …}` · `{"role":"debate","event":"converged"}` |
| `_safe` (`debate.py`) | G2 | a head fails / times out | `{"role": "head_error", "head": …, "error": …}` |
| `_synthesize` (`debate.py`) | G2 | blind-grade judge runs | `{"role": "judge_keymap", "map": …}` |
| `Renderer.handle` (`wrap/`) | G3 | each rendered code event | assistant text · tool call · cost |

The loop records *your* turn, then hands off (`renderer.handle(text)`) and trusts the engine to record
its **own** output. The loop never knows *which* engine ran — that indifference is the seam.

#### The concurrency seam — G4's one genuine bug (two triggers)

`record()`'s body is `with path.open("a") as f: f.write(…)`. That is **only safe with a single writer.**
Two threads each doing `open("a") + write` at the same instant can interleave — and council has **two
independent places** that break the single-writer assumption:

1. **CODE mode (G3).** The live session runs two pumps on separate threads — an output pump (reads what
   `claude` prints) and an input pump (forwards your keystrokes). Both can `record()` at once.
2. **ASK mode (G1→G2).** `_both()` (`debate.py`) runs the two heads on a `ThreadPoolExecutor`. The happy
   path is safe (only the main thread records, *after* the pool joins). The race is the **failure path**:
   `_safe` records a `head_error` *from each worker thread* — and because both heads share **one**
   `head_timeout`, "both heads hang" makes them time out and call `record()` at nearly the same instant.
   Simultaneous failure is the *natural* shape here, not a rare corner.

> **Severity, honestly.** `head_error` lines are tiny, so on most systems `O_APPEND` keeps each write
> offset-atomic and they won't actually tear — it's a **latent** race masked by small line size, not by
> correctness. A long debate line racing a worker write would tear; that just doesn't happen in the
> current happy path. Worth fixing anyway, because code mode triggers it for real.

**The fix belongs in G4, not G1/G3** — precisely because there are now *two* concurrent callers. One
module-level lock at the seam covers both (the debate threads and the code pumps are all same-process).

**[MERGED 3 Jul 2026]** The lock — plus an `lru_cache`'d config read (`record()` used to call
`load_config()` per event, i.e. per token chunk in code mode) — now lives **in the primary `ledger.py`
listing above**. There is exactly ONE `record()` in this manuscript; port that one.

> Same-process only. The day council goes multi-process you'd switch to `os.open(…, O_APPEND) +
> os.write(full_line)` or file-locking — but that's the multi-user swap the seam already defers.

### FILE: `council/cli.py`
Thin top, one call at the bottom, per command.

```python
"""council/cli.py — the front door: 2 commands, thin.  (`review` CUT from v1 — see G6, 3 Jul 2026.)
↔ omnigent cli.py:1161 (group), :1241 (main), the `claude` command (→ code), the `run` command (→ ask).
Dropped: 22 commands, ~28k lines of plumbing."""
from __future__ import annotations
import sys, click
from rich.console import Console
from .config import load_config
from .banner import render_banner

console = Console()

@click.group()
@click.version_option()
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
    if rounds is not None: cfg.rounds = rounds       # CLI flags must reach EVERY turn, not just
    if judge is not None:  cfg.judge_style = judge   # the first — the renderer reads cfg each turn
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
```

> **Reading it** — every command is "thin top, one call at the bottom": load config → paint banner →
> hand off to the engine seam (`ask`→G1 loop + G2 renderer, `code`→G3 wrap). **About `--resume` in
> `code`:** it is *not* a council subsystem — it's a **pass-through** to the real `claude` binary's own
> local session resume (`save_launch_cwd` just remembers which project folder you launched in). The
> resume council *dropped* (in G3) was omnigent's separate, server-based cold-resume; Claude Code's own
> local resume stays, for free.

### FILE: `council/chat.py`
The 8,183-line `_repl.py` boiled to its skeleton. The loop knows nothing about *which* engine runs —
that's the Renderer's job. That one seam is what omnigent spent its giant SDK-event adapter on.

```python
"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent chat.py:3805 (_run_repl), MINUS the SDK-event adapter — council swaps it for a Renderer."""
from __future__ import annotations
from typing import Protocol
from rich.console import Console
from .config import Config
from .ledger import record

class Renderer(Protocol):
    """One method. THINK → debate columns (G2) · REVIEW → codex output (G6).
       (CODE is the exception — it owns its own live loop; see G1 note.)"""
    def handle(self, user_input: str) -> None: ...

def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit."""
    record({"role": "session_start"})        # memory boundary: the history preamble (G2) only reads
    while True:                              # ledger rows AFTER the latest session_start
        try:
            text = console.input("[bold blue]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
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

def _slash(text: str, renderer, console: Console) -> None:
    """The 'fake toggle': /duel just flips a boolean on the renderer — no UI widget, same trick
    Claude Code's own toggles use. TIMING IS FREE: handle() blocks until the turn is done, so a
    toggle can never interrupt a turn in flight — it takes effect on the NEXT question."""
    if text == "/help":
        console.print("/duel [on|off] — toggle the codex adversary  |  /new  /exit")
    elif text.startswith("/duel"):
        import shutil
        arg = (text.split()[1:] or ["toggle"])[0]
        renderer.adversarial = {"on": True, "off": False}.get(arg, not renderer.adversarial)
        if renderer.adversarial and not shutil.which(renderer.cfg.codex_command):
            renderer.adversarial = False     # fail loud, not a one-voiced "debate"
            console.print("[red]✗ codex not found — install @openai/codex first; staying solo[/]")
        else:
            console.print("⚔ adversary ON — codex will cross-examine every answer"
                          if renderer.adversarial else "adversary off — plain claude chat")
```

> **Reading it** — `run_loop` is the "keep chatting until quit" engine for **ask**. `while True`
> loops until a `break`. Each pass: read a line (`console.input`; the `try/except` makes Ctrl-D/Ctrl-C
> quit cleanly) → handle quit-words / slash-commands / empty input (`continue` = skip to the next loop)
> → `record()` the turn → `renderer.handle(text)`. **The seam:** the loop never knows *which* engine
> runs — it just calls `handle`; ask plugs in `DebateRenderer` (solo claude by default, debate on /duel).
> `Renderer` (a `Protocol`) just means "any object with a `handle(str) -> None` method." Think of
> `run_loop` as a *waiter*: it takes your order and carries it to whatever kitchen is plugged in — it
> never cooks.

> **G1 caveat:** `run_loop` fits ASK (input → response → render). **CODE does not** — it's a
> live attached session (G3) that owns its own loop and doesn't return per-turn. So: ask uses
> `run_loop`; **code** launches the G3 wrap directly and shares only banner + ledger + config.

---

# G2 — DEBATE

Cross-family debate. Source = **Debby** (`reference/omnigent/examples/debby/`). The big transform:
Debby orchestrates with an LLM brain + an inbox; council orchestrates with a deterministic Python
loop + two subprocesses. ~95 lines vs Debby's 352.

> **G1 ↔ G2 link — who orchestrates, and how this plugs into the front door.**
>
> Three roles, three files: **`cli.py` = the door** (parse the command + dispatch, one line),
> **`chat.py` `run_loop` = the loop** (keep asking, hand each turn to a renderer), **`debate.py` `run`
> = the orchestrator** (fan out to both heads, cross-critique N rounds, present, maybe judge). Only
> `debate.py` knows what a *head* or a *round* is — so **it** is the orchestrator, not `cli.py`/`chat.py`,
> which are thin plumbing around it.
>
> `debate.run` is **one self-contained debate** (question → `DebateResult`); the *looping* lives in
> `chat.py`, kept separate so the engine can be called once or many times. Two paths reach it:
>
> ```
> ONE-SHOT   (council ask "q"):    cli.ask() ──► DebateRenderer.handle(q)    # answer once, exit
> INTERACTIVE (council ask, DEFAULT): run_loop(DebateRenderer)
>                                     └ each turn ─► DebateRenderer.handle(text)
>                                          ├ /duel OFF (default) ─► proposer only — plain claude chat, WITH memory
>                                          └ /duel ON            ─► debate.run(text)   🟠 vs 🔵
> ```
>
> **Memory (both paths):** the heads are stateless subprocesses, so `handle` prepends a
> `_history_preamble` rebuilt from the ledger each turn — that preamble IS codex's memory (it has no
> session of its own; claude's head is equally stateless in ask mode — native `--resume` is a v2
> efficiency upgrade for the claude side only). Code mode needs none of this: the hidden real Claude
> Code remembers natively; there the ledger is only a diary.
>
> Same engine at the bottom of both; the only difference is whether a loop wraps it.
>
> **The seam (socket + plug):** `chat.py` defines `Renderer` — a `Protocol`, i.e. "any object with a
> `handle(str) -> None` method" — that's the *socket*. `DebateRenderer` (defined at the end of
> `debate.py`, below) is the *plug* that fits; its `handle` just calls `debate.run`. So `run_loop` can
> drive a debate **without ever importing `debate.py`** — it depends only on the `Renderer` shape.
> (Note: `DebateRenderer` replaces the placeholder **stub** `_DebateRendererSketch` left in `chat.py`
> during G1 — it does **not** replace `chat.py` itself.)

### Step-by-step: what happens when you run `council ask`

```
   COUNCIL  (what you see)                       short-lived subprocesses (one per head call)
   ────────────────────────                      ─────────────────────────────────────────────

   (G1) you type:  council ask
        │
   cli.py `ask` ── parses, paints banner
        │
        ├ one-shot:    debate.run(q) ───────────────────┐
        │                                               │
        └ interactive: chat.run_loop( DebateRenderer )  │   ◄── THE chat loop (turn-based)
              │  each time you type ▼                   │
              DebateRenderer.handle(text) ── debate.run(text)
                                                        ▼
   ═══════════ inside ONE debate (debate.run, G2) ═══════════

   ROUND 0 — opening answers
      _both(q, q) ──┬─► _safe(proposer)  ─► claude -p   ──────────►  🟠 Claude's answer
                    └─► _safe(adversary) ─► codex exec  ──────────►  🔵 Codex's answer
                    └ run in PARALLEL (ThreadPoolExecutor); live "🟠…thinking 🔵…thinking" status
      record({round:0, …}) ──► ledger (G4)

   ROUNDS 1..N — cross-critique  (this is the "adversarial" part)
      _both( each head gets: "your last answer + the OTHER's answer → critique, then update" )
                  ──┬─► claude -p   sees 🔵 Codex's last answer → rebuts + revises
                    └─► codex exec  sees 🟠 Claude's last answer → rebuts + revises
      _moved(prev, now) < 0.10 for BOTH?  → stop early (nobody's changing their mind)
      record({round:n, …}) ──► ledger

   _present(a, b) ──►   🟠 Claude   │   🔵 Codex     side-by-side columns   ◄── the rendering

   (optional) _synthesize ──► blind-grade (strip names, shuffle A/B) ─► judge head ─► verdict / ESCALATE
                              ──► printed as "## ⚖ Synthesis"

   → returns DebateResult ;  (interactive) loop back for your next message
```

### FILE: `council/backends.py`
The two panelists. Identical brief, two modes (answer / critique), each is just a CLI we shell out to.

```python
"""council/backends.py — the two debate heads as plain functions.
↔ Debby agents/claude/config.yaml:41-64 + agents/gpt/config.yaml:49-72 (the ANSWER/CRITIQUE prompt)."""
from __future__ import annotations
import subprocess
from .config import Config

HEAD_PROMPT = """\
You are one of two voices in a council. You are a thinking-and-writing responder.
You are dispatched in one of two modes (the message makes which clear):
- ANSWER   — given a question. Answer directly and well; be concrete; offer options w/ trade-offs.
- CRITIQUE — given your own last answer + the OTHER voice's answer. Name what it gets right, where
  it's weak/wrong/incomplete, then give your updated answer. Don't cave just to agree; don't dig in
  from pride — converge toward what's correct.
Return a clear, self-contained response. You have NO tools — reason in text only.
"""

def proposer(message: str, cfg: Config) -> str:
    """Claude head — the REAL `claude` CLI, headless, NO tools."""
    return _run([cfg.claude_command, "-p", "--allowedTools", "", HEAD_PROMPT + "\n\n" + message], cfg)

def adversary(message: str, cfg: Config) -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.
    Why codex (not an openai-agents SDK): an unpinned model silently falls back to the Databricks
    gateway; `codex exec` has no such fallback."""
    return _run([cfg.codex_command, "exec", "--sandbox", "read-only", HEAD_PROMPT + "\n\n" + message], cfg)

def _run(argv: list[str], cfg: Config) -> str:
    """One subprocess → its stdout. This IS council's whole 'executor'. Timeout so a hung head can't
    wedge the debate. (Verify the tools-off flag vs `claude -p --help`; the no-tools posture is the contract.)"""
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=cfg.head_timeout).stdout.strip()
```

### FILE: `council/debate.py`
The moderator's procedure, run by Python (not an LLM brain). Ask both → cross-critique → maybe synthesize.

```python
"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ Debby config.yaml:47-55 (fan-out), :82-97 (present) + skills/debate/SKILL.md:13-56 (round loop).
   ThreadPoolExecutor replaces Debby's inbox; no orchestrator LLM."""
from __future__ import annotations
import difflib, random
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, wait
from rich.console import Console
from rich.live import Live
from rich.table import Table
from .config import Config
from .backends import proposer, adversary
from .ledger import record, trace

@dataclass
class DebateResult:
    """NOT a bare str — a judge can refuse to pick. Lets callers branch on .escalated."""
    proposer_final: str
    adversary_final: str
    synthesis: str | None = None
    escalated: bool = False
    agree: str | None = None
    differ: str | None = None

def run(question: str, *, rounds: int, judge, cfg: Config, console: Console | None = None) -> DebateResult:
    """Fan to both heads, cross-critique up to N rounds (early-stop on no movement), present, maybe judge.
    `judge`: falsy=off · 'moderator'=neutral merge · 'reasoning'=verdict, may escalate. (bool True → 'moderator'.)"""
    console = console or Console()
    if judge is True:
        judge = "moderator"
    a, b = _both(question, question, cfg, console)                      # round 0 (ANSWER mode)
    record({"role": "debate", "round": 0, "proposer": a, "adversary": b})
    for n in range(1, rounds + 1):
        prev_a, prev_b = a, b
        # Question stays in EVERY round — without it heads drift into critiquing prose style
        a, b = _both(
            f"Question:\n{question}\n\nYour last answer:\n{prev_a}\n\nThe other voice said:\n{prev_b}\n\nCRITIQUE, then update.",
            f"Question:\n{question}\n\nYour last answer:\n{prev_b}\n\nThe other voice said:\n{prev_a}\n\nCRITIQUE, then update.",
            cfg, console)
        record({"role": "debate", "round": n, "proposer": a, "adversary": b})
        if _moved(prev_a, a) < 0.10 and _moved(prev_b, b) < 0.10:        # deterministic early-stop
            record({"role": "debate", "event": "converged", "round": n}); break
    _present(console, a, b)
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console)
    return result

def _both(msg_a, msg_b, cfg, console):
    """Both heads concurrently with a live 🟠/🔵 status (block-then-present; columns can't stream-interleave)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_safe, proposer, msg_a, cfg, "claude")
        fb = pool.submit(_safe, adversary, msg_b, cfg, "codex")
        with Live(_status(fa, fb), console=console, refresh_per_second=8) as live:
            while not (fa.done() and fb.done()):
                wait([fa, fb], timeout=0.15); live.update(_status(fa, fb))
        return fa.result(), fb.result()

def _safe(fn, msg, cfg, label):
    """A panelist's mic cutting out shouldn't kill the panel: one head failing → single-voiced + logged."""
    try:
        out = fn(msg, cfg)
        if not out.strip(): raise ValueError("empty response")
        return out
    except Exception as e:
        record({"role": "head_error", "head": label, "error": str(e)})
        return f"_({label} unavailable: {e})_"

def _moved(prev, now):  # 0=identical, 1=rewritten. Crude on purpose; never fires at default rounds=1.
    return 1 - difflib.SequenceMatcher(None, prev, now).ratio()

def _status(fa, fb):
    mark = lambda f: "✓" if f.done() else "…thinking"
    return f"🟠 claude {mark(fa)}    🔵 codex {mark(fb)}"

def _present(console, a, b):
    cols = Table.grid(padding=(0, 2)); cols.add_column(); cols.add_column()
    cols.add_row(f"[orange1]## 🟠 Claude[/]\n{a}", f"[blue]## 🔵 Codex[/]\n{b}")
    console.print(cols)

def _synthesize(question, r, *, style, cfg, console):
    """OPTIONAL judge, OFF by default. 'moderator'=neutral merge (Debby's only allowed judging);
    'reasoning'=evidence verdict, may ESCALATE. Inputs BLIND-GRADED (labels stripped, A/B shuffled)."""
    pair = [("A", r.proposer_final, "claude"), ("B", r.adversary_final, "codex")]
    random.shuffle(pair)
    record({"role": "judge_keymap", "map": {slot: fam for slot, _, fam in pair}})
    blind = "\n\n".join(f"Answer {slot}:\n{text}" for slot, text, _ in pair)
    judge_fn = proposer if (cfg.heads.judge or "claude") == "claude" else adversary
    instruction = ("Merge these into ONE synthesis — do NOT add a new position or pick a winner."
                   if style == "moderator" else
                   "Weigh the evidence. Give '## Where they agree', '## Where they differ', then a verdict. "
                   "If neither is adequately supported, reply starting with the word ESCALATE and say why.")
    verdict = _safe(judge_fn, f"Question:\n{question}\n\n{blind}\n\n{instruction}", cfg, "judge")
    r.synthesis = verdict
    r.escalated = (style == "reasoning" and verdict.strip().upper().startswith("ESCALATE"))
    console.print(f"\n[bold]## ⚖ Synthesis[/] ({style})\n{verdict}")
    return r

def _history_preamble(cfg: Config) -> str:
    """Ask-mode MEMORY. Heads are stateless subprocesses (`claude -p` / `codex exec` die per call),
    so council rebuilds context every turn from the ledger. This preamble is the ONLY memory the
    codex head has; it also lets a mid-conversation `/duel on` hand codex the whole back-story.
    Scoped to THIS session: only rows after the latest session_start (else a fresh `council ask`
    would 'remember' last week). Truncated hard — each turn ships this to up to 2 heads × N rounds."""
    rows = trace()
    starts = [i for i, r in enumerate(rows) if r.get("role") == "session_start"]
    rows = rows[starts[-1] + 1:] if starts else rows
    turns = []
    for r in rows:
        if r.get("role") == "user":
            turns.append(f"USER: {r['text']}")
        elif r.get("role") == "debate" and r.get("round") is not None:
            turns.append(f"CLAUDE: {str(r.get('proposer', ''))[:800]}"
                         + (f"\nCODEX: {str(r['adversary'])[:800]}" if r.get("adversary") else ""))
    if rows and rows[-1].get("role") == "user":
        turns.pop()          # the loop records the CURRENT question before handle() runs — don't
                             # echo it back as "history"; it arrives as the question itself
    text = "\n\n".join(turns[-cfg.history_turns * 2:])[-8000:]   # last N turns, ~8k char cap
    return f"Conversation so far (context — do not re-answer old turns):\n{text}\n\n---\n\n" if text else ""

class DebateRenderer:   # the G1 seam: REPLACES chat.py's _DebateRendererSketch
    """The /duel two-way branch. adversarial=False (DEFAULT) → plain claude chat, one subprocess,
    cheap turns. adversarial=True → the full 🟠vs🔵 debate. run_loop's /duel flips the flag live;
    it takes effect next turn (handle blocks, so a turn in flight always finishes first).
    Style from cfg.judge_style (whether/how); family from cfg.heads.judge (who)."""
    def __init__(self, cfg: Config, console: Console, adversarial: bool = False):
        self.cfg, self.console, self.adversarial = cfg, console, adversarial
    def handle(self, user_input: str) -> None:
        pre = _history_preamble(self.cfg)                       # both branches get the same memory
        if not self.adversarial:                                # SOLO: claude only, with memory
            out = _safe(proposer, pre + user_input, self.cfg, "claude")
            record({"role": "debate", "round": 0, "proposer": out, "adversary": None})
            self.console.print(f"[orange1]## 🟠 Claude[/]\n{out}")
            return
        run(pre + user_input, rounds=self.cfg.rounds,           # DUEL: the full debate engine
            judge=self.cfg.judge_style, cfg=self.cfg, console=self.console)
```

### FILE: `council/skills/debate/SKILL.md`
The SAME debate procedure written for an LLM to run (the in-Claude-Code `/council-debate` form).
`debate.py` serves `council ask`; this file serves the plugin. Trimmed from Debby's 68-line SKILL.md.

---

# G3 — WRAP   *(the hard 20% — mapped in full)*

## What G3 is trying to do

Council wants to put **its own face on a tool it doesn't control.** The real reasoning engine in CODE
mode has to be the actual `claude` binary (only it loads `~/.claude`, where the-harness's commit-gate lives),
but the user must only ever see *council*. G3 is the machinery that makes that work.

It uses **one shared mailbox** (a private folder both sides read/write) and **four wires + a gate**:

| wire | direction | how | omnigent source |
|---|---|---|---|
| **inject** | in | type into the hidden tmux pane (`tmux send-keys`) | `claude_native_bridge.py` |
| **transcript** | out (authoritative) | tail Claude's own JSONL log (messages, tool calls/results) | `claude_native_forwarder.py` (read half) |
| **deltas** | out (live tokens) | a `MessageDisplay` hook dribbles each chunk to a file | `claude_native_message_display_hook.py` |
| **cost/model** | out (meta) | a `statusLine` wrapper skims Claude's status bar | `claude_native_status.py` |
| **policy gate** | — | a `PreToolUse` hook answers allow/deny/ask | `native_policy_hook.py` |

The whole trick is legal because **Claude runs every hook registered for an event**: council adds its
hooks via `--settings` *on top of* the-harness's `~/.claude` hooks — they stack, council never replaces the-harness.

**Why so much code in omnigent but so little in council:** omnigent's size is *breadth* (11 coding
agents) + *multi-user* (a server everything is POSTed to). Council keeps the four wires and the gate
and **deletes the post office.** ~30k lines → ~1,500.

### Step-by-step: what happens when you run `council code`

```
   COUNCIL  (what you see)              MAILBOX folder              HIDDEN CLAUDE  (real binary in tmux)
   ────────────────────────            ──────────────             ──────────────────────────────────────

   session.run_claude_session   ← THE CONDUCTOR (the one function)
     │
     ├ bridge.prepare_bridge_dir ──────► (creates the folder)
     ├ state.save_launch_cwd ──────────► launch.json            (so --resume reattaches here)
     ├ bridge.write_hook_settings ─────► council-settings.json ──► claude launches WITH 3 council hooks:
     └ bridge.launch_claude_in_tmux ───► tmux.json                    • render.message_display_hook
                                          (where the pane is)         • harness_status.pre_tool_use_gate
                                                                       • state.status_line_wrapper
                                                                  (these STACK on the-harness's hooks — both fire)
   ══════════ then two pumps run at the same time ══════════

   PUMP A — OUTPUT
     events.read_events                                          claude works; the 3 channels fill up:
        reads new bytes  ◄──────────── transcript.jsonl    ◄──── claude's OWN native log (messages, tools)
        since a bookmark ◄──────────── message_deltas.jsonl ◄─── render.message_display_hook (per token)
              │          ◄──────────── context.json        ◄─── state.status_line_wrapper (cost/model)
              ▼
        render.Renderer.handle ── paints in council's skin + record() → ledger (G4)

   PUMP B — INPUT
     render.Renderer.read_input ── you type into COUNCIL's box
              │
              ▼
        bridge.inject ───────────► tmux send-keys ──────────────► claude's input box (it works → Pump A picks up)

   THE GATE  (fires whenever claude wants a tool)
        claude PreToolUse ──► harness_status.pre_tool_use_gate ──► G5 policy.evaluate → allow / deny / ask
                              (+ the-harness's own commit-gate fires too — they stack)

   you quit → both pumps stop → run_claude_session returns. Ledger has the whole session.
```

> **Reminder:** none of `chat.py` / `debate.py` / `backends.py` appear here — those are the `ask`
> engine. `code` runs its own two pumps instead of `run_loop` because the hidden claude streams live.

> **How to read each table:** `council uses ← omnigent fn:line (open reference/omnigent/omnigent/…) — what it does — verdict`.

---

## `council/wrap/bridge.py`  ←  `claude_native_bridge.py` (4,789 → ~600)
The tmux plumbing + transcript reading + hook-settings writer. **The crown jewel to lift verbatim.**

| council uses | ← omnigent fn:line | what it does (in → out) | verdict |
|---|---|---|---|
| the nouns | `ClaudeTranscriptItem`:280, `TranscriptReadResult`:300, `ClaudeHookRecord`:331, `ClaudeMessageDelta`:462 | dataclasses: one conv item / a read-bundle (items + cursors) / a hook record / a token chunk | KEEP (slim) |
| **inject (write wire)** | `inject_user_message`:2347 | bridge dir + text → none (raises if undelivered). Types text into the hidden pane via a **verified** bracketed paste | **LIFT verbatim** |
| inject helpers | `_run_tmux`:2716, `_capture_pane`:2746, `_claude_prompt_rendered`:2774, `_submit_needle`:2790, `_draft_in_input_box`:2820, `_wait_for_claude_prompt_ready`:2874, `_paste_payload_bytes`:2924, `_wait_for_tmux_info`:2965 | the send-keys / read-the-screen mechanics — they encode the 16KB cap, the trailing-`\` bug, the coalesced-paste race | **LIFT** |
| read (out wires) | `read_transcript_items_from_offset`:1778, `read_message_deltas_from_offset`:504, `_message_delta_from_jsonl_text`:538 | file + byte offset → new items/deltas + new offset (complete lines only) | KEEP |
| launch wiring | `prepare_bridge_dir`:735, `build_hook_settings`:1024, `augment_claude_args`:1279 | make the mailbox; build the hooks settings; stuff `--settings` into the launch args | KEEP / TRANSFORM |
| ✂ drop | `start_tool_relay`:2990, `_serve_mcp`:3065, `_stdio_jsonrpc_loop`:3362, `display_cost_approval_popup`:2602, `post_tools_changed`:2675 | the MCP tool-relay server + server-side UI | **DROP** |

```python
"""council/wrap/bridge.py — tmux mechanics + transcript reading + hook-settings writer.
LIFT NEAR-VERBATIM (encodes real Claude-TUI bugs you must not rediscover):
  • inject_user_message:2347 — verified bracketed-paste submit (16KB cap → load-buffer/paste-buffer -p;
    trailing-\\ eats Enter → trailing newline absorbs it; coalesced paste → poll pane, re-send Enter)
  • tmux helpers :2716-2965
KEEP: transcript types + readers (:1778, :504) + build_hook_settings:1024.
DROP: MCP tool-relay (:2990/:3065/:3362), cost popup :2602, post_tools_changed :2675."""
from __future__ import annotations
import json, shlex, subprocess, sys, tempfile
from pathlib import Path
from ..config import Config

def prepare_bridge_dir(cfg: Config) -> Path:
    """A private 0700 scratch dir per session: tmux.json, message_deltas.jsonl, context.json, hook state.
    ↔ prepare_bridge_dir:735 (minus server bridge-id registration)."""
    ...

def launch_claude_in_tmux(bridge: Path, *, command, claude_args, use_claude_config) -> None:
    """`tmux -S <sock> new-session -d <command> <augmented args>`; write tmux.json (socket + target).
    ↔ _launch_claude_terminal:3779 + augment_claude_args:1279 (adds --settings <hooks>), minus the daemon wrapper."""
    ...

def inject(bridge: Path, text: str) -> None:
    """LIFT inject_user_message:2347 near-verbatim. Drop ONLY the active-session/request-id guard."""
    ...

def read_transcript_items_from_offset(transcript_path: Path, offset: int):
    """New authoritative items (user/assistant/tool) since a byte offset. ↔ :1778. KEEP."""
    ...

def write_hook_settings(bridge: Path) -> None:
    """Write a Claude --settings json registering council's hooks. ↔ build_hook_settings:1024.
    STACKS with the-harness's ~/.claude hooks. Wires: MessageDisplay→render hook, PreToolUse→harness_status gate,
    statusLine.command→state.status_line_wrapper."""
    ...
```

---

## `council/wrap/session.py`  ←  `claude_native.py` (4,404 → ~150) + `inner/claude_native_executor.py` (250 → ~15) + `inner/claude_native_harness.py` (30 → 0)
The conductor: launch the binary, run the inject + render pumps.

| council uses | ← omnigent fn:line | what it does | verdict |
|---|---|---|---|
| launch entry | `run_claude_native`:342 | kwargs → none. preflight → resolve config (skipped when `use_claude_config=True`) → launch+attach | KEEP (swap launch branch) |
| launch the pane | `_launch_claude_terminal`:3779, `_preflight_local_tools`:3696, `_strip_resume_from_claude_args`:1345 | start `claude` in tmux; fail early if tools missing; drop a stray `--resume` | KEEP-LITE |
| find transcript | `_find_claude_transcript`:1256, `_claude_project_dir_for_cwd`:1287, `_sanitize_claude_project_name`:1301 | locate Claude's JSONL log for the cwd | KEEP |
| what a "turn" is | `ClaudeNativeExecutor.run_turn`:99 (`supports_streaming`:64 → False) | a turn = just inject; output comes from the forwarder, not here | → ~15 lines |
| ✂ drop | resume picker 577–1010; multi-provider config 1382–1731; `_run_with_local_server`:1792 / `_run_with_remote_server`:2774 / daemon :2585; cold-resume 3235–3593; `create_app`:22 (FastAPI harness) | the server ring + provider breadth + a TUI picker | **DROP** |

```python
"""council/wrap/session.py — launch the REAL claude in a hidden tmux pane, attach locally.
↔ run_claude_native:342 + executor (inject) + harness (DROPPED WHOLE; council renders local)."""
from __future__ import annotations
import threading
from pathlib import Path
from .bridge import prepare_bridge_dir, launch_claude_in_tmux, inject, write_hook_settings
from .events import read_events
from .render import Renderer
from .state import save_launch_cwd
from ..config import Config

def run_claude_session(*, claude_args, use_claude_config: bool, command: str, resume: str | None, cfg: Config) -> None:
    """The CODE engine. Unlike ask/review it OWNS ITS LOOP — a live attached session that doesn't return per-turn."""
    bridge = prepare_bridge_dir(cfg)
    save_launch_cwd(bridge, Path.cwd(), resume)
    write_hook_settings(bridge)                 # council's hooks → --settings; STACK on the-harness's ~/.claude hooks
    launch_claude_in_tmux(bridge, command=command, claude_args=claude_args, use_claude_config=use_claude_config)
    renderer = Renderer(cfg, bridge)
    out = threading.Thread(target=lambda: [renderer.handle(e) for e in read_events(bridge)], daemon=True)
    out.start()                                 # pump 1: hidden claude's 3 channels → council's skin
    while out.is_alive():                       # pump 2: council's input box → the hidden pane
        try:
            text = renderer.read_input()
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            inject(bridge, text)
```

---

## `council/wrap/events.py`  ←  `claude_native_forwarder.py` (4,183 → ~200)
Tail the three out-channels → yield **local** render events. The deletions *are* the shrink.

| council uses | ← omnigent fn:line | what it does | verdict |
|---|---|---|---|
| the read loop | `forward_claude_transcript_to_session`:589 | poll transcript + deltas + status since offsets | KEEP the read half |
| read helpers | `_forward_available_items`:2683, `_forward_available_deltas`:3267, `_forward_available_status_events`:2376 | read new items/deltas/status records | KEEP |
| ✂ drop | every `_post_external_*` (3099–3797), `supervise_forwarder`:1721, subagent forwarding 877–1452, session rotation 1822–2146 | POST everything to the omnigent server | **DROP all** |

```python
"""council/wrap/events.py — tail the THREE out-channels → yield LOCAL render events.
KEEP the READ half (:2683, :3267, :2376); DROP every _post_external_* (3099-3797) and the supervisor.
Council renders locally → nothing to forward, which is why 4,183 lines collapse to ~200."""
from __future__ import annotations
import json, time
from pathlib import Path
from .bridge import read_transcript_items_from_offset

def read_events(bridge: Path):
    """Generator. Poll the three files past their last offsets; yield events until the pane dies:
       1) transcript .jsonl    → authoritative items (user/assistant-final/tool call+result)
       2) message_deltas.jsonl → LIVE token chunks {message_id,index,final,delta} → smooth streaming
       3) context.json         → cost / model / context-window (statusLine wrapper)
    Reconcile live deltas vs the authoritative final POSITIONALLY (FIFO) — message_id isn't in the transcript."""
    ...
```

---

## `council/wrap/render.py`  ←  `claude_native_message_display_hook.py` (144, lift whole)
Two halves: a tiny hook that runs *inside* claude, and the painter that runs in *council*.

| council uses | ← omnigent fn:line | what it does (in → out) | verdict |
|---|---|---|---|
| live-token hook | `main`:54, `_delta_record`:100 | a MessageDisplay payload on stdin → exit 0; appends `{message_id,index,final,delta}` to `message_deltas.jsonl` (stdlib-only, O_APPEND atomic, because Claude BLOCKS per chunk) | **LIFT whole** |
| local painter | *(new council code)* | consume `events.read_events` → Rich `Live` block in council's skin | NEW |

```python
"""council/wrap/render.py — branded local render + the per-chunk MessageDisplay hook (the writer).
↔ claude_native_message_display_hook.py (LIFT NEAR-WHOLE) + the web-UI SSE path, REPLACED by a local Rich Live view."""
from __future__ import annotations
import json, os
from rich.console import Console
from rich.live import Live
from ..ledger import record

def message_display_hook(bridge_dir: str, payload: dict) -> int:
    """Runs INSIDE claude (registered by bridge.write_hook_settings). Append the chunk to
    message_deltas.jsonl via O_APPEND. LIFT verbatim — already minimal, already correct."""
    ...

class Renderer:
    def __init__(self, cfg, bridge: Path): self.cfg, self.bridge, self.console = cfg, bridge, Console()
    def read_input(self) -> str:
        """council's branded input box (NOT Claude's prompt) → returned to session.py's inject pump."""
        ...
    def handle(self, event) -> None:
        """Stream live deltas into a Rich Live block; commit authoritative items; show cost; record() each."""
        ...
```

---

## `council/wrap/state.py`  ←  `claude_native_state.py` (279 → ~40) + `claude_native_status.py` (165, lift whole)
Remember where we launched (for `--resume`) and skim cost/model off the status bar.

| council uses | ← omnigent fn:line | what it does (in → out) | verdict |
|---|---|---|---|
| cost/model capture | `main`:25, `_write_context_atomic`:65, `_chain`:135 | Claude's statusLine JSON on stdin → writes `context.json` (cost/model/context-window), then runs the user's original statusLine | **LIFT whole** |
| resume | (`claude_native_state.py` launch-cwd) | persist launch cwd so `--resume` reattaches the right project | KEEP ~40 |

```python
"""council/wrap/state.py — launch-cwd persistence (resume) + the statusLine cost/model capture.
↔ claude_native_state.py (launch-cwd) + claude_native_status.py (LIFT NEAR-WHOLE).
claude-native emits NO response.completed event, so this statusLine hack is the ONLY cost/model source."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path

def save_launch_cwd(bridge: Path, cwd: Path, resume: str | None) -> None:
    """Persist launch cwd so `council code --resume` reattaches the right project."""
    ...

def status_line_wrapper(bridge_dir: str, chain: str | None) -> int:
    """Runs as Claude's statusLine.command. Read its stdin (context_window, cost.total_cost_usd, model),
    write those atomically to context.json for events.py, then exec the user's ORIGINAL statusLine. LIFT verbatim."""
    ...
```

---

## `council/wrap/harness_status.py`  ←  `native_policy_hook.py` (445 → ~150) + `claude_native_hook.py` (1,002 → ~120) + `runner/pending_approvals.py` (207, mostly drop)
The PreToolUse gate. Three **pure** translation functions (no I/O) → trivial to lift.

| council uses | ← omnigent fn:line | what it does (in → out) | verdict |
|---|---|---|---|
| payload → request | `hook_payload_to_evaluation_request`:91 | hook event + payload → a normalized eval request, or None | KEEP |
| verdict → output | `evaluation_response_to_hook_output`:171 | event + policy verdict → Claude hook JSON. **ALLOW→None** so the user's own consent gate still fires; DENY→`"deny"`; **ASK→`"ask"`** (pops Claude Code's native y/n prompt — schema verified vs claude 2.1.199 docs, 3 Jul 2026) | KEEP (ASK remapped) |
| fail closed | `fail_closed_hook_output`:276 | event → PreToolUse=deny, others=None (phase-aware) | KEEP |
| dispatch | `claude_native_hook.main`:72, `_main_evaluate_policy`:803, `_main_permission_request`:658 | route a hook invocation to the right mode | KEEP (decision only) |
| ✂ drop | `post_evaluate_with_retry`:319, `_post_hook_with_reattach`:565, rotation 240–492, `pending_approvals.py` | POST the decision to the server + server-side queue | **DROP** → call G5 in-process |

```python
"""council/wrap/harness_status.py — the PreToolUse policy gate, run as a Claude hook.
↔ claude_native_hook.py (decision modes :803/:658) + native_policy_hook.py (:91 / :171 / :276).
BIG DROP: post_evaluate_with_retry:319 (+ _post_hook_with_reattach:565) — council evaluates IN-PROCESS via G5.
COEXISTENCE: STACKS with the-harness's own PreToolUse commit-gate. Council's gate = blast_radius; the-harness's = git commits."""
from __future__ import annotations
from ..policy import evaluate            # G5 seam

def pre_tool_use_gate(payload: dict) -> dict:
    """Claude PreToolUse payload → ALLOW/DENY/ASK. Translate payload → eval request → G5 evaluate() → hook output.
    FAIL CLOSED (deny) on any error; a hook must never crash Claude's loop."""
    ...
```

---

## G3 in one breath

`prepare_bridge_dir` makes the mailbox. `launch_claude_in_tmux` + `write_hook_settings` start the real
binary with council's hooks stacked on the-harness's. Then `session.py` runs two pumps: **`read_events`**
tails the transcript + deltas + cost and the **`Renderer`** paints them in council's skin; **`inject`**
types your replies back into the hidden pane. `harness_status` gates each tool call by asking G5. The
user sees only council — and because the real Claude Code is what's running, the-harness stays live.

Everything genuinely hard is **small and already solved** (the inject dance, the cursor readers, two
tiny hooks, three pure policy functions). Everything **big** in omnigent is multi-user/multi-agent
plumbing council deletes. That's the whole group.

---

## G3 — HARDENING  *(must-build; NOT inherited from omnigent)*

G3's happy path is lifted from omnigent (call it **Group A** — already solved in the code we lift).
Two classes of risk survive that lift:

- **Group B — the camera is fragile.** Confirming that input was *typed and submitted* means reading
  Claude's TUI **screen** (`_draft_in_input_box`, the prompt glyph, the "[Pasted text]" placeholder).
  Claude's screen layout is not a contract — when Anthropic changes it, these checks break, and they
  break **silently**: the code falls through to "submit blind" (one unverified Enter, no error), so a
  message can vanish with no signal.
- **Group C — omnigent's *server* solved it, and we delete the server.** Two things looked handled in
  omnigent only because its runner + web UI did them: **pane geometry** (browser→server resize
  forwarding) and **"don't type while Claude is busy"** (a request/response web UI). Neither lives in
  the `bridge.py` we lift, so council must rebuild them.

### The finding that reframes both  *(verified against real transcripts + `build_hook_settings:1090`)*

Council renders from the **diary** (the transcript JSONL) and installs **hooks** — two *reliable*
channels. We can lean on them instead of the fragile camera:

- The transcript logs **user** turns live: `role:"user"`, `content`, `timestamp`, `promptId` (plus a
  `last-prompt` record). A dependable submission oracle.
- `build_hook_settings` already registers two **symmetric, event-driven** hooks:
  - **`UserPromptSubmit`** — fires the instant a submitted prompt reaches Claude, explicitly
    *including via `tmux send-keys`* (council's exact injection path). ⇒ *"the paste truly submitted."*
  - **`Stop`** (+ its error twin **`StopFailure`**) — fires when the turn ends. ⇒ *"Claude is idle again."*

Together these are a clean **busy/idle state machine + a submission confirmation — with no
screen-scraping.** Council already writes hooks and reads the mailbox, so consuming them is nearly free.
This collapses the scary parts of B and C into one keystone: **register `UserPromptSubmit` +
`Stop`/`StopFailure`, have them write a state marker to the mailbox, and drive council off that marker.**

### IMPLEMENT NOW

| # | item (group) | what to build | why now |
|---|---|---|---|
| **H1** | busy/idle interlock *(C — the the-harness-critical one)* | Add `UserPromptSubmit` + `Stop` + `StopFailure` to `write_hook_settings`; each appends `{state, promptId, ts}` to `session_state.json` in the mailbox. Input pump: after `inject`, mark **busy** and disable council's box until `UserPromptSubmit` is seen; re-enable **only** on `Stop`/`StopFailure`. | Kills the "type while Claude is busy" race, and makes a long **the-harness** Codex review render as *"working"* instead of a hang or a premature timeout. Event-driven, not camera. |
| **H2** | submission ground-truth + loud fail *(B — the core)* | Reuse H1's `UserPromptSubmit` marker as the authoritative *"it submitted"* signal. If an `inject` isn't confirmed within a timeout → surface a clear error **and** `record()` it to the ledger (G4). | Converts every *silent* "submit blind" into a *detected, logged* failure. Reuses H1 ⇒ ~free. |
| **H3** | central TUI-contract constants *(B — containment)* | One file `wrap/tui_contract.py` holding the prompt glyph, the pasted-placeholder prefix, and the scan-tail count. Every camera check imports from here. | When Claude's UI changes, it's a **one-line** fix, not a hunt through `bridge.py`. Costs nothing; pure hygiene. |
| **H4** | pin the hidden pane width *(C — geometry)* | `tmux new-session -d -x 200 -y 50` in `launch_claude_in_tmux`. | The user never sees the hidden pane (council paints its own skin), so a wide fixed width removes needle-wrapping mismatches that trigger blind-submit. One line. |
| **H5** | statusline-glyph rule *(B — self-sabotage)* | council's statusLine wrapper must never emit the prompt glyph; state it as a constraint comment in `tui_contract.py`. | Stops council's *own* status bar from fooling council's *own* camera. One line of discipline. |

### PROMOTED TO CODE — H1–H5

The rows above say *what* to build; below is the **actual Python** (no longer `...` stubs), each block
tagged `↔` with the G3 sketch file it patches. H1 and H2 share one spine — a marker file plus a
`SessionState` reader — so H2 is mostly "reuse H1." H3–H5 are the one-liners they were promised to be.
Total ≈ 120 real lines.

> **VERIFIED LIVE** (claude 2.1.197 + official hooks docs, 1 Jul 2026): all three events are real —
> `StopFailure` fires *"when the turn ends due to an API error"* — and a marker-file run confirmed the
> order **`UserPromptSubmit` → `Stop`** within a turn. Payloads carry `hook_event_name` (what
> `_EVENT_STATE` keys on) and `prompt_id` (snake_case), present on BOTH submit and stop — so a Stop can be
> matched to its exact submit by id, an available upgrade over the timestamp compare in `wait_submitted`.
> The `_EVENT_STATE` map stays the single source of truth if Anthropic ever renames an event.

#### H1 — busy/idle interlock

```python
# ═══ H1a — the WRITER: a hook that runs INSIDE claude ═══════════════════════
# ↔ NEW file wrap/state_hook.py (sibling of render.message_display_hook).
# stdlib-only + O_APPEND: claude BLOCKS on this hook, so it must be tiny and its
# writes atomic. One marker per turn-boundary event.
import json, os, sys, time

_STATE_FILE = "session_state.jsonl"     # append-per-event ⇒ jsonl (table said .json; append = jsonl)
_EVENT_STATE = {                        # the ONE place event-name → state lives (verify names HERE)
    "UserPromptSubmit": "busy",         # a prompt reached claude — incl. via council's tmux send-keys
    "Stop":             "idle",         # turn ended cleanly
    "StopFailure":      "idle",         # turn ended in error — still idle, still re-enable the box
}

def session_state_hook(bridge_dir: str, payload: dict) -> int:
    """payload = the hook's stdin JSON → append {ts,event,state,promptId} → 0 (never block claude)."""
    event = payload.get("hook_event_name", "")
    state = _EVENT_STATE.get(event)
    if state is None:
        return 0                        # not one of our three events — no-op
    marker = {"ts": time.time(), "event": event, "state": state,
              "prompt_id": payload.get("prompt_id"),      # ← verified live: field is snake_case;
              "session_id": payload.get("session_id")}    #   present on BOTH submit & stop → correlatable
    fd = os.open(os.path.join(bridge_dir, _STATE_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(marker) + "\n").encode())   # single write ⇒ offset-atomic
    finally:
        os.close(fd)
    return 0

if __name__ == "__main__":              # claude invokes it as: python -m council.wrap.state_hook <bridge>
    raise SystemExit(session_state_hook(sys.argv[1], json.load(sys.stdin)))


# ═══ H1b — the READER: the state machine council DRIVES OFF ══════════════════
# ↔ wrap/session.py (council's process). Tails the marker file; answers "busy?"
# with NO screen-scraping. THIS is what shrinks the fragile camera to nothing.
import json, time
from pathlib import Path
from ..ledger import record          # G4 — torn lines get logged, not swallowed

class SessionState:
    def __init__(self, bridge: Path):
        self._path = bridge / _STATE_FILE
        self._offset = 0
        self._buf = b""                 # holds a half-written trailing line between polls
        self.busy = False
        self.last_submit_ts = 0.0       # ts of the most recent UserPromptSubmit (H2's oracle)

    def poll(self) -> None:
        """Fold any new COMPLETE markers into .busy / .last_submit_ts. Cheap; call in a tight loop."""
        if not self._path.exists():
            return
        with self._path.open("rb") as f:
            f.seek(self._offset)
            self._buf += f.read()
            self._offset = f.tell()
        *lines, self._buf = self._buf.split(b"\n")           # keep the partial tail buffered
        for line in lines:
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                # A torn line here raises in the DAEMON thread → Python kills it SILENTLY →
                # .busy freezes True → the input box locks FOREVER with no visible cause.
                # Skip-and-log is safe: markers are cumulative state, the next good one corrects.
                record({"role": "state_parse_error", "line": line[:200].decode(errors="replace")})
                continue
            self.busy = (m["state"] == "busy")               # last marker in the batch wins
            if m["event"] == "UserPromptSubmit":
                self.last_submit_ts = m["ts"]                # tracked SEPARATELY — see H2 race note

    def wait_idle(self, timeout: float) -> bool:
        """Block until claude is idle again. True = idle; False = timed out (backstop, never hang)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if not self.busy:
                return True
            time.sleep(0.05)
        return False

    def wait_submitted(self, since_ts: float, timeout: float) -> bool:
        """H2's oracle: block until a UserPromptSubmit marker NEWER than since_ts lands.
        Keys on last_submit_ts (NOT .busy) so a turn fast enough to Stop before our first
        poll still counts as submitted. True = paste landed; False = it did not (fail loud)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.last_submit_ts > since_ts:
                return True
            time.sleep(0.05)
        return False


# ═══ H1c — REGISTER the three events (patches bridge.write_hook_settings @658) ═
# The @658 stub becomes real. council's three events STACK on the-harness's ~/.claude
# hooks AND on council's own MessageDisplay/PreToolUse — claude runs them all.
import json, shlex, sys
from pathlib import Path

def write_hook_settings(bridge: Path) -> None:
    # THE #1 FRESH-CLONE TRAP: these commands run INSIDE claude's process tree, from claude's
    # cwd — NOT council's repo. `sys.executable` pins the right python, but `-m council.…` only
    # resolves if the package is importable THERE. Defense in depth: (a) `pip install -e .` is a
    # documented setup requirement; (b) PYTHONPATH below makes hooks work even on a raw clone;
    # (c) session.py runs `{py} -m council.wrap.state_hook --check` ONCE before launching tmux
    #     and aborts with "run pip install -e ." — fail loud at launch, never silently later.
    pkg_root = Path(__file__).resolve().parents[2]      # the repo root containing council/
    py = shlex.quote(sys.executable)                    # quote: home dirs can contain spaces
    env = f"PYTHONPATH={shlex.quote(str(pkg_root))}"
    b = shlex.quote(str(bridge))
    state_cmd = f"{env} {py} -m council.wrap.state_hook {b}"                   # ← H1a
    settings = {
        "hooks": {
            "MessageDisplay": [{"hooks": [{"type": "command",
                "command": f"{env} {py} -m council.wrap.render {b}"}]}],
            "PreToolUse":     [{"hooks": [{"type": "command",
                "command": f"{env} {py} -m council.wrap.harness_status {b}"}]}],
            # ↓ H1: the interlock — one command wired to three turn-boundary events
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": state_cmd}]}],
            "Stop":             [{"hooks": [{"type": "command", "command": state_cmd}]}],
            "StopFailure":      [{"hooks": [{"type": "command", "command": state_cmd}]}],
        },
        "statusLine": {"type": "command",
                       "command": f"{env} {py} -m council.wrap.state {b}"},
    }
    (bridge / "council-settings.json").write_text(json.dumps(settings, indent=2))


# ═══ H1d — GATE the input pump on idle (patches session.run_claude_session) ══
# BEFORE:  while out.is_alive(): text = renderer.read_input(); if text: inject(bridge, text)
# AFTER:   don't even SHOW the box until claude is idle. A long the-harness review now
#          renders as "working…", never a hang or a premature double-type.
# (renderer.notice/.error = two new console-print methods on the G3 Renderer.)
def _input_pump(bridge: Path, renderer, state: SessionState, cfg) -> None:
    stalls = 0                # ESCAPE HATCH: the happy path depends on Stop ARRIVING. If claude
    while True:               # crashed / pane died / hooks broken, it never does — bound the wait.
        if not state.wait_idle(timeout=cfg.turn_timeout):
            stalls += 1
            if stalls >= 2:                                   # 2× turn_timeout with no event: check
                if not _pane_alive(bridge):                   # ground truth (tmux has-session)
                    renderer.error("hidden claude died — session over"); return
                renderer.error(f"no Stop event in {stalls * cfg.turn_timeout}s — "
                               "unlocking input (interlock degraded)")
                state.busy = False    # manual override: worst case = the pre-H1 world (typing
            else:                     # while busy) — strictly better than hanging forever
                renderer.notice("Claude still working…")      # H1: box stays LOCKED while busy
            continue
        stalls = 0
        try:
            text = renderer.read_input()                      # box is live only now
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            continue
        _inject_confirmed(bridge, text, state, renderer, cfg) # ← H2 does the actual send
```

#### H2 — submission ground-truth + loud fail

```python
# ↔ wrap/session.py. Reuses H1b's SessionState — H2 is just "wrap inject() in a
# confirmation." Converts every SILENT "submit blind" into a DETECTED, LOGGED failure.
import time
from ..ledger import record            # G4

def _inject_confirmed(bridge: Path, text: str, state: SessionState, renderer, cfg) -> None:
    sent_at = time.time()                                     # wall clock — same clock the hook stamps
    inject(bridge, text)                                      # omnigent's verified-paste (lifted, unchanged)
    if not state.wait_submitted(since_ts=sent_at, timeout=cfg.submit_timeout):
        renderer.error(f"⚠ inject NOT confirmed in {cfg.submit_timeout}s — message may not have submitted")
        record({"role": "inject_error", "text": text, "waited_s": cfg.submit_timeout})   # ← the loud fail
```

> Why this is nearly free: the hard part (knowing a submit landed) is H1's `UserPromptSubmit` marker.
> H2 only times the wait and, on a miss, does the two things omnigent's silent fall-through never did —
> **tell the user** and **write it to the ledger**.

#### H3 — central TUI-contract constants

```python
# ↔ NEW module wrap/tui_contract.py. The ONE place claude's screen layout is
# encoded; every camera check imports from here. When Anthropic changes the TUI
# it's a one-line edit, not a hunt through bridge.py.
# VALUES ARE PLACEHOLDERS — confirm against a live `claude` pane before trusting.
PROMPT_GLYPH = "│ > "                    # the input-box marker _claude_prompt_rendered scans for
PASTED_PLACEHOLDER_PREFIX = "[Pasted text"   # claude shows this instead of a long pasted body
SCAN_TAIL_LINES = 40                     # how many bottom rows of capture-pane a check reads
# H5 lives with its constant on purpose: council's OWN status bar must never
# contain PROMPT_GLYPH, or the camera would mistake council's status for a prompt.
```

#### H4 — pin the hidden pane geometry

```python
# ↔ one line inside bridge.launch_claude_in_tmux. The user never sees this pane
# (council paints its own skin), so a wide FIXED size removes the needle-wrapping
# that silently triggers a blind submit. No downside; pure robustness.
argv = ["tmux", "-S", str(sock), "new-session", "-d",
        "-x", "200", "-y", "50",         # ← H4: fixed geometry, no auto-resize surprises
        "-t", target, command, *augmented_args]
```

#### H5 — statusline-glyph rule

```python
# ↔ two lines inside state.status_line_wrapper. Enforce the H3 constraint so
# council never fools its own camera. Defensive STRIP (not assert) — a broken
# status bar must never crash the session.
from .tui_contract import PROMPT_GLYPH

def status_line_wrapper(bridge_dir: str, chain: str | None) -> int:
    ...                                  # (read stdin, write context.json, compose the cost/model line)
    status_text = status_text.replace(PROMPT_GLYPH, "")   # ← H5: our bar can't look like a prompt
    ...
```

#### Config knobs these assume (G1 `config.py`)

```python
# [MERGED 3 Jul 2026] These two fields (plus history_turns, and head_timeout 120→300) now live
# IN the G1 @dataclass Config listing — nothing left to add at port time.
turn_timeout: int = 600      # H1: max wait for a turn to finish before the stall check
submit_timeout: int = 10     # H2: max wait to confirm a paste submitted before failing loud
```

### DEFER — documented, build next time

| # | item (group) | why defer |
|---|---|---|
| **D1** | drop the camera's draft-detection entirely; drive `inject` purely off `UserPromptSubmit` (paste → Enter → wait for the hook → retry the paste **only if** unconfirmed) | The bigger simplification of omnigent's inject. Keep omnigent's verified-paste **as-is for v1** (it works). Do this once H1/H2's state machine is proven. Naturally double-submit-safe: you retry *because* the hook says the first attempt didn't land. |
| **D2** | `PermissionRequest` hook to bulletproof the re-Enter/gate risk *(A residual)* | omnigent already guards re-Enter (only fires while the draft is verifiably still in the box). A `PermissionRequest`-based suppression is belt-and-suspenders **and** is entangled with the G5 permission routing — do it *with* G5, not now. |
| **D3** | active launch self-probe (inject a known string at boot, verify the camera sees it) | H2 already fails loud on the *first real message*. A boot probe only buys failing slightly earlier. Nice, not needed for v1. |
| **D4** | timing constants → G1 `Config` knobs | The paste/submit waits are hardcoded to omnigent's hosts. Only bites on slow hardware; lift them to config when someone actually hits it. |
| **D5** | unique paste-buffer name per inject | omnigent's fixed `omnigent-paste` buffer is safe under one serialized input loop. Only needed if council ever **auto-injects concurrently** with the user (e.g. an `/effort` on launch). |

**Through-line:** because council renders from the **diary**, it can *confirm* from the diary + hooks
too — shrinking the fragile **camera** down to a single remaining job: *is the input box mounted at
boot?* **H1–H2 are the keystone; H3–H5 are one-liners; everything genuinely optional is deferred.**

---

# G5 — POLICY   *(the blast-radius brain behind the gate)*

## What G5 is trying to do

One file, one job: answer **"may this tool call run?"** with **ALLOW / ASK / DENY**, judged by *blast
radius* (reversibility). It is the brain that G3's `harness_status` PreToolUse hook calls. The division of
labour is the seam:

```
   Claude PreToolUse payload ─► harness_status (G3): translate payload → V0 event
                                      │
                                      ▼
                               policy.evaluate (G5): classify by blast radius   ← THIS GROUP
                                      │  {"result": "ALLOW"|"ASK"|"DENY", "reason": …}
                                      ▼
                               harness_status (G3): verdict → Claude hook JSON
```

G3 does the *translation* (Claude hook ⇄ normalized event); **G5 is the pure classification in the
middle** — no I/O, no network, just a command string in → a verdict out. The whole value is one idea:
let the overwhelming common case fly (reads, tests, edits, local git → **ALLOW**), gate the
outward/destructive-but-recoverable case behind a human (`git push`, `gh pr merge`, infra deploy →
**ASK**), and hard-stop the irreversible case (force-push, `rm -rf /`, hard-reset to a remote ref →
**DENY**).

**The framing that lets G5 stay small: it's a *second* layer.** Recall G3's `harness_status` maps
**ALLOW → None**, which means *Claude Code's own permission prompt still fires underneath*. So G5 isn't
the only thing between Claude and your disk — it's a coarse blast-radius net **stacked on top of** Claude
Code's native consent gate *and* the-harness's commit-gate. That's exactly why council can keep only
`blast_radius` and throw the rest of omnigent's policy module away.

> **Honest limit (omnigent says it itself, `policies.py:143`):** this is a **safety net against
> accidental / obvious damage, not a security boundary.** It does not model subshells, command
> substitution, or `eval` — a *determined* caller evades it. The real boundary is sandboxing, which
> council's `code` does not add (it runs the real `claude` in your real shell). G5 catches mistakes; the
> Claude/the-harness layers underneath catch the rest.

## `council/policy.py`  ←  `inner/nessie/policies.py` (604 → ~120)

omnigent's `policies.py` ships **four** policy factories + a YAML registry. Three of the four exist only
to corral a **fleet of sub-agents** — which council does not have (Claude Code *is* the only agent; it
never fans out through council's gate). So three drop whole, and the registry/factory indirection drops
with them. What survives is the one genuinely valuable, security-relevant piece: `blast_radius`.

| omnigent piece (`policies.py`) | what it does | council |
|---|---|---|
| `blast_radius` factory | classify a shell command by reversibility → ALLOW/ASK/DENY | **KEEP** — the whole point of G5 |
| `_shell_statements` · `_rm_severity` · `_push_severity` · `_rm_target_is_catastrophic` · sudo/env-prefix parsing · short-option bundling | the robustness helpers that make `blast_radius` not trivially evadable | **KEEP** — these *are* the value; a single regex re-introduces the exact bugs their own comments document (split `rm -r -f`, `sudo`/`CI=1` prefixes, `+`/`:` refspecs) |
| `spawn_bounds` | cap sub-agent dispatches per turn | **DROP** — council dispatches no sub-agents |
| `headless_subagent_purpose_guard` | require `args.purpose` on a sub-agent send | **DROP** — same: no `sys_session_send` through council |
| `worktree_guard` | confine a worker's writes to `.worktrees/` | **DROP** — council runs one session in your real cwd, not parallel worktrees |
| `POLICY_REGISTRY` + `factory_params` (YAML) + the `config` 2nd arg | discover & parameterize policies from YAML, runner-side | **DROP** — one policy, called directly; the one knob (`gate_pushes`) becomes a config field |

**604 → ~120 lines.** Keep one factory's body (un-nested into a plain function), delete the other three
factories, the registry, and the YAML/runner indirection.

```python
"""council/policy.py — the blast-radius gate (ALLOW / ASK / DENY). G5.
↔ omnigent inner/nessie/policies.py:346 (blast_radius) + its robustness helpers (:134–:343).
PUBLIC SURFACE: evaluate(event) — called IN-PROCESS by G3 harness_status (no server, no YAML registry).
DROPPED: spawn_bounds / purpose_guard / worktree_guard (:408–:568, all multi-agent) + POLICY_REGISTRY (:573)."""
from __future__ import annotations
import re, shlex

_ALLOW = {"result": "ALLOW"}
def _decision(result: str, reason: str) -> dict: return {"result": result, "reason": reason}

# Irreversible → DENY (regex net for the cases not worth a bespoke parser).  ↔ :64
_DENY_PATTERNS = (re.compile(r"\bgit\b.*\breset\s+--hard\s+\w+/"),)              # hard-reset to a remote ref
# Outward / destructive but recoverable → ASK.  ↔ :69
_ASK_PATTERNS = (
    re.compile(r"\bgh\s+(pr\s+merge|release|repo\s+delete)\b"),
    re.compile(r"\b(kubectl|helm|terraform|databricks)\b.*\b(apply|deploy|destroy|delete)\b"),
)

def evaluate(event: dict, *, gate_pushes: bool = True,
             deny_reason: str = "Blocked by the blast-radius policy.") -> dict:
    """The ONE public entry (was blast_radius._evaluate, :368). event = a V0 tool_call
    {'type':'tool_call','data':{'name':…,'arguments':{…}}}; G3 harness_status builds it from
    Claude's PreToolUse payload. Returns {'result','reason'}."""
    args = _tool_call(event, {"Bash", "bash", "sys_os_shell"})   # both CLIs' shell tool  ↔ :383
    if args is None: return _ALLOW
    command = args.get("command")
    if not isinstance(command, str): return _ALLOW               # malformed → nothing to gate  ↔ :390
    statements = _shell_statements(command)                       # split ; && || | newline  ↔ :395
    sev = {s for stmt in statements for s in (_rm_severity(stmt), _push_severity(stmt))}
    if "DENY" in sev or any(p.search(command) for p in _DENY_PATTERNS):
        return _decision("DENY", f"{deny_reason} (irreversible: {command!r})")
    if gate_pushes and ("ASK" in sev or any(p.search(command) for p in _ASK_PATTERNS)):
        return _decision("ASK", f"High-blast-radius command needs approval: {command!r}")
    return _ALLOW

# --- the robustness helpers KEPT verbatim from omnigent (this is the value, not boilerplate) ---
def _tool_call(event, names): ...          # ↔ :39  args dict of a matching tool_call, else None
def _shell_statements(command): ...        # ↔ :134 best-effort split into per-statement token lists
def _rm_severity(argv): ...                # ↔ :247 recursive rm? catastrophic target→DENY / scoped→ASK (flag-robust)
def _push_severity(argv): ...              # ↔ :307 git push? force/delete→DENY / outward→ASK (refspec-robust)
def _command_index_after_shell_prefixes(argv): ...   # ↔ :210 strip `CI=1 sudo -n` to reach the real command
# (+ _rm_target_is_catastrophic :164, _skip_shell_assignments :192, _push_short_option_is_destructive :287)
```

> **Reading it** — `evaluate` is omnigent's `blast_radius._evaluate` lifted out of its factory closure
> (council has one policy, so there's nothing to parameterize from YAML — `gate_pushes` becomes a plain
> keyword). It pulls the `command` string out of a `Bash`/`bash`/`sys_os_shell` tool call, splits it into
> statements, asks two flag-robust classifiers (`_rm_severity`, `_push_severity`) plus two regex nets,
> and returns the worst verdict found. **The helpers are kept verbatim on purpose:** they're the reason
> the gate isn't fooled by `cd x && rm -rf y`, `sudo -n rm -rf /etc`, `CI=1 git push`, `git push origin
> +main`, or `rm -r -f`. Collapsing them back to one regex is precisely the mistake omnigent's comments
> (`:59–63`, `:287–304`) warn against.

### What each file does

G5 is a **single file** (`council/policy.py`) — unlike G3's six. Its whole public surface is one
function:

| symbol | role |
|---|---|
| `evaluate(event, *, gate_pushes, deny_reason)` | the **only** public entry; G3 `harness_status` calls it in-process. Worst-verdict over all statements. |
| `_shell_statements(cmd)` | split a command line into per-statement token lists (`;` `&&` `\|\|` `\|` newline) so a chained `rm` is still seen |
| `_rm_severity(argv)` | recursive `rm`? of a catastrophic target (`/`, `/etc`, …) → DENY; of a scoped path → ASK; non-recursive → None |
| `_push_severity(argv)` | `git push`? force/`+refspec`/`--mirror` or delete/`:refspec`/`--prune` → DENY; plain outward push → ASK |
| `_command_index_after_shell_prefixes` (+ assignment/sudo helpers) | strip `CI=1 sudo -n` etc. so the *real* command underneath is what gets classified |
| `_DENY_PATTERNS` / `_ASK_PATTERNS` | regex net for cases not worth a parser (git reset --hard remote; gh pr merge/release/repo delete; infra apply/deploy/destroy) |

### How the verdict is surfaced (the G3 hand-off)

G5 only *classifies*; **G3's `harness_status` decides what to do with each verdict** (`groups.md` G3 table):
**ALLOW → `None`** (defer to Claude's own consent gate), **DENY → `"deny"`**, **ASK → `"ask"`** (Claude
Code renders its own native permission prompt — council's hook stays non-interactive).

> **VERIFIED (claude 2.1.199 + official hooks docs, 3 Jul 2026):** PreToolUse hook output is
> `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"deny"|"ask"|"defer",
> "permissionDecisionReason": "…"}}`. `"ask"` escalates to the user's own consent UI; `"defer"` explicitly
> falls through to the normal permission flow (the ALLOW→None trick, made explicit). The earlier v1 plan
> ("stray ASK → fail closed") is DEAD — it would have silently denied every `git push`.

## G5 in one breath

`evaluate` is a coarse blast-radius net: reads/tests/edits/local-git **ALLOW**, outward/recoverable
**ASK**, irreversible **DENY** — judged on the actual command string, robust to the flag/sudo/refspec
tricks a single regex misses. It keeps *only* omnigent's `blast_radius` because council has no fleet of
sub-agents to corral, and it stays small because it's a **second layer** over Claude Code's own consent
gate, not a sandbox.

---

# Where to open the source

| group | omnigent files (under `reference/omnigent/`) |
|---|---|
| G1 | `omnigent/cli.py`, `omnigent/chat.py`, `omnigent/repl/_repl.py` |
| G2 | `examples/debby/` (config.yaml, skills/debate/SKILL.md, agents/{claude,gpt}/config.yaml) |
| G3 | `omnigent/claude_native*.py`, `omnigent/native_policy_hook.py`, `omnigent/inner/claude_native_*.py`, `omnigent/runner/pending_approvals.py` |
| G5 | `omnigent/inner/nessie/policies.py` |

Full per-line map: `reference/omnigent/MAPPING.md`.

---

# Concerns (review findings, 2 Jul 2026)

Full-manuscript review verdict: design is sound, but a handful of real implementation traps —
and one genuine mismatch with the stated use case. Worst first.

## C0 — THE BIG ONE: `ask` as written doesn't give back-and-forth  **[RESOLVED 3 Jul 2026 — the /duel redesign]**

The stated use case is interactive adversarial dialogue ("the model and me talk back and forth").
Resolution went FURTHER than the original fix: ask mode is now a **plain Claude chat by default**,
with the codex adversary summoned per-question via a `/duel` toggle (a slash command flipping a
boolean on the renderer — no UI widget). Cheap turns stay cheap (1 subprocess); you pay the
adversarial tax only when a claim is worth stress-testing. The toggle takes effect on the NEXT
turn for free — `handle()` blocks, so a turn in flight always finishes first. The three gaps:

1. ~~The interactive loop is never wired in.~~ **FIXED** — `cli.ask` with no question now enters
   `run_loop(DebateRenderer)`; a given question runs one-shot through the same renderer.
2. ~~No memory between turns.~~ **FIXED** — `_history_preamble` (debate.py) rebuilds the last
   `cfg.history_turns` turns from the ledger each call, scoped to the current session via a
   `session_start` marker, current-turn echo excluded. This preamble is the ONLY memory the codex
   head has — which also makes a mid-conversation `/duel on` context-complete. (v2 upgrade: the
   claude head can use native `claude -p --resume` instead of the paste; codex keeps the preamble.)
3. ~~Critique rounds drop the original question.~~ **FIXED** — `Question:` prepended every round.

## Per-group findings

### G1 — FRONT
- **[RESOLVED]** ~~`cli.py review` imports a `review.py` that doesn't exist → ImportError.~~
  The `review` command is CUT from v1 (cli.py, tables, banner all updated); G6 stays as future work.
- **[RESOLVED]** ~~`--judge` flag_value eats the question.~~ `--judge` now requires an explicit
  value (`--judge moderator`); the ambiguous bare-flag form is gone.
- **[RESOLVED]** ~~`record()` calls `load_config()` per event.~~ `lru_cache`'d `_cfg()` in ledger.py.
- **[RESOLVED]** ~~locked vs unlocked `record()`.~~ Merged — the primary listing IS the locked one.

### G2 — DEBATE
- **[RESOLVED]** ~~`head_timeout: 120` too low.~~ Default now **300 s** in the G1 Config listing.
- **[OPEN — verify live]** `codex exec` stdout is not just the answer (session headers/metadata).
  Verify live; likely need `--json`/last-message handling. Same class as the `--allowedTools ""`
  verification for `claude -p`. (codex CLI installed 3 Jul 2026 — needs auth, then verify.)
- **[OPTIONAL — deferred by design]** Judge reuses proposer/adversary, which prepend `HEAD_PROMPT` —
  judge is told it's one of two debate voices, then given instructions matching neither mode. The judge
  is intentionally off by default and out of v1 scope; if/when it's ever turned on, add a third thin
  function (or system param) without the debate head prompt. Kept here as future reference only.
- `claude -p` in the proposer still loads `~/.claude` → the-harness hooks + global settings apply to
  debate calls. Probably harmless with tools off, but not a clean-room call.

### G3 — WRAP
- **Terminal contention** unaddressed: output pump paints Rich from a daemon thread while main
  thread blocks in `read_input()`. H1 interlock mostly saves it, but permission prompts / late
  deltas can interleave. Plan a Rich `Live` layout with pinned input row; accept v1 jank.
- **[RESOLVED]** ~~`-m council.…` importability (#1 fresh-clone trap).~~ Triple defense in
  `write_hook_settings`: documented `pip install -e .` + `PYTHONPATH` injected into every hook
  command (shlex-quoted) + a launch-time `--check` self-probe that aborts loudly.
- **[OPEN — verify live]** **Verify `MessageDisplay` is a real hook event** the same way
  UserPromptSubmit/Stop/StopFailure were live-verified — it's taken on faith from omnigent. If
  absent, the live-delta channel is silently dead (degraded to transcript finals, not broken —
  but know which world before `render.py`).
- **[RESOLVED]** ~~`SessionState.poll()` bare `json.loads`.~~ try/except + skip + a
  `state_parse_error` ledger row — a torn line can no longer freeze the box forever.
- **[RESOLVED]** ~~`_input_pump` loops forever if Stop never fires.~~ Stall counter: after 2×
  `turn_timeout`, dead pane → clean exit; live pane → unlock with an "interlock degraded" warning.
- **statusLine is single-valued** (doesn't stack like hooks) — council's `--settings` silently
  replaces the user's. The `_chain` param exists in the sketch; don't skip implementing it.

### G4 — PERSISTENCE
- **[RESOLVED]** ~~Lock version + config caching.~~ Both merged into the primary ledger.py listing.
- **`trace()` usage rule (standing, not a bug):** human-triggered, one-shot reads only — the history
  preamble (once per turn), replay, debugging filters. NEVER in a machine-paced loop: it re-parses
  the whole file per call, O(n²) over a session. Polling consumers (live viewer) must use the
  offset-tail pattern that already exists in `SessionState.poll`.

### G5 — POLICY
- **[RESOLVED 3 Jul 2026]** v1 "stray ASK → fail closed (deny)" meant **every `git push` is silently
  denied** — a wall, not a gate. Fixed: ASK now maps to `"permissionDecision": "ask"` (native prompt),
  verified live against claude 2.1.199's hooks docs — see the G5 "How the verdict is surfaced" section.
- Keep `_rm_severity`/`_push_severity` helpers **verbatim** in the port; don't collapse into one regex.

### G6 — REVIEW
- **[RESOLVED]** ~~Pending, internally inconsistent, outside the two-mode goal.~~ Dropped from v1:
  cli command deleted, tables/banner updated, G6 doc section kept as future work.

## Repo-port checklist (before pushing)
- Vendored omnigent under `reference/`: confirm LICENSE permits redistribution, keep NOTICE intact;
  if provenance unclear, ship without `reference/` and cite MAPPING.md line numbers only.
- `pyproject.toml` with `council = council.cli:main`, deps `click` + `rich`; **editable install as a
  documented setup step** (required for the `-m` hooks, per G3).
- Decide fate of `groups.py` (55 KB) — if superseded sketch, don't port; groups.md is the manuscript.
- `.gitignore`: `.DS_Store`, `__pycache__/`; note that `~/.council/` ledger contains full
  conversation text (privacy note for a public repo).
- ~~`Config` dataclass needs `turn_timeout` + `submit_timeout` added~~ **[DONE]** — both (plus
  `history_turns`) are now in the G1 listing.
- codex CLI: installed globally via npm (3 Jul 2026) but **not yet authenticated** — run `codex`
  once to log in before the first `/duel`; then live-verify its `exec` stdout format (G2 open item).

## Priority order  *(updated 3 Jul 2026 — 1–4 are DONE in the manuscript)*
1. ~~Wire interactive loop + conversation memory into `ask`.~~ **DONE** — /duel redesign + preamble.
2. ~~Drop G6/review from cli.~~ **DONE.**
3. ~~H1 hardening + `-m`-importability + `json.loads` guards in G3.~~ **DONE.**
4. ~~ASK → native-ask in G5.~~ **DONE** (schema verified vs claude 2.1.199).

Remaining, in order:
1. **Port the manuscript to real code** (the sketches are now self-consistent).
2. Live-verify the two on-faith items: `MessageDisplay` hook event (G3) + `codex exec` stdout
   format (G2 — codex installed, needs auth first).
3. Implement the statusLine `_chain` param (G3 — statusLine doesn't stack; don't clobber the user's).
4. Terminal contention in code mode: accept v1 jank, plan a Rich Live layout with pinned input row.
5. v2: claude head switches from preamble-paste to native `claude -p --resume` memory.
