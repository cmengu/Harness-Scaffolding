"""council/preamble.py — the ONE owner of ask-mode conversation recap.

Heads are stateless subprocesses (`claude -p` / `codex exec` die per call), so council
rebuilds the back-story every turn from the ledger. That recap logic — chain flattening,
the clip window, note queueing, the /compact summary lead — used to be scattered across
debate.py and chat.py with the clip window and the dead-marker check each written twice.
It lives here now, behind three functions: turns() · notes() · preamble(). chat and debate
are callers. Depends only on ledger + config (never on debate/chat), so there is no cycle.
"""
from __future__ import annotations

from .config import Config
from .ledger import chain_rows, is_answer, is_note, is_user

VOICE_CHARS = 800       # per-voice answer clip inside a flattened turn
WINDOW_CHARS = 8000     # hard cap on the whole turn window shipped to a head
SUMMARY_CHARS = 4000    # cap on a /compact summary when it leads the preamble


def is_dead(text: str) -> bool:
    """A head's failure/cancellation marker (the `_(claude unavailable: …)_` convention
    _safe and the tape both emit). Load-bearing: run() must never feed these to the other
    head as 'answers' — live-observed 11 Jul, a dead round 0 produced a round 1 of heads
    earnestly critiquing each other's error messages. /compact reuses it to never bake a
    failed summary into memory."""
    t = text.strip()
    return t.startswith("_(") and t.endswith(")_")


def turns() -> tuple[str | None, list[str]]:
    """The active chain flattened to preamble-shaped turn strings (+ its /compact summary).
    Shared by preamble() (slices + caps), /context (measures), /compact (summarizes ALL).
    A question only becomes history once ANSWERED: a user row is held until a debate row
    lands after it — so the current question (recorded before handle() runs) and cancelled
    turns never echo back as fake memory. is_answer keeps event rows (converged/cancelled
    markers share role=debate) from injecting empty CLAUDE: turns."""
    summary, rows = chain_rows()
    out, pending = [], None
    for r in rows:
        if is_user(r):
            pending = f"USER: {r['text']}"
        elif is_answer(r):
            if pending:
                out.append(pending)
                pending = None
            out.append(f"CLAUDE: {str(r.get('proposer', ''))[:VOICE_CHARS]}"
                       + (f"\nCODEX: {str(r['adversary'])[:VOICE_CHARS]}" if r.get("adversary") else ""))
    return summary, out


def notes() -> str:
    """Notes (/note, 11 Jul) recorded since the last ANSWERED turn, shaped as facts-from-
    the-boss and prepended to the next message — message-borne, not preamble-borne, so
    live head sessions (which skip the preamble) receive them too. Consumed by answering:
    once a debate row lands after them, they're history, not pending."""
    _, rows = chain_rows()
    last_answer = max((i for i, r in enumerate(rows) if is_answer(r)), default=-1)
    pending = [r["text"] for r in rows[last_answer + 1:] if is_note(r)]
    if not pending:
        return ""
    facts = "\n".join(f"- {n}" for n in pending)
    return f"Facts from the user (constraints — treat as given, not suggestions):\n{facts}\n\n"


def window_size(cfg: Config) -> int:
    """How many flattened rows the recency window keeps: history_turns×2 (a turn is up to a
    USER row + an answer row). The ONE place this formula lives."""
    return cfg.history_turns * 2


def window(flattened: list[str], cfg: Config) -> list[str]:
    """The turns that survive the recency window. The ONE place the window is sliced —
    preamble() ships it, /context counts it."""
    return flattened[-window_size(cfg):]


def clip(flattened: list[str], cfg: Config) -> str:
    """The windowed turns joined and hard-capped to WINDOW_CHARS. The ONE clip-window
    implementation; both the preamble the heads receive and the /context meter read it."""
    return "\n\n".join(window(flattened, cfg))[-WINDOW_CHARS:]


def preamble(cfg: Config) -> str:
    """Ask-mode MEMORY — the ONLY memory the codex head has, and what a mid-conversation
    `/duel on` hands codex as the whole back-story. Scope = the ACTIVE CHAIN (this session
    plus whatever /switch·/fork spliced in front); a /compact summary caps the chain and
    leads the preamble. Truncated hard — each turn ships this to up to 2 heads × N rounds."""
    summary, flattened = turns()
    text = clip(flattened, cfg)
    if summary:
        text = (f"Summary of the conversation so far (from a /compact):\n{summary.strip()[:SUMMARY_CHARS]}"
                + (f"\n\n{text}" if text else ""))
    return f"Conversation so far (context — do not re-answer old turns):\n{text}\n\n---\n\n" if text else ""
