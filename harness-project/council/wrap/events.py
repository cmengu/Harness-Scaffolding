"""council/wrap/events.py — tail the FOUR out-channels → yield LOCAL render events.

↔ omnigent claude_native_forwarder.py: KEEP the read half, DROP every _post_external_*
and the supervisor. Council renders locally → nothing to forward — which is why
4,183 lines collapse to this.

Yields ("delta", MessageDelta) · ("item", dict) · ("context", dict) ·
("approval", dict) until the pane dies. The fourth channel tails approvals.jsonl — the
PreToolUse/PostToolUse hook's own approvals memory (harness_status.py) — so council can
print "remembered" / budget-checkpoint lines the hook itself never could: a hook's
stdout can inject into claude's own context, so it only ever writes to disk.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..ledger import is_approval
from .bridge import (
    _read_complete_lines,
    pane_alive,
    read_message_deltas_from_offset,
    read_transcript_items_from_offset,
)
from .display_hook import MESSAGE_DELTAS_FILE
from .harness_status import APPROVALS_FILE
from .state import read_context, read_launch_state

_POLL_S = 0.1
_PANE_CHECK_EVERY_S = 2.0
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _project_dir_for_cwd(cwd: str) -> Path:
    """claude stores transcripts under ~/.claude/projects/<sanitized-cwd>/; the observed
    sanitizer replaces non-alphanumerics with '-'. ↔ claude_native.py:1287/:1301."""
    return _CLAUDE_PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _find_transcript(bridge: Path) -> Path | None:
    """The session's transcript. Fresh launch: ONLY a .jsonl modified at/after launch —
    never fall back to an older file (a prior session's transcript in the same cwd would
    be replayed from byte 0 as stale history; hit live, 4 Jul 2026). The file appears a
    beat after launch, so callers must keep polling while this returns None.
    --resume: claude reuses the resumed transcript, so newest-by-mtime is correct."""
    launch = read_launch_state(bridge)
    project_dir = _project_dir_for_cwd(launch.get("cwd", ""))
    if not project_dir.is_dir():
        return None
    candidates = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if launch.get("resume"):
        return candidates[0] if candidates else None
    launched_at = launch.get("launched_at", 0.0)
    born_after = [p for p in candidates if p.stat().st_mtime >= launched_at - 0.5]
    return born_after[0] if born_after else None


def _read_approval_events(bridge: Path, byte_offset: int) -> tuple[list[dict], int]:
    """New approval-memory rows since byte_offset, as (rows, new_offset).

    "pending" rows are display noise (the hook's own permissionDecisionReason already
    told the user it's asking) — only "approved" and "auto" are surfaced. Malformed
    lines and non-dict rows are skipped, never raised (↔ _read_approvals' own fold).
    """
    lines, offset = _read_complete_lines(bridge / APPROVALS_FILE, byte_offset)
    rows: list[dict] = []
    for text in lines:
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        if is_approval(row):
            rows.append(row)
    return rows, offset


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_events(bridge: Path, *, fresh: bool = True):
    """Generator: poll the four files past their cursors; yield until the pane dies.
    Live deltas stream tokens; transcript items are authoritative (reconciled positionally
    by the Renderer — message_id isn't in the transcript); context.json carries cost/model;
    approvals.jsonl carries the PreToolUse/PostToolUse hook's remembered/auto-withdrawn asks.

    fresh=False (attach): the TRANSCRIPT alone replays from byte 0 — it is the history
    repaint — while deltas and approvals skip to EOF (token chunks and old ⚑ notices are
    live-only noise once the transcript covers them). A ("live", {}) sentinel fires once
    the replay catches up to where the transcript stood at attach, flipping the Renderer
    out of replay mode."""
    transcript: Path | None = None
    transcript_offset = 0
    deltas_offset = _size(bridge / MESSAGE_DELTAS_FILE) if not fresh else 0
    approvals_offset = _size(bridge / APPROVALS_FILE) if not fresh else 0
    replay_until: int | None = None if not fresh else 0    # transcript size at attach
    live_sent = False
    last_context: dict = {}
    last_pane_check = 0.0
    while True:
        deltas, deltas_offset = read_message_deltas_from_offset(bridge, deltas_offset)
        for delta in deltas:
            yield ("delta", delta)
        if transcript is None:
            transcript = _find_transcript(bridge)
            if transcript is not None and replay_until is None:
                replay_until = _size(transcript)
        if transcript is not None:
            items, transcript_offset = read_transcript_items_from_offset(transcript, transcript_offset)
            for item in items:
                yield ("item", item)
        if not live_sent and transcript_offset >= (replay_until if replay_until is not None else 0):
            live_sent = True                    # fresh: fires immediately; attach: caught up
            yield ("live", {})
        context = read_context(bridge)
        if context and context != last_context:
            last_context = context
            yield ("context", context)
        approvals, approvals_offset = _read_approval_events(bridge, approvals_offset)
        for row in approvals:
            yield ("approval", row)
        now = time.monotonic()
        if now - last_pane_check >= _PANE_CHECK_EVERY_S:
            last_pane_check = now
            if not pane_alive(bridge):
                return
        time.sleep(_POLL_S)
