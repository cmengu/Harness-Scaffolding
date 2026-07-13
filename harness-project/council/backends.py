"""council/backends.py — the two debate heads as plain functions.
↔ Debby agents/claude/config.yaml:41-64 + agents/gpt/config.yaml:49-72 (the ANSWER/CRITIQUE prompt)."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass

from . import flight
from .config import Config
from .contract import schema_json, system_prompt_file
from .ledger import head_cost, record
from .pricing import codex_rate


def codex_usd(usage: dict, cfg: Config) -> float:
    """Price a codex turn locally: its CLI reports tokens, never dollars (probes 11 Jul),
    so folding codex into one dollar total means multiplying token usage by list rates. The
    rate comes from pricing.codex_rate(cfg.codex_model) — the per-model table — so /model
    codex <id> re-prices automatically; cfg.codex_pricing=False disables it (token-only).
    input_tokens is the whole prompt incl. cached; the cached slice bills at the discounted
    rate. reasoning tokens are already inside output_tokens (OpenAI billing), not re-added."""
    if not isinstance(usage, dict) or not cfg.codex_pricing:
        return 0.0
    (p_in, p_cached, p_out), _model, _exact = codex_rate(cfg.codex_model)
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or usage.get("cached_input")
                 or (usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    billed_input = max(0, inp - cached)
    return round(billed_input / 1e6 * p_in + cached / 1e6 * p_cached + out / 1e6 * p_out, 6)


def _record_codex_cost(usage: dict, cfg: Config) -> float:
    """The shared tail of both codex readers (block + stream): price the turn, write its
    head_cost row (raw tokens for truth, dollars for aggregation), pulse the context meter.
    Returns the usd so the streaming path can surface it in its live cost event."""
    usd = codex_usd(usage, cfg)
    record(head_cost("codex", usd=usd, tokens=usage))
    _context_beat("codex", usage)
    return usd


def _record_claude_cost(payload: dict) -> None:
    """Record a claude turn's billed cost from its `--output-format json` payload (claude reports
    dollars direct). The twin of _record_codex_cost; shared by proposer and trailer_retry so the
    cost-capture guard lives in one place. A missing/odd figure is simply skipped — cost capture
    must never kill the head."""
    usd = payload.get("total_cost_usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        record(head_cost("claude", usd=float(usd)))


def _context_beat(head: str, usage: dict | None) -> None:
    """Feed the flight panel's context meter from a call's usage block. Claude reports
    cache reads/writes SEPARATELY from input_tokens (API semantics); codex's input_tokens
    already includes its cached share. Wrong-shaped usage must never kill a head."""
    if not isinstance(usage, dict):
        return
    try:
        if head == "claude":
            used = sum(int(usage.get(k) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        else:
            used = int(usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        return
    if used > 0:
        flight.context_tokens(head, used)

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


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the head AND its children. codex spawns workers that inherit the stdout pipe:
    killing only the parent leaves readline blocked until the orphans exit (live-observed
    11 Jul: a '300s' timeout that returned after 389s). Heads start in their own session
    (start_new_session=True), so the process group id IS the head's pid."""
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, PermissionError):
        try:
            proc.kill()                      # group already gone (or not ours): plain kill
        except OSError:
            pass


def kill_inflight() -> None:
    """^C during a duel: the interrupt lands in the MAIN thread (the spinner loop) while the
    heads run in pool workers. Mark + kill every live head here; each worker's communicate()
    then returns and its _run raises Cancelled — so the pool drains instead of deadlocking
    on shutdown, waiting for subprocesses nobody would ever reap."""
    with _ILOCK:
        procs = list(_INFLIGHT)
        _KILLED.update(procs)
    for p in procs:
        _kill_tree(p)


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
             thinking: int = 0, tools: bool = False, contract: str = "") -> str:
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
    (read-only research set, no Bash) — headless -p auto-approves allowlisted tools.
    `contract`: the armed output contract, injected via --append-system-prompt-file (a tmp file,
    not argv — keeps it off `ps`); empty on solo/unarmed turns, which stay contract-free."""
    with system_prompt_file(contract) as spf:
        argv = [cfg.claude_command, "-p", "--output-format", "json",
                "--allowedTools", cfg.claude_tools if tools else ""]
        if spf:
            argv += ["--append-system-prompt-file", spf]
        if session is not None:
            argv += ["--resume", session.claude] if session.claude \
                else ["--session-id", str(uuid.uuid4())]
        if cfg.claude_model:
            argv += ["--model", cfg.claude_model]    # /model claude <name> — shipped verbatim
        env = {"MAX_THINKING_TOKENS": str(thinking)} if thinking > 0 else None
        raw = _run(argv, cfg, stdin=_head_prompt(tools) + "\n\n" + message, env=env)
    try:
        payload = json.loads(raw)
        _record_claude_cost(payload)
        _context_beat("claude", payload.get("usage"))
        if session is not None and payload.get("session_id"):
            session.claude = str(payload["session_id"])   # authoritative id (mint OR resume)
        return str(payload["result"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return raw


def adversary(message: str, cfg: Config, *, session: HeadSessions | None = None,
              effort: str | None = None, tools: bool = False, contract: str = "") -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.
    Why codex (not an openai-agents SDK): an unpinned model silently falls back to the Databricks
    gateway; `codex exec` has no such fallback. /model·/effort ride as `-m` and
    `-c model_reasoning_effort=…` (unquoted — matches what codex receives from a shell user).
    With a session: `--json` events expose the `thread_id` (thread.started) that
    `exec resume <id>` continues; `--skip-git-repo-check` because a duel can be armed in
    any cwd, not just a trusted git repo (probes 11 Jul). Sessionless stays plain-stdout.
    Depth: `effort` overrides cfg.codex_effort per call (the duel arms high); `tools`
    adds live web search (`tools.web_search=true` — the `--search` flag is interactive-only;
    file reading is already native to codex's read-only sandbox).
    `contract`: codex has no append-system-prompt flag (and writing AGENTS.md would pollute the
    user's repo), so the armed output contract is PREPENDED to the composed message instead."""
    effort = effort or cfg.codex_effort
    depth_argv = (["-c", f"model_reasoning_effort={effort}"] if effort else []) \
        + (["-c", "tools.web_search=true"] if tools else [])
    composed = (contract + "\n\n" if contract else "") + _head_prompt(tools) + "\n\n" + message
    if session is not None:
        if session.codex:
            argv = [cfg.codex_command, "exec", "resume", session.codex,
                    "--json", "--skip-git-repo-check"]
        else:
            argv = [cfg.codex_command, "exec", "--json", "--sandbox", "read-only",
                    "--skip-git-repo-check"]
            if cfg.codex_model:
                argv += ["-m", cfg.codex_model]      # model is fixed at mint; resume inherits it
        return _codex_events(_run(argv + depth_argv + [composed], cfg), session, cfg)
    argv = [cfg.codex_command, "exec", "--sandbox", "read-only"]
    if cfg.codex_model:
        argv += ["-m", cfg.codex_model]
    return _run(argv + depth_argv + [composed], cfg)


def _codex_events(raw: str, session: HeadSessions, cfg: Config) -> str:
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
        _record_codex_cost(usage, cfg)
    return "\n\n".join(t for t in texts if t).strip() or raw


_TRAILER_RETRY_MSG = (
    "Your previous answer's === TRAILER === block was missing or did not parse. Emit ONLY the "
    "corrected TRAILER JSON for the answer you just gave — a single JSON object, no prose, no "
    "markers, nothing before or after it.")


def trailer_retry(head: str, session: HeadSessions, cfg: Config, round_no: int) -> str:
    """The one cheap corrective retry (output-contract.md §enforcement): a follow-up on the
    SAME head session asking for trailer-only, with the native schema flag attached — since the
    whole retry output is now the trailer, the flag applies and the reply is shape-guaranteed
    (or errors cleanly). Resumed → cents-level. Returns the raw trailer text, or "" when there
    is no session to resume or the call fails — a retry that itself fails just means we degrade,
    so this must NEVER raise into the duel loop."""
    schema = schema_json(round_no)
    try:
        if head == "claude":
            if session is None or not session.claude:
                return ""
            argv = [cfg.claude_command, "-p", "--output-format", "json",
                    "--resume", session.claude, "--json-schema", schema]
            if cfg.claude_model:
                argv += ["--model", cfg.claude_model]
            raw = _run(argv, cfg, stdin=_TRAILER_RETRY_MSG)
            try:
                payload = json.loads(raw)
                _record_claude_cost(payload)
                return str(payload.get("result", raw)).strip()
            except (json.JSONDecodeError, KeyError, TypeError):
                return raw
        if session is None or not session.codex:
            return ""
        with system_prompt_file(schema) as spath:     # a throwaway 0600 file for --output-schema
            argv = [cfg.codex_command, "exec", "resume", session.codex,
                    "--json", "--skip-git-repo-check", "--output-schema", spath]
            return _codex_events(_run(argv + [_TRAILER_RETRY_MSG], cfg), session, cfg)
    except Exception:                                  # transport/exit failure → degrade, never die
        return ""


# ── the streaming pump (step 4) ─────────────────────────────────────────────────────
# Events are the product; renderers are guests (ROADMAP). Each head grows a streaming
# twin yielding plain dicts {head, kind, payload, ts} — JSONL-able, Rich-free — so the
# CLI tape (consumer #1) and the web SSE view (consumer #2) drink the same stream.
# kind: thinking | tool | text | final | cost  (+ retry | error added by _safe_stream).

def _ev(head: str, kind: str, payload) -> dict:
    return {"head": head, "kind": kind, "payload": payload, "ts": time.time()}


def proposer_stream(message: str, cfg: Config, *, session: HeadSessions | None = None,
                    thinking: int = 0, tools: bool = False, contract: str = ""):
    """Streaming twin of proposer: `--output-format stream-json --include-partial-messages`
    (REQUIRES --verbose under -p — probes 11 Jul). Claude's thinking TEXT arrives redacted
    headless; the pump still forwards the empty deltas (self-healing if a release lifts the
    redaction) and emits the authoritative token count from message_delta as the pulse.
    `contract`: same --append-system-prompt-file injection as the block twin."""
    with system_prompt_file(contract) as spf:
        argv = [cfg.claude_command, "-p", "--output-format", "stream-json",
                "--include-partial-messages", "--verbose",
                "--allowedTools", cfg.claude_tools if tools else ""]
        if spf:
            argv += ["--append-system-prompt-file", spf]
        if session is not None:
            argv += ["--resume", session.claude] if session.claude \
                else ["--session-id", str(uuid.uuid4())]
        if cfg.claude_model:
            argv += ["--model", cfg.claude_model]
        env = {"MAX_THINKING_TOKENS": str(thinking)} if thinking > 0 else None
        for line in _run_lines(argv, cfg, stdin=_head_prompt(tools) + "\n\n" + message, env=env):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from _claude_events(e, session)


def _claude_events(e: dict, session: HeadSessions | None):
    t = e.get("type")
    if t == "stream_event":
        ev = e.get("event") or {}
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                yield _ev("claude", "text", d["text"])
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                yield _ev("claude", "thinking", {"text": d["thinking"]})
        elif ev.get("type") == "message_delta":
            details = (ev.get("usage") or {}).get("output_tokens_details") or {}
            if details.get("thinking_tokens"):
                yield _ev("claude", "thinking", {"tokens": details["thinking_tokens"]})
    elif t == "assistant":       # tool calls ride whole assistant messages, not stream deltas
        for block in ((e.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield _ev("claude", "tool", {"name": block.get("name"),
                                             "input": str(block.get("input") or "")[:200]})
    elif t == "result" or (t is None and "result" in e):
        # t None = the block `--output-format json` shape; tolerated so an older CLI (or a
        # block-era stub) that answers a stream request in one JSON line still lands.
        if session is not None and e.get("session_id"):
            session.claude = str(e["session_id"])
        _context_beat("claude", e.get("usage"))
        usd = e.get("total_cost_usd")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            record(head_cost("claude", usd=float(usd)))
            yield _ev("claude", "cost", {"usd": float(usd)})
        yield _ev("claude", "final", str(e.get("result", "")).strip())


def adversary_stream(message: str, cfg: Config, *, session: HeadSessions | None = None,
                     effort: str | None = None, tools: bool = False, contract: str = ""):
    """Streaming twin of adversary: `exec --json` is already line-events on stdout.
    `model_reasoning_summary=detailed` makes codex's reasoning arrive as READABLE text
    items (probes 11 Jul) — the glass box's counterweight to claude's redacted thinking.
    `contract`: prepended to the composed message, same as the block twin."""
    effort = effort or cfg.codex_effort
    depth_argv = (["-c", f"model_reasoning_effort={effort}"] if effort else []) \
        + (["-c", "tools.web_search=true"] if tools else []) \
        + (["-c", "model_reasoning_summary=detailed"])
    composed = (contract + "\n\n" if contract else "") + _head_prompt(tools) + "\n\n" + message
    if session is not None and session.codex:
        argv = [cfg.codex_command, "exec", "resume", session.codex,
                "--json", "--skip-git-repo-check"]
    else:
        argv = [cfg.codex_command, "exec", "--json", "--sandbox", "read-only",
                "--skip-git-repo-check"]
        if cfg.codex_model:
            argv += ["-m", cfg.codex_model]
    plain, saw_final = [], False
    for line in _run_lines(argv + depth_argv + [composed], cfg):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            plain.append(line)               # a non-JSONL codex (or plain-stdout stub)
            continue
        for ev in _codex_stream_events(e, session, cfg):
            saw_final = saw_final or ev["kind"] == "final"
            yield ev
    if not saw_final and "".join(plain).strip():
        yield _ev("codex", "final", "".join(plain).strip())


def _codex_stream_events(e: dict, session: HeadSessions | None, cfg: Config):
    t = e.get("type")
    item = e.get("item") or {}
    if t == "thread.started" and e.get("thread_id") and session is not None:
        session.codex = str(e["thread_id"])
    elif t == "item.completed":
        kind = item.get("type")
        if kind == "reasoning" and item.get("text"):
            yield _ev("codex", "thinking", {"text": item["text"]})
        elif kind == "agent_message":
            yield _ev("codex", "final", str(item.get("text", "")).strip())
        elif kind in ("web_search", "command_execution", "file_read"):
            yield _ev("codex", "tool", {"name": kind,
                                        "input": str(item.get("query")
                                                     or item.get("command") or "")[:200]})
    elif t == "item.started" and item.get("type") == "web_search":
        yield _ev("codex", "tool", {"name": "web_search",
                                    "input": str(item.get("query") or "")[:200]})
    elif t == "turn.completed" and isinstance(e.get("usage"), dict):
        usd = _record_codex_cost(e["usage"], cfg)
        yield _ev("codex", "cost", {"usd": usd, "tokens": e["usage"]})
    elif t in ("turn.failed", "error"):
        raise RuntimeError(f"codex turn failed: {str(e)[:300]}")


def _run_lines(argv: list[str], cfg: Config, stdin: str = "", env: dict | None = None):
    """Streaming twin of _run: one subprocess → its stdout, line by line, as a generator.
    Same contracts: explicit stdin, inflight registry (^C reaches mid-flight heads), and a
    watchdog — an IDLE timeout, not wall-clock (11 Jul live finding: a deep research round
    is healthily streaming events at 295s; killing it at head_timeout threw away a whole
    report). A head is dead only after head_timeout seconds of SILENCE; every line resets
    the clock. The watchdog thread kills the whole process group (codex leaves orphans
    holding the pipe otherwise). stderr is read after EOF (heads keep it small — a >64KB
    flood would deadlock, accepted). Early generator close kills the child in the finally."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                            start_new_session=True,
                            env={**os.environ, **env} if env else None)
    with _ILOCK:
        _INFLIGHT.add(proc)
    last = [time.monotonic()]
    timed_out = threading.Event()

    def _watch() -> None:
        while proc.poll() is None:
            idle = time.monotonic() - last[0]
            if idle >= cfg.head_timeout:
                timed_out.set()
                _kill_tree(proc)
                return
            time.sleep(min(5.0, cfg.head_timeout - idle))

    watchdog = threading.Thread(target=_watch, daemon=True)
    watchdog.start()
    err = ""
    try:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass                              # a fast-dying head: the exit check below reports it
        for line in proc.stdout:
            last[0] = time.monotonic()
            yield line
        err = proc.stderr.read()
        proc.wait()
    finally:
        if proc.poll() is None:               # consumer closed us early — reap the child
            _kill_tree(proc)
            proc.wait()
        with _ILOCK:
            _INFLIGHT.discard(proc)
            killed = proc in _KILLED
            _KILLED.discard(proc)
    if killed:
        raise Cancelled(f"{argv[0]} killed by ^C")
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(argv, cfg.head_timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {(err or '').strip()[-300:]}")


def _run(argv: list[str], cfg: Config, stdin: str = "", env: dict | None = None) -> str:
    """One subprocess → its stdout. This IS council's whole 'executor'. Timeout so a hung head
    can't wedge the debate; the inflight registry so ^C can reach a head mid-flight. stdin is
    ALWAYS explicit (default: closed-empty) — an inherited terminal makes `codex exec` read
    "additional input from stdin" and fight run_loop for keystrokes (verified 4 Jul).
    `env` = EXTRA vars layered over the inherited environment (depth pack: MAX_THINKING_TOKENS)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                            start_new_session=True,
                            env={**os.environ, **env} if env else None)
    with _ILOCK:
        _INFLIGHT.add(proc)
    try:
        out, err = proc.communicate(input=stdin, timeout=cfg.head_timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # KeyboardInterrupt = the SOLO path: the main thread is right here in communicate().
        # (Duel-path ^C lands in the spinner instead → kill_inflight marks + kills from there.)
        _kill_tree(proc)
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
