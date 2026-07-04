"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ Debby config.yaml:47-55 (fan-out), :82-97 (present) + skills/debate/SKILL.md:13-56 (round loop).
   ThreadPoolExecutor replaces Debby's inbox; no orchestrator LLM."""
from __future__ import annotations

import difflib
import random
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .backends import adversary, proposer
from .config import Config
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
            record({"role": "debate", "event": "converged", "round": n})
            break
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
                wait([fa, fb], timeout=0.15)
                live.update(_status(fa, fb))
        return fa.result(), fb.result()


def _safe(fn, msg, cfg, label):
    """A panelist's mic cutting out shouldn't kill the panel: one head failing → single-voiced + logged."""
    try:
        out = fn(msg, cfg)
        if not out.strip():
            raise ValueError("empty response")
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
    cols = Table.grid(padding=(0, 2))
    cols.add_column()
    cols.add_column()
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
