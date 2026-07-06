"""council/wrap/harness_status.py — the PreToolUse/PostToolUse policy gate, run as a Claude hook.

↔ omnigent native_policy_hook.py (:91 payload→request, :171 verdict→output, :276 fail-closed)
  + claude_native_hook.py dispatch. BIG DROP: post_evaluate_with_retry — council evaluates
  IN-PROCESS via G5 policy.evaluate; there is no server round-trip left to fail.

DIVERGENCE from omnigent (deliberate, per manuscript): omnigent resolved ASK server-side and
mapped a stray ASK → deny. Council HAS no server — ASK maps to permissionDecision "ask",
which pops Claude Code's native y/n prompt (schema verified vs claude 2.1.199, 3 Jul 2026).

COEXISTENCE: STACKS with the-harness's own PreToolUse commit-gate — claude runs both;
council's gate = blast radius, the-harness's = git commits.

APPROVALS MEMORY (session-scoped "don't ask me again"): a fresh process per hook call means
nothing survives in memory between one ASK and the next, so state goes to disk — one
append-only JSONL in the bridge dir, APPROVALS_FILE, folded on every read (never
read-modify-write; concurrent hook calls only ever single-os.write-append, ↔ state_hook.py):
  {"event": "pending",  "key", "call", "ts"}  — an ASK just fired, awaiting evidence it was OK'd
  {"event": "approved", "key", "ts"}          — the gated tool RAN ⇒ the user answered yes
  {"event": "auto",     "key", "ts"}          — a remembered key silently withdrew a later ask
TWO-GATE INVARIANT still holds with memory in the loop: a remembered key withdraws only OUR
ask (pre_tool_use_gate returns None) — Claude Code's own y/n prompt still runs underneath.
Memory never auto-executes anything; it only stops council from asking twice for the
identical thing.
POSTTOOLUSE = APPROVAL EVIDENCE: council cannot observe the user's answer to claude's own
prompt directly, so it infers consent from the effect — PreToolUse ASK'd, and the SAME call
(by _call_key) went on to reach PostToolUse ⇒ claude's gate let it through ⇒ approved. A
DENIED call never reaches PostToolUse, so its pending row is simply left to go stale.
↔ omnigent repl/_repl.py:733 _ApprovalState — same "approve once, remember" idea, but keyed
FINER than omnigent's (policy_name, phase): council keys on the exact normalized command, so
approving one `rm -rf build` doesn't silence every future blast-radius ask.
SESSION-SCOPED, NOT PERSISTED: approvals.jsonl lives in the bridge dir, which dies with the
session — there is no config knob to disable this because there is nothing to disable.

BUDGET GATE (independent of approvals memory; lives here for locality, not mechanism): cfg.
code_budget_usd carves the RUNNING session cost (context.json, written by the statusLine
wrapper — state.read_context) into a checkpoint ladder (1x, 2x, 3x budget, ...). Crossing a
NEW checkpoint always asks once; approving that checkpoint is remembered exactly like a cmd
approval. Message shape ↔ omnigent's cost popup (bridge.py:2602).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from math import floor
from pathlib import Path

from ..config import load_config            # stdlib tomllib/dataclasses — safe in a hook
from ..policy import evaluate                # G5 seam — stdlib-only (re + shlex), safe in a hook
from .state import read_context              # stdlib-only reader of context.json

APPROVALS_FILE = "approvals.jsonl"           # bridge-dir-relative; dies with the session
_STALE_PENDING_S = 600.0                     # an ask nobody answered in 10min is abandoned


# ── approvals log: fold-on-read, single-write-append (↔ state_hook.py's O_APPEND pattern) ──

def _call_key(payload: dict) -> str:
    """Identify ONE exact tool call across Pre→PostToolUse: name + a hash of its input.
    Hashed (not embedded raw) so approvals.jsonl rows stay short and fixed-shape."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    digest = hashlib.sha1(json.dumps(tool_input, sort_keys=True, default=str).encode()).hexdigest()
    return f"{tool_name}:{digest[:16]}"


def _cmd_key(command: str) -> str:
    """Whitespace-normalized exact command, as an approvals key.

    Deliberately FINER than omnigent's (policy_name, phase) key (↔ omnigent
    repl/_repl.py:733 _ApprovalState): approving one `rm -rf build` must not silence
    every future blast-radius ask — only a re-run of this exact command.
    """
    return "cmd:" + " ".join(command.split())


def _read_approvals(bridge: Path) -> tuple[set[str], list[dict]]:
    """Fold approvals.jsonl into (approved keys, still-pending rows). Tolerates a missing
    file and malformed lines (skipped, never raised). A pending row older than
    _STALE_PENDING_S is dropped during the fold — an ask nobody ever answered must not
    gate the identical thing forever."""
    approved: set[str] = set()
    pending: dict[str, dict] = {}
    now = time.time()
    try:
        text = (bridge / APPROVALS_FILE).read_text()
    except OSError:
        return approved, []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str):
            continue
        event = row.get("event")
        if event in ("approved", "auto"):
            approved.add(key)
            pending.pop(key, None)
        elif event == "pending":
            ts = row.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and now - ts <= _STALE_PENDING_S:
                pending[key] = row
            else:
                pending.pop(key, None)
    return approved, list(pending.values())


def _append_approval(bridge: Path, row: dict) -> None:
    """Single-os.write append, copied verbatim from state_hook.py: hooks can run
    CONCURRENTLY (parallel tool calls), so every writer must be exactly one atomic
    write of one line — never read-modify-write."""
    marker = {"ts": time.time(), **row}
    fd = os.open(str(bridge / APPROVALS_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(marker) + "\n").encode())
    finally:
        os.close(fd)


def _pre_tool_output(decision: str, reason: str | None) -> dict:
    """The hookSpecificOutput shape shared by every ask/deny exit of pre_tool_use_gate."""
    output = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        output["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": output}


# ── PreToolUse ───────────────────────────────────────────────────────────────

def pre_tool_use_gate(payload: dict, bridge: Path | None) -> dict | None:
    """Claude PreToolUse payload → hook output JSON (None = no opinion → claude's own gate runs).

    ALLOW → None: emitting "allow" would auto-approve the tool and silence the user's own
    consent prompt, collapsing two independent gates into one. FAIL CLOSED (deny) on any
    internal error, and on any verdict this file doesn't recognize — a hook must never
    crash claude's loop OR silently pass.

    Order, worst wins: blast-radius verdict (DENY short-circuits here, unmaskable by
    anything below) → cost-budget ladder (fires on ANY non-denied call, bridge-scoped) →
    cmd-approval memory (only ever modifies an ASK verdict, bridge-scoped).
    """
    try:
        tool_input = payload.get("tool_input")
        arguments = tool_input if isinstance(tool_input, dict) else {}
        event = {"type": "tool_call",
                 "data": {"name": payload.get("tool_name", ""), "arguments": arguments}}
        verdict = evaluate(event)
        result = verdict.get("result", "ALLOW")
        if result not in ("ALLOW", "ASK"):      # DENY, or anything unrecognized → fail closed
            return _pre_tool_output("deny", verdict.get("reason"))

        if bridge is not None:
            cfg = load_config()
            budget = cfg.code_budget_usd
            if budget > 0:
                cost = read_context(bridge).get("total_cost_usd")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= budget:
                    n = max(1, floor(cost / budget))
                    key = f"budget-{n}"
                    approved, _pending = _read_approvals(bridge)
                    if key not in approved:
                        _append_approval(bridge, {"event": "pending", "key": key,
                                                   "call": _call_key(payload)})
                        reason = (f"session cost ${cost:.2f} crossed checkpoint #{n} "
                                  f"(${n * budget:.2f}) — approve to continue past it")
                        return _pre_tool_output("ask", reason)
                    # already approved: fall through WITHOUT an "auto" row — budget re-checks
                    # every call once past a checkpoint, so logging each would spam for no
                    # new information.

        if result == "ASK":
            command = arguments.get("command")
            if isinstance(command, str):
                key = _cmd_key(command)
                if bridge is not None:
                    cmd_approved, _pending = _read_approvals(bridge)
                    if key in cmd_approved:
                        _append_approval(bridge, {"event": "auto", "key": key})
                        return None    # withdraw OUR gate; claude's own consent still applies
                    _append_approval(bridge, {"event": "pending", "key": key,
                                               "call": _call_key(payload)})
                reason = verdict.get("reason") or ""
                suffix = " — approving once silences this exact command for this session"
                return _pre_tool_output("ask", f"{reason}{suffix}")
            return _pre_tool_output("ask", verdict.get("reason"))   # no command string: no memory

        return None      # ALLOW
    except Exception as exc:                                            # noqa: BLE001
        return _pre_tool_output("deny", f"council policy gate errored (fail closed): {exc}")


# ── PostToolUse ──────────────────────────────────────────────────────────────

def post_tool_use_note(payload: dict, bridge: Path | None) -> int:
    """PostToolUse: promote any pending ASK whose call matches this one to 'approved'.

    INFERENCE: council can't see the user's answer to claude's OWN y/n prompt directly —
    it infers a yes from the effect. The gated tool RAN (this hook fired) ⇒ claude's gate
    let it through ⇒ the user approved. A DENIED call never reaches PostToolUse, so its
    pending row is simply left to go stale (dropped by _read_approvals after 600s).

    MUST NEVER EMIT STDOUT, even on error: PostToolUse output can inject text back into
    claude's own context, and this hook has nothing worth saying to the model — only
    worth recording to disk. Every path returns 0.
    """
    try:
        if bridge is None:
            return 0
        call = _call_key(payload)
        approved, pending = _read_approvals(bridge)
        for row in pending:
            key = row.get("key")
            if isinstance(key, str) and key not in approved and row.get("call") == call:
                _append_approval(bridge, {"event": "approved", "key": key})
        return 0
    except Exception:                                                    # noqa: BLE001
        return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    bridge = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if payload.get("hook_event_name") == "PostToolUse":
        post_tool_use_note(payload, bridge)     # never raises, never prints — see docstring
        return 0
    # "PreToolUse", or a missing/unknown event name (back-compat) → the same gate as always.
    output = pre_tool_use_gate(payload, bridge)
    if output is not None:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":   # claude invokes: python -m council.wrap.harness_status <bridge>
    raise SystemExit(main())    # bridge is now READ (approvals memory + budget context)
