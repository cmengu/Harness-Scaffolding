"""council/wrap/state_hook.py — H1a: the busy/idle marker WRITER (runs INSIDE claude).

stdlib-only + O_APPEND: claude BLOCKS on this hook, so it must be tiny and its
writes atomic. One marker per turn-boundary event. Registered by
bridge.write_hook_settings for UserPromptSubmit / Stop / StopFailure.

Also the fresh-clone probe: `python -m council.wrap.state_hook --check` proves the
package is importable from an arbitrary cwd BEFORE tmux launches (session.py runs it).
"""
from __future__ import annotations

import json
import os
import sys
import time

STATE_FILE = "session_state.jsonl"      # append-per-event ⇒ jsonl
_EVENT_STATE = {                        # the ONE place event-name → state lives (verify names HERE)
    "UserPromptSubmit": "busy",         # a prompt reached claude — incl. via council's tmux send-keys
    "Stop":             "idle",         # turn ended cleanly
    "StopFailure":      "idle",         # turn ended in error — still idle, still re-enable the box
    # D2: the hidden pane can stall forever on a permission prompt council can't see.
    # "blocked" is still busy (mid-turn) — SessionState surfaces it, never unlocks on it.
    # UNVERIFIED-LIVE (unlike the three above): if this event name is wrong claude just
    # never fires it, and D2 degrades to the pre-D2 world — no other behavior changes.
    "PermissionRequest": "blocked",     # claude is waiting on a permission decision
}


def session_state_hook(bridge_dir: str, payload: dict) -> int:
    """payload = the hook's stdin JSON → append {ts,event,state,prompt_id} → 0 (never block claude)."""
    event = payload.get("hook_event_name", "")
    state = _EVENT_STATE.get(event)
    if state is None:
        return 0                        # not one of our three events — no-op
    marker = {"ts": time.time(), "event": event, "state": state,
              "prompt_id": payload.get("prompt_id"),      # verified live: snake_case,
              "session_id": payload.get("session_id")}    # present on BOTH submit & stop
    fd = os.open(os.path.join(bridge_dir, STATE_FILE),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(marker) + "\n").encode())   # single write ⇒ offset-atomic
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":              # claude invokes: python -m council.wrap.state_hook <bridge>
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        raise SystemExit(0)             # importability probe for session.py's preflight
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        raise SystemExit(0)             # malformed input must never block claude
    raise SystemExit(session_state_hook(sys.argv[1], payload if isinstance(payload, dict) else {}))
