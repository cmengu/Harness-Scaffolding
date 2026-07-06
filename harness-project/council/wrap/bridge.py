"""council/wrap/bridge.py — tmux mechanics + transcript reading + hook-settings writer.

LIFTED NEAR-VERBATIM from omnigent claude_native_bridge.py (encodes real Claude-TUI bugs
you must not rediscover):
  • inject :2347 — bracketed-paste submit (16KB tmux cap → load-buffer/paste-buffer -p;
    trailing-\\ eats Enter → trailing newline absorbs it). D1 DEMOTION: omnigent verified
    delivery by scraping the pane; council's oracle is the UserPromptSubmit receipt
    (session.py), and the scrape here is advisory-only evidence toward deleting it.
  • camera helpers :2716-2965 (glyph/needle values centralized in tui_contract — H3)
KEPT SLIM: the JSONL cursor readers (:2237/:504/:1778 — transcript items simplified to plain
dicts; council renders locally, no SessionEventInput shape needed).
DROPPED: the MCP tool-relay, cost popup, server registration — the whole post office.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .display_hook import MESSAGE_DELTAS_FILE
from .tui_contract import (
    DRAFT_NEEDLE_MAX_CHARS,
    PASTED_PLACEHOLDER_PREFIX,
    PROMPT_GLYPH,
    SCAN_TAIL_LINES,
)

TMUX_FILE = "tmux.json"
SETTINGS_FILE = "council-settings.json"

_TMUX_READY_TIMEOUT_S = 30.0
_TMUX_SEND_TIMEOUT_S = 5.0
_CLAUDE_READY_POLL_INTERVAL_S = 0.15
_PASTE_SETTLE_S = 0.1          # let the TUI commit a paste before the separate submit Enter
_PASTE_COMMIT_TIMEOUT_S = 5.0  # default advisory draft-watch window (cfg.draft_watch_timeout)
_FAILURE_TAIL_LINES = 12
_FAILURE_TAIL_CHARS = 800


# ── mailbox ───────────────────────────────────────────────────────────────────

def prepare_bridge_dir() -> Path:
    """A private 0700 scratch dir per session under /tmp/council-<uid>/.
    ↔ prepare_bridge_dir:735 minus the server bridge-id registration and the
    multi-CLI root whitelist (council has exactly one kind of bridge)."""
    root = Path(tempfile.gettempdir()) / f"council-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    bridge = root / uuid.uuid4().hex[:12]
    bridge.mkdir(mode=0o700)
    return bridge


def write_tmux_target(bridge: Path, *, socket_path: Path, tmux_target: str) -> None:
    (bridge / TMUX_FILE).write_text(json.dumps(
        {"socket_path": str(socket_path), "tmux_target": tmux_target, "updated_at": time.time()}))


def _wait_for_tmux_info(bridge: Path, *, timeout_s: float) -> dict[str, str]:
    """Wait for tmux.json. ↔ :2965."""
    deadline = time.monotonic() + timeout_s
    path = bridge / TMUX_FILE
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            sock, target = payload.get("socket_path"), payload.get("tmux_target")
            if isinstance(sock, str) and isinstance(target, str):
                return {"socket_path": sock, "tmux_target": target}
        time.sleep(0.05)
    raise RuntimeError("claude terminal tmux target was never advertised (launch failed?)")


# ── launch ────────────────────────────────────────────────────────────────────

def write_hook_settings(bridge: Path) -> None:
    """Write the --settings json registering council's hooks + statusLine (H1c promoted code).
    STACKS with the-harness's ~/.claude hooks — claude runs every hook registered for an event."""
    # THE #1 FRESH-CLONE TRAP: these commands run INSIDE claude's process tree, from claude's
    # cwd — NOT council's repo. sys.executable pins the right python; PYTHONPATH makes
    # `-m council.…` resolve even on a raw clone (no pip install -e needed).
    pkg_root = Path(__file__).resolve().parents[2]      # the dir containing council/
    py = shlex.quote(sys.executable)
    env = f"PYTHONPATH={shlex.quote(str(pkg_root))}"
    b = shlex.quote(str(bridge))
    state_cmd = f"{env} {py} -m council.wrap.state_hook {b}"                   # H1a
    status_cmd = f"{env} {py} -m council.wrap.state {b}"
    user_chain = _user_status_line_command()
    if user_chain:
        status_cmd += f" {shlex.quote(user_chain)}"     # chain, don't clobber, the user's bar
    settings = {
        "hooks": {
            "MessageDisplay": [{"hooks": [{"type": "command",
                "command": f"{env} {py} -m council.wrap.display_hook {b}"}]}],
            "PreToolUse":     [{"hooks": [{"type": "command",
                "command": f"{env} {py} -m council.wrap.harness_status {b}"}]}],
            # Approval evidence: a gated tool that RAN means the user said yes — PostToolUse
            # promotes the pending ask to a remembered approval (same module, event-dispatched).
            "PostToolUse":    [{"hooks": [{"type": "command",
                "command": f"{env} {py} -m council.wrap.harness_status {b}"}]}],
            # H1: the busy/idle interlock — one command wired to three turn-boundary events
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": state_cmd}]}],
            "Stop":             [{"hooks": [{"type": "command", "command": state_cmd}]}],
            "StopFailure":      [{"hooks": [{"type": "command", "command": state_cmd}]}],
            # D2: surface a hidden permission prompt instead of looking hung (see state_hook)
            "PermissionRequest": [{"hooks": [{"type": "command", "command": state_cmd}]}],
        },
        "statusLine": {"type": "command", "command": status_cmd},
    }
    (bridge / SETTINGS_FILE).write_text(json.dumps(settings, indent=2))


def _user_status_line_command() -> str | None:
    """The user's pre-existing statusLine.command from ~/.claude/settings.json, if any."""
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        command = settings.get("statusLine", {}).get("command")
        return command if isinstance(command, str) and command.strip() else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def launch_claude_in_tmux(bridge: Path, *, command: str, claude_args: tuple[str, ...],
                          resume: str | None) -> None:
    """`tmux -S <sock> new-session -d` with council's --settings appended.
    ↔ _launch_claude_terminal:3779 + augment_claude_args:1279, minus the daemon wrapper."""
    sock = bridge / "tmux.sock"
    argv = ["tmux", "-S", str(sock), "new-session", "-d",
            "-x", "200", "-y", "50",             # H4: fixed geometry — the user never sees this
            "-s", "council",                     # pane; a wide pinned width kills needle-wrapping
            command,
            *(["--resume", resume] if resume else []),
            *claude_args,
            "--settings", str(bridge / SETTINGS_FILE)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux launch failed (rc={proc.returncode}): {detail}")
    write_tmux_target(bridge, socket_path=sock, tmux_target="council")


def pane_alive(bridge: Path) -> bool:
    """Ground truth for the H1d stall check: does the hidden session still exist?"""
    try:
        info = _wait_for_tmux_info(bridge, timeout_s=0.2)
    except RuntimeError:
        return False
    proc = subprocess.run(["tmux", "-S", info["socket_path"], "has-session",
                           "-t", info["tmux_target"]], capture_output=True)
    return proc.returncode == 0


def kill_session(bridge: Path) -> None:
    """Hard stop for the hidden claude on council exit. ↔ kill_session:2513."""
    with contextlib.suppress(RuntimeError):
        info = _wait_for_tmux_info(bridge, timeout_s=0.2)
        _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])


# ── inject (LIFT :2347, D1-DEMOTED: the H2 receipt is the delivery oracle) ────

def inject(bridge: Path, content: str, *,
           timeout_s: float = _TMUX_READY_TIMEOUT_S,
           settle_s: float = _PASTE_SETTLE_S,
           draft_watch_s: float = _PASTE_COMMIT_TIMEOUT_S) -> dict:
    """Paste a user message into the hidden claude pane and press Enter ONCE.

    D1 (scrape demoted to advisory): this function no longer decides whether delivery
    succeeded — the UserPromptSubmit receipt (session.SessionState) is the only oracle,
    and the receipt-driven Enter retry lives in session._inject_confirmed. The pane
    snapshots kept here are timing + advisory evidence only:
      • waits for the input box to render (keystrokes into a booting TUI are dropped)
      • clears a stale draft ONLY when one is visible — an unconditional C-a into an
        input box that doesn't bind it lands as a literal \\x01 in the message
      • pastes via load-buffer/paste-buffer -p (tmux caps one client→server command at
        ~16KB; -p keeps interior newlines as data, claude-code#52126)
      • watches for the draft to render before Enter — an Enter mid-paste coalesces
        into the draft as a newline; no longer fatal (the receipt retry re-submits),
        but worth avoiding on the happy path
    Returns {"needle", "draft_seen"} so the caller can log scrape/receipt disagreement
    — the empty-disagreement-log run is what earns deleting the scrape entirely."""
    info = _wait_for_tmux_info(bridge, timeout_s=timeout_s)
    _wait_for_claude_prompt_ready(info["socket_path"], info["tmux_target"], timeout_s=timeout_s)
    if _input_box_tail(_capture_pane(info["socket_path"], info["tmux_target"])):
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-a")
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "C-k")
    # Trailing newline absorbs a trailing "\" so it can't escape the submit Enter.
    with tempfile.NamedTemporaryFile(dir=bridge, prefix="paste_", suffix=".bin",
                                     delete=False) as paste_file:
        paste_file.write(_paste_payload_bytes(content + "\n"))
        paste_path = paste_file.name
    buffer_name = f"council-paste-{uuid.uuid4().hex[:8]}"   # D5: concurrent injects must not share
    try:
        _run_tmux(info["socket_path"], "load-buffer", "-b", buffer_name, paste_path)
        _run_tmux(info["socket_path"], "paste-buffer", "-p", "-d", "-b", buffer_name,
                  "-t", info["tmux_target"])
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)
    needle = _submit_needle(content)
    draft_seen = False
    deadline = time.monotonic() + draft_watch_s
    while time.monotonic() < deadline:
        if _draft_in_input_box(_capture_pane(info["socket_path"], info["tmux_target"]), needle):
            draft_seen = True
            break
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    time.sleep(settle_s)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")
    return {"needle": needle, "draft_seen": draft_seen}


def press_enter(bridge: Path) -> None:
    """The receipt-driven retry key (session._inject_confirmed): re-sent while the
    UserPromptSubmit receipt is missing. Enter on an already-empty box is a no-op,
    so a retry racing a slow receipt is safe."""
    info = _wait_for_tmux_info(bridge, timeout_s=0.2)
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")


# Named answers → tmux key names for the permission answer path.
_ANSWER_KEYS = {"": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape",
                "up": "Up", "down": "Down", "tab": "Tab"}


def send_keys(bridge: Path, answer: str) -> None:
    """Forward ONE permission-prompt answer to the hidden pane (the D2 answer path).
    NOT inject(): that targets the input BOX (bracketed paste + needle verification);
    a permission prompt is a MENU — it wants raw keystrokes and there is no receipt
    to verify against. Named keys map to tmux key names; a single character goes
    literally WITHOUT Enter (claude's numbered menus act on the digit immediately —
    a trailing Enter would land on the next UI state); longer text gets Enter."""
    info = _wait_for_tmux_info(bridge, timeout_s=0.2)
    ans = answer.strip()
    key = _ANSWER_KEYS.get(ans.lower())
    if key is not None:
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], key)
        return
    _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "-l", ans)
    if len(ans) > 1:
        _run_tmux(info["socket_path"], "send-keys", "-t", info["tmux_target"], "Enter")


def list_bridges(prune: bool = True) -> list[Path]:
    """Every LIVE bridge dir under /tmp/council-<uid>/, newest first (the `attach`
    surface). A live pane with no council attached = a /detach or a crashed wrapper —
    exactly what attach exists for. Dead dirs are pruned by default: they are ours,
    session-scoped, and only ever litter once the pane is gone."""
    root = Path(tempfile.gettempdir()) / f"council-{os.getuid()}"
    if not root.is_dir():
        return []
    live: list[Path] = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        if pane_alive(d):
            live.append(d)
        elif prune:
            shutil.rmtree(d, ignore_errors=True)
    return live


def draft_lingering(bridge: Path, needle: str) -> bool:
    """Advisory snapshot: does the draft still sit in the input box? NEVER authoritative —
    session records it as scrape/receipt disagreement evidence for D1's final deletion."""
    if not needle:
        return False
    try:
        info = _wait_for_tmux_info(bridge, timeout_s=0.2)
    except RuntimeError:
        return False
    return _draft_in_input_box(_capture_pane(info["socket_path"], info["tmux_target"]), needle)


def pane_tail(bridge: Path) -> str:
    """Diagnostic-only: claude's last on-screen lines, attached to inject-failure records
    (replaces the detail the authoritative scrape used to put in its RuntimeError)."""
    try:
        info = _wait_for_tmux_info(bridge, timeout_s=0.2)
    except RuntimeError:
        return ""
    return _format_failure_tail(_capture_pane(info["socket_path"], info["tmux_target"]))


# ── camera helpers (LIFT :2716-2965; constants live in tui_contract) ─────────

def _run_tmux(socket_path: str, *args: str) -> None:
    cmd = ["tmux", "-S", socket_path, *args]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True,
                              timeout=_TMUX_SEND_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Never raises — a transient capture failure during boot means 'not ready yet'."""
    try:
        proc = subprocess.run(["tmux", "-S", socket_path, "capture-pane", "-t", tmux_target, "-p"],
                              check=False, capture_output=True, text=True,
                              timeout=_TMUX_SEND_TIMEOUT_S)
    except (subprocess.SubprocessError, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _claude_prompt_rendered(pane: str) -> bool:
    """Tail-scan only: the live input box always sits at the bottom, so scrollback echoes
    of the glyph can't false-positive."""
    non_empty = [line for line in pane.splitlines() if line.strip()]
    return any(PROMPT_GLYPH in line for line in non_empty[-SCAN_TAIL_LINES:])


def _submit_needle(content: str) -> str:
    """First usable line of the draft, truncated at the first control char — what claude
    renders verbatim on the prompt row. Empty ⇒ callers skip draft-visibility checks."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        for idx, ch in enumerate(line):
            if ord(ch) < 0x20:
                line = line[:idx]
                break
        line = line.strip()
        if line:
            return line[:DRAFT_NEEDLE_MAX_CHARS]
    return ""


def _input_box_tail(pane: str) -> str:
    """Text after the glyph on the LIVE input-box line ('' = box empty or not found).
    Only the LAST glyph line counts — never the transcript echo above it."""
    glyph_lines = [line for line in pane.splitlines() if PROMPT_GLYPH in line]
    if not glyph_lines:
        return ""
    return glyph_lines[-1].rsplit(PROMPT_GLYPH, 1)[1].strip()


def _draft_in_input_box(pane: str, needle: str) -> bool:
    tail = _input_box_tail(pane)
    if PASTED_PLACEHOLDER_PREFIX in tail:
        return True
    return bool(needle) and needle in tail


def _format_failure_tail(pane: str) -> str:
    """Attach claude's own on-screen output (often a startup crash) to a readiness error."""
    lines = [line.rstrip() for line in pane.splitlines() if line.strip()]
    if not lines:
        return ""
    tail = "\n".join(lines[-_FAILURE_TAIL_LINES:])
    if len(tail) > _FAILURE_TAIL_CHARS:
        tail = "…" + tail[-_FAILURE_TAIL_CHARS:]
    return f" Last terminal output:\n{tail}"


def _wait_for_claude_prompt_ready(socket_path: str, tmux_target: str, *, timeout_s: float) -> None:
    """Block until the input box is mounted — tmux.json only means the SESSION exists;
    the box mounts seconds later, and keystrokes into that gap are silently dropped."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _claude_prompt_rendered(_capture_pane(socket_path, tmux_target)):
            return
        time.sleep(_CLAUDE_READY_POLL_INTERVAL_S)
    pane = _capture_pane(socket_path, tmux_target)
    raise RuntimeError(
        f"claude did not become ready within {timeout_s}s (input prompt never rendered)."
        + _format_failure_tail(pane))


def _paste_payload_bytes(text: str) -> bytes:
    """Paste-buffer content bytes: every line break → one CR (what a real paste carries
    inside bracketed-paste markers); tabs kept; other control bytes DROPPED — a stray ESC
    in the content would prematurely close the bracketed-paste sequence."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
        elif ch == "\t":
            body.append(0x09)
        elif ord(ch) < 0x20:
            continue
        else:
            body.extend(ch.encode("utf-8"))
    return bytes(body)


# ── cursor readers (KEEP slim; ↔ :2237 / :504 / :1778) ───────────────────────

def _read_complete_lines(path: Path, byte_offset: int) -> tuple[list[str], int]:
    """Complete newline-terminated lines after byte_offset + the new offset. A partial
    trailing line is NOT consumed — the next poll retries it after the writer finishes.
    A truncated/rotated file (offset past EOF) restarts from 0."""
    lines: list[str] = []
    position = byte_offset
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if byte_offset > handle.tell():
                handle.seek(0)
                position = 0
            else:
                handle.seek(byte_offset)
            while True:
                raw = handle.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                position += len(raw)
                try:
                    lines.append(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    continue        # advance past the bad line, never wedge the tail
    except FileNotFoundError:
        return [], byte_offset
    return lines, position


@dataclass(frozen=True)
class MessageDelta:
    """One streamed assistant-text chunk from the display hook. message_id does NOT
    appear in the transcript — finals are correlated positionally (FIFO), not by id."""
    message_id: str
    index: int
    final: bool
    delta: str


def read_message_deltas_from_offset(bridge: Path, byte_offset: int) -> tuple[list[MessageDelta], int]:
    lines, offset = _read_complete_lines(bridge / MESSAGE_DELTAS_FILE, byte_offset)
    deltas = []
    for text in lines:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        message_id, delta, index = payload.get("message_id"), payload.get("delta"), payload.get("index")
        if not (isinstance(message_id, str) and message_id and isinstance(delta, str)):
            continue
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        deltas.append(MessageDelta(message_id, index, bool(payload.get("final")), delta))
    return deltas, offset


def read_transcript_items_from_offset(transcript_path: Path, byte_offset: int) -> tuple[list[dict], int]:
    """New authoritative items since a byte offset, as plain dicts:
      {"kind": "user_text" | "assistant_text", "text": ...}
      {"kind": "tool_use", "name": ..., "input": {...}}
    Sidechain records (inlined sub-agent traffic) are skipped. ↔ :1778, minus the
    omnigent SessionEventInput shaping — council renders locally."""
    lines, offset = _read_complete_lines(transcript_path, byte_offset)
    items: list[dict] = []
    for text in lines:
        try:
            entry = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain") is True:
            continue
        kind = entry.get("type")
        message = entry.get("message")
        if kind not in ("user", "assistant") or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if kind == "user" and content.strip():
                items.append({"kind": "user_text", "text": content})
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and kind == "assistant" and block.get("text", "").strip():
                items.append({"kind": "assistant_text", "text": block["text"]})
            elif btype == "text" and kind == "user" and block.get("text", "").strip():
                items.append({"kind": "user_text", "text": block["text"]})
            elif btype == "tool_use":
                items.append({"kind": "tool_use", "name": block.get("name", "?"),
                              "input": block.get("input") or {}})
    return items, offset
