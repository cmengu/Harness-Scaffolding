"""council/debate.py — the THINK orchestrator. A deterministic Python loop, NOT an LLM brain.
↔ Debby config.yaml:47-55 (fan-out), :82-97 (present) + skills/debate/SKILL.md:13-56 (round loop).
   ThreadPoolExecutor replaces Debby's inbox; no orchestrator LLM."""
from __future__ import annotations

import difflib
import queue
import random
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from . import contract as contract_tpl
from . import flight
from .backends import (Cancelled, HeadSessions, _classify, adversary,
                       adversary_stream, kill_inflight, proposer, proposer_stream,
                       trailer_retry)
from .config import Config
from .ledger import (briefing, debate_event, debate_round, head_call, head_error,
                     head_retry, head_session, judge as judge_row, judge_keymap,
                     quarantine, record, round0_agreed, save_artifact, syco_flag,
                     trailer as trailer_row, unresolved)
from . import preamble


@dataclass
class DebateResult:
    """NOT a bare str — a judge can refuse to pick. Lets callers branch on .escalated."""
    proposer_final: str
    adversary_final: str
    synthesis: str | None = None
    escalated: bool = False
    agree: str | None = None
    differ: str | None = None


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


def _contract_pass(head, raw, round_no, sessions, cfg, console):
    """The output contract's in-round machinery: slice the `=== TRAILER ===` block off a head's
    answer, validate it against the schema, and on failure fire ONE corrective retry on the same
    session (native schema flag attached). Still failing → degrade, never die: keep the prose,
    store the trailer raw, mark the ledger row unparsed, dim ⚠ on the tape. Records the trailer
    row via the constructor. Returns (body_without_trailer, parsed_or_None). A dead head has no
    answer to parse — passed through untouched so run()'s dead-reply path still fires on it."""
    if preamble.is_dead(raw):
        return raw, None
    body, raw_trailer = contract_tpl.split_trailer(raw)
    parsed = contract_tpl.parse_trailer(raw_trailer, round_no)
    if parsed is None and sessions is not None and getattr(sessions, head, None):
        record(head_retry(head, 0, kind="trailer"))          # a corrective retry was fired
        parsed = contract_tpl.parse_trailer(
            trailer_retry(head, sessions, cfg, round_no), round_no)
    if parsed is not None:
        record(trailer_row(head, round_no, parsed=parsed))
    else:
        record(trailer_row(head, round_no, raw=raw_trailer or raw))
        console.print(f"[dim]⚠ {head} trailer unparsed — prose kept, stance features skip[/]")
    return body, parsed


def _answer_rule(console, color, g, name, round_no, conf=None) -> None:
    """The header rule an answer commits under in the tape — shared by the contract and legacy
    finals so the round tag and optional confidence suffix live in one place."""
    console.rule(f"[{color}]{g} {name}[/]"
                 + (f"  [dim]round {round_no}[/]" if round_no else "")
                 + (f"  [dim]conf {conf:.2f}[/]" if conf is not None else ""),
                 style=color, align="left")


def _on_tty() -> bool:
    return sys.stdout.isatty()


def _launch(path) -> None:
    """Hand the artifact to the platform browser opener. Seam: tests replace this to assert the
    open fired without spawning a real browser."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass                                 # no opener on this box: the file + row still landed


def _open_artifact(path, cfg) -> None:
    if cfg.artifact_open and _on_tty():      # headless/remote runs (or the off-switch) never open
        _launch(path)


def _emit_artifact(head, body, question, cfg, console) -> None:
    """Final-round only (output-contract.md §artifacts): if a head's ANSWER carries a
    self-contained HTML artifact, save it under the run (0700/0600), record the ledger row
    (path, title, head), and auto-open it on a TTY behind the artifact_open off-switch. A dead
    head or an `ARTIFACT: none` is a no-op."""
    if preamble.is_dead(body):
        return
    secs = contract_tpl.sections(contract_tpl.split_trailer(body)[0])
    html = contract_tpl.artifact_html(secs.get("artifact"))
    if not html:
        return
    title = (secs.get("position") or question or "artifact").splitlines()[0][:60]
    path = save_artifact(head, title, html)          # persists AND records the ledger row
    console.print(f"[dim]🎨 {head} artifact → {path}[/]")
    _open_artifact(path, cfg)


def _heads_agree(ta, tb) -> bool:
    """The two heads committed the same position this round (cross-head convergence). Reads the
    validated trailers, so no positions → no agreement (an unparsed pair never fakes closure)."""
    return contract_tpl.positions_agree(contract_tpl.position_of(ta), contract_tpl.position_of(tb))


def _positions(ta, tb) -> dict:
    """This round's committed positions per head — the prior round's copy feeds the capitulation
    check (did a head move toward its opponent?)."""
    return {"claude": contract_tpl.position_of(ta), "codex": contract_tpl.position_of(tb)}


def _capitulated(pos_now: str, pos_prev: str, opp_prev: str, parsed) -> bool:
    """Capitulation (docs/debate-techniques A6): a head's new position sits closer to its
    opponent's PREVIOUS position than to its own previous one — it moved toward the opponent —
    with no evidenced stance in its trailer to justify the move. Needs all three positions."""
    if not (pos_now and pos_prev and opp_prev):
        return False
    moved_toward = (contract_tpl.position_similarity(pos_now, opp_prev)
                    > contract_tpl.position_similarity(pos_now, pos_prev))
    return moved_toward and not contract_tpl.has_evidenced_stance(parsed)


def _opp_conf(conf_val) -> str:
    """The opponent-confidence line woven into a round-N message so each head prices in how sure
    the other side is (user story 15). Empty when the opponent left no parsed confidence."""
    return ("" if conf_val is None
            else f"The other voice stated confidence {conf_val:.2f} in its position.\n")


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
    briefing) rides in front of round 0 only, PER HEAD: only a head about to mint gets it —
    first turn, or one whose session _safe cleared after a failure (12 Jul: a half-minted
    pair otherwise left the dead head permanently context-less — no resume AND no seed).
    A live head already holds the back-story; reseeding would double its memory.
    Sessionless critiques keep repeating the seed, because a stateless head forgets it
    between subprocesses."""
    console = console or Console()
    if judge is True:
        judge = "moderator"
    depth = depth or _duel_depth(cfg)                                   # a debate defaults to armed depth
    both = partial(_stream_both if cfg.stream_tape else _both,
                   cfg=cfg, console=console, sessions=sessions, depth=depth, live=live)
    seeded = seed + question
    if sessions is not None:
        q_a = (seed if sessions.claude is None else "") + question
        q_b = (seed if sessions.codex is None else "") + question
    else:
        q_a = q_b = seeded
    con0 = contract_tpl.injection(0) if cfg.contract else ""            # round-0 contract, both heads
    a, b = both(q_a, q_b, round_no=0, con_a=con0, con_b=con0)           # round 0 (ANSWER mode)
    conf = {"claude": None, "codex": None}                             # opponent confidence carry
    ta = tb = None                                                     # round trailers (mechanics)
    if cfg.contract:
        a, ta = _contract_pass("claude", a, 0, sessions, cfg, console)
        b, tb = _contract_pass("codex", b, 0, sessions, cfg, console)
        conf = {"claude": contract_tpl.confidence(ta), "codex": contract_tpl.confidence(tb)}
    record(debate_round(0, a, b))
    dead = [h for h, t in (("claude", a), ("codex", b)) if preamble.is_dead(t)]
    if dead:                                # a dead round 0 ends the debate — critiquing a
        record(debate_event("round0_failed", dead=dead))                     # corpse is noise
        if len(dead) == 2:
            console.print("[red]✗ both heads failed — turn abandoned (nothing was answered)[/]")
        else:
            hint = (" (session cleared — it will be re-briefed next turn)"
                    if sessions is not None and getattr(sessions, dead[0]) is None else "")
            console.print(f"[yellow]⚠ {dead[0]} failed — single-voiced answer, no debate{hint}[/]")
        return DebateResult(proposer_final=a, adversary_final=b, escalated=len(dead) == 2)
    # ROUND-0 AGREEMENT ROUTER (docs/debate-techniques A3): both openings already state the same
    # position → skip the critique round entirely. An easy armed turn costs 2 head calls, not 4.
    agreed0 = cfg.contract and _heads_agree(ta, tb)
    if agreed0:
        record(round0_agreed(position=contract_tpl.position_of(ta)))
        console.print("[dim]✓ both heads opened in agreement — critique round skipped[/]")
    prev_pos = _positions(ta, tb)
    # No critique rounds when the router fired. The for-else below still runs on the empty loop,
    # but its `not agreed0` guard keeps an agreed duel from recording a phantom disagreement.
    critique_rounds = range(1, rounds + 1) if not agreed0 else ()
    for n in critique_rounds:
        prev_a, prev_b = a, b
        # One combined critique-and-final call per head per round (decision 11 Jul: 2 calls
        # per head at default rounds=1). The reply carries scratch critique + ===ANSWER===
        # + a standalone answer — any round may end the duel (early-stop), so EVERY round's
        # answer must stand alone.
        # Each head is shown its opponent's stated confidence (claude's opponent = codex).
        oc_a, oc_b = _opp_conf(conf["codex"]), _opp_conf(conf["claude"])
        if sessions is not None:
            msg_a = f"The other voice said:\n{prev_b}\n{oc_a}\n{_CRIT_INSTR}"
            msg_b = f"The other voice said:\n{prev_a}\n{oc_b}\n{_CRIT_INSTR}"
        else:
            # Question (incl. seed) stays in EVERY round — a stateless head otherwise
            # drifts into critiquing prose style
            msg_a = f"Question:\n{seeded}\n\nYour last answer:\n{prev_a}\n\nThe other voice said:\n{prev_b}\n{oc_a}\n{_CRIT_INSTR}"
            msg_b = f"Question:\n{seeded}\n\nYour last answer:\n{prev_b}\n\nThe other voice said:\n{prev_a}\n{oc_b}\n{_CRIT_INSTR}"
        # The contract template is per-round, not per-head (opponent confidence rides the
        # message above); both heads get the same round-N injection.
        con_n = contract_tpl.injection(n, final_round=(n == rounds)) if cfg.contract else ""
        raw_a, raw_b = both(msg_a, msg_b, round_no=n, con_a=con_n, con_b=con_n)
        died = [h for h, t in (("claude", raw_a), ("codex", raw_b)) if preamble.is_dead(t)]
        if died:                             # mid-debate death: keep the last GOOD answers
            record(debate_event("round_failed", round=n, dead=died))
            console.print(f"[yellow]⚠ {' and '.join(died)} failed in round {n} — "
                          "keeping the previous round's answers[/]")
            a, b = prev_a, prev_b
            break
        if cfg.contract:                     # strip + validate trailers BEFORE splitting prose
            raw_a, ta = _contract_pass("claude", raw_a, n, sessions, cfg, console)
            raw_b, tb = _contract_pass("codex", raw_b, n, sessions, cfg, console)
            conf = {"claude": contract_tpl.confidence(ta), "codex": contract_tpl.confidence(tb)}
            # CAPITULATION FLAG (A6): a head that moved toward its opponent with no evidenced stance.
            for h, parsed in (("claude", ta), ("codex", tb)):
                opp = "codex" if h == "claude" else "claude"
                if _capitulated(contract_tpl.position_of(parsed), prev_pos[h], prev_pos[opp], parsed):
                    record(syco_flag(h, n))
                    console.print(f"[dim]⚠ {h} moved toward its opponent without evidence (syco_flag)[/]")
        crit_a, a = _split_verdict(raw_a)
        crit_b, b = _split_verdict(raw_b)
        # the deliverable (proposer/adversary) stays clean; the scratch critiques survive
        # for replay/audit — None drops, so a critique-less round stays bare.
        record(debate_round(n, a, b, proposer_critique=crit_a or None,
                            adversary_critique=crit_b or None))
        # CROSS-HEAD EARLY-STOP (A4): under the contract, stop on genuine cross-head AGREEMENT —
        # char-churn can't tell "both stood firm apart" from "both converged", so it is only the
        # no-contract fallback (no positions to compare). `agreed` also drives the cap check below.
        agreed = _heads_agree(ta, tb) if cfg.contract else False
        stop = agreed if cfg.contract else (_moved(prev_a, a) < 0.10 and _moved(prev_b, b) < 0.10)
        if stop:
            record(debate_event("converged", round=n, cross_head=cfg.contract))
            break
        prev_pos = _positions(ta, tb)
    else:
        # the loop hit the rounds cap without converging: an honest, recorded disagreement so
        # fake closure never masks a live one (a /judge or gate can escalate on it later).
        # (Short-circuits before `agreed` on the empty-loop paths, where it is unset.)
        if not agreed0 and rounds >= 1 and cfg.contract and not agreed:
            record(unresolved(round=rounds))
    if cfg.contract:                     # final round only: save + open any self-contained artifact
        _emit_artifact("claude", a, question, cfg, console)
        _emit_artifact("codex", b, question, cfg, console)
    if not cfg.stream_tape:
        _present(console, a, b, cfg)                     # the tape already showed everything live
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console, live=live)
    return result


def _both(msg_a, msg_b, cfg, console, sessions=None, depth=None, round_no=0, live=True,
          con_a="", con_b=""):
    """Both heads concurrently with a live per-head status (block-then-present; columns can't
    stream-interleave). The classic path (stream_tape=false); round_no is the tape's cue and
    is ignored here. `con_a`/`con_b` = the per-head output contract injected on this call (empty
    when contract is off). ^C here lands in the MAIN thread (this spinner loop) while the heads
    run in workers — kill the subprocesses FIRST (their communicate() unblocks, each worker
    finishes via _safe's Cancelled branch), THEN re-raise; otherwise pool.__exit__ blocks
    forever waiting on workers stuck in communicate()."""
    depth = depth or {}
    pa = partial(proposer, session=sessions,
                 thinking=depth.get("thinking", 0), tools=depth.get("tools", False), contract=con_a)
    pb = partial(adversary, session=sessions,
                 effort=depth.get("effort"), tools=depth.get("tools", False), contract=con_b)
    flight.begin("claude", "thinking")       # coarse: subprocesses give no events until done,
    flight.begin("codex", "thinking")        # so idle == elapsed here (matches _run's wall clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            fa = pool.submit(_safe, pa, msg_a, cfg, "claude", sessions)
            fb = pool.submit(_safe, pb, msg_b, cfg, "codex", sessions)
            if live:
                with Live(_status(fa, fb, cfg), console=console, refresh_per_second=8) as disp:
                    while not (fa.done() and fb.done()):
                        wait([fa, fb], timeout=0.15)
                        _flight_futures(fa, fb)
                        disp.update(_status(fa, fb, cfg))
            else:                            # composer owns the screen: no Live, just wait
                while not (fa.done() and fb.done()):
                    wait([fa, fb], timeout=0.15)
                    _flight_futures(fa, fb)
        except KeyboardInterrupt:
            kill_inflight()
            raise
        return fa.result(), fb.result()


def _flight_futures(fa, fb) -> None:
    if fa.done():
        flight.done("claude")
    if fb.done():
        flight.done("codex")


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
                record(head_retry(label, attempt, kind=kind, error=str(e)[:500]))   # rows = retries taken
                time.sleep(cfg.retry_base_delay * 2 ** attempt)
        record(head_call(label, ok=True, attempts=attempts,
                         secs=round(time.monotonic() - t0, 2)))
        return out
    except Cancelled:                       # user's ^C, not a failure: no head_error row (replay
        record(head_call(label, ok=False, cancelled=True,
                         secs=round(time.monotonic() - t0, 2)))   # stays clean), /report skips it
        return f"_({label} cancelled)_"
    except Exception as e:
        friendly = _err_text(e, cfg)
        record(head_call(label, ok=False,
                         secs=round(time.monotonic() - t0, 2), error=friendly[:500]))
        record(head_error(label, kind=kind, error=friendly))
        quarantine(label, e, {"kind": kind, "attempts": attempts, "question": msg})
        if sessions is not None:
            sessions.clear(label)
        return f"_({label} unavailable: {friendly})_"


_HEAD_STYLE = {"claude": ("orange1", "Claude"), "codex": ("blue", "Codex")}


def _glyph(cfg, head):
    return cfg.claude_glyph if head == "claude" else cfg.codex_glyph


def _stream_both(msg_a, msg_b, cfg, console, sessions=None, depth=None, round_no=0, live=True,
                 con_a="", con_b=""):
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
        "claude": partial(proposer_stream, session=sessions, thinking=depth.get("thinking", 0),
                          tools=depth.get("tools", False), contract=con_a),
        "codex": partial(adversary_stream, session=sessions, effort=depth.get("effort"),
                         tools=depth.get("tools", False), contract=con_b),
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
    flight.begin("claude")                   # the flight panel mirrors phase/spent below —
    flight.begin("codex")                    # same facts, rendered by the composer status line
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
    # Run-grouped boxes (QoL, 11 Jul): consecutive scratch events from ONE head stream
    # inside a colored border, closed the moment the OTHER head interrupts (or its answer
    # lands). Scrollback can't be edited after the fact, so the box is drawn LIVE —
    # header when a run opens, │-edged lines while it lasts, footer when it ends.
    open_run: list[str | None] = [None]

    def close_box() -> None:
        if open_run[0] is None:
            return
        c, _ = _HEAD_STYLE.get(open_run[0], ("white", open_run[0]))
        console.print(f"[dim {c}]╰─[/]")
        open_run[0] = None

    def box_line(head: str, text: str) -> None:
        c, name_ = _HEAD_STYLE.get(head, ("white", head))
        if open_run[0] != head:
            close_box()
            console.print(f"[dim {c}]╭─[/] [{c}]{_glyph(cfg, head)} {name_}[/]")
            open_run[0] = head
        for ln in text.splitlines() or [""]:
            console.print(f"[dim {c}]│[/] [dim]{ln}[/dim]")
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
                flight.beat(head)                               # every event = proof of life
                if kind == "_done":
                    done.add(head)
                    flight.done(head)
                    if open_run[0] == head:  # a head that dies mid-run leaves no open box
                        close_box()
                elif kind == "text":
                    phase[head] = "writing"                     # prose buffers; commits on final
                    flight.phase(head, "writing")
                elif kind == "thinking":
                    phase[head] = "thinking"
                    flight.phase(head, "thinking")
                    # cfg.tape_verbose re-read PER EVENT: Ctrl+T / /tape flips it mid-turn
                    # and the pump honors it immediately (hides the text, never the phase).
                    if not cfg.tape_verbose:
                        pass
                    elif isinstance(payload, dict) and payload.get("text"):
                        box_line(head, payload["text"].strip())
                    elif isinstance(payload, dict) and payload.get("tokens"):
                        box_line(head, f"thought for {payload['tokens']} tokens "
                                       "(trace hidden headless)")
                elif kind == "tool":
                    phase[head] = "researching"
                    flight.phase(head, "researching")
                    if cfg.tape_verbose:
                        box_line(head, f"🔍 {payload.get('name', '?')}"
                                       f"({str(payload.get('input', ''))[:80]})")
                elif kind == "retry":
                    if cfg.tape_verbose:
                        box_line(head, f"↻ retrying (attempt {payload.get('attempt')})"
                                       f" — {payload.get('error', '')}")
                elif kind == "cost":
                    if isinstance(payload, dict) and payload.get("usd") is not None:
                        spent[head] = f" ~${payload['usd']:.2f}"
                        flight.cost(head, float(payload["usd"]))
                elif kind == "final":
                    finals[head] = str(payload)          # raw back to run(), which re-splits
                    fbody, ftrailer = contract_tpl.split_trailer(finals[head])
                    fsecs = contract_tpl.sections(fbody) if cfg.contract else {}
                    if fsecs.get("answer"):
                        # CONTRACT render: DELIBERATION in the thinking register (the dim box),
                        # CLAIMS dim, the standalone ANSWER as the deliverable with overall
                        # confidence on its rule. (⚠ for an unparsed trailer rides the tape from
                        # run()'s _contract_pass, which owns the authoritative post-retry verdict.)
                        delib = fsecs.get("deliberation")
                        if delib and cfg.tape_verbose:
                            box_line(head, f"{name} deliberates:")
                            box_line(head, escape(delib))    # model text: never parse as markup
                        close_box()
                        for ln in (fsecs.get("claims") or "").splitlines():
                            if ln.strip():                   # CLAIMS are literally `[id] …` — escape
                                console.print(f"[dim]  {escape(ln)}[/]")
                        conf = contract_tpl.confidence(contract_tpl.parse_trailer(ftrailer, round_no))
                        _answer_rule(console, color, g, name, round_no, conf)
                        console.print(fsecs["answer"])
                    else:                                # LEGACY (contract off / free-form)
                        crit, ans = _split_verdict(finals[head]) if round_no else ("", finals[head])
                        if crit:                         # scratch work: dim, honestly labelled
                            box_line(head, f"{name} challenges:")
                            box_line(head, crit)
                        close_box()                      # answers stand OUTSIDE the scratch box
                        _answer_rule(console, color, g, name, round_no)
                        console.print(ans)
                elif kind == "error":
                    if isinstance(payload, dict) and payload.get("cancelled"):
                        finals[head] = f"_({head} cancelled)_"
                    else:
                        err = (payload or {}).get("error", "?")
                        finals[head] = f"_({head} unavailable: {err})_"
                        close_box()
                        console.print(f"[red]{g} {head} unavailable — {str(err)[:120]}[/red]")
                update()
    except KeyboardInterrupt:
        kill_inflight()                     # workers unblock, _safe_stream yields error, _done lands
        close_box()
        for t in threads:
            t.join(timeout=5)
        raise
    close_box()
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
            record(head_call(label, ok=True, attempts=attempt + 1,
                             secs=round(time.monotonic() - t0, 2), stream=True))
            return
        except Cancelled:
            record(head_call(label, ok=False, cancelled=True,
                             secs=round(time.monotonic() - t0, 2), stream=True))
            yield _ev(label, "error", {"cancelled": True})
            return
        except Exception as e:
            kind = _classify(e)
            friendly = _err_text(e, cfg)
            if kind == "transient" and attempt < cfg.head_retries and not visible:
                record(head_retry(label, attempt, kind=kind, error=friendly[:500]))
                yield _ev(label, "retry", {"attempt": attempt + 1, "error": friendly[:200]})
                time.sleep(cfg.retry_base_delay * 2 ** attempt)
                continue
            record(head_call(label, ok=False, stream=True,
                             secs=round(time.monotonic() - t0, 2), error=friendly[:500]))
            record(head_error(label, kind=kind, error=friendly))
            quarantine(label, e, {"kind": kind, "attempts": attempt + 1, "question": msg})
            if sessions is not None:
                sessions.clear(label)
            yield _ev(label, "error", {"kind": kind, "error": friendly[:500]})
            return


def _moved(prev, now):  # 0=identical, 1=rewritten. Crude on purpose; never fires at default rounds=1.
    return 1 - difflib.SequenceMatcher(None, prev, now).ratio()


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # wall-clock indexed so redraws from other sources don't jitter it


def _status(fa, fb, cfg):
    spin = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
    mark = lambda f: "✓" if f.done() else f"{spin} thinking"
    return (f"[orange1]{cfg.claude_glyph}[/] claude {mark(fa)}    "
            f"[blue]{cfg.codex_glyph}[/] codex {mark(fb)}    [dim]^C cancels[/]")


def _present(console, a, b, cfg):
    """Duel output, width-adaptive: side-by-side only when each voice gets readable prose
    width (≥~52 chars/column at 110 cols); narrower terminals get full-width blocks under
    rule headers — content owns the terminal, not the layout. This is a DELIVERABLE surface
    (block-path present, /report answer views, /last): it shows the standalone ANSWER only, so
    a contract answer's DELIBERATION/CLAIMS/trailer never leak in. Free-form answers pass
    through unchanged (answer_of returns them whole)."""
    a, b = contract_tpl.answer_of(a), contract_tpl.answer_of(b)
    ga, gb = cfg.claude_glyph, cfg.codex_glyph
    if console.width >= 110:
        cols = Table.grid(padding=(0, 2))
        cols.add_column()
        cols.add_column()
        cols.add_row(f"[orange1]## {ga} Claude[/]\n{a}", f"[blue]## {gb} Codex[/]\n{b}")
        console.print(cols)
    else:
        console.rule(f"[orange1]{ga} Claude[/]", style="orange1", align="left")
        console.print(a)
        console.rule(f"[blue]{gb} Codex[/]", style="blue", align="left")
        console.print(b)


def _synthesize(question, r, *, style, cfg, console, live=True):
    """OPTIONAL judge, OFF by default. 'moderator'=neutral merge (Debby's only allowed judging);
    'reasoning'=evidence verdict, may ESCALATE. Inputs BLIND-GRADED (labels stripped, A/B shuffled)."""
    pair = [("A", r.proposer_final, "claude"), ("B", r.adversary_final, "codex")]
    random.shuffle(pair)
    record(judge_keymap({slot: fam for slot, _, fam in pair}))
    blind = "\n\n".join(f"Answer {slot}:\n{text}" for slot, text, _ in pair)
    judge_fn = proposer if (cfg.heads.judge or "claude") == "claude" else adversary
    instruction = ("Merge these into ONE synthesis — do NOT add a new position or pick a winner."
                   if style == "moderator" else
                   "Weigh the evidence. Give '## Where they agree', '## Where they differ', then a verdict. "
                   "If neither is adequately supported, reply starting with the word ESCALATE and say why.")
    judge_msg = f"Question:\n{question}\n\n{blind}\n\n{instruction}"
    with flight.track("judge", "weighing"):
        if live:
            with console.status("[dim]⚖ judge weighing…[/]", spinner="dots"):   # 20s+ silent otherwise
                verdict = _safe(judge_fn, judge_msg, cfg, "judge")
        else:
            console.print("[dim]⚖ judge weighing…[/]")
            verdict = _safe(judge_fn, judge_msg, cfg, "judge")
    record(judge_row(style, verdict))                            # the verdict must survive the
    r.synthesis = verdict                                        # session — /last + replay read it
    r.escalated = (style == "reasoning" and verdict.strip().upper().startswith("ESCALATE"))
    console.print(f"\n[bold]## ⚖ Synthesis[/] ({style})\n{verdict}")
    return r


class DebateRenderer:   # the G1 seam: REPLACES chat.py's _DebateRendererSketch
    """The /duel two-way branch. adversarial=False (DEFAULT) → plain claude chat, one subprocess,
    cheap turns. adversarial=True → the full ✳-vs-⬡ debate. run_loop's /duel flips the flag live;
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
        pre = preamble.preamble(self.cfg)
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
        picked = self._pick_briefing()               # arrow-key picker on a TTY (11 Jul ask)
        if picked == "custom":
            choice = self.console.input("[bold]your briefing ›[/] ").strip()   # "" falls
            low = choice.lower()                     # through to A below, same as classic
        elif picked is not None:
            choice = low = picked
        else:                                        # no TTY / no pt: the classic typed path
            self.console.print("[bold]A[/] send this briefing (recommended) · [bold]B[/] send "
                               f"the last {self.cfg.history_turns} turns · [bold]C[/] send the "
                               "full transcript · or type your own")
            choice = self.console.input("[bold]briefing ›[/] ").strip()
            low = choice.lower()
        if low in ("", "a"):
            self.briefing_seed = ("Briefing on the conversation so far (written by the other "
                                  f"voice, confirmed by the user):\n{draft}\n\n")
        elif low == "b":
            self.briefing_seed = None                        # the default preamble
        elif low == "c":
            _, turns = preamble.turns()
            self.briefing_seed = ("Full transcript of the conversation so far:\n"
                                  + "\n\n".join(turns) + "\n\n---\n\n")
        else:                                                # free text = the user's own briefing
            self.briefing_seed = f"Briefing from the user:\n{choice}\n\n"
        record(briefing(low if low in ("", "a", "b", "c") else "custom",
                        text=(self.briefing_seed or "")[:2000]))

    def _pick_briefing(self) -> str | None:
        """Seam for tests/headless: returns 'a'/'b'/'c'/'custom' via the arrow picker,
        or None when there's no TTY/pt — the caller falls back to typed input."""
        if not sys.stdin.isatty():
            return None
        try:
            from .composer import show_picker
        except ImportError:
            return None
        options = [("A", "send this briefing (recommended)"),
                   ("B", f"send the last {self.cfg.history_turns} turns"),
                   ("C", "send the full transcript"),
                   ("D", "type your own briefing")]
        got = show_picker(options, accent=self.cfg.accent_color, title="briefing ›")
        return {0: "a", 1: "b", 2: "c", 3: "custom", None: "a"}[got]   # Esc/^C = default A

    def handle(self, user_input: str) -> None:
        user_input = preamble.notes() + user_input          # /note facts ride EVERY next message
        if not self.adversarial:                            # SOLO: claude only, with memory
            pre = preamble.preamble(self.cfg)
            solo = partial(proposer, thinking=self.cfg.solo_thinking_tokens,
                           tools=self.cfg.solo_tools)       # fast by default; owner may arm
            g = self.cfg.claude_glyph
            with flight.track("claude"):
                if self.live_status:
                    with self.console.status(f"[dim]{g} claude thinking… (^C cancels)[/]",
                                             spinner="dots"):
                        out = _safe(solo, pre + user_input, self.cfg, "claude")
                else:
                    self.console.print(f"[dim]{g} claude thinking… (^C cancels)[/]")
                    out = _safe(solo, pre + user_input, self.cfg, "claude")
            record(debate_round(0, out, None))
            self.console.print(f"[orange1]## {g} Claude[/]\n{out}")
            return
        if self.cfg.head_sessions and self.sessions is None:
            self.sessions = HeadSessions()
        s = self.sessions
        fresh = self._fresh()
        # Reseed whenever ANY head is about to mint — not only when both are (12 Jul):
        # a failed head gets its session cleared by _safe, and without this it resumed
        # nothing AND got no preamble — a permanent cold start. run() applies the seed
        # per head, so the live head never receives it twice.
        reseed = s is None or s.claude is None or s.codex is None
        seed = (self.briefing_seed if fresh and self.briefing_seed is not None
                else preamble.preamble(self.cfg)) if reseed else ""
        self.briefing_seed = None                           # one briefing seeds one session
        pre_ids = (s.claude, s.codex) if s is not None else None
        run(user_input, rounds=self.cfg.rounds,             # DUEL: the full debate engine
            judge=self.cfg.judge_style, cfg=self.cfg, console=self.console,
            sessions=s, seed=seed, live=self.live_status)
        if s is not None and (s.claude or s.codex) and (s.claude, s.codex) != pre_ids:
            record(head_session(claude=s.claude, codex=s.codex))
