"""council/wrap/render.py — the LOCAL painter: hidden claude's events in council's skin.

The manuscript sketched the MessageDisplay hook here too; it lives in display_hook.py
instead (stdlib-only — claude blocks on it per streamed chunk; this module imports rich).

v1 rendering is deliberately simple (manuscript: "accept v1 jank; plan a Rich Live layout
with pinned input row later"): deltas stream raw to stdout; the authoritative transcript
final is used for the LEDGER (exact text) but not re-printed when it was already streamed —
reconciled POSITIONALLY (FIFO), since message_id never appears in the transcript.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from ..config import Config
from ..ledger import record


class Renderer:
    def __init__(self, cfg: Config, bridge: Path, replaying: bool = False):
        self.cfg, self.bridge = cfg, bridge
        self.console = Console(highlight=False)
        self.replaying = replaying              # attach: repainting history — dim, NO ledger
        self._streaming_id: str | None = None   # message_id currently streaming to stdout
        self._streamed_finals = 0               # deltas-side finals seen (FIFO reconcile)
        self._transcript_finals = 0             # transcript-side assistant texts seen

    # ── input side ────────────────────────────────────────────────────────────
    def read_input(self) -> str:
        return self.console.input("[bold blue]⚖ ›[/] ").strip()

    def notice(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/]")

    def error(self, text: str) -> None:
        self.console.print(f"[red]{text}[/]")

    # ── output side ───────────────────────────────────────────────────────────
    def handle(self, event: tuple) -> None:
        kind, payload = event
        if kind == "delta":
            self._handle_delta(payload)
        elif kind == "item":
            self._handle_item(payload)
        elif kind == "context":
            self._handle_context(payload)
        elif kind == "approval":
            self._handle_approval(payload)
        elif kind == "live":
            self._go_live()

    def _go_live(self) -> None:
        """The events pump caught the replayed transcript up to its attach-time size:
        stop replaying — history was PAINTED, not re-lived. _streamed_finals aligns to
        the transcript count so the FIFO delta-reconcile starts even, and everything
        from here records to the ledger as normal."""
        if not self.replaying:
            return
        self.replaying = False
        self._streamed_finals = self._transcript_finals
        self.console.print("[dim]— caught up · live —[/]")

    def _handle_delta(self, delta) -> None:
        if self.replaying:
            return                              # attach skips delta history at the offset
        if delta.message_id != self._streaming_id:
            self._streaming_id = delta.message_id
            sys.stdout.write("\n\033[38;5;208m🟠 \033[0m")      # new assistant message
        sys.stdout.write(delta.delta)
        sys.stdout.flush()
        if delta.final:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._streaming_id = None
            self._streamed_finals += 1

    def _handle_item(self, item: dict) -> None:
        kind = item["kind"]
        if self.replaying:                      # attach history repaint: dim, and NEVER
            self._replay_item(item)             # re-recorded (the original run already
            return                              # ledgered these rows)
        if kind == "assistant_text":
            self._transcript_finals += 1
            record({"role": "code_assistant", "text": item["text"]})
            if self._transcript_finals > self._streamed_finals:
                # No delta stream covered this message (MessageDisplay hook missing/off) —
                # the transcript is authoritative, so paint it now rather than drop it.
                self._streamed_finals = self._transcript_finals
                self.console.print(f"\n[orange1]🟠[/] {item['text']}")
        elif kind == "user_text":
            record({"role": "code_user", "text": item["text"]})
        elif kind == "tool_use":
            summary = _tool_summary(item)
            record({"role": "code_tool", "name": item["name"], "summary": summary})
            self.console.print(f"[dim]⚙ {item['name']}  {summary}[/]")

    def _replay_item(self, item: dict) -> None:
        """History repaint. User turns are painted here (live mode never paints them — the
        user just typed them), assistant turns count into _transcript_finals so _go_live's
        FIFO alignment is exact."""
        kind = item["kind"]
        if kind == "assistant_text":
            self._transcript_finals += 1
            self.console.print(f"\n[dim]🟠 {item['text']}[/]")
        elif kind == "user_text":
            self.console.print(f"\n[dim]› {item['text']}[/]")
        elif kind == "tool_use":
            self.console.print(f"[dim]⚙ {item['name']}  {_tool_summary(item)}[/]")

    def _handle_context(self, context: dict) -> None:
        record({"role": "code_context", **context})
        cost = context.get("total_cost_usd")
        model = context.get("model")
        pct = context.get("used_percentage")
        parts = [p for p in (
            model,
            f"${cost:.2f}" if isinstance(cost, (int, float)) else None,
            f"ctx {pct:.0f}%" if isinstance(pct, (int, float)) else None,
        ) if p]
        if parts:
            self.console.print(f"[dim]· {'  ·  '.join(parts)}[/]")

    def _handle_approval(self, row: dict) -> None:
        """Council-side visibility for the hook's decisions — and the ONE place they reach
        the ledger (the hook process has its own random RUN_ID, so it never writes)."""
        record({"role": "code_approval", **row})
        key = str(row.get("key", ""))
        label = key.removeprefix("cmd:")
        if key.startswith("budget-"):
            label = f"budget checkpoint #{key.removeprefix('budget-')}"
        verb = "approved for this session" if row.get("event") == "approved" else "auto-allowed (remembered)"
        self.console.print(f"[dim]⚑ {verb}: {label}[/]")


def _tool_summary(item: dict) -> str:
    """One dim line per tool call: the most informative single input value, truncated."""
    tool_input = item.get("input") or {}
    for key in ("command", "file_path", "pattern", "prompt", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            value = " ".join(value.split())
            return value[:100] + ("…" if len(value) > 100 else "")
    return ""
