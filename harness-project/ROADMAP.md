# council — the road from here (10 Jul 2026; decisions re-locked 11 Jul)

Written against commit 61cadf0 (3,685 engine lines, 37 tests green). 11 Jul grilling
locked: judge default OFF, human-confirmed briefing popup, `/note` (no `#`), plan-B
fallback for sessions, 2 calls per head. Vocabulary lives in CONTEXT.md. Order per the owner:
**1) make the CLI excellent → 2) professionalize it → 3) web tier for the non-technical
team → 4) CI gate** (Phase 4 is cheap and can be pulled ahead if demand shows).

One constraint binds every phase, so it goes first:

> **Events are the product; renderers are guests.** Everything a duel produces — thinking
> deltas, tool calls, answer blocks, verdicts, costs — becomes ONE renderer-neutral event
> stream (plain dicts, JSONL-serializable, same register as the ledger rows). The CLI tape
> is consumer #1. The web view is consumer #2 and drinks the SAME stream over SSE. Build
> the pump this way in Phase 1 and Phase 3a is a weekend; couple it to Rich and Phase 3
> is a rewrite. (In-repo precedent: wrap/events.py already does poll→neutral-events for
> code mode — ask mode gets the same shape.)

## Phase 1 — the glass-box duel engine (3–4 weekends)

1. **Probes** (½ day; keep artifacts in scratchpad, findings pinned as config comments)
   - claude: `-p --output-format stream-json --include-partial-messages` → thinking
     deltas headless? which thinking trigger works under `-p` (MAX_THINKING_TOKENS env
     vs prompt phrase)? `total_cost_usd` still on the final event? `--session-id` mint
     then `-p --resume <id>` reuse (memory + tool state)?
   - codex: `exec --json` event schema; which event carries the session id; reasoning-
     summary config; web_search config key; `exec resume <id> <prompt>` behavior;
     `-o last-message` as the clean final-answer channel.
   - Fallback pre-agreed (11 Jul): if resume fails any probe, plan B = tools in round 0
     only + today's preamble replay for later rounds. The build proceeds either way;
     sessions are an upgrade, not a prerequisite.
   - Done when: one-page probe report committed + every flag pinned.

2. **Persistent per-head sessions** (the omnigent answer-sheet pattern: partners are
   long-lived chats keyed by a stable id — Debby's `title` trick, our session ids)
   - Sessions mint on the FIRST ARMED MESSAGE, not the toggle flip (after the briefing
     popup resolves — see item 8): claude session (`--session-id <uuid>`) + codex
     session id captured from its first `--json` run; store on renderer state + a
     `head_session` ledger row.
   - Round 0 seeds each head ONCE from the human-confirmed briefing; later rounds
     `--resume` / `exec resume` carrying ONLY the new critique message.
   - `/new` `/switch` `/fork` invalidate head sessions AND disarm the duel (11 Jul: a
     new chat starts duel-off; re-arming triggers a fresh briefing).
   - Done when: a 2-round duel = 4 subprocess calls where rounds ≥1 send only deltas,
     and the heads demonstrably remember their own round-0 research.

3. **Depth + capability pack** (decisions locked 10 Jul: tools ON, cost accepted)
   - Duel-arm defaults: claude thinking max (per probe), codex
     `model_reasoning_effort=high`; tools ON — claude
     `--allowedTools "Read Grep Glob WebSearch WebFetch"` (no Bash in v1), codex keeps
     its read-only sandbox + web search enabled. (Under plan B, tools arm in round 0
     only — the research round.)
   - `/think <head> <level|off>` per-head toggle; `/status` shows the depth profile.
   - Solo turns DEFAULT fast/tools-off/cheap, but configurable (11 Jul): `solo_thinking`
     + `solo_tools` config knobs (and `/think solo …`) let the owner arm depth for solo
     turns too.
   - Done when: both heads visibly research during a duel; solo defaults unmoved; a
     config flip demonstrably deepens solo turns.

4. **Streaming pump** (backends grow stream twins; block fns stay for judge/compact)
   - `proposer_stream(msg, cfg) -> Iterator[Event]` + codex twin. Event =
     `{head, kind: thinking|tool|text|final|cost|retry|error, payload, ts}`.
   - _safe's retry/quarantine semantics wrap the iterator (retry = emit `retry` event,
     restart the stream; ^C = kill_inflight, unchanged).
   - Done when: all 37 tests still green through the block path + a new test drives a
     fake stream stub end-to-end.

5. **The tape** (CLI consumer #1)
   - One scroll column. Every block gutter-tagged with brand glyphs (11 Jul; terminals
     can't render image logos): `▌✳` claude (Anthropic starburst, orange) / `▌⬡` codex
     (nearest to the OpenAI knot, blue-white) — theme-configurable like the banner. Thinking + tool lines
     interleave live in the dim register; ANSWER blocks buffer and commit WHOLE in
     finish order (never interleave prose). Bottom status: per-head phase
     (thinking/tools/writing) + seconds + $.
   - Critique rounds stay in the dim register with honest labels (`🔵 challenges 🟠`) —
     the debate reads as the system's thinking, but never disguised.
   - Drill-in: existing show_overlay becomes the per-head full-transcript viewer.
   - Done when: a 2-round duel reads like an argument; ^C cancels cleanly mid-stream.

6. **Duel ending** (revised 11 Jul)
   - Default depth = round 0 + ONE combined critique-and-final round = 2 calls per
     head. The round-1 prompt critiques the other's answer, THEN writes the best
     standalone answer (incorporate what the debate conceded, no reference to the
     other model). The critique streams in the dim register; ONLY the standalone
     answer renders as the ANSWER block. `/rounds N` deepens.
   - Judge OFF by default (reverses 10 Jul). `/judge on` = Claude judges, blind-graded,
     reasoning style; output = agree / differ / verdict + the "how the debate moved
     them" digest (Debby's best output format — keep it); ESCALATE → loud banner.
     A verdict is a biased-but-useful opinion — the judge is also a debater; the human
     is always the final judge. (Phase 4's gate forces the judge ON per-run — a gate
     exists to rule.)
   - Done when: a default armed duel ends with two standalone answers; a `/judge on`
     duel ends verdict-first; `/last` replays either.

7. **Parked orchestrator** (the composer never blocks — omnigent's park-and-wake,
   collapsed to one process)
   - handle() moves off the input thread; the tape renders from a pump thread; the
     composer stays live mid-duel. Notes (`/note <text>` — explicit command only, no
     `#` prefix; 11 Jul) fire no model, ack with `✎ noted`, append to the ledger +
     ride into the next round's context as facts-from-the-boss for BOTH heads. One duel in
     flight at a time; a second question queues with a visible chip.
   - Done when: you can type a note while both heads stream and see it acknowledged.

8. **Arming UX + test refresh**
   - Shift+Tab (`s-tab`) toggles the duel INSTANTLY: marker flips to `⚔ ›`, banner
     gains the hint, codex-missing guard refuses loudly in the toggle; `/duel` stays
     as the synonym.
   - The FIRST ARMED MESSAGE opens the briefing popup (11 Jul): claude drafts a
     briefing for codex; picker = (A) claude's briefing [recommended, Enter accepts] /
     (B) last `history_turns` turns / (C) full transcript / type-your-own. Nothing
     reaches codex before confirmation. Non-interactive runs (CI, piped) skip the
     popup → auto-(B). No dedicated "challenge" key — arm + whatever you type covers it.
   - Stream-mode shell stubs (fake JSONL emitters) join tests/stubs; CI green.

## Phase 2 — professional pass (2–3 weekends)

- `council doctor`: binaries, auth state, tmux, versions, config lint (boot-probe code
  reused). Crisp error cards everywhere — a stranger never sees a raw traceback.
- Packaging: pipx-clean install, semver + CHANGELOG.md, GitHub Releases with notes;
  tag v0.1.0 at Phase-1 end.
- First-run experience: detect claude/codex auth, offer doctor, teach ⚔ in the banner.
- README: 90-second asciinema GIF of a duel→verdict above the fold; mkdocs site with
  five pages (install · the duel · commands · config · CI).
- Done when: a stranger installs and reaches a verdict in under 5 minutes, unassisted.

## Phase 3 — web tier for the team (staged; 3c is a DECISION, not a default)

- **3a — read-only live viewer (1–2 weekends).** FastAPI + one static page:
  `GET /runs` (report), `GET /runs/{id}` (replay), `GET /live` (SSE tail of the current
  event stream). Non-technical teammates watch duels and read verdicts from a browser.
  LAN/tailnet + shared token; no accounts.
- **3b — interactive single-seat web chat (2–3 weekends).** `POST /ask` streams the
  duel back over SSE — same engine, second consumer of the same events. One live duel
  at a time (matches the engine guard); the CLI and the page are two views of one run.
- **3c — multi-user (STOP AND DECIDE).** Concurrent users, auth, per-user sessions =
  the 45% server ring omnigent built. Options, chosen with 3a/3b usage data in hand:
  (i) thin single-tenant build — one box, team ≤ ~30, sqlite, session-per-browser;
  feasible solo but a real project; (ii) adopt omnigent's hosting layer for the web
  tier only, council engine behind it; (iii) stop at 3b — the viewer is already
  multi-user, only ASKING is single-seat.
- Security note (biotech team): keep it inside the VPN/tailnet; the ledger carries
  proprietary reasoning — treat the host like a lab-notebook server.

## Phase 4 — CI gate (1–2 weekends; the wedge — pull forward if demand shows)

- `council gate -p "…" --json`: headless duel → verdict JSON
  `{verdict, escalated, agree, differ, cost, run_id}` + exit code
  (0 pass · 1 escalate · 2 error).
- GitHub Action wrapper: run on PR, comment the digest, fail on escalate.
- This phase is why events and ledger rows stay machine-readable everywhere.
