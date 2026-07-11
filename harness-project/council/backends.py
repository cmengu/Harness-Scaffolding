"""council/backends.py — the two debate heads as plain functions.
↔ Debby agents/claude/config.yaml:41-64 + agents/gpt/config.yaml:49-72 (the ANSWER/CRITIQUE prompt)."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass

from .config import Config
from .ledger import record

_HEAD_PROMPT_BASE = """\
You are one of two voices in a council. You are a thinking-and-writing responder.
You are dispatched in one of two modes (the message makes which clear):
- ANSWER   — given a question. Answer directly and well; be concrete; offer options w/ trade-offs.
- CRITIQUE — given your own last answer + the OTHER voice's answer. Name what it gets right, where
  it's weak/wrong/incomplete, then give your updated answer. Don't cave just to agree; don't dig in
  from pride — converge toward what's correct.
Return a clear, self-contained response. """


def _head_prompt(tools: bool) -> str:
    return _HEAD_PROMPT_BASE + (
        "You have read-only research tools (files, web search) — use them when the question "
        "genuinely benefits from checked facts, not reflexively.\n"
        if tools else "You have NO tools — reason in text only.\n")


HEAD_PROMPT = _head_prompt(False)   # the classic prompt; kept for callers that predate depth


@dataclass
class HeadSessions:
    """One duel conversation's native head memory (probes 11 Jul: docs/probes-2026-07-11.md).
    claude = the `--session-id`/`--resume` uuid; codex = the `thread_id` from `exec --json`.
    None = not yet minted (next call seeds and captures). Owned by DebateRenderer; cleared on
    disarm / /new / /switch / /fork, and per-head by _safe when a head fails PERMANENTLY —
    the next duel reseeds from the ledger instead of resuming a corpse."""
    claude: str | None = None
    codex: str | None = None

    def clear(self, head: str) -> None:
        if head in ("claude", "codex"):
            setattr(self, head, None)


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


def proposer(message: str, cfg: Config, *, session: HeadSessions | None = None,
             thinking: int = 0, tools: bool = False) -> str:
    """Claude head — the REAL `claude` CLI, headless.
    Prompt goes via STDIN: `--allowedTools` is variadic and eats a trailing positional
    prompt as a tool name (live-verified vs claude 2.1.200, 4 Jul 2026).
    `--output-format json` → per-call cost lands in the ledger (fields `result` +
    `total_cost_usd`, live-verified 5 Jul 2026); a parse failure falls back to raw text —
    cost capture must NEVER kill the head.
    With a session: first call mints `--session-id`, later calls `--resume` — the head
    then remembers its own earlier rounds (incl. tool-derived facts) and resumed turns
    ride the cache at ~5x lower cost (probes 11 Jul).
    Depth: `thinking` rides the MAX_THINKING_TOKENS env (probes 11 Jul; the text stays
    redacted headless — only the token count comes back); `tools` opens cfg.claude_tools
    (read-only research set, no Bash) — headless -p auto-approves allowlisted tools."""
    argv = [cfg.claude_command, "-p", "--output-format", "json",
            "--allowedTools", cfg.claude_tools if tools else ""]
    if session is not None:
        argv += ["--resume", session.claude] if session.claude \
            else ["--session-id", str(uuid.uuid4())]
    if cfg.claude_model:
        argv += ["--model", cfg.claude_model]        # /model claude <name> — shipped verbatim
    env = {"MAX_THINKING_TOKENS": str(thinking)} if thinking > 0 else None
    raw = _run(argv, cfg, stdin=_head_prompt(tools) + "\n\n" + message, env=env)
    try:
        payload = json.loads(raw)
        usd = payload.get("total_cost_usd")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            record({"role": "head_cost", "head": "claude", "usd": float(usd)})
        if session is not None and payload.get("session_id"):
            session.claude = str(payload["session_id"])   # authoritative id (mint OR resume)
        return str(payload["result"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw


def adversary(message: str, cfg: Config, *, session: HeadSessions | None = None,
              effort: str | None = None, tools: bool = False) -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.
    Why codex (not an openai-agents SDK): an unpinned model silently falls back to the Databricks
    gateway; `codex exec` has no such fallback. /model·/effort ride as `-m` and
    `-c model_reasoning_effort=…` (unquoted — matches what codex receives from a shell user).
    With a session: `--json` events expose the `thread_id` (thread.started) that
    `exec resume <id>` continues; `--skip-git-repo-check` because a duel can be armed in
    any cwd, not just a trusted git repo (probes 11 Jul). Sessionless stays plain-stdout.
    Depth: `effort` overrides cfg.codex_effort per call (the duel arms high); `tools`
    adds live web search (`tools.web_search=true` — the `--search` flag is interactive-only;
    file reading is already native to codex's read-only sandbox)."""
    effort = effort or cfg.codex_effort
    depth_argv = (["-c", f"model_reasoning_effort={effort}"] if effort else []) \
        + (["-c", "tools.web_search=true"] if tools else [])
    if session is not None:
        if session.codex:
            argv = [cfg.codex_command, "exec", "resume", session.codex,
                    "--json", "--skip-git-repo-check"]
        else:
            argv = [cfg.codex_command, "exec", "--json", "--sandbox", "read-only",
                    "--skip-git-repo-check"]
            if cfg.codex_model:
                argv += ["-m", cfg.codex_model]      # model is fixed at mint; resume inherits it
        return _codex_events(
            _run(argv + depth_argv + [_head_prompt(tools) + "\n\n" + message], cfg), session)
    argv = [cfg.codex_command, "exec", "--sandbox", "read-only"]
    if cfg.codex_model:
        argv += ["-m", cfg.codex_model]
    return _run(argv + depth_argv + [_head_prompt(tools) + "\n\n" + message], cfg)


def _codex_events(raw: str, session: HeadSessions) -> str:
    """`exec --json` JSONL → the answer text, capturing thread_id + token usage on the way.
    Schema (probes 11 Jul): thread.started{thread_id} · item.completed{item:{type,text}} ·
    turn.completed{usage}. Codex reports tokens, never dollars — head_cost carries `tokens`.
    Anything unparseable falls through to raw text: session capture must never kill the head."""
    texts, usage = [], None
    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "thread.started" and e.get("thread_id"):
            session.codex = str(e["thread_id"])
        item = e.get("item") or {}
        if e.get("type") == "item.completed" and item.get("type") == "agent_message":
            texts.append(str(item.get("text", "")))
        if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
            usage = e["usage"]
    if usage:
        record({"role": "head_cost", "head": "codex", "tokens": usage})
    return "\n\n".join(t for t in texts if t).strip() or raw


def _run(argv: list[str], cfg: Config, stdin: str = "", env: dict | None = None) -> str:
    """One subprocess → its stdout. This IS council's whole 'executor'. Timeout so a hung head
    can't wedge the debate; the inflight registry so ^C can reach a head mid-flight. stdin is
    ALWAYS explicit (default: closed-empty) — an inherited terminal makes `codex exec` read
    "additional input from stdin" and fight run_loop for keystrokes (verified 4 Jul).
    `env` = EXTRA vars layered over the inherited environment (depth pack: MAX_THINKING_TOKENS)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ, **env} if env else None)
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
