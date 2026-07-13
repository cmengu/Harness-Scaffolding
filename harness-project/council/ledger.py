"""council/ledger.py — the ONE persistence seam (write + read).
↔ replaces omnigent stores/conversation_store + repl/_session_log.py (572 lines → ~30)."""
from __future__ import annotations

import json
import os
import re
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
    record(quarantined(head, context.get("kind"), path))
    return path


def save_artifact(head: str, title: str, html: str) -> "os.PathLike":
    """Persist a duel's self-contained HTML artifact under the run (output-contract.md §artifacts):
    `~/.council/artifacts/<run_id>/<slug>-<head>.html`. Same privacy stance as the ledger — the
    file can hold anything a head rendered → dir 0700, file 0600. The `-<head>` suffix keeps the
    two heads from colliding when they slugify to the same name; returns the path for the row."""
    from pathlib import Path
    adir: Path = _cfg().ledger_path.parent / "artifacts" / RUN_ID
    adir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(adir, 0o700)                     # repair a dir born under an older umask
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "artifact"
    path = adir / f"{slug}-{head}.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, html.encode())
    finally:
        os.close(fd)
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
    record(session_start(sid, **extra))          # constructor drops None → bare calls stay bare
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


# ── row vocabulary: constructors + classifiers (expand half of issue #3) ────────────
# Every KIND of ledger row gets one constructor here; every question a reader asks of a
# row gets one classifier. Today these live BESIDE the hand-built dict literals at the
# call sites — nothing is migrated yet, so nothing can break (the contract half, which
# swaps writers/readers over and deletes the literals, is a later ticket). Constructors
# are pure: they RETURN a dict for record() to write, they never write, so a caller stays
#     record(ledger.head_cost("claude", usd=0.02))
# and a row's shape/spelling is defined in exactly one place instead of retyped at every
# writer and re-matched as a magic string at every reader.

def _row(role: str, **fields) -> dict:
    """A row is a role plus fields, with None fields dropped so an omitted optional never
    writes a null (mirrors start_session's bare-row rule)."""
    return {"role": role, **{k: v for k, v in fields.items() if v is not None}}


# -- run / session / conversation lifecycle --
def run_start(mode: str, overrides=None) -> dict:
    return _row("run_start", mode=mode, overrides=list(overrides) if overrides else None)


def session_start(session_id: str, **extra) -> dict:
    return _row("session_start", session_id=session_id, **extra)


def user(text: str) -> dict:
    return _row("user", text=text)


def note(text: str) -> dict:
    return _row("note", text=text)


# -- head calls, retries, errors, cost, session handles --
def head_call(head: str, ok: bool, **fields) -> dict:
    return _row("head_call", head=head, ok=ok, **fields)


def head_retry(head: str, attempt: int, **fields) -> dict:
    return _row("head_retry", head=head, attempt=attempt, **fields)


def head_error(head: str, kind: str, error: str) -> dict:
    return _row("head_error", head=head, kind=kind, error=error)


def head_session(**fields) -> dict:
    return _row("head_session", **fields)


def head_cost(head: str, usd=None, tokens=None) -> dict:
    """Normalize the two heads' spend onto ONE shape: claude reports dollars, codex reports
    a token-usage object. Both fields ride on the cost row (the absent one dropped), so a
    single reader — cost_usd() — sums a mixed run instead of special-casing per head."""
    return _row("head_cost", head=head,
                usd=float(usd) if usd is not None else None, tokens=tokens)


# -- debate flow --
def debate_round(round: int, proposer, adversary=None, **extra) -> dict:
    """A round's answers. **extra carries the optional scratch critiques
    (proposer_critique / adversary_critique) — None values drop, keeping event rows and
    critique-less rounds bare."""
    return _row("debate", round=round, proposer=proposer, adversary=adversary, **extra)


def debate_event(event: str, **fields) -> dict:
    return _row("debate", event=event, **fields)


# -- judge --
def judge(style: str, text: str) -> dict:
    return _row("judge", style=style, text=text)


def judge_keymap(map: dict) -> dict:
    return _row("judge_keymap", map=map)


# -- persistence / failure bookkeeping --
def quarantined(head: str, kind, path) -> dict:
    return _row("quarantined", head=head, kind=kind, path=str(path))


# -- briefing / shadow --
def briefing(choice: str, **fields) -> dict:
    return _row("briefing", choice=choice, **fields)


def run_start_shadow(overrides) -> dict:
    return _row("run_start", mode="shadow", overrides=list(overrides))


def shadow_arm(arm: str, answer, **fields) -> dict:
    return _row("shadow_arm", arm=arm, answer=answer, **fields)


# -- code mode (wrap/) --
def code_session(event: str, **fields) -> dict:
    return _row("code_session", event=event, **fields)


def code_assistant(text: str) -> dict:
    return _row("code_assistant", text=text)


def code_user(text: str) -> dict:
    return _row("code_user", text=text)


def code_tool(name: str, summary) -> dict:
    return _row("code_tool", name=name, summary=summary)


def code_context(**context) -> dict:
    return _row("code_context", **context)


def code_approval(**row) -> dict:
    return _row("code_approval", **row)


def code_permission(answer, **fields) -> dict:
    return _row("code_permission", answer=answer, **fields)


def state_parse_error(line: str) -> dict:
    return _row("state_parse_error", line=line)


def paste_retry(text: str) -> dict:
    return _row("paste_retry", text=text)


def inject_error(text: str, **fields) -> dict:
    return _row("inject_error", text=text, **fields)


def scrape_advisory(text: str, **fields) -> dict:
    return _row("scrape_advisory", text=text, **fields)


# -- NEW kinds for the output-contract + debate-mechanics tickets. Constructors only: no
#    writer emits them yet (they land when those tickets migrate). Identifying fields are
#    fixed; the rest ride as **fields until the consuming ticket firms the shape. --
def trailer(head: str, round: int, *, parsed: "dict | None" = None,
            raw: "str | None" = None) -> dict:
    """The machine-authoritative tail of a contract answer. parsed → validated fields
    (position/confidence/claims/stances/concessions) with contract='parsed'; raw → the
    unvalidated text with contract='unparsed' (the degrade-never-die path)."""
    if parsed is not None:
        return _row("trailer", head=head, round=round, contract="parsed", **parsed)
    return _row("trailer", head=head, round=round, contract="unparsed", raw=raw)


def artifact(head: str, path, title: str) -> dict:
    return _row("artifact", head=head, path=str(path), title=title)


def round0_agreed(**fields) -> dict:
    """Round 0's two openings already agreed — the duel ends without an adversarial round."""
    return _row("round0_agreed", **fields)


def unresolved(round: int, **fields) -> dict:
    """The duel ran its rounds and never converged."""
    return _row("unresolved", round=round, **fields)


def syco_flag(head: str, round: int, **fields) -> dict:
    """Capitulation: a head moved position with no evidenced REFUTE/SUPPORT behind the move."""
    return _row("syco_flag", head=head, round=round, **fields)


def _is(row, role: str) -> bool:
    return isinstance(row, dict) and row.get("role") == role


def is_run_start(row) -> bool: return _is(row, "run_start")
def is_user(row) -> bool: return _is(row, "user")
def is_any_user(row) -> bool: return _is(row, "user") or _is(row, "code_user")
def is_note(row) -> bool: return _is(row, "note")
def is_session_start(row) -> bool: return _is(row, "session_start")
def is_head_call(row) -> bool: return _is(row, "head_call")
def is_head_error(row) -> bool: return _is(row, "head_error")
def is_head_retry(row) -> bool: return _is(row, "head_retry")
def is_cost(row) -> bool: return _is(row, "head_cost")
def is_code_session(row) -> bool: return _is(row, "code_session")
def is_code_context(row) -> bool: return _is(row, "code_context")
def is_code_assistant(row) -> bool: return _is(row, "code_assistant")
def is_code_tool(row) -> bool: return _is(row, "code_tool")
def is_judge(row) -> bool: return _is(row, "judge")
def is_quarantined(row) -> bool: return _is(row, "quarantined")
def is_shadow_arm(row) -> bool: return _is(row, "shadow_arm")


def is_answer(row) -> bool:
    """'Is this row a proposed answer?' — a debate row with a round set and a proposer,
    the check report/chat use to reconstruct the conversation."""
    return _is(row, "debate") and row.get("round") is not None and "proposer" in row


def is_cancelled(row) -> bool:
    return _is(row, "debate") and row.get("event") == "cancelled"


def is_approval(row) -> bool:
    """A code-mode permission that was granted (approved for the session, or auto-allowed)."""
    return isinstance(row, dict) and row.get("event") in ("approved", "auto")


def is_trailer(row) -> bool: return _is(row, "trailer")
def is_artifact(row) -> bool: return _is(row, "artifact")
def is_round0_agreed(row) -> bool: return _is(row, "round0_agreed")
def is_unresolved(row) -> bool: return _is(row, "unresolved")
def is_syco_flag(row) -> bool: return _is(row, "syco_flag")


def cost_usd(row) -> float:
    """The normalized cost reader: dollars from a head_cost row, 0.0 for anything else, so
    `sum(cost_usd(r) for r in rows)` totals a mixed-head run. Both heads now write a usd
    figure (claude billed direct; codex priced from tokens at write time — backends.codex_usd),
    so codex spend is included. Pre-#4 codex rows carry only tokens and read as 0.0."""
    return float(row.get("usd") or 0.0) if is_cost(row) else 0.0
