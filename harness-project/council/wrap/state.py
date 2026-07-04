"""council/wrap/state.py — launch-cwd persistence (resume) + the statusLine cost/model capture.

↔ omnigent claude_native_state.py (launch-cwd, slimmed) + claude_native_status.py (LIFTED NEAR-WHOLE).
claude-native emits NO response.completed event, so the statusLine hack is the ONLY cost/model source.

STDLIB-ONLY on purpose: the __main__ half runs INSIDE claude as its statusLine.command
on every render — importing rich/council here would stutter the TUI.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONTEXT_FILE = "context.json"
LAUNCH_FILE = "launch.json"

# H5 (defensive strip, see tui_contract): our chained status output must never
# look like claude's input prompt, or bridge's camera checks would false-positive.
_PROMPT_GLYPH = "❯"


# ── council-side: launch state ────────────────────────────────────────────────

def save_launch_cwd(bridge: Path, cwd: Path, resume: str | None) -> None:
    """Persist launch cwd + wall-clock so events.py finds the right transcript
    (newest .jsonl in claude's project dir for this cwd, born after launched_at)."""
    payload = {"cwd": str(cwd), "resume": resume, "launched_at": time.time()}
    (bridge / LAUNCH_FILE).write_text(json.dumps(payload))


def read_launch_state(bridge: Path) -> dict:
    try:
        payload = json.loads((bridge / LAUNCH_FILE).read_text())
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read_context(bridge: Path) -> dict:
    """The council-side reader of context.json (cost/model/context-window)."""
    try:
        payload = json.loads((bridge / CONTEXT_FILE).read_text())
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ── claude-side: the statusLine wrapper (LIFT from claude_native_status.py) ──

def _write_context_atomic(bridge_dir: Path, payload: dict) -> None:
    """Persist the statusLine payload's context fields to context.json (atomic replace,
    so events.py never observes a half-written file). Soft-fails on a malformed payload."""
    record: dict = {}
    context = payload.get("context_window")
    if isinstance(context, dict):
        size = context.get("context_window_size")
        if isinstance(size, int) and size > 0:
            record["context_window_size"] = size
            usage = context.get("current_usage")
            if isinstance(usage, dict):
                record["current_usage"] = usage
            used_pct = context.get("used_percentage")
            if isinstance(used_pct, (int, float)):
                record["used_percentage"] = used_pct
    cost = payload.get("cost")
    if isinstance(cost, dict):
        total_cost = cost.get("total_cost_usd")
        if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool) and total_cost >= 0:
            record["total_cost_usd"] = float(total_cost)
    model = payload.get("model")            # {"id": ..., "display_name": ...} or bare string
    model_id = None
    if isinstance(model, dict):
        raw_model = model.get("id") or model.get("display_name")
        if isinstance(raw_model, str) and raw_model.strip():
            model_id = raw_model.strip()
    elif isinstance(model, str) and model.strip():
        model_id = model.strip()
    if model_id is not None:
        record["model"] = model_id
    if not record:
        return
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".context-", dir=str(bridge_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, separators=(",", ":"))
            os.replace(tmp_path, str(bridge_dir / CONTEXT_FILE))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
    except OSError as exc:
        print(f"council status: write failed: {exc}", file=sys.stderr)


def _chain(command: str, stdin_payload: str) -> None:
    """Run the user's ORIGINAL statusLine command with the same stdin, so their
    status bar still renders under council. We don't clobber — we chain."""
    try:
        proc = subprocess.run(command, input=stdin_payload, shell=True,
                              capture_output=True, text=True, check=False, timeout=5.0)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"council status: chain failed: {exc}", file=sys.stderr)
        return
    if proc.stdout:
        sys.stdout.write(proc.stdout.replace(_PROMPT_GLYPH, ""))   # ← H5: never look like a prompt
    if proc.stderr:
        sys.stderr.write(proc.stderr)


def status_line_wrapper(bridge_dir: str, chain: str | None) -> int:
    """Runs as claude's statusLine.command. Read stdin (context_window / cost / model),
    write context.json atomically, then chain the user's original statusLine."""
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        _write_context_atomic(Path(bridge_dir), parsed)
    if chain:
        _chain(chain, raw)
    return 0


if __name__ == "__main__":   # claude invokes: python -m council.wrap.state <bridge> [<chain-cmd>]
    raise SystemExit(status_line_wrapper(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
