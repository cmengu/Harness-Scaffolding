# council

![ci](https://github.com/cmengu/Harness-Scaffolding/actions/workflows/ci.yml/badge.svg)

Think and code with a **cross-family second opinion**. `council` is a thin harness that
wraps the real Claude Code binary and can summon an OpenAI Codex adversary on demand —
Claude proposes, Codex critiques, you decide.

Two modes, one ledger:

| Command | What it does |
|---|---|
| `council ask` | **THINK** — chat with Claude; toggle `/duel` to make Codex challenge each answer (`claude -p` vs `codex exec`) |
| `council code` | **CODE** — a branded front over the *real* `claude` binary, hidden in tmux; your `~/.claude` hooks and settings stay live |
| `council attach` | reconnect to a live code session — after a `/detach` or a crashed wrapper — with the whole conversation repainted |
| `council shadow` | the understudy: one question under your current config (arm A) and current-plus-overrides (arm B), answers side by side |

Duel fan-outs run both heads concurrently — **~1.5× faster than serial** measured across
real duels (the two heads rarely take equal time; equal-length heads approach 2×).

Every turn from both modes is appended to a single JSONL ledger, so debates and coding
sessions share one provenance trail. Ask-mode conversations are durable *because* of that:
`/switch` lists and resumes past conversations (across processes), `/fork` branches one,
`/compact` folds a long thread into a summary and keeps going, `/history` and `/context`
show exactly what the heads remember and how close to the cap it is. `/model` and `/effort`
re-point a head mid-session, and `^C` cancels a turn in flight without killing the REPL —
`/help` lists everything.

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

Three safety/lifecycle layers ride those hooks:

- **Permission relay** — when the hidden claude stops on a permission prompt (its own, or
  one raised by council's blast-radius gate), council shows the prompt text and forwards
  your answer (`1`/`2`/`y`/`esc`) as raw keystrokes. Previously this state was a dead-end
  stall.
- **Budget checkpoints** — set `code_budget_usd` and the PreToolUse gate asks (through
  claude's own permission prompt) each time the session's running cost crosses another
  multiple, stopping a runaway agentic loop at the next tool boundary. Approving a
  checkpoint silences it; the next multiple asks again.
- **Approval memory** — approving a gated command once (evidenced by the tool actually
  running) silences that *exact* command for the rest of the session. Session-scoped by
  construction: the memory file lives in the per-session bridge dir and dies with it.
  Auto-allowed calls are printed (`⚑`) and ledgered, never silent.

`/detach` leaves the hidden claude running and returns your terminal; `council attach`
lists live sessions and reconnects (dead session litter is pruned as it lists).

## Configuration

`~/.council/config.toml` (all optional; `COUNCIL_*` env vars override):

```toml
rounds = 1                  # debate rounds per duel
history_turns = 6           # ask-mode memory: past turns carried into each head call
claude_model = ""           # ask-mode model overrides ("" = each CLI's default; /model flips live)
codex_model = ""
codex_effort = ""           # codex reasoning effort: minimal·low·medium·high (/effort)
code_budget_usd = 0.0       # code-mode budget; > 0 = ask at each crossed multiple (0 = off)
ask_budget_usd = 0.0        # ask-mode budget; > 0 = red nag in the turn receipt once crossed
head_timeout = 300          # per-head subprocess timeout, s
head_retries = 2            # extra attempts on TRANSIENT head failures (429/quota/connection)
retry_base_delay = 1.0      # backoff between attempts: 1s → 2s → 4s
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

## Failure semantics

A head failing must never kill a debate. Transient failures (429 / rate limit / quota /
connection resets / timeouts) are retried with exponential backoff (`head_retries`,
`retry_base_delay`); permanent ones (bad flag, bad auth) fail fast — retrying those is
just failing slowly. A head that stays dead degrades the turn to single-voice and leaves
a **quarantine postmortem** (`~/.council/quarantine/*.md`): what was asked, what the error
said, and whether rerunning is worth it. `council report` shows the failure and retry
rates; `council show <run-id>` replays any run including what went wrong.

## Testing

```sh
pip install -e "harness-project[dev]"
cd harness-project && pytest -q
```

The suite runs the entire debate loop against **stub heads** (`tests/stubs/`) — shell
scripts that impersonate `claude -p` and `codex exec`, including rate-limited, hung, and
flaky-once variants. No API keys, no network, no cost; CI runs the same suite on every
push. **No secrets live in this repo** — credentials stay inside the `claude`/`codex`
CLIs themselves.

## Privacy

`~/.council/ledger.jsonl` stores the **full text of your conversations** (both modes).
It is created owner-only (0600), but treat it like a shell history file: don't commit it,
don't share it casually. Quarantine postmortems carry prompt text too, so they get the
same owner-only treatment.

## Attribution

Portions of this code (notably `council/policy.py` and `council/wrap/bridge.py`) are
adapted from [omnigent](https://github.com/omnigent-ai/omnigent) by Databricks, Inc.,
under the Apache License 2.0 — see `harness-project/NOTICE`. The full omnigent source is
not distributed here.

## Status / roadmap

- Pane screen-scraping is **demoted to advisory** — the hook receipt is the sole delivery
  oracle; the scrape will be deleted once the disagreement log stays empty in real use.
- `council review` (cross-family code review) is documented future work, cut from v1.
- The `PermissionRequest` hook event name is still **unverified against a live session**.
  The permission relay, budget checkpoints, and approval memory all degrade gracefully if
  it never fires (back to the stall-warning world), but the first live `council code` run
  should confirm the event actually arrives.
