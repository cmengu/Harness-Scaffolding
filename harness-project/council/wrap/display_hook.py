"""council/wrap/display_hook.py — the per-chunk MessageDisplay hook (runs INSIDE claude).

↔ omnigent claude_native_message_display_hook.py — LIFTED NEAR-WHOLE.
Split out of render.py (where the manuscript sketched it) on purpose: claude BLOCKS
on this hook once per streamed text chunk, and render.py imports rich — a per-chunk
rich import would visibly stutter streaming. This module is stdlib-only.

Appends {"message_id","index","final","delta"} lines to <bridge>/message_deltas.jsonl;
events.py tails that file for live token streaming.
"""
from __future__ import annotations

import json
import os
import sys

MESSAGE_DELTAS_FILE = "message_deltas.jsonl"


def _delta_record(payload: dict) -> dict | None:
    """Extract the forwardable fields, or None when the payload lacks a usable pair."""
    message_id = payload.get("message_id")
    delta = payload.get("delta")
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(delta, str):
        return None
    raw_index = payload.get("index")
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
    return {"message_id": message_id, "index": index,
            "final": bool(payload.get("final")), "delta": delta}


def main(bridge_dir: str) -> int:
    """Append one MessageDisplay chunk. Always returns 0 — a hook failure must never block claude."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    record = _delta_record(payload)
    if record is None:
        return 0
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        # O_APPEND makes a single short-line write atomic on POSIX, so concurrent
        # per-chunk hook subprocesses never interleave their lines.
        fd = os.open(os.path.join(bridge_dir, MESSAGE_DELTAS_FILE),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":              # claude invokes: python -m council.wrap.display_hook <bridge>
    raise SystemExit(main(sys.argv[1]))
