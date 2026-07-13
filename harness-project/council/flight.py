"""council/flight.py — shared per-head liveness state: the flight panel (backlog 6+9).

Writers: the streaming pump in debate.py (event-grained) and the blocking paths
(_both / solo / judge — coarse: one begin, one done). Reader: chat.py's composer
status line on its 0.5s tick. Module-level dict + lock is honest here: chat.py
enforces ONE turn in flight per process, so there is exactly one panel to describe.

The `beat` timestamp is the same metric backends._run_lines' watchdog kills on —
time since the head last SAID anything — so the human watching the status bar and
the machine holding the axe judge "stuck" by the same clock (decision 11 Jul: idle,
not wall-clock). Blocking paths never beat: their idle == elapsed, which matches
_run's wall-clock timeout exactly.

Context fractions survive turn_over(): how full a head's window is stays true
between turns, and is measured fresh on the next call anyway."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_LOCK = threading.Lock()
_ACTIVE: dict[str, dict] = {}        # head → phase/t0/beat/usd/done, insertion-ordered
_TOKENS: dict[str, int] = {}         # head → prompt tokens of its LAST call (context %)


def begin(head: str, phase: str = "working") -> None:
    with _LOCK:
        now = time.monotonic()
        _ACTIVE[head] = {"phase": phase, "t0": now, "beat": now, "usd": None, "done": False}


def phase(head: str, phase: str) -> None:
    with _LOCK:
        if head in _ACTIVE:
            _ACTIVE[head]["phase"] = phase
            _ACTIVE[head]["beat"] = time.monotonic()


def beat(head: str) -> None:
    with _LOCK:
        if head in _ACTIVE:
            _ACTIVE[head]["beat"] = time.monotonic()


def cost(head: str, usd: float) -> None:
    with _LOCK:
        if head in _ACTIVE:
            _ACTIVE[head]["usd"] = usd


def done(head: str) -> None:
    with _LOCK:
        if head in _ACTIVE:
            _ACTIVE[head]["done"] = True


def turn_over() -> None:
    with _LOCK:
        _ACTIVE.clear()


def context_tokens(head: str, tokens: int) -> None:
    with _LOCK:
        _TOKENS[head] = tokens


def snapshot() -> tuple[list[tuple[str, dict]], dict[str, int]]:
    """(active heads in start order, last-known prompt tokens) — copies, render-safe."""
    with _LOCK:
        return [(h, dict(info)) for h, info in _ACTIVE.items()], dict(_TOKENS)


@contextmanager
def track(head: str, phase_: str = "thinking"):
    """The blocking paths' whole contract in one line: begin → (work) → done."""
    begin(head, phase_)
    try:
        yield
    finally:
        done(head)
