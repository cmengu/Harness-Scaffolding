"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ Debby config.yaml:47-55 (fan-out), :82-97 (present) + skills/debate/SKILL.md:13-56 (round loop).
   ThreadPoolExecutor replaces Debby's inbox; no orchestrator LLM."""
from __future__ import annotations

import difflib
import random
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .backends import Cancelled, HeadSessions, _classify, adversary, kill_inflight, proposer
from .config import Config
from .ledger import chain_rows, quarantine, record


@dataclass
class DebateResult:
    """NOT a bare str — a judge can refuse to pick. Lets callers branch on .escalated."""
    proposer_final: str
    adversary_final: str
    synthesis: str | None = None
    escalated: bool = False
    agree: str | None = None
    differ: str | None = None


def run(question: str, *, rounds: int, judge, cfg: Config, console: Console | None = None,
        sessions: HeadSessions | None = None, seed: str = "") -> DebateResult:
    """Fan to both heads, cross-critique up to N rounds (early-stop on no movement), present, maybe judge.
    `judge`: falsy=off · 'moderator'=neutral merge · 'reasoning'=verdict, may escalate. (bool True → 'moderator'.)
    `sessions` = native head memory (11 Jul): rounds ≥1 send ONLY the other voice's answer —
    a resumed head already remembers the question and its own words. `seed` (history preamble /
    briefing) rides in front of round 0 only; sessionless critiques keep repeating it, because
    a stateless head forgets it between subprocesses."""
    console = console or Console()
    if judge is True:
        judge = "moderator"
    seeded = seed + question
    a, b = _both(seeded, seeded, cfg, console, sessions)                # round 0 (ANSWER mode)
    record({"role": "debate", "round": 0, "proposer": a, "adversary": b})
    for n in range(1, rounds + 1):
        prev_a, prev_b = a, b
        if sessions is not None:
            msg_a = f"The other voice said:\n{prev_b}\n\nCRITIQUE, then update your answer."
            msg_b = f"The other voice said:\n{prev_a}\n\nCRITIQUE, then update your answer."
        else:
            # Question (incl. seed) stays in EVERY round — a stateless head otherwise
            # drifts into critiquing prose style
            msg_a = f"Question:\n{seeded}\n\nYour last answer:\n{prev_a}\n\nThe other voice said:\n{prev_b}\n\nCRITIQUE, then update."
            msg_b = f"Question:\n{seeded}\n\nYour last answer:\n{prev_b}\n\nThe other voice said:\n{prev_a}\n\nCRITIQUE, then update."
        a, b = _both(msg_a, msg_b, cfg, console, sessions)
        record({"role": "debate", "round": n, "proposer": a, "adversary": b})
        if _moved(prev_a, a) < 0.10 and _moved(prev_b, b) < 0.10:        # deterministic early-stop
            record({"role": "debate", "event": "converged", "round": n})
            break
    _present(console, a, b)
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console)
    return result


def _both(msg_a, msg_b, cfg, console, sessions=None):
    """Both heads concurrently with a live 🟠/🔵 status (block-then-present; columns can't
    stream-interleave). ^C here lands in the MAIN thread (this spinner loop) while the heads
    run in workers — kill the subprocesses FIRST (their communicate() unblocks, each worker
    finishes via _safe's Cancelled branch), THEN re-raise; otherwise pool.__exit__ blocks
    forever waiting on workers stuck in communicate()."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            fa = pool.submit(_safe, partial(proposer, session=sessions), msg_a, cfg, "claude", sessions)
            fb = pool.submit(_safe, partial(adversary, session=sessions), msg_b, cfg, "codex", sessions)
            with Live(_status(fa, fb), console=console, refresh_per_second=8) as live:
                while not (fa.done() and fb.done()):
                    wait([fa, fb], timeout=0.15)
                    live.update(_status(fa, fb))
        except KeyboardInterrupt:
            kill_inflight()
            raise
        return fa.result(), fb.result()


def _safe(fn, msg, cfg, label, sessions=None):
    """A panelist's mic cutting out shouldn't kill the panel: one head failing → single-voiced + logged.
    Also the per-call flight recorder: label + try/except both live here, so every head call
    (judge included) gets a head_call row with real seconds — errors time-stamped for free.
    TRANSIENT failures get cfg.head_retries more tries with exponential backoff (heads are
    stateless one-shot subprocesses, so a retry is idempotent by construction); a head that
    stays dead leaves a quarantine postmortem, not just one easy-to-miss ledger row.
    `sessions`: a head that fails for good gets its native session cleared — the next duel
    reseeds it from the ledger instead of resuming into the same wreck."""
    t0 = time.monotonic()
    kind, attempts = "permanent", 0
    try:
        for attempt in range(max(0, cfg.head_retries) + 1):
            attempts = attempt + 1
            try:
                out = fn(msg, cfg)
                if not out.strip():
                    raise ValueError("empty response")
                break
            except Cancelled:               # a ^C is a decision, not a flake — never retried
                raise
            except Exception as e:
                kind = _classify(e)
                if kind == "permanent" or attempt >= cfg.head_retries:
                    raise                   # permanent = retrying is failing slowly; else exhausted
                record({"role": "head_retry", "head": label, "attempt": attempt,
                        "kind": kind, "error": str(e)[:500]})   # rows = retries actually taken
                time.sleep(cfg.retry_base_delay * 2 ** attempt)
        record({"role": "head_call", "head": label, "ok": True, "attempts": attempts,
                "secs": round(time.monotonic() - t0, 2)})
        return out
    except Cancelled:                       # user's ^C, not a failure: no head_error row (replay
        record({"role": "head_call", "head": label, "ok": False, "cancelled": True,
                "secs": round(time.monotonic() - t0, 2)})   # stays clean), /report skips it
        return f"_({label} cancelled)_"
    except Exception as e:
        record({"role": "head_call", "head": label, "ok": False,
                "secs": round(time.monotonic() - t0, 2), "error": str(e)[:500]})
        record({"role": "head_error", "head": label, "kind": kind, "error": str(e)})
        quarantine(label, e, {"kind": kind, "attempts": attempts, "question": msg})
        if sessions is not None:
            sessions.clear(label)
        return f"_({label} unavailable: {e})_"


def _moved(prev, now):  # 0=identical, 1=rewritten. Crude on purpose; never fires at default rounds=1.
    return 1 - difflib.SequenceMatcher(None, prev, now).ratio()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # wall-clock indexed so redraws from other sources don't jitter it


def _status(fa, fb):
    spin = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
    mark = lambda f: "✓" if f.done() else f"{spin} thinking"
    return f"🟠 claude {mark(fa)}    🔵 codex {mark(fb)}    [dim]^C cancels[/]"


def _present(console, a, b):
    """Duel output, width-adaptive: side-by-side only when each voice gets readable prose
    width (≥~52 chars/column at 110 cols); narrower terminals get full-width blocks under
    rule headers — content owns the terminal, not the layout."""
    if console.width >= 110:
        cols = Table.grid(padding=(0, 2))
        cols.add_column()
        cols.add_column()
        cols.add_row(f"[orange1]## 🟠 Claude[/]\n{a}", f"[blue]## 🔵 Codex[/]\n{b}")
        console.print(cols)
    else:
        console.rule("[orange1]🟠 Claude[/]", style="orange1", align="left")
        console.print(a)
        console.rule("[blue]🔵 Codex[/]", style="blue", align="left")
        console.print(b)


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
    with console.status("[dim]⚖ judge weighing…[/]", spinner="dots"):   # 20s+ silent otherwise
        verdict = _safe(judge_fn, f"Question:\n{question}\n\n{blind}\n\n{instruction}", cfg, "judge")
    record({"role": "judge", "style": style, "text": verdict})   # the verdict must survive the
    r.synthesis = verdict                                        # session — /last + replay read it
    r.escalated = (style == "reasoning" and verdict.strip().upper().startswith("ESCALATE"))
    console.print(f"\n[bold]## ⚖ Synthesis[/] ({style})\n{verdict}")
    return r


def _chain_turns() -> tuple[str | None, list[str]]:
    """The active chain flattened to preamble-shaped turn strings (+ its /compact summary).
    Shared by the preamble (slices + caps), /context (measures), /compact (summarizes ALL).
    A question only becomes history once ANSWERED: a user row is held until a debate row
    lands after it — so the current question (recorded before handle() runs) and cancelled
    turns never echo back as fake memory. The `"proposer" in r` guard keeps event rows
    (converged/cancelled markers share role=debate) from injecting empty CLAUDE: turns."""
    summary, rows = chain_rows()
    turns, pending = [], None
    for r in rows:
        if r.get("role") == "user":
            pending = f"USER: {r['text']}"
        elif r.get("role") == "debate" and r.get("round") is not None and "proposer" in r:
            if pending:
                turns.append(pending)
                pending = None
            turns.append(f"CLAUDE: {str(r.get('proposer', ''))[:800]}"
                         + (f"\nCODEX: {str(r['adversary'])[:800]}" if r.get("adversary") else ""))
    return summary, turns


def _history_preamble(cfg: Config) -> str:
    """Ask-mode MEMORY. Heads are stateless subprocesses (`claude -p` / `codex exec` die per call),
    so council rebuilds context every turn from the ledger. This preamble is the ONLY memory the
    codex head has; it also lets a mid-conversation `/duel on` hand codex the whole back-story.
    Scope = the ACTIVE CHAIN (ledger.chain_rows): this session plus whatever /switch·/fork spliced
    in front of it; a /compact summary caps the chain and leads the preamble. Truncated hard —
    each turn ships this to up to 2 heads × N rounds."""
    summary, turns = _chain_turns()
    text = "\n\n".join(turns[-cfg.history_turns * 2:])[-8000:]   # last N turns, ~8k char cap
    if summary:
        text = (f"Summary of the conversation so far (from a /compact):\n{summary.strip()[:4000]}"
                + (f"\n\n{text}" if text else ""))
    return f"Conversation so far (context — do not re-answer old turns):\n{text}\n\n---\n\n" if text else ""


class DebateRenderer:   # the G1 seam: REPLACES chat.py's _DebateRendererSketch
    """The /duel two-way branch. adversarial=False (DEFAULT) → plain claude chat, one subprocess,
    cheap turns. adversarial=True → the full 🟠vs🔵 debate. run_loop's /duel flips the flag live;
    it takes effect next turn (handle blocks, so a turn in flight always finishes first).
    Style from cfg.judge_style (whether/how); family from cfg.heads.judge (who).
    Owns the duel's HeadSessions: minted on the FIRST armed message (seeded once from the
    history preamble — the briefing popup replaces that seed later), carried across armed
    turns, dropped by reset_sessions() (disarm · /new · /switch · /fork)."""

    def __init__(self, cfg: Config, console: Console, adversarial: bool = False):
        self.cfg, self.console, self.adversarial = cfg, console, adversarial
        self.sessions: HeadSessions | None = None

    def reset_sessions(self) -> None:
        self.sessions = None

    def handle(self, user_input: str) -> None:
        if not self.adversarial:                                # SOLO: claude only, with memory
            pre = _history_preamble(self.cfg)
            with self.console.status("[dim]🟠 claude thinking… (^C cancels)[/]", spinner="dots"):
                out = _safe(proposer, pre + user_input, self.cfg, "claude")
            record({"role": "debate", "round": 0, "proposer": out, "adversary": None})
            self.console.print(f"[orange1]## 🟠 Claude[/]\n{out}")
            return
        if self.cfg.head_sessions and self.sessions is None:
            self.sessions = HeadSessions()
        s = self.sessions
        # Seed while UNMINTED, not just on the first call — a ^C-cancelled or failed first
        # duel leaves empty sessions, and the retry must still carry the back-story. Live
        # sessions already hold it; reseeding would double the memory.
        fresh = s is None or (s.claude is None and s.codex is None)
        run(user_input, rounds=self.cfg.rounds,                 # DUEL: the full debate engine
            judge=self.cfg.judge_style, cfg=self.cfg, console=self.console,
            sessions=s, seed=_history_preamble(self.cfg) if fresh else "")
        if fresh and s is not None and (s.claude or s.codex):
            record({"role": "head_session", "claude": s.claude, "codex": s.codex})
