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
from ..ledger import (code_permission, code_session, inject_error, paste_retry,
                      record, scrape_advisory, state_parse_error)
from .bridge import (draft_lingering, inject, kill_session, launch_claude_in_tmux,
                     pane_alive, pane_tail, press_enter, send_keys, write_hook_settings)
from .bridge import prepare_bridge_dir
from .events import read_events
from .render import Renderer
from .state import save_launch_cwd
from .state_hook import STATE_FILE
from .tui_contract import PROMPT_GLYPH


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
                record(state_parse_error(line[:200].decode(errors="replace")))
                continue
            self.busy = (m["state"] != "idle")               # last marker in the batch wins
            self.blocked = (m["state"] == "blocked")         # D2: any later marker clears it
            if m["event"] == "UserPromptSubmit":
                self.last_submit_ts = m["ts"]

    def wait_idle(self, timeout: float) -> str:
        """Wait for the turn to end. Returns "idle" | "blocked" | "timeout".
        "blocked" returns IMMEDIATELY (a D2 permission-wait marker showed up): the pump
        owns the response — show the prompt, forward an answer — not a callback here."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if not self.busy:
                return "idle"
            if self.blocked:
                return "blocked"
            time.sleep(0.05)
        return "timeout"

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

def _deliver(bridge: Path, text: str, state: SessionState, renderer, cfg: Config) -> tuple[bool, dict]:
    """One paste → a receipt window with Enter re-presses → at most ONE full re-paste.
    Returns (confirmed, advisory). The re-paste exists for the first-ever launch in a
    directory: claude drops the first paste during first-visit init even though the
    composer glyph is already rendered (live-hit 7 Jul 2026; second visits never see it).
    Re-pasting is gated on the draft being INVISIBLE — if the text is (or might be) in
    the box, pasting again would double it, so we fail loud instead. Enter re-presses
    inside the window stay safe by the old argument: Enter on an empty box is a no-op."""
    sent_at = time.time()                        # wall clock — same clock the hook stamps
    advisory: dict = {"needle": "", "draft_seen": False}
    for attempt in range(2):
        advisory = inject(bridge, text, timeout_s=cfg.tmux_ready_timeout,
                          settle_s=cfg.paste_settle, draft_watch_s=cfg.draft_watch_timeout)
        deadline = time.monotonic() + cfg.submit_timeout
        while time.monotonic() < deadline:
            slice_s = min(cfg.submit_retry_interval, max(deadline - time.monotonic(), 0.05))
            if state.wait_submitted(since_ts=sent_at, timeout=slice_s):
                return True, advisory
            with contextlib.suppress(RuntimeError):
                press_enter(bridge)
        if attempt == 0 and not advisory["draft_seen"] \
                and not draft_lingering(bridge, advisory["needle"]):
            record(paste_retry(text[:200]))          # evidence trail, like scrape_advisory
            renderer.notice("first paste vanished (fresh-launch wart) — re-pasting once…")
            continue
        break                                    # draft visible (or 2nd attempt): never paste again
    return False, advisory


def _inject_confirmed(bridge: Path, text: str, state: SessionState, renderer, cfg: Config) -> None:
    """D1: the H2 receipt DRIVES delivery. inject pastes and presses Enter once; while the
    receipt is missing we re-press Enter (the common miss = a swallowed/coalesced Enter,
    and Enter on an empty box is a no-op). The scrape's opinion is advisory: recorded to
    the ledger ONLY when it disagrees with the receipt — an empty scrape_advisory log over
    real usage is the evidence that earns deleting the scrape from bridge.py entirely."""
    try:
        confirmed, advisory = _deliver(bridge, text, state, renderer, cfg)
    except RuntimeError as exc:
        renderer.error(f"⚠ inject failed: {exc}")
        record(inject_error(text, error=str(exc)))
        return
    if confirmed:
        if advisory["draft_seen"] and draft_lingering(bridge, advisory["needle"]):
            record(scrape_advisory(text,
                    note="receipt confirmed but the pane still shows the draft"))
        return
    renderer.error(f"⚠ inject NOT confirmed in {cfg.submit_timeout}s — "
                   "message may not have submitted")
    record(inject_error(text, waited_s=cfg.submit_timeout,
                        draft_seen=advisory["draft_seen"],
                        draft_lingering=draft_lingering(bridge, advisory["needle"]),
                        pane_tail=pane_tail(bridge)))


# ── D2 answered — the permission relay ────────────────────────────────────────

def _answer_permission(bridge: Path, renderer, state: SessionState) -> str | None:
    """The hidden pane is showing a permission prompt (a menu the user can't see).
    Show claude's own prompt text, forward ONE answer verbatim, and optimistically
    clear the blocked flag: no marker fires when a prompt is ANSWERED (PermissionRequest
    only fires when one OPENS), so waiting for a clearing marker would re-prompt forever.
    A wrong keystroke degrades to the stall path — which now shows the pane tail.
    Returns a pump verb ("exit"/"detach") when the user typed one — an agentic claude can
    re-raise the prompt every few seconds, and forwarding /exit into that menu would trap
    the user in the relay forever (live-hit 6 Jul 2026: the only way out was kill-session)."""
    renderer.console.rule("[yellow]⛔ permission — the hidden claude is asking[/]",
                          style="yellow", align="left")
    renderer.console.print(pane_tail(bridge) or "[dim](could not capture the prompt)[/]")
    try:
        ans = renderer.console.input("[bold yellow]answer (1/2/y/esc/enter) ›[/] ").strip()
    except (EOFError, KeyboardInterrupt):
        renderer.notice("left unanswered — the prompt stays up; the stall check takes over")
        state.blocked = False
        return None
    if ans in ("/exit", "/quit", "exit", "quit"):     # pump verbs act on the WRAPPER,
        return "exit"                                 # never on claude's menu
    if ans == "/detach":
        return "detach"
    try:
        send_keys(bridge, ans)
    except RuntimeError as exc:
        renderer.error(f"⚠ could not reach the pane: {exc}")
    record(code_permission(ans))
    state.blocked = False
    return None


# ── H1d — the input pump, gated on idle ───────────────────────────────────────

def _input_pump(bridge: Path, renderer, state: SessionState, cfg: Config) -> str:
    """Returns why it stopped: "exit" (kill the hidden claude) | "detach" (leave it
    running — `council attach` reconnects)."""
    stalls = 0                # the happy path depends on Stop ARRIVING; bound the wait
    while True:
        outcome = state.wait_idle(timeout=cfg.turn_timeout)
        if outcome == "blocked":
            verb = _answer_permission(bridge, renderer, state)   # D2: answer it, don't just warn
            if verb:
                return verb                                      # /exit·/detach typed AT the relay
            continue
        if outcome == "timeout":
            stalls += 1
            if stalls >= 2:                                   # 2× turn_timeout with no event
                if not pane_alive(bridge):                    # ground truth
                    renderer.error("hidden claude died — session over")
                    return "exit"
                renderer.error(f"no Stop event in {stalls * cfg.turn_timeout}s — "
                               "unlocking input (interlock degraded)")
                tail = pane_tail(bridge)                      # a lingering unanswered
                if tail:                                      # permission prompt shows HERE
                    renderer.notice(f"pane tail:\n{tail}")
                state.busy = False    # manual override: worst case = the pre-H1 world
            else:
                renderer.notice("Claude still working…")
            continue
        stalls = 0
        if not pane_alive(bridge):
            renderer.notice("session ended")
            return "exit"
        try:
            text = renderer.read_input()                      # box is live only now
        except (EOFError, KeyboardInterrupt):
            return "exit"
        if text in ("/exit", "/quit", "exit", "quit"):
            return "exit"
        if text == "/detach":
            return "detach"
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
    try:                                             # same delivery path real messages use —
        confirmed, _ = _deliver(bridge, "council boot probe — reply with just: ok",
                                state, renderer, cfg)   # incl. the fresh-launch re-paste
    except RuntimeError as exc:
        sys.exit(f"council: boot probe could not reach the hidden claude: {exc}")
    if not confirmed:
        tail = pane_tail(bridge)                     # self-diagnosing: a blocking dialog or a
        sys.exit("council: boot probe was never confirmed — either council's hooks are not "
                 "firing inside claude (check --settings registration) or a dialog is eating "
                 "the input; refusing to run blind. The hidden pane shows:\n"
                 + (tail or "(could not capture the pane)"))     # dead pane shows itself HERE
    renderer.notice("boot probe confirmed ✓")


# ── first-launch trust dialog ─────────────────────────────────────────────────

def _answer_boot_dialog(bridge: Path, renderer, cfg: Config) -> None:
    """First launch in a NEW cwd: claude shows "Do you trust the files in this folder?"
    BEFORE mounting the input box, and NO hook event fires for it — a blind inject types
    into the menu and its Enter accepts the dialog silently (live-hit 6 Jul 2026: the
    boot probe's text vanished into it). The pane is the only channel that can see this,
    so this one boot-time check is deliberately a scrape: poll until the composer glyph
    appears (normal boot in a trusted dir → zero added latency) or the dialog text shows —
    then relay it like any permission prompt. Never auto-answered: trust is the user's call."""
    deadline = time.monotonic() + cfg.tmux_ready_timeout
    while time.monotonic() < deadline:
        tail = pane_tail(bridge) or ""
        if "trust the files" in tail.lower():
            renderer.console.rule("[yellow]⛔ first-launch check — the hidden claude is asking[/]",
                                  style="yellow", align="left")
            renderer.console.print(tail)
            try:
                ans = renderer.console.input("[bold yellow]answer (1/2/esc/enter) ›[/] ").strip()
            except (EOFError, KeyboardInterrupt):
                renderer.notice("left unanswered — the dialog stays up; the first inject will fail loud")
                return
            with contextlib.suppress(RuntimeError):
                send_keys(bridge, ans)
            record(code_permission(ans, dialog="trust"))
            return
        if PROMPT_GLYPH in tail:             # composer is up — nothing blocking, boot on
            return
        time.sleep(0.3)


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
    """The CODE engine, launch half. use_claude_config is HARDWIRED True by cli.py — the
    whole point is that the real binary loads ~/.claude, so the-harness's hooks stay live."""
    _preflight(command)
    bridge = prepare_bridge_dir()
    save_launch_cwd(bridge, Path.cwd(), resume)
    write_hook_settings(bridge)                 # council's hooks STACK on ~/.claude's
    launch_claude_in_tmux(bridge, command=command, claude_args=tuple(claude_args), resume=resume)
    record(code_session("start", bridge=str(bridge)))
    _attached_loop(bridge, cfg, fresh=True)


def attach_claude_session(bridge: Path, cfg: Config) -> None:
    """Reconnect to a live hidden claude (after /detach, or a crashed wrapper). The
    transcript replays from byte 0 — the whole conversation repaints as history — then
    the session goes live exactly as if never left."""
    record(code_session("attach", bridge=str(bridge)))
    _attached_loop(bridge, cfg, fresh=False)


def _attached_loop(bridge: Path, cfg: Config, *, fresh: bool) -> None:
    """The shared attach half: renderer + state + the two pumps. `fresh` gates the boot
    probe (an attach must not spend a turn) and puts the renderer in replay mode so
    repainted history isn't re-recorded to the ledger."""
    renderer = Renderer(cfg, bridge, replaying=not fresh)   # attach repaints history dim
    renderer.notice(f"engine hidden in tmux (bridge {bridge.name}) — /exit to quit · /detach to leave it running")
    state = SessionState(bridge)
    out = threading.Thread(target=lambda: [renderer.handle(e)
                                           for e in read_events(bridge, fresh=fresh)],
                           daemon=True)
    out.start()                                 # pump 1: claude's 4 channels → council's skin
    why = "exit"
    try:
        if fresh:
            _answer_boot_dialog(bridge, renderer, cfg)  # trust dialog fires NO hook — scrape once
        if fresh and cfg.boot_probe:
            _boot_probe(bridge, state, renderer, cfg)   # D3: fail at boot, not mid-conversation
        why = _input_pump(bridge, renderer, state, cfg)  # pump 2: council's box → the hidden pane
    finally:
        if why == "detach":
            record(code_session("detach"))
            renderer.console.print(f"\n[bold]⚖ detached[/] — hidden claude keeps running · "
                                   f"[dim]council attach {bridge.name}[/] to return")
        else:
            kill_session(bridge)                # never leave a hidden claude running on EXIT
            record(code_session("end"))
            renderer.console.print("\n[bold]⚖ council code session ended[/]")
