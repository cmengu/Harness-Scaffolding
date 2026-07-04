"""council/wrap/session.py — the CODE conductor: launch the REAL claude hidden in tmux,
attach locally through council's skin. Replaces the interim PTY stand-in (4 Jul 2026).

↔ omnigent run_claude_native:342 + executor, MINUS the server ring / providers / cold-resume.
Owns its own loop (a live attached session) — chat.run_loop is ask-only.

H1 (busy/idle interlock) + H2 (submission ground-truth) live here: SessionState tails the
markers state_hook.py appends; the input pump only opens the box when claude is idle, and
every inject must be confirmed by a UserPromptSubmit marker or it fails LOUD.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import Config
from ..ledger import record
from .bridge import (draft_lingering, inject, kill_session, launch_claude_in_tmux,
                     pane_alive, pane_tail, press_enter, write_hook_settings)
from .bridge import prepare_bridge_dir
from .events import read_events
from .render import Renderer
from .state import save_launch_cwd
from .state_hook import STATE_FILE


# ── H1b — the state machine council drives off (no screen-scraping) ──────────

class SessionState:
    def __init__(self, bridge: Path):
        self._path = bridge / STATE_FILE
        self._offset = 0
        self._buf = b""                 # holds a half-written trailing line between polls
        self.busy = False
        self.blocked = False            # D2: mid-turn but waiting on a permission decision
        self.last_submit_ts = 0.0       # ts of the most recent UserPromptSubmit (H2's oracle)

    def poll(self) -> None:
        """Fold any new COMPLETE markers into .busy / .last_submit_ts. Cheap; tight-loop safe."""
        if not self._path.exists():
            return
        with self._path.open("rb") as f:
            f.seek(self._offset)
            self._buf += f.read()
            self._offset = f.tell()
        *lines, self._buf = self._buf.split(b"\n")           # keep the partial tail buffered
        for line in lines:
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                # A torn line raising here would kill the daemon thread SILENTLY and freeze
                # .busy=True forever. Skip-and-log: markers are cumulative, the next corrects.
                record({"role": "state_parse_error", "line": line[:200].decode(errors="replace")})
                continue
            self.busy = (m["state"] != "idle")               # last marker in the batch wins
            self.blocked = (m["state"] == "blocked")         # D2: any later marker clears it
            if m["event"] == "UserPromptSubmit":
                self.last_submit_ts = m["ts"]

    def wait_idle(self, timeout: float, on_blocked=None) -> bool:
        """True = idle; False = timed out (backstop — never hang).
        on_blocked fires ONCE if a D2 permission-wait marker shows up mid-wait."""
        deadline = time.monotonic() + timeout
        announced = False
        while time.monotonic() < deadline:
            self.poll()
            if not self.busy:
                return True
            if self.blocked and on_blocked and not announced:
                on_blocked()
                announced = True
            time.sleep(0.05)
        return False

    def wait_submitted(self, since_ts: float, timeout: float) -> bool:
        """H2's oracle. Keys on last_submit_ts (NOT .busy) so a turn fast enough to Stop
        before our first poll still counts as submitted."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.last_submit_ts > since_ts:
                return True
            time.sleep(0.05)
        return False


# ── H2 — confirmed inject ─────────────────────────────────────────────────────

def _inject_confirmed(bridge: Path, text: str, state: SessionState, renderer, cfg: Config) -> None:
    """D1: the H2 receipt DRIVES delivery. inject pastes and presses Enter once; while the
    receipt is missing we re-press Enter (the common miss = a swallowed/coalesced Enter,
    and Enter on an empty box is a no-op). The scrape's opinion is advisory: recorded to
    the ledger ONLY when it disagrees with the receipt — an empty scrape_advisory log over
    real usage is the evidence that earns deleting the scrape from bridge.py entirely."""
    sent_at = time.time()                        # wall clock — same clock the hook stamps
    try:
        advisory = inject(bridge, text, timeout_s=cfg.tmux_ready_timeout,
                          settle_s=cfg.paste_settle, draft_watch_s=cfg.draft_watch_timeout)
    except RuntimeError as exc:
        renderer.error(f"⚠ inject failed: {exc}")
        record({"role": "inject_error", "text": text, "error": str(exc)})
        return
    deadline = time.monotonic() + cfg.submit_timeout
    while time.monotonic() < deadline:
        slice_s = min(cfg.submit_retry_interval, max(deadline - time.monotonic(), 0.05))
        if state.wait_submitted(since_ts=sent_at, timeout=slice_s):
            if advisory["draft_seen"] and draft_lingering(bridge, advisory["needle"]):
                record({"role": "scrape_advisory", "text": text,
                        "note": "receipt confirmed but the pane still shows the draft"})
            return
        with contextlib.suppress(RuntimeError):
            press_enter(bridge)
    renderer.error(f"⚠ inject NOT confirmed in {cfg.submit_timeout}s — "
                   "message may not have submitted")
    record({"role": "inject_error", "text": text, "waited_s": cfg.submit_timeout,
            "draft_seen": advisory["draft_seen"],
            "draft_lingering": draft_lingering(bridge, advisory["needle"]),
            "pane_tail": pane_tail(bridge)})


# ── H1d — the input pump, gated on idle ───────────────────────────────────────

def _input_pump(bridge: Path, renderer, state: SessionState, cfg: Config) -> None:
    stalls = 0                # the happy path depends on Stop ARRIVING; bound the wait
    warn_blocked = lambda: renderer.error(       # D2: a hidden permission prompt looks like a hang
        "⚠ hidden claude is waiting on a PERMISSION prompt council can't answer — "
        "it will sit until the turn stalls (consider --permission-mode or pre-allowed tools)")
    while True:
        if not state.wait_idle(timeout=cfg.turn_timeout, on_blocked=warn_blocked):
            stalls += 1
            if stalls >= 2:                                   # 2× turn_timeout with no event
                if not pane_alive(bridge):                    # ground truth
                    renderer.error("hidden claude died — session over")
                    return
                renderer.error(f"no Stop event in {stalls * cfg.turn_timeout}s — "
                               "unlocking input (interlock degraded)")
                state.busy = False    # manual override: worst case = the pre-H1 world
            else:
                renderer.notice("Claude still working…")
            continue
        stalls = 0
        if not pane_alive(bridge):
            renderer.notice("session ended")
            return
        try:
            text = renderer.read_input()                      # box is live only now
        except (EOFError, KeyboardInterrupt):
            return
        if text in ("/exit", "/quit", "exit", "quit"):
            return
        if not text:
            continue
        _inject_confirmed(bridge, text, state, renderer, cfg)


# ── D3 — boot self-probe (opt-in: cfg.boot_probe) ────────────────────────────

def _boot_probe(bridge: Path, state: SessionState, renderer, cfg: Config) -> None:
    """Spend one tiny turn at launch proving the H2 receipt loop end-to-end, so a
    misregistered hook fails HERE instead of as the first real message. Off by default:
    it costs a turn and a line of conversation, and post-D1 the first real inject
    already fails loud within submit_timeout anyway."""
    renderer.notice("boot probe: verifying the hook receipt loop…")
    sent_at = time.time()
    try:
        inject(bridge, "council boot probe — reply with just: ok",
               timeout_s=cfg.tmux_ready_timeout, settle_s=cfg.paste_settle,
               draft_watch_s=cfg.draft_watch_timeout)
    except RuntimeError as exc:
        sys.exit(f"council: boot probe could not reach the hidden claude: {exc}")
    if not state.wait_submitted(since_ts=sent_at, timeout=cfg.submit_timeout):
        sys.exit("council: boot probe was never confirmed — council's hooks are not firing "
                 "inside claude (check --settings registration); refusing to run blind")
    renderer.notice("boot probe confirmed ✓")


# ── the conductor ─────────────────────────────────────────────────────────────

def _preflight(command: str) -> None:
    """Fail loud at launch, never silently later: tmux present, claude present, and the
    council package importable from an arbitrary cwd (the hooks run from claude's cwd)."""
    if shutil.which("tmux") is None:
        sys.exit("council: tmux not found — `brew install tmux` (code mode hides claude in tmux)")
    if shutil.which(command) is None:
        sys.exit(f"council: `{command}` binary not found on PATH")
    pkg_root = Path(__file__).resolve().parents[2]
    probe = subprocess.run([sys.executable, "-m", "council.wrap.state_hook", "--check"],
                           env={"PYTHONPATH": str(pkg_root), "PATH": "/usr/bin:/bin"},
                           capture_output=True)
    if probe.returncode != 0:
        sys.exit("council: hook self-probe failed — the council package is not importable "
                 f"from other cwds ({probe.stderr.decode().strip()})")


def run_claude_session(*, claude_args, use_claude_config: bool, command: str,
                       resume: str | None, cfg: Config) -> None:
    """The CODE engine. use_claude_config is HARDWIRED True by cli.py — the whole point
    is that the real binary loads ~/.claude, so the-harness's hooks stay live."""
    _preflight(command)
    bridge = prepare_bridge_dir()
    save_launch_cwd(bridge, Path.cwd(), resume)
    write_hook_settings(bridge)                 # council's hooks STACK on ~/.claude's
    launch_claude_in_tmux(bridge, command=command, claude_args=tuple(claude_args), resume=resume)
    record({"role": "code_session", "event": "start", "bridge": str(bridge)})
    renderer = Renderer(cfg, bridge)
    renderer.notice(f"engine hidden in tmux (bridge {bridge.name}) — /exit to quit")
    state = SessionState(bridge)
    out = threading.Thread(target=lambda: [renderer.handle(e) for e in read_events(bridge)],
                           daemon=True)
    out.start()                                 # pump 1: claude's 3 channels → council's skin
    try:
        if cfg.boot_probe:
            _boot_probe(bridge, state, renderer, cfg)   # D3: fail at boot, not mid-conversation
        _input_pump(bridge, renderer, state, cfg)   # pump 2: council's box → the hidden pane
    finally:
        kill_session(bridge)                    # never leave a hidden claude running
        record({"role": "code_session", "event": "end"})
        renderer.console.print("\n[bold]⚖ council code session ended[/]")
