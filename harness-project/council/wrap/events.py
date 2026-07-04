"""council/wrap/events.py — tail the THREE out-channels → yield LOCAL render events.

↔ omnigent claude_native_forwarder.py: KEEP the read half, DROP every _post_external_*
and the supervisor. Council renders locally → nothing to forward — which is why
4,183 lines collapse to this.

Yields ("delta", MessageDelta) · ("item", dict) · ("context", dict) until the pane dies.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from .bridge import pane_alive, read_message_deltas_from_offset, read_transcript_items_from_offset
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


def read_events(bridge: Path):
    """Generator: poll the three files past their cursors; yield until the pane dies.
    Live deltas stream tokens; transcript items are authoritative (reconciled positionally
    by the Renderer — message_id isn't in the transcript); context.json carries cost/model."""
    transcript: Path | None = None
    transcript_offset = 0
    deltas_offset = 0
    last_context: dict = {}
    last_pane_check = 0.0
    while True:
        deltas, deltas_offset = read_message_deltas_from_offset(bridge, deltas_offset)
        for delta in deltas:
            yield ("delta", delta)
        if transcript is None:
            transcript = _find_transcript(bridge)
        if transcript is not None:
            items, transcript_offset = read_transcript_items_from_offset(transcript, transcript_offset)
            for item in items:
                yield ("item", item)
        context = read_context(bridge)
        if context and context != last_context:
            last_context = context
            yield ("context", context)
        now = time.monotonic()
        if now - last_pane_check >= _PANE_CHECK_EVERY_S:
            last_pane_check = now
            if not pane_alive(bridge):
                return
        time.sleep(_POLL_S)
