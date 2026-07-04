# council

Think and code with a **cross-family second opinion**. `council` is a thin harness that
wraps the real Claude Code binary and can summon an OpenAI Codex adversary on demand —
Claude proposes, Codex critiques, you decide.

Two modes, one ledger:

| Command | What it does |
|---|---|
| `council ask` | **THINK** — chat with Claude; toggle `/duel` to make Codex challenge each answer (`claude -p` vs `codex exec`) |
| `council code` | **CODE** — a branded front over the *real* `claude` binary, hidden in tmux; your `~/.claude` hooks and settings stay live |

Every turn from both modes is appended to a single JSONL ledger, so debates and coding
sessions share one provenance trail.

## Install

```sh
pip install -e harness-project
council --help
```

Requirements:

- Python 3.11+
- `tmux` (code mode hides the real Claude Code in a detached tmux session)
- the `claude` binary on PATH (code mode) and optionally `codex` (duels)

## How code mode works

`council code` launches the genuine Claude Code TUI in a hidden tmux pane and drives it
through two verified channels:

- **Inject** — your input is delivered by a bracketed tmux paste. Delivery is confirmed by
  a `UserPromptSubmit` hook receipt written from *inside* Claude Code — not by
  screen-scraping. If no receipt arrives, council re-sends Enter and eventually fails loud.
- **Hooks out** — streamed text, tool calls, and busy/idle state come back through Claude
  Code's hook and statusLine interfaces, rendered in council's skin.

Council's hooks are passed via `--settings` and *stack* with whatever is already registered
in `~/.claude` — your existing setup keeps working underneath.

## Configuration

`~/.council/config.toml` (all optional; `COUNCIL_*` env vars override):

```toml
rounds = 1                  # debate rounds per duel
head_timeout = 300          # per-head subprocess timeout, s
turn_timeout = 600          # max wait for a code-mode turn
submit_timeout = 10         # max wait for a delivery receipt before failing loud
submit_retry_interval = 1.0 # re-send Enter this often while the receipt is missing
tmux_ready_timeout = 30.0   # boot: tmux target + input box mounted
paste_settle = 0.1          # gap between paste and the submit Enter
draft_watch_timeout = 5.0   # advisory-only pane watch window
boot_probe = false          # spend one turn at launch proving the receipt loop works

[heads]
proposer = "claude"
adversary = "codex"
```

## Privacy

`~/.council/ledger.jsonl` stores the **full text of your conversations** (both modes).
It is created owner-only (0600), but treat it like a shell history file: don't commit it,
don't share it casually.

## Attribution

Portions of this code (notably `council/policy.py` and `council/wrap/bridge.py`) are
adapted from [omnigent](https://github.com/omnigent-ai/omnigent) by Databricks, Inc.,
under the Apache License 2.0 — see `harness-project/NOTICE`. The full omnigent source is
not distributed here.

## Status / roadmap

- Pane screen-scraping is **demoted to advisory** — the hook receipt is the sole delivery
  oracle; the scrape will be deleted once the disagreement log stays empty in real use.
- `council review` (cross-family code review) is documented future work, cut from v1.
- The `PermissionRequest` hook (surfacing hidden permission prompts) is wired but not yet
  verified against a live session.
