"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ Debby config.yaml:47-55 (fan-out), :82-97 (present) + skills/debate/SKILL.md:13-56 (round loop).
   ThreadPoolExecutor replaces Debby's inbox; no orchestrator LLM."""
from __future__ import annotations

import difflib
import queue
import random
import subprocess
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial

from rich.console import Console
from rich.live import Live
from rich.table import Table

from .backends import (Cancelled, HeadSessions, _classify, adversary,
                       adversary_stream, kill_inflight, proposer, proposer_stream)
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


def _is_dead(text: str) -> bool:
    """A head's failure/cancellation marker (the `_(claude unavailable: …)_` convention
    _safe and the tape both emit). Load-bearing: run() must never feed these to the other
    head as 'answers' — live-observed 11 Jul, a dead round 0 produced a round 1 of heads
    earnestly critiquing each other's error messages."""
    t = text.strip()
    return t.startswith("_(") and t.endswith(")_")


def _err_text(e: Exception, cfg: Config) -> str:
    """Human words for a head failure. A TimeoutExpired repr truncates into what looks
    like a broken command (live-observed 11 Jul) — say what actually happened instead."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"⏱ no output for {cfg.head_timeout}s — killed as hung"
    return str(e)


_ANSWER_MARK = "===ANSWER==="
_CRIT_INSTR = (
    "CRITIQUE the other voice first — name what it gets right and where it is weak, wrong or "
    "incomplete (this part may reference them freely; it is scratch work). Then write a line "
    f"containing only {_ANSWER_MARK} and give your best updated answer: complete, self-contained, "
    "incorporating whatever the debate conceded, and NEVER mentioning the other voice or this "
    "debate — it must stand alone.")


def _split_verdict(text: str) -> tuple[str, str]:
    """(critique, standalone answer). A head that ignores the marker forfeits the dim
    register — its whole reply is treated as the answer, never silently dropped."""
    if _ANSWER_MARK in text:
        crit, _, ans = text.partition(_ANSWER_MARK)
        return crit.strip(), (ans.strip() or crit.strip())
    return "", text.strip()


def _duel_depth(cfg: Config) -> dict:
    """The armed profile (locked 10-11 Jul): claude thinks at the configured max, codex at
    /effort or high, both heads may research. Solo turns build their own cheaper dict."""
    return {"thinking": cfg.duel_thinking_tokens, "tools": cfg.duel_tools,
            "effort": cfg.codex_effort or "high"}


def run(question: str, *, rounds: int, judge, cfg: Config, console: Console | None = None,
        sessions: HeadSessions | None = None, seed: str = "",
        depth: dict | None = None, live: bool = True) -> DebateResult:
    """Fan to both heads, cross-critique up to N rounds (early-stop on no movement), present, maybe judge.
    `judge`: falsy=off · 'moderator'=neutral merge · 'reasoning'=verdict, may escalate. (bool True → 'moderator'.)
    `sessions` = native head memory (11 Jul): rounds ≥1 send ONLY the other voice's answer —
    a resumed head already remembers the question and its own words. `seed` (history preamble /
    briefing) rides in front of round 0 only; sessionless critiques keep repeating it, because
    a stateless head forgets it between subprocesses."""
    console = console or Console()
    if judge is True:
        judge = "moderator"
    depth = depth or _duel_depth(cfg)                                   # a debate defaults to armed depth
    both = partial(_stream_both if cfg.stream_tape else _both,
                   cfg=cfg, console=console, sessions=sessions, depth=depth, live=live)
    seeded = seed + question
    a, b = both(seeded, seeded, round_no=0)                             # round 0 (ANSWER mode)
    record({"role": "debate", "round": 0, "proposer": a, "adversary": b})
    dead = [h for h, t in (("claude", a), ("codex", b)) if _is_dead(t)]
    if dead:                                # a dead round 0 ends the debate — critiquing a
        record({"role": "debate", "event": "round0_failed", "dead": dead})   # corpse is noise
        if len(dead) == 2:
            console.print("[red]✗ both heads failed — turn abandoned (nothing was answered)[/]")
        else:
            console.print(f"[yellow]⚠ {dead[0]} failed — single-voiced answer, no debate[/]")
        return DebateResult(proposer_final=a, adversary_final=b, escalated=len(dead) == 2)
    for n in range(1, rounds + 1):
        prev_a, prev_b = a, b
        # One combined critique-and-final call per head per round (decision 11 Jul: 2 calls
        # per head at default rounds=1). The reply carries scratch critique + ===ANSWER===
        # + a standalone answer — any round may end the duel (early-stop), so EVERY round's
        # answer must stand alone.
        if sessions is not None:
            msg_a = f"The other voice said:\n{prev_b}\n\n{_CRIT_INSTR}"
            msg_b = f"The other voice said:\n{prev_a}\n\n{_CRIT_INSTR}"
        else:
            # Question (incl. seed) stays in EVERY round — a stateless head otherwise
            # drifts into critiquing prose style
            msg_a = f"Question:\n{seeded}\n\nYour last answer:\n{prev_a}\n\nThe other voice said:\n{prev_b}\n\n{_CRIT_INSTR}"
            msg_b = f"Question:\n{seeded}\n\nYour last answer:\n{prev_b}\n\nThe other voice said:\n{prev_a}\n\n{_CRIT_INSTR}"
        raw_a, raw_b = both(msg_a, msg_b, round_no=n)
        died = [h for h, t in (("claude", raw_a), ("codex", raw_b)) if _is_dead(t)]
        if died:                             # mid-debate death: keep the last GOOD answers
            record({"role": "debate", "event": "round_failed", "round": n, "dead": died})
            console.print(f"[yellow]⚠ {' and '.join(died)} failed in round {n} — "
                          "keeping the previous round's answers[/]")
            a, b = prev_a, prev_b
            break
        crit_a, a = _split_verdict(raw_a)
        crit_b, b = _split_verdict(raw_b)
        row = {"role": "debate", "round": n, "proposer": a, "adversary": b}
        if crit_a:
            row["proposer_critique"] = crit_a       # the deliverable stays clean; the scratch
        if crit_b:
            row["adversary_critique"] = crit_b      # work survives for replay/audit
        record(row)
        if _moved(prev_a, a) < 0.10 and _moved(prev_b, b) < 0.10:        # deterministic early-stop
            record({"role": "debate", "event": "converged", "round": n})
            break
    if not cfg.stream_tape:
        _present(console, a, b)                          # the tape already showed everything live
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console, live=live)
    return result


def _both(msg_a, msg_b, cfg, console, sessions=None, depth=None, round_no=0, live=True):
    """Both heads concurrently with a live 🟠/🔵 status (block-then-present; columns can't
    stream-interleave). The classic path (stream_tape=false); round_no is the tape's cue and
    is ignored here. ^C here lands in the MAIN thread (this spinner loop) while the heads
    run in workers — kill the subprocesses FIRST (their communicate() unblocks, each worker
    finishes via _safe's Cancelled branch), THEN re-raise; otherwise pool.__exit__ blocks
    forever waiting on workers stuck in communicate()."""
    depth = depth or {}
    pa = partial(proposer, session=sessions,
                 thinking=depth.get("thinking", 0), tools=depth.get("tools", False))
    pb = partial(adversary, session=sessions,
                 effort=depth.get("effort"), tools=depth.get("tools", False))
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            fa = pool.submit(_safe, pa, msg_a, cfg, "claude", sessions)
            fb = pool.submit(_safe, pb, msg_b, cfg, "codex", sessions)
            if live:
                with Live(_status(fa, fb), console=console, refresh_per_second=8) as disp:
                    while not (fa.done() and fb.done()):
                        wait([fa, fb], timeout=0.15)
                        disp.update(_status(fa, fb))
            else:                            # composer owns the screen: no Live, just wait
                while not (fa.done() and fb.done()):
                    wait([fa, fb], timeout=0.15)
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
        friendly = _err_text(e, cfg)
        record({"role": "head_call", "head": label, "ok": False,
                "secs": round(time.monotonic() - t0, 2), "error": friendly[:500]})
        record({"role": "head_error", "head": label, "kind": kind, "error": friendly})
        quarantine(label, e, {"kind": kind, "attempts": attempts, "question": msg})
        if sessions is not None:
            sessions.clear(label)
        return f"_({label} unavailable: {friendly})_"


_HEAD_STYLE = {"claude": ("orange1", "Claude"), "codex": ("blue", "Codex")}


def _glyph(cfg, head):
    return cfg.claude_glyph if head == "claude" else cfg.codex_glyph


def _stream_both(msg_a, msg_b, cfg, console, sessions=None, depth=None, round_no=0, live=True):
    """The tape (step 5): both heads stream CONCURRENTLY into ONE scroll column —
    docker-compose-logs, not side-by-side panes (decision 10 Jul). Thinking/tool/retry
    lines print dim the moment they happen and interleave freely; ANSWER prose buffers
    per head and commits WHOLE in finish order (prose never interleaves). A Rich Live
    line at the bottom tracks per-head phase + seconds + $. Critique rounds open with an
    honestly-labelled rule (⬡ challenges ✳) — the debate reads as the system's thinking,
    never disguised as claude's own. ^C: kill the heads, drain the workers, re-raise —
    same contract as _both."""
    depth = depth or {}
    q: queue.Queue = queue.Queue()
    streams = {
        "claude": partial(proposer_stream, session=sessions,
                          thinking=depth.get("thinking", 0), tools=depth.get("tools", False)),
        "codex": partial(adversary_stream, session=sessions,
                         effort=depth.get("effort"), tools=depth.get("tools", False)),
    }
    def worker(label, fn, msg):
        try:
            for ev in _safe_stream(fn, msg, cfg, label, sessions):
                q.put(ev)
        finally:
            q.put({"head": label, "kind": "_done", "payload": None, "ts": time.time()})
    threads = [threading.Thread(target=worker, args=("claude", streams["claude"], msg_a), daemon=True),
               threading.Thread(target=worker, args=("codex", streams["codex"], msg_b), daemon=True)]
    if round_no:
        console.rule(f"[dim]round {round_no} — {cfg.codex_glyph} and {cfg.claude_glyph} "
                     f"challenge each other[/]", style="dim", align="left")
    t0 = time.monotonic()
    finals: dict[str, str] = {}
    phase = {"claude": "working", "codex": "working"}
    spent = {"claude": "", "codex": ""}
    done: set[str] = set()
    for t in threads:
        t.start()
    def status():
        spin = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
        parts = []
        for head in ("claude", "codex"):
            color, _ = _HEAD_STYLE[head]
            state = "✓" if head in done else f"{spin} {phase[head]}"
            parts.append(f"[{color}]{_glyph(cfg, head)}[/] {state}{spent[head]}")
        return f"{'    '.join(parts)}    [dim]{time.monotonic() - t0:.0f}s · ^C cancels[/]"
    # live=False = the composer owns the bottom of the screen (step 7): events still print
    # as they land, but no Rich Live status line (Live + patch_stdout fight over the tty).
    disp = Live(status(), console=console, refresh_per_second=8, transient=True) if live else None
    update = (lambda: disp.update(status())) if disp else (lambda: None)
    try:
        with disp or nullcontext():
            while len(done) < 2:
                try:
                    ev = q.get(timeout=0.15)
                except queue.Empty:
                    update()
                    continue
                head, kind, payload = ev["head"], ev["kind"], ev["payload"]
                color, name = _HEAD_STYLE.get(head, ("white", head))
                g = _glyph(cfg, head)
                if kind == "_done":
                    done.add(head)
                elif kind == "text":
                    phase[head] = "writing"                     # prose buffers; commits on final
                elif kind == "thinking":
                    phase[head] = "thinking"
                    if isinstance(payload, dict) and payload.get("text"):
                        console.print(f"[dim][{color}]{g}[/] {payload['text'].strip()}[/dim]")
                    elif isinstance(payload, dict) and payload.get("tokens"):
                        console.print(f"[dim][{color}]{g}[/] thought for "
                                      f"{payload['tokens']} tokens (trace hidden headless)[/dim]")
                elif kind == "tool":
                    phase[head] = "researching"
                    console.print(f"[dim][{color}]{g}[/] 🔍 {payload.get('name', '?')}"
                                  f"({str(payload.get('input', ''))[:80]})[/dim]")
                elif kind == "retry":
                    console.print(f"[dim][{color}]{g}[/] ↻ retrying "
                                  f"(attempt {payload.get('attempt')}) — {payload.get('error', '')}[/dim]")
                elif kind == "cost":
                    if isinstance(payload, dict) and payload.get("usd") is not None:
                        spent[head] = f" ${payload['usd']:.2f}"
                elif kind == "final":
                    finals[head] = str(payload)          # raw back to run(), which re-splits
                    crit, ans = _split_verdict(finals[head]) if round_no else ("", finals[head])
                    if crit:                             # scratch work: dim, honestly labelled
                        console.print(f"[dim][{color}]{g}[/] {name} challenges:[/dim]")
                        console.print(f"[dim]{crit}[/dim]")
                    console.rule(f"[{color}]{g} {name}[/]"
                                 + (f"  [dim]round {round_no}[/]" if round_no else ""),
                                 style=color, align="left")
                    console.print(ans)
                elif kind == "error":
                    if isinstance(payload, dict) and payload.get("cancelled"):
                        finals[head] = f"_({head} cancelled)_"
                    else:
                        err = (payload or {}).get("error", "?")
                        finals[head] = f"_({head} unavailable: {err})_"
                        console.print(f"[red]{g} {head} unavailable — {str(err)[:120]}[/red]")
                update()
    except KeyboardInterrupt:
        kill_inflight()                     # workers unblock, _safe_stream yields error, _done lands
        for t in threads:
            t.join(timeout=5)
        raise
    for t in threads:
        t.join()
    return (finals.get("claude") or "_(claude unavailable: empty stream)_",
            finals.get("codex") or "_(codex unavailable: empty stream)_")


def _safe_stream(fn, msg, cfg, label, sessions=None):
    """_safe's contract for the streaming pump (step 4): flight-recorder head_call rows,
    transient retries, quarantine postmortems — event-shaped. A retry restarts the stream
    and announces itself as a `retry` event, but ONLY while nothing user-visible has
    streamed yet (text/final): duplicating half an answer is worse than failing. Terminal
    failure yields one `error` event instead of raising — a dead panelist mid-tape is a
    line on the tape, not a crash (the tape, step 5, renders it single-voiced)."""
    from .backends import _ev
    t0 = time.monotonic()
    visible = False
    for attempt in range(max(0, cfg.head_retries) + 1):
        try:
            for ev in fn(msg, cfg):
                visible = visible or ev["kind"] in ("text", "final")
                yield ev
            record({"role": "head_call", "head": label, "ok": True, "attempts": attempt + 1,
                    "secs": round(time.monotonic() - t0, 2), "stream": True})
            return
        except Cancelled:
            record({"role": "head_call", "head": label, "ok": False, "cancelled": True,
                    "secs": round(time.monotonic() - t0, 2), "stream": True})
            yield _ev(label, "error", {"cancelled": True})
            return
        except Exception as e:
            kind = _classify(e)
            friendly = _err_text(e, cfg)
            if kind == "transient" and attempt < cfg.head_retries and not visible:
                record({"role": "head_retry", "head": label, "attempt": attempt,
                        "kind": kind, "error": friendly[:500]})
                yield _ev(label, "retry", {"attempt": attempt + 1, "error": friendly[:200]})
                time.sleep(cfg.retry_base_delay * 2 ** attempt)
                continue
            record({"role": "head_call", "head": label, "ok": False, "stream": True,
                    "secs": round(time.monotonic() - t0, 2), "error": friendly[:500]})
            record({"role": "head_error", "head": label, "kind": kind, "error": friendly})
            quarantine(label, e, {"kind": kind, "attempts": attempt + 1, "question": msg})
            if sessions is not None:
                sessions.clear(label)
            yield _ev(label, "error", {"kind": kind, "error": friendly[:500]})
            return


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


def _synthesize(question, r, *, style, cfg, console, live=True):
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
    judge_msg = f"Question:\n{question}\n\n{blind}\n\n{instruction}"
    if live:
        with console.status("[dim]⚖ judge weighing…[/]", spinner="dots"):   # 20s+ silent otherwise
            verdict = _safe(judge_fn, judge_msg, cfg, "judge")
    else:
        console.print("[dim]⚖ judge weighing…[/]")
        verdict = _safe(judge_fn, judge_msg, cfg, "judge")
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


def _pending_notes() -> str:
    """Notes (/note, 11 Jul) recorded since the last ANSWERED turn, shaped as facts-from-
    the-boss and prepended to the next message — message-borne, not preamble-borne, so
    live head sessions (which skip the preamble) receive them too. Consumed by answering:
    once a debate row lands after them, they're history, not pending."""
    _, rows = chain_rows()
    last_answer = max((i for i, r in enumerate(rows)
                       if r.get("role") == "debate" and r.get("round") is not None
                       and "proposer" in r), default=-1)
    notes = [r["text"] for r in rows[last_answer + 1:] if r.get("role") == "note"]
    if not notes:
        return ""
    facts = "\n".join(f"- {n}" for n in notes)
    return f"Facts from the user (constraints — treat as given, not suggestions):\n{facts}\n\n"


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
        self.briefing_seed: str | None = None    # set by prepare_briefing, consumed by handle
        self.live_status = True                  # False when the composer stays live (step 7):
                                                 # Rich Live + patch_stdout can't share a screen

    def reset_sessions(self) -> None:
        self.sessions = None
        self.briefing_seed = None

    def _fresh(self) -> bool:
        # Seed while UNMINTED, not just on the first call — a ^C-cancelled or failed first
        # duel leaves empty sessions, and the retry must still carry the back-story. Live
        # sessions already hold it; reseeding would double the memory.
        s = self.sessions
        return s is None or (s.claude is None and s.codex is None)

    def prepare_briefing(self, question: str) -> None:
        """The briefing popup (11 Jul): on the FIRST armed message of a conversation with
        history, claude drafts what codex needs to know; the human confirms A (the draft,
        default) / B (last turns) / C (full transcript) or types their own. MAIN-thread
        only, BETWEEN composer reads — console.input is safe there. Non-interactive
        callers skip this entirely (auto-B: the default preamble). An empty chat has
        nothing to brief — no popup, straight to the duel."""
        if not (self.adversarial and self.cfg.head_sessions
                and (self.sessions is None or self._fresh())):
            return
        pre = _history_preamble(self.cfg)
        if not pre:
            return
        draft_prompt = (f"{pre}A second AI voice is about to join this conversation to debate "
                        "the next question. Write a compact briefing (under 250 words) telling "
                        "it what it needs to know: the topic, key facts and decisions so far, "
                        "and any user constraints. Do NOT answer the question itself.\n\n"
                        f"Next question:\n{question}")
        with self.console.status(f"[dim]{self.cfg.claude_glyph} drafting a briefing for "
                                 f"{self.cfg.codex_glyph}…[/]", spinner="dots"):
            draft = _safe(partial(proposer, thinking=0, tools=False),
                          draft_prompt, self.cfg, "claude")
        self.console.rule(f"[dim]briefing {self.cfg.codex_glyph} codex will receive[/]",
                          style="dim", align="left")
        self.console.print(f"[dim]{draft}[/dim]")
        self.console.print("[bold]A[/] send this briefing (recommended) · [bold]B[/] send the "
                           f"last {self.cfg.history_turns} turns · [bold]C[/] send the full "
                           "transcript · or type your own")
        choice = self.console.input("[bold]briefing ›[/] ").strip()
        low = choice.lower()
        if low in ("", "a"):
            self.briefing_seed = ("Briefing on the conversation so far (written by the other "
                                  f"voice, confirmed by the user):\n{draft}\n\n")
        elif low == "b":
            self.briefing_seed = None                        # the default preamble
        elif low == "c":
            _, turns = _chain_turns()
            self.briefing_seed = ("Full transcript of the conversation so far:\n"
                                  + "\n\n".join(turns) + "\n\n---\n\n")
        else:                                                # free text = the user's own briefing
            self.briefing_seed = f"Briefing from the user:\n{choice}\n\n"
        record({"role": "briefing", "choice": low if low in ("", "a", "b", "c") else "custom",
                "text": (self.briefing_seed or "")[:2000]})

    def handle(self, user_input: str) -> None:
        user_input = _pending_notes() + user_input          # /note facts ride EVERY next message
        if not self.adversarial:                            # SOLO: claude only, with memory
            pre = _history_preamble(self.cfg)
            solo = partial(proposer, thinking=self.cfg.solo_thinking_tokens,
                           tools=self.cfg.solo_tools)       # fast by default; owner may arm
            if self.live_status:
                with self.console.status("[dim]🟠 claude thinking… (^C cancels)[/]", spinner="dots"):
                    out = _safe(solo, pre + user_input, self.cfg, "claude")
            else:
                self.console.print("[dim]🟠 claude thinking… (^C cancels)[/]")
                out = _safe(solo, pre + user_input, self.cfg, "claude")
            record({"role": "debate", "round": 0, "proposer": out, "adversary": None})
            self.console.print(f"[orange1]## 🟠 Claude[/]\n{out}")
            return
        if self.cfg.head_sessions and self.sessions is None:
            self.sessions = HeadSessions()
        s = self.sessions
        fresh = self._fresh()
        seed = (self.briefing_seed if fresh and self.briefing_seed is not None
                else _history_preamble(self.cfg)) if fresh else ""
        self.briefing_seed = None                           # one briefing seeds one session
        run(user_input, rounds=self.cfg.rounds,             # DUEL: the full debate engine
            judge=self.cfg.judge_style, cfg=self.cfg, console=self.console,
            sessions=s, seed=seed, live=self.live_status)
        if fresh and s is not None and (s.claude or s.codex):
            record({"role": "head_session", "claude": s.claude, "codex": s.codex})
