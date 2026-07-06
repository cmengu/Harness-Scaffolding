"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30)."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from functools import lru_cache

from .config import load_config

_LOCK = threading.Lock()   # record() has concurrent callers: code-mode pumps + ask-mode failure path

# One process = one run (a single `council ask`/`code` invocation). Safe as a module global
# because EVERY record() caller lives in the main process — the statusLine hack is a separate
# process but only writes context.json; render.py reads it back and records from here.
RUN_ID = uuid.uuid4().hex[:12]


@lru_cache(maxsize=1)
def _cfg():
    """Config once per process, NOT once per event — code mode records per token chunk."""
    return load_config()


def record(event: dict) -> None:
    """The only writer. Append one event. (Local jsonl now; POST to a shared server the day
    you get a second user — callers never change.)"""
    row = {"ts": time.time(), "run_id": RUN_ID, **event}   # run_id → trace(run_id=…) threads a run
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


def quarantine(head: str, exc: Exception, context: dict) -> "os.PathLike":
    """A readable corpse for a head that stayed dead after retries (further_steps 3c) —
    a flaky-API day should yield a folder of postmortems, not silent gaps. Persistence,
    so it lives here with the same privacy stance as the ledger: the postmortem carries
    full prompt text → dir 0700, file 0600."""
    from pathlib import Path
    qdir: Path = _cfg().ledger_path.parent / "quarantine"
    qdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(qdir, 0o700)                    # repair a dir born under an older umask
    path = qdir / (f"{time.strftime('%Y%m%d-%H%M%S')}-{RUN_ID}-{head}"
                   f"-{uuid.uuid4().hex[:4]}.md")   # suffix: two failures in one second must not overwrite
    path.write_text(f"""# head failure: {head}
run: {RUN_ID}   when: {time.ctime()}   class: {context.get('kind', '?')}   attempts: {context.get('attempts', '?')}

## what was asked (first 500 chars)
{str(context.get('question', ''))[:500]}

## what the error said
{str(exc)[-2000:]}

## what to do
transient → the world was flaky; rerun when the provider recovers.
permanent → the command is wrong; check the argv above against the CLI's --help.
""")
    os.chmod(path, 0o600)
    record({"role": "quarantined", "head": head, "kind": context.get("kind"), "path": str(path)})
    return path


def trace(**filters) -> list[dict]:
    """The only reader (history-replay + the live viewer tail this). NB: this 'resume' means
    council re-reading its OWN ledger — NOT 'code --resume', which is Claude Code's session resume."""
    path = _cfg().ledger_path
    if not path.exists():
        return []
    rows = (json.loads(l) for l in path.read_text().splitlines() if l.strip())
    return [r for r in rows if all(r.get(k) == v for k, v in filters.items())]


# ── the session chain (G2 continuity: /switch · /fork · /compact) ──────────────────
# A "conversation" stays what it always was — the rows between session_start markers,
# anchored on the LAST marker. Continuity is one extra field: a session_start carrying
# resumes=<sid> splices an older session's turns in front of its own. Append-only
# storage never rewrites history; moving around it = appending one pointer row.

def start_session(**extra) -> str:
    """Open a session (memory boundary) and return its id. Extra fields thread the chain:
    resumes=<sid> continues an older session · summary=<text> replaces one (/compact) ·
    title=<str> names a fork. None values are dropped so bare calls stay bare rows."""
    sid = uuid.uuid4().hex[:12]
    record({"role": "session_start", "session_id": sid,
            **{k: v for k, v in extra.items() if v is not None}})
    return sid


def _sid(row: dict) -> str:
    """A session_start row's identity. Rows born before session ids get a stable one
    derived from their timestamp, so old ledgers stay listable and resumable."""
    return row.get("session_id") or f"ts{int(row.get('ts', 0) * 1000)}"


def sessions() -> list[dict]:
    """Every session in file order: {sid, start, rows} — rows run to the next
    session_start regardless of run_id (the file IS the timeline, runs interleave)."""
    all_rows = trace()
    starts = [i for i, r in enumerate(all_rows) if r.get("role") == "session_start"]
    out = []
    for j, i in enumerate(starts):
        end = starts[j + 1] if j + 1 < len(starts) else len(all_rows)
        out.append({"sid": _sid(all_rows[i]), "start": all_rows[i], "rows": all_rows[i + 1:end]})
    return out


def chain_rows() -> tuple[str | None, list[dict]]:
    """The ACTIVE conversation: the last session_start plus its `resumes` ancestry.
    Returns (summary, rows) — rows chronological across the whole chain; summary = the
    text a /compact left at the chain's end (None otherwise). Hop cap + seen-set keep a
    hand-edited cycle from spinning; a missing ancestor just ends the chain early."""
    segs = sessions()
    if not segs:
        return None, []
    by_sid = {s["sid"]: s for s in segs}
    chain, seen = [segs[-1]], {segs[-1]["sid"]}
    while len(chain) < 10:
        nxt = chain[-1]["start"].get("resumes")
        if not nxt or nxt not in by_sid or nxt in seen:
            break
        chain.append(by_sid[nxt])
        seen.add(nxt)
    chain.reverse()                                   # oldest first
    summary = chain[0]["start"].get("summary")        # a /compact row is always a chain END
    return summary, [r for seg in chain for r in seg["rows"]]
