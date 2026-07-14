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


def _contract_pass(head, raw, round_no, sessions, cfg, renderer):
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
        renderer.notice(f"[dim]⚠ {head} trailer unparsed — prose kept, stance features skip[/]")
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


def _emit_artifact(head, body, question, cfg, renderer) -> None:
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
    renderer.notice(f"[dim]🎨 {head} artifact → {path}[/]")
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
        depth: dict | None = None, live: bool = True, renderer=None) -> DebateResult:
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
    # Renderers are guests: the engine emits events, a renderer paints. Tape when armed for the
    # terminal; a Quiet renderer (records events, paints nothing) for tests, block mode, and
    # shadow arms — where run() presents at the end instead of streaming.
    renderer = renderer or (TapeRenderer(cfg, console, live) if cfg.stream_tape
                            else QuietRenderer(console))
    fan = partial(_run_round, cfg=cfg, sessions=sessions, depth=depth, renderer=renderer)
    seeded = seed + question
    if sessions is not None:
        q_a = (seed if sessions.claude is None else "") + question
        q_b = (seed if sessions.codex is None else "") + question
    else:
        q_a = q_b = seeded
    con0 = contract_tpl.injection(0) if cfg.contract else ""            # round-0 contract, both heads
    renderer.round_start(0)
    a, b = fan(q_a, q_b, con_a=con0, con_b=con0)                        # round 0 (ANSWER mode)
    conf = {"claude": None, "codex": None}                             # opponent confidence carry
    ta = tb = None                                                     # round trailers (mechanics)
    if cfg.contract:
        a, ta = _contract_pass("claude", a, 0, sessions, cfg, renderer)
        b, tb = _contract_pass("codex", b, 0, sessions, cfg, renderer)
        conf = {"claude": contract_tpl.confidence(ta), "codex": contract_tpl.confidence(tb)}
    record(debate_round(0, a, b))
    dead = [h for h, t in (("claude", a), ("codex", b)) if preamble.is_dead(t)]
    if dead:                                # a dead round 0 ends the debate — critiquing a
        record(debate_event("round0_failed", dead=dead))                     # corpse is noise
        if len(dead) == 2:
            renderer.notice("[red]✗ both heads failed — turn abandoned (nothing was answered)[/]")
        else:
            hint = (" (session cleared — it will be re-briefed next turn)"
                    if sessions is not None and getattr(sessions, dead[0]) is None else "")
            renderer.notice(f"[yellow]⚠ {dead[0]} failed — single-voiced answer, no debate{hint}[/]")
        return DebateResult(proposer_final=a, adversary_final=b, escalated=len(dead) == 2)
    # ROUND-0 AGREEMENT ROUTER (docs/debate-techniques A3): both openings already state the same
    # position → skip the critique round entirely. An easy armed turn costs 2 head calls, not 4.
    agreed0 = cfg.contract and _heads_agree(ta, tb)
    if agreed0:
        record(round0_agreed(position=contract_tpl.position_of(ta)))
        renderer.notice("[dim]✓ both heads opened in agreement — critique round skipped[/]")
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
        renderer.round_start(n)
        raw_a, raw_b = fan(msg_a, msg_b, con_a=con_n, con_b=con_n)
        died = [h for h, t in (("claude", raw_a), ("codex", raw_b)) if preamble.is_dead(t)]
        if died:                             # mid-debate death: keep the last GOOD answers
            record(debate_event("round_failed", round=n, dead=died))
            renderer.notice(f"[yellow]⚠ {' and '.join(died)} failed in round {n} — "
                            "keeping the previous round's answers[/]")
            a, b = prev_a, prev_b
            break
        if cfg.contract:                     # strip + validate trailers BEFORE splitting prose
            raw_a, ta = _contract_pass("claude", raw_a, n, sessions, cfg, renderer)
            raw_b, tb = _contract_pass("codex", raw_b, n, sessions, cfg, renderer)
            conf = {"claude": contract_tpl.confidence(ta), "codex": contract_tpl.confidence(tb)}
            # CAPITULATION FLAG (A6): a head that moved toward its opponent with no evidenced stance.
            for h, parsed in (("claude", ta), ("codex", tb)):
                opp = "codex" if h == "claude" else "claude"
                if _capitulated(contract_tpl.position_of(parsed), prev_pos[h], prev_pos[opp], parsed):
                    record(syco_flag(h, n))
                    renderer.notice(f"[dim]⚠ {h} moved toward its opponent without evidence (syco_flag)[/]")
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
        _emit_artifact("claude", a, question, cfg, renderer)
        _emit_artifact("codex", b, question, cfg, renderer)
    if not cfg.stream_tape:
        _present(console, a, b, cfg)                     # the tape already showed everything live
    result = DebateResult(proposer_final=a, adversary_final=b)
    if judge:
        result = _synthesize(question, result, style=judge, cfg=cfg, console=console, live=live)
    return result


class QuietRenderer:
    """Records the engine's event stream and paints nothing — the TEST subscriber (assert events,
    not printed text) and the block/shadow path (present-at-end, no live tape). Its `events` list
    is the whole duel: `round_start` markers plus every {head, kind, payload, ts} dict."""

    def __init__(self, console: Console | None = None):
        # console=None → tests (notices are events only). A console → the block/production path
        # (no streamed tape, but engine notices still reach the user; run() presents at the end).
        self.events: list[dict] = []
        self.console = console

    def round_start(self, round_no: int) -> None:
        self.events.append({"kind": "round_start", "round": round_no})

    def live_context(self):
        return nullcontext()

    def handle(self, ev: dict) -> None:
        self.events.append(ev)

    def notice(self, text: str) -> None:
        self.events.append({"kind": "notice", "text": text})
        if self.console is not None:
            self.console.print(text)

    def tick(self) -> None:
        pass

    def finish(self) -> None:
        pass

    def kinds(self, head: str | None = None) -> list[str]:
        """The event kinds seen (optionally for one head) — the shape most engine tests assert."""
        return [e["kind"] for e in self.events
                if head is None or e.get("head") == head]


class TapeRenderer:
    """Paints the live interleaved tape (docker-compose-logs, not panes — decision 10 Jul). A
    SUBSCRIBER to the engine's event stream: ALL terminal painting for a duel lives here, none in
    the run loop. Thinking/tool/retry lines print dim as they land and interleave freely; ANSWER
    prose commits WHOLE in finish order. A transient Rich Live line tracks per-head phase/secs/$."""

    def __init__(self, cfg: Config, console: Console, live: bool = True):
        self.cfg, self.console, self.live = cfg, console, live
        self.round_no = 0
        self.phase = {"claude": "working", "codex": "working"}
        self.spent = {"claude": "", "codex": ""}
        self.done: set[str] = set()
        self.open_run: str | None = None
        self.t0 = time.monotonic()
        self._disp = None

    def round_start(self, round_no: int) -> None:
        self.round_no = round_no
        self.phase = {"claude": "working", "codex": "working"}
        self.spent = {"claude": "", "codex": ""}   # per-round, like the old fan-out's call-local dict
        self.done = set()
        self.t0 = time.monotonic()
        if round_no:            # critique rounds open with an honestly-labelled rule
            self.console.rule(f"[dim]round {round_no} — {self.cfg.codex_glyph} and "
                              f"{self.cfg.claude_glyph} challenge each other[/]",
                              style="dim", align="left")

    def _status(self) -> str:
        spin = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
        parts = []
        for head in ("claude", "codex"):
            color, _ = _HEAD_STYLE[head]
            state = "✓" if head in self.done else f"{spin} {self.phase[head]}"
            parts.append(f"[{color}]{_glyph(self.cfg, head)}[/] {state}{self.spent[head]}")
        return f"{'    '.join(parts)}    [dim]{time.monotonic() - self.t0:.0f}s · ^C cancels[/]"

    def live_context(self):
        # live=False = the composer owns the bottom of the screen (Live + patch_stdout fight over
        # the tty): events still print as they land, just no Rich Live status line.
        self._disp = (Live(self._status(), console=self.console, refresh_per_second=8,
                           transient=True) if self.live else None)
        return self._disp or nullcontext()

    def tick(self) -> None:
        if self._disp:
            self._disp.update(self._status())

    def _close_box(self) -> None:
        if self.open_run is None:
            return
        c, _ = _HEAD_STYLE.get(self.open_run, ("white", self.open_run))
        self.console.print(f"[dim {c}]╰─[/]")
        self.open_run = None

    def _box_line(self, head: str, text: str) -> None:
        c, name_ = _HEAD_STYLE.get(head, ("white", head))
        if self.open_run != head:
            self._close_box()
            self.console.print(f"[dim {c}]╭─[/] [{c}]{_glyph(self.cfg, head)} {name_}[/]")
            self.open_run = head
        for ln in text.splitlines() or [""]:
            self.console.print(f"[dim {c}]│[/] [dim]{ln}[/dim]")

    def finish(self) -> None:
        self._close_box()

    def notice(self, text: str) -> None:
        """An engine status line (dead head, router skip, syco flag…) — painted between rounds
        when no box is open, so it never lands inside the dim scratch border."""
        self._close_box()
        self.console.print(text)

    def handle(self, ev: dict) -> None:
        cfg, console = self.cfg, self.console
        head, kind, payload = ev["head"], ev["kind"], ev["payload"]
        color, name = _HEAD_STYLE.get(head, ("white", head))
        g = _glyph(cfg, head)
        if kind == "_done":
            self.done.add(head)
            if self.open_run == head:       # a head that dies mid-run leaves no open box
                self._close_box()
        elif kind == "text":
            self.phase[head] = "writing"    # prose buffers; commits on final
        elif kind == "thinking":
            self.phase[head] = "thinking"
            if not cfg.tape_verbose:         # Ctrl+T / /tape flips it live; honored per event
                pass
            elif isinstance(payload, dict) and payload.get("text"):
                self._box_line(head, escape(payload["text"].strip()))
            elif isinstance(payload, dict) and payload.get("tokens"):
                self._box_line(head, f"thought for {payload['tokens']} tokens "
                                     "(trace hidden headless)")
        elif kind == "tool":
            self.phase[head] = "researching"
            if cfg.tape_verbose:
                self._box_line(head, f"🔍 {payload.get('name', '?')}"
                                     f"({str(payload.get('input', ''))[:80]})")
        elif kind == "retry":
            if cfg.tape_verbose:
                self._box_line(head, f"↻ retrying (attempt {payload.get('attempt')})"
                                     f" — {payload.get('error', '')}")
        elif kind == "cost":
            if isinstance(payload, dict) and payload.get("usd") is not None:
                self.spent[head] = f" ~${payload['usd']:.2f}"
        elif kind == "final":
            self._render_final(head, str(payload), color, name, g)
        elif kind == "error":
            if not (isinstance(payload, dict) and payload.get("cancelled")):
                self._close_box()
                console.print(f"[red]{g} {head} unavailable — "
                              f"{str((payload or {}).get('error', '?'))[:120]}[/red]")

    def _render_final(self, head, raw, color, name, g) -> None:
        cfg, console, round_no = self.cfg, self.console, self.round_no
        fbody, ftrailer = contract_tpl.split_trailer(raw)
        fsecs = contract_tpl.sections(fbody) if cfg.contract else {}
        if fsecs.get("answer"):
            delib = fsecs.get("deliberation")           # DELIBERATION → the thinking register
            if delib and cfg.tape_verbose:
                self._box_line(head, f"{name} deliberates:")
                self._box_line(head, escape(delib))
            self._close_box()
            for ln in (fsecs.get("claims") or "").splitlines():   # CLAIMS dim (literal [id])
                if ln.strip():
                    console.print(f"[dim]  {escape(ln)}[/]")
            conf = contract_tpl.confidence(contract_tpl.parse_trailer(ftrailer, round_no))
            _answer_rule(console, color, g, name, round_no, conf)
            console.print(fsecs["answer"])
        else:                                            # LEGACY (contract off / free-form)
            crit, ans = _split_verdict(raw) if round_no else ("", raw)
            if crit:
                self._box_line(head, f"{name} challenges:")
                self._box_line(head, crit)
            self._close_box()
            _answer_rule(console, color, g, name, round_no)
            console.print(ans)


def _run_round(msg_a, msg_b, *, cfg, sessions, depth, con_a, con_b, renderer):
    """The ONE fan-out (step 5): both heads' streaming backends run concurrently into a queue;
    every event updates flight telemetry and is handed to the renderer; the finals come back. No
    painting here — renderers are guests. ^C lands in the MAIN thread (this loop) while the heads
    run in workers: kill the subprocesses FIRST (their reads unblock, each worker yields an error
    and a _done), let the renderer close out, THEN re-raise."""
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
    finals: dict[str, str] = {}
    done: set[str] = set()
    flight.begin("claude")
    flight.begin("codex")
    for t in threads:
        t.start()
    try:
        with renderer.live_context():
            while len(done) < 2:
                try:
                    ev = q.get(timeout=0.15)
                except queue.Empty:
                    renderer.tick()
                    continue
                head, kind, payload = ev["head"], ev["kind"], ev["payload"]
                flight.beat(head)                        # every event = proof of life
                if kind == "_done":
                    done.add(head)
                    flight.done(head)
                elif kind == "final":
                    finals[head] = str(payload)          # engine keeps the text; renderer paints it
                elif kind == "error":
                    p = payload or {}
                    finals[head] = (f"_({head} cancelled)_" if p.get("cancelled")
                                    else f"_({head} unavailable: {p.get('error', '?')})_")
                elif kind == "text":
                    flight.phase(head, "writing")
                elif kind == "thinking":
                    flight.phase(head, "thinking")
                elif kind == "tool":
                    flight.phase(head, "researching")
                elif kind == "cost" and isinstance(payload, dict) and payload.get("usd") is not None:
                    flight.cost(head, float(payload["usd"]))
                renderer.handle(ev)
                renderer.tick()
    except KeyboardInterrupt:
        kill_inflight()                     # workers unblock, _safe_stream yields error, _done lands
        renderer.finish()
        for t in threads:
            t.join(timeout=5)
        raise
    renderer.finish()
    for t in threads:
        t.join()
    return (finals.get("claude") or "_(claude unavailable: empty stream)_",
            finals.get("codex") or "_(codex unavailable: empty stream)_")


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

def present_columns(console, cells: list[str]) -> None:
    """The ONE side-by-side grid — shared by the duel present and shadow's arm compare (step 5
    dedup). The caller owns the wide/narrow gate; this just lays out the cells it is handed."""
    grid = Table.grid(padding=(0, 2))
    for _ in cells:
        grid.add_column()
    grid.add_row(*cells)
    console.print(grid)


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
        present_columns(console, [f"[orange1]## {ga} Claude[/]\n{a}", f"[blue]## {gb} Codex[/]\n{b}"])
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

    def needs_briefing(self) -> bool:
        """Whether a fresh briefing applies this turn: an armed duel with head-session memory
        whose sessions haven't been briefed yet. The REPL's briefing popup asks this instead of
        reaching into private state — the renderer owns the answer."""
        return bool(self.adversarial and self.cfg.head_sessions
                    and (self.sessions is None or self._fresh()))

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
