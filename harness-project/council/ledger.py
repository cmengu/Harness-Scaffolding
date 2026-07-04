"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30)."""
from __future__ import annotations

import json
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
