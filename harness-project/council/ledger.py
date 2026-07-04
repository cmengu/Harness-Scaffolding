"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30)."""
from __future__ import annotations

import json
import os
import threading
import time
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
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The ledger holds FULL conversation text — owner-only, and fchmod (not create-mode)
    # so a ledger born world-readable under an older version gets fixed on the next write.
    with _LOCK:                         # the lock: two simultaneous appends must never interleave
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(row) + "\n").encode())
        finally:
            os.close(fd)


def trace(**filters) -> list[dict]:
    """The only reader (history-replay + the live viewer tail this). NB: this 'resume' means
    council re-reading its OWN ledger — NOT 'code --resume', which is Claude Code's session resume."""
    path = _cfg().ledger_path
    if not path.exists():
        return []
    rows = (json.loads(l) for l in path.read_text().splitlines() if l.strip())
    return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
