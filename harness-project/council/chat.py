"""council/chat.py — ONE turn-based loop, pluggable renderer per mode.
↔ omnigent chat.py:3805 (_run_repl), MINUS the SDK-event adapter — council swaps it for a Renderer."""
from __future__ import annotations

from typing import Protocol

from rich.console import Console

from .config import Config
from .ledger import record


class Renderer(Protocol):
    """One method. THINK → debate columns (G2) · REVIEW → codex output (G6).
       (CODE is the exception — it owns its own live loop; see G1 note.)"""
    def handle(self, user_input: str) -> None: ...


def run_loop(renderer: Renderer, cfg: Config, console: Console) -> None:
    """Read → record → dispatch → render, until exit."""
    record({"role": "session_start"})        # memory boundary: the history preamble (G2) only reads
    while True:                              # ledger rows AFTER the latest session_start
        try:
            text = console.input("[bold blue]›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("/exit", "/quit", "exit", "quit"):
            break
        if text.startswith("/"):
            _slash(text, renderer, console)  # slash commands may mutate renderer state (/duel)
            continue
        if not text:
            continue
        record({"role": "user", "text": text})
        renderer.handle(text)


def _slash(text: str, renderer, console: Console) -> None:
    """The 'fake toggle': /duel just flips a boolean on the renderer — no UI widget, same trick
    Claude Code's own toggles use. TIMING IS FREE: handle() blocks until the turn is done, so a
    toggle can never interrupt a turn in flight — it takes effect on the NEXT question."""
    if text == "/help":
        console.print("/duel [on|off] — toggle the codex adversary  |  /new  /exit")
    elif text == "/new":
        record({"role": "session_start"})    # fresh memory boundary, same process
        console.print("— new session — history preamble reset —")
    elif text.startswith("/duel"):
        import shutil
        arg = (text.split()[1:] or ["toggle"])[0]
        renderer.adversarial = {"on": True, "off": False}.get(arg, not renderer.adversarial)
        if renderer.adversarial and not shutil.which(renderer.cfg.codex_command):
            renderer.adversarial = False     # fail loud, not a one-voiced "debate"
            console.print("[red]✗ codex not found — install @openai/codex first; staying solo[/]")
        else:
            console.print("⚔ adversary ON — codex will cross-examine every answer"
                          if renderer.adversarial else "adversary off — plain claude chat")
    else:
        console.print(f"[dim]unknown command {text!r} — try /help[/]")
