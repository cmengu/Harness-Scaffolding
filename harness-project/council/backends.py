"""council/backends.py — the two debate heads as plain functions.
↔ Debby agents/claude/config.yaml:41-64 + agents/gpt/config.yaml:49-72 (the ANSWER/CRITIQUE prompt)."""
from __future__ import annotations

import subprocess

from .config import Config

HEAD_PROMPT = """\
You are one of two voices in a council. You are a thinking-and-writing responder.
You are dispatched in one of two modes (the message makes which clear):
- ANSWER   — given a question. Answer directly and well; be concrete; offer options w/ trade-offs.
- CRITIQUE — given your own last answer + the OTHER voice's answer. Name what it gets right, where
  it's weak/wrong/incomplete, then give your updated answer. Don't cave just to agree; don't dig in
  from pride — converge toward what's correct.
Return a clear, self-contained response. You have NO tools — reason in text only.
"""


def proposer(message: str, cfg: Config) -> str:
    """Claude head — the REAL `claude` CLI, headless, NO tools.
    Prompt goes via STDIN: `--allowedTools` is variadic and eats a trailing positional
    prompt as a tool name (live-verified vs claude 2.1.200, 4 Jul 2026)."""
    return _run([cfg.claude_command, "-p", "--allowedTools", ""], cfg,
                stdin=HEAD_PROMPT + "\n\n" + message)


def adversary(message: str, cfg: Config) -> str:
    """Codex head — `codex exec`, headless, read-only sandbox.
    Why codex (not an openai-agents SDK): an unpinned model silently falls back to the Databricks
    gateway; `codex exec` has no such fallback."""
    return _run([cfg.codex_command, "exec", "--sandbox", "read-only",
                 HEAD_PROMPT + "\n\n" + message], cfg)


def _run(argv: list[str], cfg: Config, stdin: str = "") -> str:
    """One subprocess → its stdout. This IS council's whole 'executor'. Timeout so a hung head can't
    wedge the debate. stdin is ALWAYS explicit (default: closed-empty) — an inherited terminal makes
    `codex exec` read "additional input from stdin" and fight run_loop for keystrokes (verified 4 Jul)."""
    return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                          check=True, timeout=cfg.head_timeout).stdout.strip()
