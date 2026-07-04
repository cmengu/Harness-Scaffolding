"""council/wrap/harness_status.py — the PreToolUse policy gate, run as a Claude hook.

↔ omnigent native_policy_hook.py (:91 payload→request, :171 verdict→output, :276 fail-closed)
  + claude_native_hook.py dispatch. BIG DROP: post_evaluate_with_retry — council evaluates
  IN-PROCESS via G5 policy.evaluate; there is no server round-trip left to fail.

DIVERGENCE from omnigent (deliberate, per manuscript): omnigent resolved ASK server-side and
mapped a stray ASK → deny. Council HAS no server — ASK maps to permissionDecision "ask",
which pops Claude Code's native y/n prompt (schema verified vs claude 2.1.199, 3 Jul 2026).

COEXISTENCE: STACKS with the-harness's own PreToolUse commit-gate — claude runs both;
council's gate = blast radius, the-harness's = git commits.
"""
from __future__ import annotations

import json
import sys

from ..policy import evaluate            # G5 seam — stdlib-only (re + shlex), safe in a hook


def pre_tool_use_gate(payload: dict) -> dict | None:
    """Claude PreToolUse payload → hook output JSON (None = no opinion → claude's own gate runs).

    ALLOW → None: emitting "allow" would auto-approve the tool and silence the user's
    own consent prompt, collapsing two independent gates into one. FAIL CLOSED (deny)
    on any internal error — a hook must never crash claude's loop OR silently pass."""
    try:
        event = {"type": "tool_call",
                 "data": {"name": payload.get("tool_name", ""),
                          "arguments": payload.get("tool_input") or {}}}
        verdict = evaluate(event)
        result = verdict.get("result", "ALLOW")
        if result == "ALLOW":
            return None
        decision = {"DENY": "deny", "ASK": "ask"}.get(result, "deny")   # unknown verdict → closed
        output = {"hookEventName": "PreToolUse", "permissionDecision": decision}
        reason = verdict.get("reason")
        if reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}
    except Exception as exc:                                            # noqa: BLE001
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"council policy gate errored (fail closed): {exc}",
        }}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    output = pre_tool_use_gate(payload if isinstance(payload, dict) else {})
    if output is not None:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":   # claude invokes: python -m council.wrap.harness_status <bridge>
    raise SystemExit(main())    # (bridge arg accepted-and-ignored: the gate needs no mailbox)
