# reference/omnigent — vendored read-only source

This folder is a **read-only copy of the omnigent files that council is derived from**, so
that every `↔ <file>:<line>` citation in `../../groups.py` points at a file you can actually
open. Nothing here is council's code — it is the **reference implementation** we copy *from*.

- **Upstream:** github.com/omnigent-ai/omnigent (Databricks). License: **Apache-2.0** (see `LICENSE`/`NOTICE`).
- **Snapshot:** shallow clone taken 2026-06-29. omnigent moves fast, so **line numbers track THIS
  snapshot.** `groups.py`'s G1 notes were written against an earlier clone and drift by a few lines;
  corrected G1 anchors live in `MAPPING.md`. The G2/G3/G5 anchors were verified against this snapshot.
- **Do not edit.** Treat as a frozen reference. If you don't want ~30k lines of third-party code in
  your own git history, add `reference/omnigent/` to `.gitignore` (the copy is reproducible by
  re-cloning omnigent).

**Start with `MAPPING.md`** — the matching map of what council takes from each file and how it applies.

## What's here (lines : path)
```
  examples/debby/                          G2 — DEBATE source (the whole example)
  omnigent/cli.py            (12,620)      G1 — FRONT (the 3 commands)
  omnigent/chat.py            (4,156)      G1 — the repl loop
  omnigent/repl/_repl.py      (8,294)      G1 — the banner
  omnigent/claude_native.py   (4,404)      G3 — launch the real claude in tmux
  omnigent/claude_native_bridge.py (4,789) G3 — tmux inject + transcript read + hook settings
  omnigent/claude_native_forwarder.py (4,183) G3 — the read loop (council drops the POST half)
  omnigent/claude_native_hook.py (1,002)   G3 — the policy/observer hook
  omnigent/native_policy_hook.py (445)     G3 — policy payload<->hook-output translation
  omnigent/claude_native_state.py (279)    G3 — launch-cwd persistence (resume)
  omnigent/claude_native_status.py (165)   G3 — the statusLine cost/model capture
  omnigent/claude_native_message_display_hook.py (144) G3 — the per-token-chunk delta hook
  omnigent/inner/claude_native_executor.py (250) G3 — what a "turn" is (just inject text)
  omnigent/inner/claude_native_harness.py (30)   G3 — the FastAPI server harness (council DROPS)
  omnigent/runner/pending_approvals.py (207)     G3 — server-side approvals queue (mostly DROP)
  omnigent/inner/nessie/policies.py (604)  G5 — blast_radius / spawn_bounds (next group)
```
