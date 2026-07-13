# Spec: Phase 1.5 — the output contract + the refactors that carry it

> **Mirror**, published into the repo 13 Jul 2026 by owner decision so a repo-only agent has full context (supersedes this doc's own "stays in the private tracker" note). The private tracker copy (`.scratch/phase-1.5-adoption/spec.md` in the owner's workspace) remains canonical for ticket state; content identical at mirror time.

Status: ready-for-agent
Sources: wayfinder map `.scratch/council-adoption/` (6 resolved tickets, 12 Jul 2026) · `harness-project/ROADMAP.md` Phase 1.5 (owns the ORDER) · `harness-project/docs/output-contract.md` (owns the CONTRACT CONTENT — single source of truth; this spec does not restate it, it operationalizes it) · `harness-project/docs/artifact-skills-2026-07-12.md` + `docs/debate-techniques-2026-07-12.md` (evidence base).

## Problem Statement

The owner runs duels — two AI heads (proposer ✳, adversary ⬡) answering and critiquing across rounds — but each head answers in whatever shape it likes. Free-form answers make critiques vague (nothing concrete to take a stance on), make sycophantic caving invisible (a head can silently adopt its opponent's position), make machine features guesswork (reports and any future judge/gate must regex-guess at prose), and leave the duel's output feeling weaker than interactive Claude Code — no artifacts, no structure. Underneath, three code problems make every improvement expensive: ledger rows are hand-built dicts in seven writers with duplicated reader guards; the conversation-recap logic (Preamble) is duplicated across files and has already caused a real bug; and the duel engine paints the terminal while it thinks, so its behavior is only testable through printed text. The deepest safety module (the code-mode policy gate) has zero test-suite coverage.

## Solution

A structured duel: every armed answer follows the output contract — in critique rounds, a deliberation streamed live as the head's visible thinking (process, never part of the answer), then the deliverable: position, claims with confidence/evidence/falsification, a standalone final answer, an optional self-contained HTML artifact that opens in the browser, and a small validated JSON trailer carrying the machine facts, including the formal stance-by-stance record. Built in an order that makes each step cheap: commit the baseline, protect the policy gate with tests, give the Ledger its row vocabulary and the Preamble one owner, then implement the contract, then the three mechanics the trailer enables (skip-when-agreed, honest deadlocks, sycophancy flag), then finish "renderers are guests" by moving painting behind an event stream.

## User Stories

**Baseline & safety (steps 0–1)**
1. As the owner, I want the current working tree committed and green before refactors start, so that every change lands on a known-good baseline.
2. As the owner, I want the policy gate covered by the test suite, so that the code that blocks dangerous commands cannot regress silently.

**Ledger & Preamble (step 2)**
3. As a developer extending council, I want one constructor per ledger row kind, so that adding or changing a row edits one module instead of seven writers.
4. As a developer, I want one classifier per reader question (e.g. "is this row an answer?"), so that the five duplicated guards cannot drift apart.
5. As the owner, I want codex spend visible in every cost total, so that `/cost` and `/report` stop under-reporting what duels cost.
6. As the owner, I want `/context` to measure the real Preamble (the recap actually sent to heads), so that what I see is what the model got.
7. As the owner, I want memory bugs to live in one module, so that the next half-minted-session-style bug is a one-file fix.

**The contract (step 3)**
8. As the owner, I want both heads to answer duels in the same sections (position, claims, answer, artifact, trailer — with critique-round deliberation streamed separately as thinking), so that answers are directly comparable and stay clean of critique content.
9. As the owner, I want every claim to carry a confidence number, its evidence, and what would prove it wrong, so that confidence theater is structurally impossible.
10. As the owner, I want the adversary to take an explicit SUPPORT / REFUTE / UNCERTAIN stance on each of the proposer's claims (and vice versa) — argued in its live deliberation and committed formally in the trailer, never inside the answer itself — so that critique aims at something concrete instead of vibes while every answer stays standalone.
11. As the owner, I want concessions to count only when they carry checkable evidence, so that a head cannot cave just to be agreeable.
12. As the owner, I want the prose — deliberation and answer alike — to stream live into the tape while the machine facts arrive in a validated trailer, so that I keep the glass-box experience and the machinery gets reliable data.
13. As the owner, I want a malformed trailer to trigger one cheap corrective retry and then degrade gracefully with a visible ⚠, so that formatting never kills a debate.
14. As the owner, I want to see each head's overall confidence on its answer block, so that I can weigh the two finals at a glance.
15. As the owner, I want each head shown its opponent's stated confidence during the critique round, so that the debate prices in how sure the other side is.
16. As the owner, I want a visual/interactive question to end with a self-contained HTML artifact saved under the run and opened in my browser, so that a duel can produce artifact-grade output like interactive Claude Code.
17. As the owner, I want artifact auto-open to have a config off-switch, so that headless or remote runs don't try to open browsers.
18. As the owner, I want the contract injected fresh on every armed call (never written into my repo, never part of the recap replay), so that my project tree stays clean and history budget stays for history.
19. As a solo-turn user, I want unarmed chat completely untouched by the contract, so that quick questions stay fast and free-form.
20. As the future judge (when turned on), I want my verdict shape already specified (verdict/escalated/digest trailer), so that enabling judging later needs no redesign.

**Trailer mechanics (step 4)**
21. As the owner, I want the duel to skip the critique round when both round-0 positions already agree (recording an agreed marker), so that easy questions cost 2 calls instead of 4.
22. As the owner, I want the duel to stop early when the heads have genuinely converged on each other, so that I don't pay for rounds that only restate agreement.
23. As the owner, I want a duel that ends still-disagreeing to write an honest `unresolved` marker, so that fake closure never masks a live disagreement.
24. As the owner, I want a head that moves toward its opponent without an evidenced stance to be flagged (`syco_flag`) and surfaced in `/report`, so that capitulation is visible the moment it happens.

**Event seam (step 5)**
25. As a developer, I want the duel engine to emit renderer-neutral events with painting done by subscribers, so that engine tests assert events instead of scraping printed text.
26. As the owner, I want the composer/input loop testable without a TTY, so that the deepest untested behavior gets under test.
27. As a future web-tier teammate, I want the same event stream the CLI tape consumes to be available to a browser view, so that Phase 3a is a weekend, not a rewrite.
28. As the owner, I want the briefing popup owned by the REPL (not the engine), so that UI concerns live where the other popups live.

**Cross-cutting**
29. As the owner, I want every new behavior recorded as ledger rows through the new constructors, so that replay, `/report`, and future analysis read one consistent record.
30. As a developer, I want each Phase 1.5 step to leave CI green, so that the sequence can pause safely at any boundary.

## Implementation Decisions

- **Order is fixed** (ROADMAP Phase 1.5): commit tree → policy-gate tests → Ledger row vocabulary + Preamble module (one sitting) → contract → mechanics trio → event seam. Contract lands before the event seam deliberately: its core (injection, slicing, validation, retry) barely touches painting; only a small render migration follows.
- **Ledger row vocabulary**: row construction and classification move into the ledger module — one constructor per row kind, one classifier per reader question; cost rows normalize `usd | tokens` so both heads' spend aggregates. All 26 existing row kinds (plus new contract/mechanics rows) get constructors. This is a wide refactor: sequence it expand–contract (add constructors beside literals, migrate writers in batches, delete literals last).
- **Preamble module**: chain flattening, the clip window (single implementation), note queueing, and compact-summary lead move behind one interface (`preamble() · turns() · notes()`); chat and debate become callers. The dead-marker check unifies to one implementation while these files are open.
- **Contract implementation** follows `docs/output-contract.md` §everything: three injection variants (round-0 / round-N / judge) generated from one in-council template; claude gets it via append-system-prompt flag, codex as a prefixed block in the composed message; trailer sliced off the tail and validated council-side; on failure one retry call on the same head session requesting trailer-only with the native schema flag attached; still-failing → prose kept, trailer stored raw, row marked, ⚠ in tape. The community prompt-line pack (anti-deference, refute-by-reproduction, agreement-without-new-argument-fails) is woven into the template text. Critique is process, not product (owner call, 13 Jul): rounds 1+ open with a `DELIBERATION` block rendered in the thinking register and excluded from deliverable surfaces (`/report` answer views, final-answer excerpts); the formal stances ride the trailer, so the mechanics trio and `syco_flag` are unaffected.
- **Trailer schema** (from the prototype; trimmed to the decision-rich shape — full schema in the contract spec):
  ```json
  {"required": ["position", "confidence"],
   "properties": {"position": "string", "confidence": "0..1",
                  "claims":  [{"id", "confidence", "falsified_by"}],
                  "stances": [{"on", "stance: SUPPORT|REFUTE|UNCERTAIN", "evidence"}],
                  "concessions": [{"adopted", "evidence"}]}}
  ```
- **Artifacts**: final round only, model judgment per the contract rule; exactly one self-contained HTML file (no external requests, no build step — duel sandboxes are read-only); saved under the run id with ledger permissions; ledger row records path/title/head; auto-open via the platform opener behind an `artifact_open` config knob.
- **Mechanics trio** (all read validated trailers, zero extra model calls): round-0 agreement router (normalized position match → skip critique round, `round0_agreed` row); cross-head agreement early-stop upgrading the existing self-churn check, with an `unresolved` row at the rounds cap; capitulation flag (position moved toward opponent + no evidenced stance → `syco_flag` row, surfaced in `/report`).
- **Event seam**: the duel engine's run loop emits the event dicts the backends already produce (`{head, kind, payload, ts}`); the tape renderer and a quiet test renderer subscribe; the two existing fan-out implementations collapse into one; the briefing popup moves to the REPL side; the shadow-mode side-by-side layout duplication collapses into the shared renderer.
- **Config knobs added**: `artifact_open`; a contract on/off knob (default on when armed) so shadow mode can A/B the whole contract later.
- **Deliberate non-decisions honored** (parked with triggers, do not build): Head seam (third head), SessionState (knob persistence), the judge bucket (both-orders, faithfulness scoring, jury, quote verifier, recalibration), the disagreement-intensity experiment (runs post-contract, outside this spec).

## Testing Decisions

- **Good tests here assert external behavior**: ledger rows written, calls made/skipped, events emitted, files saved — never internal call graphs or printed-string internals (the event seam exists precisely to retire substring assertions).
- **Primary seam — stub heads (existing)**: the `COUNCIL_*_COMMAND` env seam swaps real CLIs for shell stubs. New stubs join the existing nine (flaky-once, quota, slow, escalate…): contract-valid, trailer-malformed (recovers on retry), trailer-malformed-twice (degrades), round-0-agreeing pair (router fires), evidence-free-cave (flag fires), artifact-emitting. Tests drive real duels through the real engine and assert rows/behavior.
- **Pure-function units (new, small)**: row constructors and classifiers; the preamble builder (clip window edge cases); the trailer validator (schema acceptance/rejection tables); the position-similarity check the router and flag share.
- **Event-seam tests (arrive with step 5)**: engine tests assert event sequences via the quiet renderer; the input loop gets its first TTY-free tests.
- **Policy gate (step 1)**: port the module's own self-test into the suite — allow/deny/ask verdict tables as data-driven cases.
- **Prior art**: the existing stub conventions, the conftest pattern that clears the ledger's cached config between tests, and the depth/session test files as style reference.
- **Every step's exit criterion includes CI green**; the live-model smoke path stays out of CI (owner declined a live seam — stubs carry the suite).

## Out of Scope

- Running the disagreement-intensity A/B experiment (specified, waits for the contract; runs on contract variants).
- Everything in the judge bucket (both-orders judging, faithfulness scoring, PoLL jury, quote verifier, recalibration) — held until judge demand or the CI-gate phase.
- Head seam and SessionState refactors — parked behind named triggers.
- Artifact-craft skill vendoring (web-artifacts-builder / frontend-design), solo-turn or code-mode contracts, ROADMAP Phases 2–4 (doctor/packaging, web tier, CI gate).
- Any change to metis coexistence, the commit gate, or code-mode wrap behavior.

## Further Notes

- **Single-source-of-truth split**: ROADMAP.md Phase 1.5 owns the order; `docs/output-contract.md` owns contract content; this spec operationalizes both for execution and adds the testing contract. If they ever disagree, fix the owning doc first.
- **Skills**: verified 13 Jul 2026 — Anthropic ships no native debate skill (Claude Code's bundled roster and the public `anthropics/skills` repo both checked); the contract template *is* the debate skill, distilled from the verified technique catalog (`docs/debate-techniques-2026-07-12.md`), and it is taught by per-call injection, deliberately not a skill install (wayfinder ticket 04). The one skills decision in scope stays parked in Out of Scope: artifact-craft vendoring (`web-artifacts-builder` / `frontend-design`), because duel sandboxes can't run its build scripts mid-duel.
- The harness-project repo is public: nothing execution produces (docs, comments, fixtures) may reference the owner's employer or private tooling; this spec itself stays in the private tracker.
- The wayfinder map (`.scratch/council-adoption/`) holds the full decision rationale — six resolved tickets including the rejected alternatives; consult it before relitigating any decision.
- Sized for `/to-tickets` next: steps 0–1 are single tickets; step 2 is an expand–contract sequence; step 3 splits naturally (injection+validation / tape+artifact); steps 4–5 are one ticket each or small clusters. Work the frontier one ticket per fresh session with `/implement`.
