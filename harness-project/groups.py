"""council/groups.py — SKETCH MANUSCRIPT (not an importable module).

WHAT THIS IS
  A single-file collection of the council-harness sketches, organized by GROUP
  (G1, G2, ...). It is a *manuscript of several files-to-be*, NOT a module you
  import: each `# ── FILE: council/<x>.py ──` block is its own future file, so
  imports / `from __future__` repeat ON PURPOSE. Split them into real files when
  you build for real.

CONVENTIONS
  • Sketches only — the real logic lives where the comments point.
  • `↔` annotations cite the omnigent line(s) each block derives from
    (repo: github.com/omnigent-ai/omnigent, Apache-2.0; re-clonable).
  • Groups are labeled G1..G6. New groups APPEND AT THE BOTTOM under their banner
    (see the ▼ anchor at the end of the file).

GROUP INDEX
  G1 — FRONT        config.py · banner.py · cli.py · chat.py          [DONE below]
  G2 — DEBATE       backends.py · debate.py · skills/debate/SKILL.md  [DONE below]
  G3 — WRAP         wrap/{session,bridge,events,render,state,harness_status}.py     [DONE below]
  G4 — PERSISTENCE  ledger.py  (record/trace — the scaling seam)      [DONE below, used by G1]
  G5 — POLICY       policy.py  (blast_radius ALLOW/DENY/ASK)          [DONE below]
  G6 — HARNESS BRIDGE launch.py · scripts/call-reviewer.sh             [pending]
"""

# ════════════════════════════════════════════════════════════════════════════
# G1 — FRONT   (the branded door + the turn-based loop)
#   files: config.py · banner.py · cli.py · chat.py   (+ ledger.py, a G4 file G1 leans on)
# ════════════════════════════════════════════════════════════════════════════


# ─── FILE: council/config.py ─────────────────────────────────────────────────
# Plain English: which model plays each role, where the log lives. That's it.
"""council/config.py — thin config.
↔ omnigent cli.py:384 (_load_effective_config) + onboarding/.../model_catalog/*.json,
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
    claude_command: str = "claude"   # the REAL binary CODE wraps   ↔ cli.py:4158
    codex_command: str = "codex"
    rounds: int = 1                  # debate default               ↔ Debby SKILL.md:14-18
    head_timeout: int = 120          # per-head subprocess timeout, seconds   ↔ G2 backends._run
    judge_style: str | None = None   # interactive-loop judge STYLE: None | 'moderator' | 'reasoning'   ↔ G2 debate._synthesize
    heads: Heads = field(default_factory=Heads)

def load_config() -> Config:
    """Defaults ← ~/.council/config.toml ← env overrides.
    (Omnigent merges global+local+effective across 250 lines; council keeps one file.)"""
    cfg = Config()
    # ... read toml if present; apply COUNCIL_* env overrides (like the-harness's HARNESS_* knobs) ...
    return cfg


# ─── FILE: council/banner.py ─────────────────────────────────────────────────
# This is the only face the user ever sees. Under CODE, Claude Code + the-harness run behind this.
"""council/banner.py — branded startup banner.
↔ omnigent repl/_repl.py:248-512 (_StartupHeader, _render_startup_banner_ansi, _display_cwd)."""
from __future__ import annotations
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from .config import Config

def render_banner(console: Console, cfg: Config, mode: str) -> None:
    """Paint council's skin once, at launch. Same skin for all 3 modes — only the
    subtitle changes — so the user never sees a different UI when CODE swaps in
    the hidden real Claude Code."""
    subtitle = {
        "ask":    f"think · {cfg.heads.proposer} vs {cfg.heads.adversary}"
                  + (f" · judge:{cfg.heads.judge}" if cfg.heads.judge else ""),
        "code":   "code · Claude Code + the-harness  (hidden engine)",
        "review": f"review · {cfg.heads.adversary}",
    }[mode]
    body = Text.assemble(
        (subtitle + "\n", "cyan"),
        (f"cwd  {_display_cwd()}\n", "dim"),
        (f"log  {cfg.ledger_path}", "dim"),
    )
    console.print(Panel(body, title=Text("⚖  COUNCIL", style="bold"), border_style="blue"))

def _display_cwd() -> str:
    """Home-relative cwd.  ↔ _repl.py:280 (_display_cwd)."""
    p = Path.cwd()
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


# ─── FILE: council/ledger.py ─────────────────────────────────────────────────
# NOTE: this is a G4 file — shown inside G1 because all of G1 leans on it.
# The scaling seam: two functions. Nothing else in council ever touches the
# ledger file. Swap these two bodies (local jsonl → shared server) and the rest
# of the codebase doesn't change.
"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30).
Single→multi-user later = swap ONLY these two bodies; skills/debate/wrap/review unchanged."""
from __future__ import annotations
import json, time
from .config import load_config

def record(event: dict) -> None:
    """The only writer. Append one event. (Local jsonl now; POST to a shared
    server the day you get a second user — callers never change.)"""
    row = {"ts": time.time(), **event}
    path = load_config().ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")

def trace(**filters) -> list[dict]:
    """The only reader (resume + the live viewer tail this)."""
    path = load_config().ledger_path
    if not path.exists():
        return []
    rows = (json.loads(l) for l in path.read_text().splitlines() if l.strip())
    return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]


# ─── FILE: council/cli.py ────────────────────────────────────────────────────
# The whole point of the last few turns: thin top, one call at the bottom, per command.
"""council/cli.py — the front door: 3 commands, thin.
↔ omnigent cli.py:1161 (group), :1241 (main), :4090 (claude→code), :6323 (run→ask).
Dropped: _OmnigentCLI shorthand magic (:1134), 21 commands, ~28k lines of plumbing."""
from __future__ import annotations
import sys, click
from rich.console import Console
from .config import load_config
from .banner import render_banner

console = Console()

@click.group()
@click.version_option()
def cli() -> None:
    """council — think, code, and review with a cross-family second opinion."""

@cli.command()
@click.argument("question", required=False)
@click.option("-p", "--prompt", default=None, help="The question to think through.")
@click.option("--rounds", default=None, type=int, help="Debate rounds (default: config).")
@click.option("--judge", is_flag=False, flag_value="moderator", default=None,
              type=click.Choice(["moderator", "reasoning"]),
              help="Optional judge: bare --judge = moderator (neutral merge); --judge reasoning = verdict, may escalate.")
def ask(question, prompt, rounds, judge):
    """THINK — debate a question across families (Claude vs Codex).  ↔ run (cli.py:6323)."""
    cfg = load_config()
    render_banner(console, cfg, "ask")
    q = prompt or question or console.input("[bold blue]›[/] ")
    from .debate import run as debate_run            # G2 seam   ↔ _dispatch_run (cli.py:6475)
    debate_run(q, rounds=rounds or cfg.rounds, judge=judge, cfg=cfg, console=console)  # console=console: G1 delta

@cli.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})  # ↔ :4090
@click.option("--resume", default=None, help="Resume the last coding session.")
@click.option("--command", "claude_command", default=None, help="Claude binary to wrap.")  # ↔ :4158
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)                            # ↔ :4170
def code(resume, claude_command, claude_args):
    """CODE — branded front over the REAL Claude Code (the-harness gate live).  ↔ claude (cli.py:4090)."""
    if sys.platform == "win32":
        raise click.ClickException("council code needs a PTY (macOS/Linux).")  # ↔ :4198
    cfg = load_config()
    render_banner(console, cfg, "code")
    from .wrap.session import run_claude_session     # G3 seam   ↔ run_claude_native (cli.py:4244)
    run_claude_session(
        claude_args=claude_args,
        use_claude_config=True,        # HARDWIRED → loads ~/.claude, so the-harness hooks fire   ↔ :4137
        command=claude_command or cfg.claude_command,
        resume=resume,
        cfg=cfg,
    )

@cli.command()
@click.argument("target", required=False)  # commit range / path; default = staged diff
def review(target):
    """REVIEW — cross-family review via codex (the-harness engine, no Claude).  ↔ codex (cli.py:4256, but headless)."""
    cfg = load_config()
    render_banner(console, cfg, "review")
    from .review import run as review_run            # G6 seam → scripts/call-reviewer.sh
    review_run(target, cfg=cfg)

def main() -> None:
    """Console entry.  ↔ omnigent main (cli.py:1241) minus shorthand/update-check/ad-hoc rejection."""
    cli(standalone_mode=True)


# ─── FILE: council/chat.py ───────────────────────────────────────────────────
# The run_repl shape (omnigent's 8,183-line _repl.py) boiled down to its skeleton.
# The trick: the loop knows nothing about WHICH engine runs — that's the Renderer's
# job. That one seam is what omnigent spent the giant _SessionsChatReplAdapter on;
# council makes it a one-method protocol.
"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent repl/_repl.py:2844 (run_repl) + chat.py:3782 (_run_repl), MINUS the SDK-event
   adapter (_repl.py:1142-2623) — council swaps that whole thing for a Renderer."""
from __future__ import annotations
from typing import Protocol
from rich.console import Console
from .config import Config
from .ledger import record          # every turn lands in the ledger

class Renderer(Protocol):
    """One method. Each mode plugs in its own engine + skin:
       THINK → debate columns (G2) · REVIEW → codex output (G6).
       (CODE is the exception — see note below — it owns its own live loop.)"""
    def handle(self, user_input: str) -> None: ...

def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit. The engine is hidden inside
    `renderer`; this loop is identical for ask and review."""
    while True:
        try:
            text = console.input("[bold blue]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/exit", "/quit", "exit", "quit"):
            break
        if text.startswith("/"):
            _slash(text, console)        # ↔ _cmd registry (_repl.py:4400) — keep only /help, /new
            continue
        if not text:
            continue
        record({"role": "user", "text": text})
        renderer.handle(text)            # engine runs, prints in council's skin, records its own output

def _slash(text: str, console: Console) -> None:
    """A handful of slash commands, not omnigent's dozens."""
    if text == "/help":
        console.print("ask · code · review  |  /new  /exit")
    # /new → start a fresh ledger thread, etc.

# --- example renderer (lives in debate.py, G2; shown here to make the seam concrete) ---
class _DebateRendererSketch:
    def __init__(self, cfg: Config, console: Console): ...
    def handle(self, user_input: str) -> None:
        # run proposer (`claude -p`) + adversary (`codex exec`) concurrently,
        # cross-critique N rounds, print 🟠 / 🔵 columns   ↔ Debby config.yaml:82-97,
        # and record() each turn. No omnigent inbox/SDK events — just two subprocesses.
        ...


# ─── G1 NOTES ────────────────────────────────────────────────────────────────
# ONE HONEST CAVEAT ON THE LOOP
#   The turn-based run_loop above fits ASK and REVIEW cleanly (input → response →
#   render). CODE does NOT — it's a live attached session: wrap/session.py (G3)
#   launches the real `claude` in a PTY and continuously tails its transcript, so
#   it owns its own loop and doesn't return per-turn. So the real shape is:
#     • ask / review → share run_loop + a Renderer.
#     • code         → launches the G3 wrap directly; shares banner + ledger +
#                      config, NOT the loop.
#   The "one unified branded chat for all three" is the v2 aspiration; the clean
#   first cut is shared skin + shared ledger, two loop styles. Pretending one loop
#   covers a live PTY would be a lie.
#
# THE PAYOFF
#   G1 across these files ≈ ~200 lines you write, referencing ~8 spots in
#   omnigent's ~29,000-line front-end (cli.py + chat.py + repl/). Everything else
#   there was server, daemon, multi-vendor onboarding, SDK-event streaming, pickers.


# ════════════════════════════════════════════════════════════════════════════
# G2 — DEBATE   (the two heads + the cross-critique round loop)
#   files: backends.py · debate.py · skills/debate/SKILL.md
#   The one big transform: Debby orchestrates with an LLM brain + an inbox;
#   council orchestrates with a deterministic Python loop + two subprocesses.
#   Same debate ALGORITHM, far simpler executor — that swap deletes most of
#   Debby's 352 lines (config.yaml 148 + SKILL.md 68 + 2× agent config 136).
# ════════════════════════════════════════════════════════════════════════════


# ─── FILE: council/backends.py ───────────────────────────────────────────────
# Plain English: the two panelists. Identical brief, two modes (answer / critique),
# each one is just a CLI we shell out to. No SDK, no agent-spec YAML, no inbox.
"""council/backends.py — the two debate heads as plain functions.
↔ omnigent examples/debby/agents/claude/config.yaml + agents/gpt/config.yaml:
   the ANSWER/CRITIQUE prompt (@41-64 / @49-72) + executor harness (@11-14 / @19-22).
   Drops: YAML agent-spec wrapper, os_env tool registration, blast_radius (→G5)."""
from __future__ import annotations
import subprocess
from .config import Config

# The head contract — IDENTICAL for both heads, two modes.  ↔ both head prompts @41-64 / @49-72
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
    """Claude head — the REAL `claude` CLI, headless, NO tools.  ↔ harness: claude-sdk @11-14.
    Cold relative to CODE's live session: loads project CLAUDE.md but is blind to the live
    conversation (see G2 NOTES — the cold-context asymmetry)."""
    return _run([cfg.claude_command, "-p", "--allowedTools", "", HEAD_PROMPT + "\n\n" + message], cfg)

def adversary(message: str, cfg: Config) -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.  ↔ harness: codex @19-22.
    Why codex (not an openai-agents SDK): omnigent's gpt comment @7-18 — an unpinned model
    silently falls back to the Databricks gateway; `codex exec` has no such fallback."""
    return _run([cfg.codex_command, "exec", "--sandbox", "read-only", HEAD_PROMPT + "\n\n" + message], cfg)

def _run(argv: list[str], cfg: Config) -> str:
    """One subprocess → its stdout. This IS council's whole 'executor' — no omnigent
    inner/executor.py, no SDK, no harness registry. Timeout so a hung head can't wedge the
    debate (the _safe wrapper in debate.py turns a raise here into single-voiced + logged).
    (Exact tools-off flag = verify vs `claude -p --help`; the no-tools POSTURE is the contract.)"""
    return subprocess.run(
        argv, capture_output=True, text=True, check=True, timeout=cfg.head_timeout
    ).stdout.strip()


# ─── FILE: council/debate.py ─────────────────────────────────────────────────
# Plain English: the moderator's procedure, run by Python (not an LLM brain).
# Ask both → have each critique the other → optionally synthesize → return a result.
"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ omnigent examples/debby/config.yaml (fan-out @47-55, present @82-97, stance @106-111)
   + skills/debate/SKILL.md (the round loop @13-56).
   Drops Debby's claude-sdk orchestrator brain (@27-111) and the inbox machinery (@57-80) —
   ThreadPoolExecutor replaces both."""
from __future__ import annotations
import difflib, random
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, wait
from rich.console import Console
from rich.live import Live
from rich.table import Table
from .config import Config
from .backends import proposer, adversary
from .ledger import record


@dataclass
class DebateResult:
    """What a debate returns — NOT a bare str, because a judge can refuse to pick. Lets one-shot /
    loop / bin/council all branch on .escalated, and lets THINK feed CODE later."""
    proposer_final: str
    adversary_final: str
    synthesis: str | None = None      # filled only if a judge ran
    escalated: bool = False           # judge said "neither settled it — human decides"
    agree: str | None = None          # the '## Where they agree' block  ↔ SKILL.md:41-56
    differ: str | None = None         # the '## Where they differ' block


def run(question: str, *, rounds: int, judge, cfg: Config,
        console: Console | None = None) -> DebateResult:
    """Fan a question to both heads, cross-critique up to N rounds (early-stop on no movement),
    present side-by-side, optionally synthesize.  ↔ SKILL.md:13-56.
    `judge`: falsy=off · 'moderator'=neutral merge · 'reasoning'=verdict, may escalate.
    (G1's bool --judge maps True → 'moderator', the safe Debby-sanctioned merge.)"""
    console = console or Console()
    if judge is True:
        judge = "moderator"

    # Round 0 — opening answers, both heads in parallel (ANSWER mode).  ↔ SKILL.md:22-28 + config.yaml:47-55
    a, b = _both(question, question, cfg, console)
    record({"role": "debate", "round": 0, "proposer": a, "adversary": b})

    # Rounds 1..N — each head critiques the OTHER's last answer, never its own.  ↔ SKILL.md:30-39
    for n in range(1, rounds + 1):
        prev_a, prev_b = a, b
        a, b = _both(
            f"Your last answer:\n{prev_a}\n\nThe other voice said:\n{prev_b}\n\nCRITIQUE, then update.",
            f"Your last answer:\n{prev_b}\n\nThe other voice said:\n{prev_a}\n\nCRITIQUE, then update.",
            cfg, console,
        )
        record({"role": "debate", "round": n, "proposer": a, "adversary": b})
        # Early-stop: if neither voice moved, they've converged or deadlocked — stop arguing.
        # Deterministic (difflib), so the LOOP stays Python, not an LLM brain.  ↔ SKILL.md:58-65
        if _moved(prev_a, a) < 0.10 and _moved(prev_b, b) < 0.10:
            record({"role": "debate", "event": "converged", "round": n})
            break

    _present(console, a, b)                                  # the two columns ARE the output  ↔ config.yaml:82-91
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:                                                # optional, de-emphasized — Debby refuses to judge
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console)  # ↔ SKILL.md:41-56
    return result


def _both(msg_a: str, msg_b: str, cfg: Config, console: Console) -> tuple[str, str]:
    """Run both heads concurrently with a live status — replaces Debby's inbox dispatch @57-80.
    Block-then-present (not token-streaming): two columns can't interleave cleanly in a terminal,
    so we just flip 🟠/🔵 '…thinking' → '✓' while the futures run."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_safe, proposer, msg_a, cfg, "claude")
        fb = pool.submit(_safe, adversary, msg_b, cfg, "codex")
        with Live(_status(fa, fb), console=console, refresh_per_second=8) as live:
            while not (fa.done() and fb.done()):
                wait([fa, fb], timeout=0.15)
                live.update(_status(fa, fb))
        return fa.result(), fb.result()


def _safe(fn, msg: str, cfg: Config, label: str) -> str:
    """A panelist's mic cutting out shouldn't kill the panel. One head failing (not installed,
    empty, timed out) degrades to single-voiced + a ledger note.  ↔ SKILL.md:66-68."""
    try:
        out = fn(msg, cfg)
        if not out.strip():
            raise ValueError("empty response")
        return out
    except Exception as e:                     # noqa: BLE001 — we deliberately swallow ANY head failure
        record({"role": "head_error", "head": label, "error": str(e)})
        return f"_({label} unavailable: {e})_"


def _moved(prev: str, now: str) -> float:
    """0 = identical, 1 = totally rewritten. Crude on purpose — a don't-burn-rounds guard, not a
    truth signal (the judge reads the real content). Never fires at the default rounds=1."""
    return 1 - difflib.SequenceMatcher(None, prev, now).ratio()


def _status(fa, fb) -> str:
    """Per-head spinner line for the Live block."""
    mark = lambda f: "✓" if f.done() else "…thinking"
    return f"🟠 claude {mark(fa)}    🔵 codex {mark(fb)}"


def _present(console: Console, a: str, b: str) -> None:
    """Side-by-side, even-handed — attribute every view.  ↔ config.yaml:82-97."""
    cols = Table.grid(padding=(0, 2)); cols.add_column(); cols.add_column()
    cols.add_row(f"[orange1]## 🟠 Claude[/]\n{a}", f"[blue]## 🔵 Codex[/]\n{b}")
    console.print(cols)


def _synthesize(question: str, r: DebateResult, *, style: str, cfg: Config,
                console: Console) -> DebateResult:
    """OPTIONAL judge — real but OFF by default. Two flavors:
       'moderator' — neutral merge: a synthesis of the two, never a new position, never a winner.
                     ↔ Debby's debate-skill synthesis (SKILL.md:41-56), the ONLY judging Debby allows.
       'reasoning' — evidence verdict: may favor one side OR rule 'no consensus → escalate to human'.
                     Council's optional add; voting herds to wrong consensus → judge-not-vote (2510.01499).
    Inputs are BLIND-GRADED: family labels stripped and A/B order shuffled, so the judge can't
    pattern-match 'Claude usually wins'. The real mapping goes to the ledger (audit knows), not the
    judge.  ↔ Karpathy llm-council anonymized cross-review."""
    pair = [("A", r.proposer_final, "claude"), ("B", r.adversary_final, "codex")]
    random.shuffle(pair)                                   # position ≠ family
    record({"role": "judge_keymap", "map": {slot: fam for slot, _, fam in pair}})
    blind = "\n\n".join(f"Answer {slot}:\n{text}" for slot, text, _ in pair)
    judge_fn = proposer if (cfg.heads.judge or "claude") == "claude" else adversary  # rotating family
    instruction = (
        "Merge these into ONE synthesis — do NOT add a new position or pick a winner."
        if style == "moderator" else
        "Weigh the evidence. Give '## Where they agree', '## Where they differ', then a verdict. "
        "If neither is adequately supported, reply starting with the word ESCALATE and say why."
    )
    verdict = _safe(judge_fn, f"Question:\n{question}\n\n{blind}\n\n{instruction}", cfg, "judge")
    r.synthesis = verdict
    r.escalated = (style == "reasoning" and verdict.strip().upper().startswith("ESCALATE"))
    console.print(f"\n[bold]## ⚖ Synthesis[/] ({style})\n{verdict}")
    return r


# --- the G1 seam: this REPLACES the _DebateRendererSketch stub at chat.py:241 ---
class DebateRenderer:                                       # ↔ G1 chat.py Renderer @210-214
    """3-line adapter so the interactive loop and the one-shot `ask` share ONE engine.
    run() is the kitchen; this is the waiter carrying the same order in.
    Style comes from cfg.judge_style (config = whether/how); family from cfg.heads.judge (who)."""
    def __init__(self, cfg: Config, console: Console):
        self.cfg, self.console = cfg, console
    def handle(self, user_input: str) -> None:
        run(user_input, rounds=self.cfg.rounds,
            judge=self.cfg.judge_style, cfg=self.cfg, console=self.console)


# ─── FILE: council/skills/debate/SKILL.md ────────────────────────────────────
# Plain English: the SAME debate procedure, written for an LLM to run instead of Python.
# This is the in-Claude-Code form: a plugin user (or the hidden CODE session) types
# /council-debate and Claude orchestrates the rounds Debby-style. debate.py serves
# `council ask`; this file serves `/council-debate`. Two front-ends, one procedure.
SKILL_MD = r'''
---
name: council-debate
description: Cross-family debate — relay each voice's answer to the other, loop, converge.
---

# Debate

Run a structured debate between two voices (🟠 Claude proposer, 🔵 Codex adversary). Relay each
answer to the other, loop until they stop moving, then present — and only synthesize if asked. You
are the moderator: attribute every view, never merge silently, don't favor the home team.

↔ trimmed from omnigent examples/debby/skills/debate/SKILL.md (68 lines).

## Rounds
Default **1** exchange (answer → cross-critique). More only if the question is genuinely contested.

## Round 0 — opening answers
Dispatch the question to BOTH voices in ANSWER mode. Collect both before showing anything.

## Each round — cross-critique
Give each voice (a) its own last answer and (b) the OTHER's last answer, in CRITIQUE mode: name
what's right, where it's weak/wrong/incomplete, then update. Pass answers as TEXT — never let one
voice impersonate the other. Stop early if neither voice moves.

## Converge (only if asked)
- **moderator** — a neutral synthesis of the two; do NOT add a new position or pick a winner.
- **reasoning** — weigh the evidence; '## Where they agree', '## Where they differ', a verdict; if
  neither is adequately supported, say so and escalate to the human. Judge BLIND (strip the names,
  shuffle order) so family reputation doesn't decide it.

## Notes
- Even-handed: don't favor the home team (Claude). Converge early if there's no movement.
- If a voice returns empty/garbled, say so and proceed single-voiced — don't stall.
'''


# ─── G2 NOTES ────────────────────────────────────────────────────────────────
# CONFIG ADDITIONS (config.py, G1) this group assumes:
#   • head_timeout: int = 120        — _run() timeout so a hung head can't wedge a debate.
#   • judge_style: str | None = None — interactive-loop judge style (off); one-shot uses --judge.
#   • heads.judge stays the FAMILY that runs the judge (who) — separate from judge_style (whether/how).
#
# G1 SEAMS THIS GROUP CLOSES:
#   • chat.py:241 _DebateRendererSketch  → REPLACED by DebateRenderer (the 3-line adapter above).
#   • cli.py:161 `ask` should pass console=console, and its --judge/--no-judge bool should become
#     --judge [moderator|reasoning] (a flag_value choice); run() normalizes bare True → 'moderator'.
#
# THE TOOLS-OFF POSTURE (shrinks G5's job here):
#   THINK heads are "thinking-and-writing responders" — run with tools DISABLED
#   (claude -p --allowedTools "" / codex exec --sandbox read-only). With no tools, blast_radius is a
#   no-op in THINK; the real G5 policy work lives in CODE/REVIEW. (Verify the exact claude flag; the
#   POSTURE — heads can't touch the filesystem — is the contract.)
#
# THE COLD-CONTEXT ASYMMETRY (state it out loud):
#   In CODE the proposer is the LIVE Claude Code session (free repo + conversation context). In THINK
#   the proposer is headless `claude -p`: loads project CLAUDE.md (warm-ish) but is blind to the live
#   conversation. Fine for general questions; feed context via the prompt / --add-dir for repo-specific ones.
#
# THE PAYOFF
#   Thin G2 was ~55 lines and plausible. Deep G2 is ~95 and TRUE: the DebateRenderer (plugs into G1's
#   loop), difflib early-stop + _safe head-guard (the loop survives reality), a DebateResult that can
#   .escalate (the judge can honestly refuse), blind-graded synthesis, and the live-status / cold-context
#   / tools-off truths subprocess.run hid — against Debby's 352 lines across 4 LLM-orchestrated files.
#   Net-new concepts: 3 (the result object, the two judge flavors, tools-off heads); the rest is hardening.


# ════════════════════════════════════════════════════════════════════════════
# G3 — WRAP   (the hard 20%: run the REAL claude HIDDEN, render it in council's skin)
#   files: wrap/{session,bridge,events,render,state,harness_status}.py
#   SOURCE IS NOT DEBBY — it's omnigent's claude_native* family. Debby was G2.
#   The model (read from source, NOT guessed): launch real `claude` in a private tmux
#   pane, inject input with `tmux send-keys`, and learn what it did from THREE
#   out-channels Claude Code feeds us via hooks + a statusLine wrapper:
#     1) the transcript .jsonl   — authoritative items (user / assistant-final / tools)
#     2) message_deltas.jsonl    — LIVE token chunks, written by a MessageDisplay hook
#     3) context.json            — cost / model / context-window, from a statusLine wrapper
#   Plus a PreToolUse hook = the policy gate. The user only ever sees COUNCIL.
#   STRIP MATH: omnigent's whole native subsystem ≈ ~30k lines across ELEVEN coding
#   agents (claude, codex, cursor, antigravity, opencode, hermes, kimi, goose, qwen,
#   kiro, pi) + an MCP tool-relay + the multi-user server/daemon. Council needs the
#   claude_* family ONLY (~15.2k), and within it a single-user subset ≈ ~1,500 lines.
#   This is the ONE group that's a real build, an order of magnitude over G1 (~200) / G2 (~95).
# ════════════════════════════════════════════════════════════════════════════


# ─── FILE: council/wrap/session.py ───────────────────────────────────────────
# Plain English: the conductor. Launch claude hidden, wire our hooks on top of the-harness's,
# then run two pumps forever — one tails its output into our skin, one types our input in.
"""council/wrap/session.py — launch the REAL claude in a hidden tmux pane, attach locally.
↔ omnigent claude_native.run_claude_native @342 (entry) + inner/claude_native_executor (inject)
   + inner/claude_native_harness.py (a FastAPI server-harness — DROPPED WHOLE; council renders local).
   DROPS vs run_claude_native: resume-workspace picker (@577-1010, prompt_toolkit TUI),
   multi-provider config (@1382-1731: bedrock/ucode/provider registry — council hardwires ~/.claude),
   _run_with_local_server / _run_with_remote_server / daemon (@1792-3007 — the server ring),
   cold-resume-from-server-items (@3235-3593). ~4,404 → ~150."""
from __future__ import annotations
import threading
from pathlib import Path
from .bridge import prepare_bridge_dir, launch_claude_in_tmux, inject, write_hook_settings
from .events import read_events
from .render import Renderer
from .state import save_launch_cwd
from ..config import Config

def run_claude_session(*, claude_args, use_claude_config: bool, command: str,
                       resume: str | None, cfg: Config) -> None:
    """The CODE engine. Unlike ask/review (G1 run_loop) this OWNS ITS LOOP — a live
    attached session that doesn't return per-turn (the honest G1 caveat, now cashed in)."""
    bridge = prepare_bridge_dir(cfg)                       # private scratch: tmux.json, *_deltas, context.json
    save_launch_cwd(bridge, Path.cwd(), resume)            # so `--resume` reattaches the right project
    write_hook_settings(bridge)                            # council's hooks → a --settings json   ↔ build_hook_settings @1024
    # use_claude_config HARDWIRED True by the G1 caller → loads ~/.claude so HARNESS's hooks fire;
    # our --settings ADDS council's hooks ON TOP. Multiple hooks per event COEXIST — we don't replace the-harness.
    launch_claude_in_tmux(bridge, command=command, claude_args=claude_args, use_claude_config=use_claude_config)

    # Two concurrent pumps over the one pane (omnigent splits these across processes;
    # solo we just use two threads). The user sees only what the Renderer paints.
    renderer = Renderer(cfg, bridge)
    out = threading.Thread(target=lambda: [renderer.handle(e) for e in read_events(bridge)], daemon=True)
    out.start()
    while out.is_alive():                                  # input pump: council's box → the hidden pane
        try:
            text = renderer.read_input()                  # council's branded prompt, not Claude's
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            inject(bridge, text)                           # ↔ inject_user_message @2347 (lifted; see bridge.py)


# ─── FILE: council/wrap/bridge.py ────────────────────────────────────────────
# Plain English: the tmux plumbing + reading Claude's transcript. The injection code here
# is the single most bug-hardened thing in omnigent — LIFT it, don't rewrite it.
"""council/wrap/bridge.py — tmux pane mechanics + transcript reading + hook-settings writer.
↔ omnigent claude_native_bridge.py (4789 → ~600).
   LIFT NEAR-VERBATIM (hard-won, encodes real Claude-TUI bugs you must not rediscover):
     • inject_user_message @2347-2480 — verified bracketed-paste submit:
         - ~16KB `tmux send-keys` cap  → write to a temp file + `load-buffer` + `paste-buffer -p`
         - a trailing "\\" eats the submit Enter → trailing newline inside the paste absorbs it
         - Claude coalesces rapid stdin → submit is VERIFIED not fire-and-forget: poll capture-pane
           until the draft shows, send Enter, poll until it leaves, re-send Enter if stuck (↔ anthropics/claude-code#52126)
     • tmux helpers @2716-2965 (_run_tmux/_capture_pane/_claude_prompt_rendered/_submit_needle/
       _draft_in_input_box/_wait_for_claude_prompt_ready/_wait_for_tmux_info)
   KEEP: transcript types (ClaudeTranscriptItem @280, ClaudeHookRecord @331) + readers
     (read_transcript_items_from_offset @1778, read_message_deltas_from_offset @504) + build_hook_settings @1024.
   DROP: the MCP tool-relay server (start_tool_relay @2990 / _serve_mcp @3065 / _stdio_jsonrpc_loop @3362 —
     'MCP optional later'), HTTP ingress, display_cost_approval_popup @2602, post_tools_changed @2675 (server)."""
from __future__ import annotations
import json, shlex, subprocess, sys, tempfile
from pathlib import Path
from ..config import Config

def prepare_bridge_dir(cfg: Config) -> Path:
    """A private 0700 scratch dir per session: tmux.json, message_deltas.jsonl, context.json, hook state.
    ↔ prepare_bridge_dir @735 (minus server bridge-id registration)."""
    ...

def launch_claude_in_tmux(bridge: Path, *, command, claude_args, use_claude_config) -> None:
    """`tmux -S <sock> new-session -d <command> <augmented args>` on a PRIVATE socket; write tmux.json
    (socket_path + target) so inject/capture can find the pane. ↔ _launch_claude_terminal @3779 +
    augment_claude_args @1279 (adds --settings <our hooks>, --disallowedTools, etc.), minus the daemon request wrapper."""
    ...

def inject(bridge: Path, text: str) -> None:
    """LIFT inject_user_message @2347 near-verbatim. Council drops ONLY the active-session/request-id
    guard (single user, single pane) — every TUI-race fix stays."""
    ...

def read_transcript_items_from_offset(transcript_path: Path, offset: int):
    """New authoritative items (user/assistant/tool) since a byte offset. ↔ @1778. KEEP."""
    ...

def write_hook_settings(bridge: Path) -> None:
    """Write a Claude `--settings` json registering council's hooks → council's hook modules + bridge dir.
    ↔ build_hook_settings @1024. STACKS with the-harness's ~/.claude hooks; does NOT replace them.
    Events wired: MessageDisplay→render.message_display_hook (live deltas), PreToolUse→harness_status (policy gate),
    Stop/UserPromptSubmit→status events. statusLine.command→state.status_line_wrapper (cost/model)."""
    ...


# ─── FILE: council/wrap/events.py ────────────────────────────────────────────
# Plain English: read the three files Claude leaves us and turn new bytes into render events.
# Nothing here talks to a network — that deletion IS most of the forwarder.
"""council/wrap/events.py — tail the THREE out-channels → yield LOCAL render events.
↔ omnigent claude_native_forwarder.py (4183 → ~200). KEEP the READ half
   (_forward_available_items @2683, _forward_available_deltas @3267, _forward_available_status_events @2376),
   DROP every byte that POSTS to the server: all _post_external_* (@3099-3797), subagent forwarding
   (@877-1452), session rotation on clear/fork (@1822-2146), supervise_forwarder @1721. Council renders
   locally → there is nothing to forward, which is why a 4k-line file collapses to ~200."""
from __future__ import annotations
import json, time
from pathlib import Path
from .bridge import read_transcript_items_from_offset

def read_events(bridge: Path):
    """Generator. Poll the three files past their last offsets; yield events until the pane dies:
       1) transcript .jsonl    → authoritative items (user msg, assistant final, tool call/result)
       2) message_deltas.jsonl → LIVE token chunks {message_id,index,final,delta} → smooth streaming
       3) context.json         → cost / model / context-window (statusLine wrapper)
    Reconcile live deltas against the authoritative final POSITIONALLY (FIFO) — message_id is NOT in
    the transcript (↔ display_hook docstring @32-37). This is the forwarder's read loop, HTTP removed."""
    ...


# ─── FILE: council/wrap/render.py ────────────────────────────────────────────
# Plain English: two halves. A tiny hook that runs INSIDE claude and dumps each token chunk to a
# file; and the part in council's process that reads those and paints our skin.
"""council/wrap/render.py — branded local render + the per-chunk MessageDisplay hook (the writer).
↔ omnigent claude_native_message_display_hook.py (144, LIFT NEAR-WHOLE — stdlib-only per-chunk appender;
   Claude BLOCKS on this hook, so it must import nothing heavy) + the web-UI SSE render path, REPLACED by
   a local Rich Live view (the whole point: the user sees council, never Claude Code's TUI)."""
from __future__ import annotations
import json, os
from rich.console import Console
from rich.live import Live
from ..ledger import record

# --- runs INSIDE the launched claude, as a hook (registered by wrap/bridge.write_hook_settings) ---
def message_display_hook(bridge_dir: str, payload: dict) -> int:
    """Append {message_id,index,final,delta} to message_deltas.jsonl via O_APPEND (atomic short writes).
    LIFT claude_native_message_display_hook verbatim — already minimal, already correct."""
    ...

# --- runs in council's process: consume events.read_events → council's skin ---
class Renderer:
    def __init__(self, cfg, bridge: Path): self.cfg, self.bridge, self.console = cfg, bridge, Console()
    def read_input(self) -> str:
        """council's branded input box (NOT Claude's prompt) → returned to session.py's inject pump."""
        ...
    def handle(self, event) -> None:
        """Stream live deltas into a Rich Live block; commit authoritative items; show cost in the
        status bar; record() each to the ledger. The skin is council's; the engine is the hidden Claude."""
        ...


# ─── FILE: council/wrap/state.py ─────────────────────────────────────────────
# Plain English: remember where we launched (for --resume) and skim cost/model off the status bar.
"""council/wrap/state.py — launch-cwd persistence (resume) + the statusLine cost/model capture.
↔ omnigent claude_native_state.py (279 → ~40: launch-cwd for resume) + claude_native_status.py
   (165, LIFT NEAR-WHOLE). The statusLine hack matters: claude-native emits NO response.completed event,
   so a statusLine wrapper reading Claude's own status stdin is the ONLY place cost/model are exposed."""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path

def save_launch_cwd(bridge: Path, cwd: Path, resume: str | None) -> None:
    """Persist launch cwd so `council code --resume` reattaches the right project. ↔ claude_native_state."""
    ...

# --- runs as Claude Code's statusLine.command wrapper ---
def status_line_wrapper(bridge_dir: str, chain: str | None) -> int:
    """Read Claude's statusLine stdin (context_window, cost.total_cost_usd, model), write those fields
    atomically to context.json for events.py, then exec the user's ORIGINAL statusLine so their bar still
    renders. LIFT claude_native_status.main verbatim (it's already a clean ~50-line wrapper)."""
    ...


# ─── FILE: council/wrap/harness_status.py ──────────────────────────────────────
# Plain English: the bouncer. Claude asks "may I run this tool?"; we answer allow/deny/ask using G5,
# in-process — no server. It stands shoulder-to-shoulder with the-harness's commit bouncer, doesn't shove it aside.
"""council/wrap/harness_status.py — the PreToolUse policy gate, run as a Claude hook.
↔ omnigent claude_native_hook.py (1002 → ~120: the decision modes _main_permission_request @658,
   _main_evaluate_policy @803) + native_policy_hook.py (445 → ~150: hook_payload_to_evaluation_request @91,
   evaluation_response_to_hook_output @171, fail_closed_hook_output @276) + runner/pending_approvals.py
   (207, mostly DROP — a server-side approvals queue).
   THE BIG DROP: post_evaluate_with_retry @319 + _post_hook_with_reattach @565 (POST the decision to the
   omnigent server). Council evaluates IN-PROCESS by calling G5 policy.py — no network round-trip.
   COEXISTENCE: this hook STACKS with the-harness's own PreToolUse commit-gate — Claude runs ALL hooks for an
   event. Council's gate = blast_radius (G5); the-harness's gate = git commits. Same mechanism, different jobs."""
from __future__ import annotations
from ..policy import evaluate            # G5 seam (ALLOW/DENY/ASK on argv-parsed blast_radius)

def pre_tool_use_gate(payload: dict) -> dict:
    """Claude PreToolUse payload → ALLOW/DENY/ASK hook output. Translate payload → eval request →
    G5 evaluate() → hook output. FAIL CLOSED (deny) on any error; a hook must never crash Claude's loop.
    ↔ native_policy_hook hook_payload_to_evaluation_request + evaluation_response_to_hook_output + fail_closed."""
    ...


# ─── G3 NOTES ────────────────────────────────────────────────────────────────
# WHY THIS IS THE HARD 20% (and worth it)
#   G1/G2 were re-expressions (prose → thin Python). G3 is a genuine single-user REWRITE of a
#   multi-user subsystem. But the cut is clean because omnigent's size here is almost entirely:
#     (a) ELEVEN coding agents — council keeps ONLY claude_*  → ~half the subsystem gone;
#     (b) the multi-user SERVER/daemon/remote + every _post_external_* forward → most of what's left;
#     (c) an MCP tool-relay + a prompt_toolkit resume picker + cold-resume-from-server → the rest.
#   What survives is small and SHARP: launch-in-tmux, the verified-paste inject, three file-tailers,
#   a local Rich renderer, two tiny hooks (delta + statusLine), one policy gate.
#
# THE ONE THING TO LIFT, NOT REWRITE
#   inject_user_message + its tmux helpers (bridge.py ↔ @2347-2965). It encodes real Claude-TUI
#   failure modes (16KB send-keys cap, trailing-\\ eating Enter, coalesced-paste submit race). Rewriting
#   it = rediscovering those bugs one outage at a time. Copy it; trim only the request-id guard.
#
# MECHANISM CONFIRMED (supersedes the old "verify option B (stream-json) vs option A (PTY)" hedge)
#   Read from source: omnigent's real path is option A, and it's cleaner than feared because HOOKS do the
#   work, not transcript-scraping. Live text = a MessageDisplay hook → message_deltas.jsonl. Cost/model =
#   a statusLine wrapper → context.json. Authoritative items + tools = the transcript. Council copies this
#   exactly, minus the server. (stream-json `claude -p` stays the THINK path in G2; CODE is this PTY attach.)
#
# HARNESS COEXISTENCE IS STRUCTURAL, NOT A HACK
#   Claude runs every hook registered for an event. `use_claude_config=True` loads ~/.claude (the-harness's
#   commit-gate), and council's `--settings` ADDS its render+policy hooks on top. They stack. That single
#   fact is why council can "hide" Claude Code and still keep the-harness live — exactly the v2 requirement.
#
# THE PAYOFF
#   ~1,500 lines you write, against omnigent's ~15.2k claude-native family (~30k whole native subsystem).
#   The deletions: 10 other agents, the server ring, the MCP relay, the resume picker, cold-resume, and
#   ~3.7k lines of _post_external_* that exist only to feed a web UI council replaces with a local Rich view.


# ▼▼▼ APPEND NEW GROUPS BELOW (G5, G6 ...) — one banner each ▼▼▼


# ════════════════════════════════════════════════════════════════════════════
# G5 — POLICY   (the blast-radius brain behind G3's gate)
#   file: policy.py   (one function the G3 PreToolUse hook calls in-process)
# ════════════════════════════════════════════════════════════════════════════
#
# WHAT G5 IS TRYING TO DO
#   One job: answer "may this tool call run?" with ALLOW / ASK / DENY, judged by
#   BLAST RADIUS (reversibility). G3's harness_status does the translation (Claude
#   hook payload ⇄ normalized V0 event); G5 is the PURE classification in the
#   middle — command string in, verdict out, no I/O, no network.
#       reads / tests / edits / local git    → ALLOW  (the common case flies)
#       git push / gh pr merge / infra deploy → ASK    (human approves first)
#       force-push / rm -rf / reset --hard ref→ DENY   (irreversible, hard stop)
#
# WHY G5 STAYS SMALL — IT'S A SECOND LAYER
#   harness_status maps ALLOW→None, so Claude Code's OWN consent gate still fires
#   underneath. G5 is a coarse net STACKED ON the-harness's commit-gate + Claude's
#   native permissions — not the only thing guarding the disk. So council keeps
#   ONLY blast_radius and deletes the rest of omnigent's policy module.

# ─── FILE: council/policy.py ─────────────────────────────────────────────────
# omnigent's nessie/policies.py ships 4 factories + a YAML registry. THREE exist
# only to corral a FLEET of sub-agents — which council does not have (Claude Code
# IS the only agent; it never fans out through council's gate). They drop whole,
# and the registry/factory indirection drops with them. blast_radius survives —
# it's the one genuinely security-relevant piece — WITH its robustness helpers
# kept verbatim (those helpers ARE the value; a single regex re-introduces the
# bugs their own comments document: split `rm -r -f`, sudo/CI= prefixes, refspecs).
"""council/policy.py — the blast-radius gate (ALLOW / ASK / DENY).  G5.
↔ omnigent inner/nessie/policies.py:346 (blast_radius) + helpers :134–:343.
PUBLIC SURFACE: evaluate(event) — called IN-PROCESS by G3 harness_status (no server, no YAML registry).
DROPPED: spawn_bounds / purpose_guard / worktree_guard (:408–:568, all multi-agent) + POLICY_REGISTRY (:573).
NOT A SECURITY BOUNDARY (omnigent :143): a safety net vs accidental/obvious damage; doesn't model
  subshells/substitution/eval. The real boundary is sandboxing — which council `code` does not add."""
from __future__ import annotations
import re, shlex

_ALLOW = {"result": "ALLOW"}
def _decision(result: str, reason: str) -> dict:
    return {"result": result, "reason": reason}

_DENY_PATTERNS = (re.compile(r"\bgit\b.*\breset\s+--hard\s+\w+/"),)              # hard-reset to a remote ref  ↔ :64
_ASK_PATTERNS = (
    re.compile(r"\bgh\s+(pr\s+merge|release|repo\s+delete)\b"),                  # ↔ :69
    re.compile(r"\b(kubectl|helm|terraform|databricks)\b.*\b(apply|deploy|destroy|delete)\b"),
)

def evaluate(event: dict, *, gate_pushes: bool = True,
             deny_reason: str = "Blocked by the blast-radius policy.") -> dict:
    """The ONE public entry (was blast_radius._evaluate @:368, un-nested from its factory).
    event = a V0 tool_call {'type':'tool_call','data':{'name':…,'arguments':{…}}}, built by G3
    harness_status from Claude's PreToolUse payload. Returns the WORST verdict over all statements."""
    args = _tool_call(event, {"Bash", "bash", "sys_os_shell"})   # both CLIs' shell tool  ↔ :383
    if args is None:
        return _ALLOW
    command = args.get("command")
    if not isinstance(command, str):                              # malformed → nothing to gate  ↔ :390
        return _ALLOW
    statements = _shell_statements(command)                       # split ; && || | newline  ↔ :395
    sev = {s for stmt in statements for s in (_rm_severity(stmt), _push_severity(stmt))}
    if "DENY" in sev or any(p.search(command) for p in _DENY_PATTERNS):
        return _decision("DENY", f"{deny_reason} (irreversible: {command!r})")
    if gate_pushes and ("ASK" in sev or any(p.search(command) for p in _ASK_PATTERNS)):
        return _decision("ASK", f"High-blast-radius command needs approval: {command!r}")
    return _ALLOW

# --- robustness helpers KEPT VERBATIM from omnigent (this is the value, not boilerplate) ---
#   Lift these unchanged — they are why the gate isn't fooled by chaining, sudo, or refspec tricks.
def _tool_call(event, names): ...          # ↔ :39  args dict of a matching tool_call, else None (→ALLOW)
def _shell_statements(command): ...        # ↔ :134 best-effort split into per-statement token lists
def _rm_severity(argv): ...                # ↔ :247 recursive rm? catastrophic target→DENY / scoped→ASK (flag-robust)
def _push_severity(argv): ...              # ↔ :307 git push? force/delete→DENY / outward→ASK (refspec-robust)
def _command_index_after_shell_prefixes(argv): ...   # ↔ :210 strip `CI=1 sudo -n` to reach the real command
# (+ _rm_target_is_catastrophic :164, _skip_shell_assignments :192, _push_short_option_is_destructive :287)


# ─── G5 NOTES ────────────────────────────────────────────────────────────────
# THE FACTORY COLLAPSE
#   omnigent's policies are FACTORIES: a YAML registry instantiates each with
#   factory_params and the runner calls the returned closure. Council has exactly
#   one policy, so there's nothing to discover or parameterize — evaluate() is the
#   closure lifted out, and the single knob (gate_pushes) is a plain keyword.
#   POLICY_REGISTRY (:573) and the unused `config` 2nd arg drop with it.
#
# WHAT THE HELPERS BUY (don't "simplify" them away)
#   _rm_severity / _push_severity exist because a single regex MISSED: split flags
#   (`rm -r -f`, `--recursive`), sudo/env prefixes (`CI=1 sudo -n rm …`), root
#   children (`rm -rf /etc/x`), and force/delete refspecs (`git push origin +main`,
#   `:branch`). Keeping them is the difference between a real gate and theatre.
#
# THE VERDICT HAND-OFF (lives in G3, named here so the seam is whole)
#   G5 only classifies; harness_status (G3) acts: ALLOW→None (defer to Claude's own
#   consent gate), DENY→"deny", stray ASK→fail-closed in v1 (a hook can't cleanly
#   prompt mid-stream). Wiring ASK to Claude Code's native `ask` decision is the
#   one thing to verify against `claude`'s current hook schema.
#
# THE PAYOFF
#   604 → ~120 lines: one factory body un-nested + its helpers, minus three
#   multi-agent policies, the YAML registry, and the runner indirection.
