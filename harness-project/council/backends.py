"""council/backends.py — the two debate heads as plain functions.
↔ Debby agents/claude/config.yaml:41-64 + agents/gpt/config.yaml:49-72 (the ANSWER/CRITIQUE prompt)."""
from __future__ import annotations

import json
import subprocess
import threading

from .config import Config
from .ledger import record

HEAD_PROMPT = """\
You are one of two voices in a council. You are a thinking-and-writing responder.
You are dispatched in one of two modes (the message makes which clear):
- ANSWER   — given a question. Answer directly and well; be concrete; offer options w/ trade-offs.
- CRITIQUE — given your own last answer + the OTHER voice's answer. Name what it gets right, where
  it's weak/wrong/incomplete, then give your updated answer. Don't cave just to agree; don't dig in
  from pride — converge toward what's correct.
Return a clear, self-contained response. You have NO tools — reason in text only.
"""


class Cancelled(Exception):
    """A head deliberately killed by the user's ^C — a cancellation, not a failure.
    _safe records it as head_call cancelled=True; /report excludes it from the failure rate."""


_INFLIGHT: set[subprocess.Popen] = set()
_KILLED: set[subprocess.Popen] = set()   # marked by kill_inflight so _run can tell ^C from a crash
_ILOCK = threading.Lock()


def kill_inflight() -> None:
    """^C during a duel: the interrupt lands in the MAIN thread (the spinner loop) while the
    heads run in pool workers. Mark + kill every live head here; each worker's communicate()
    then returns and its _run raises Cancelled — so the pool drains instead of deadlocking
    on shutdown, waiting for subprocesses nobody would ever reap."""
    with _ILOCK:
        procs = list(_INFLIGHT)
        _KILLED.update(procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def _classify(exc: Exception) -> str:
    """transient = the world was flaky (a retry may help); permanent = WE are wrong
    (bad flag, bad auth — fail fast, retrying is just failing slowly). _run folds the
    stderr tail into the RuntimeError message, so text-matching str(exc) sees it.
    A timeout counts as transient: retrying costs another head_timeout wait, but a
    stalled network call is exactly the failure a second try tends to clear."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "transient"
    msg = str(exc).lower()
    if any(m in msg for m in ("429", "rate limit", "quota", "overloaded",
                              "529", "503", "connection", "timed out")):
        return "transient"
    return "permanent"


def proposer(message: str, cfg: Config) -> str:
    """Claude head — the REAL `claude` CLI, headless, NO tools.
    Prompt goes via STDIN: `--allowedTools` is variadic and eats a trailing positional
    prompt as a tool name (live-verified vs claude 2.1.200, 4 Jul 2026).
    `--output-format json` → per-call cost lands in the ledger (fields `result` +
    `total_cost_usd`, live-verified 5 Jul 2026); a parse failure falls back to raw text —
    cost capture must NEVER kill the head."""
    argv = [cfg.claude_command, "-p", "--output-format", "json", "--allowedTools", ""]
    if cfg.claude_model:
        argv += ["--model", cfg.claude_model]        # /model claude <name> — shipped verbatim
    raw = _run(argv, cfg, stdin=HEAD_PROMPT + "\n\n" + message)
    try:
        payload = json.loads(raw)
        usd = payload.get("total_cost_usd")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            record({"role": "head_cost", "head": "claude", "usd": float(usd)})
        return str(payload["result"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw


def adversary(message: str, cfg: Config) -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.
    Why codex (not an openai-agents SDK): an unpinned model silently falls back to the Databricks
    gateway; `codex exec` has no such fallback. /model·/effort ride as `-m` and
    `-c model_reasoning_effort=…` (unquoted — matches what codex receives from a shell user)."""
    argv = [cfg.codex_command, "exec", "--sandbox", "read-only"]
    if cfg.codex_model:
        argv += ["-m", cfg.codex_model]
    if cfg.codex_effort:
        argv += ["-c", f"model_reasoning_effort={cfg.codex_effort}"]
    return _run(argv + [HEAD_PROMPT + "\n\n" + message], cfg)


def _run(argv: list[str], cfg: Config, stdin: str = "") -> str:
    """One subprocess → its stdout. This IS council's whole 'executor'. Timeout so a hung head
    can't wedge the debate; the inflight registry so ^C can reach a head mid-flight. stdin is
    ALWAYS explicit (default: closed-empty) — an inherited terminal makes `codex exec` read
    "additional input from stdin" and fight run_loop for keystrokes (verified 4 Jul)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    with _ILOCK:
        _INFLIGHT.add(proc)
    try:
        out, err = proc.communicate(input=stdin, timeout=cfg.head_timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # KeyboardInterrupt = the SOLO path: the main thread is right here in communicate().
        # (Duel-path ^C lands in the spinner instead → kill_inflight marks + kills from there.)
        proc.kill()
        proc.communicate()               # reap; drains the pipes so kill can't leave a zombie
        raise
    finally:
        with _ILOCK:
            _INFLIGHT.discard(proc)
            killed = proc in _KILLED
            _KILLED.discard(proc)
    if killed:
        raise Cancelled(f"{argv[0]} killed by ^C")
    if proc.returncode != 0:             # stderr tail in the message → _safe's ledger row says WHY
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {(err or '').strip()[-300:]}")
    return out.strip()
