# council duel output contract — v1 spec

Status: **specified 12 Jul 2026 · revised 13 Jul 2026, not yet implemented.** Decisions grilled with the owner; register chosen against three mock transcripts. Revision (owner call, 13 Jul): critique is **process, not product** — the prose block is now `DELIBERATION`, streamed live as the head's visible thinking and excluded from the answer deliverable; the formal stance-by-stance record lives in the TRAILER. Companions: `docs/artifact-skills-2026-07-12.md` (feasibility research), `docs/debate-techniques-2026-07-12.md` (the techniques several clauses subsume).

## Purpose

Give every agent in a duel the same structure to **follow** when answering, to **critique against**, and to **output** at the end — so answers are comparable, critiques are mechanical to aim, machine features (sycophancy flag, stance reports, a future judge/gate) read validated data instead of regex guesses, and a duel can end in an artifact.

## Scope

- Binds **armed (duel) turns only** — both heads, every round.
- **Solo turns**: unaffected, stay fast free-form chat. **Code mode**: unaffected.
- **Judge** (when `/judge on`): emits its own verdict trailer (below).

## Register

**C — hybrid trailer** (chosen over strict-JSON and markdown-only against mocks): the head writes natural markdown sections that stream live into the tape, then ends with a small JSON **trailer** — the authoritative machine copy. Prose is presentation; when prose and trailer disagree, the trailer wins for machinery, and the judge prompt instructs flagging the contradiction.

## Sections, per round

Marker convention `=== NAME ===` extends the existing `===ANSWER===` family. Emission order = table order: rounds 1+ open with DELIBERATION (think first), then the deliverable sections.

| Section | Round 0 | Round 1+ | Content |
|---|---|---|---|
| `=== DELIBERATION ===` | — | required | **Process, not product** — the head's working, streamed live in the thinking register: engage each of the opponent's claims and each criticism received; refute by reproduction, not assertion; adopt a criticism only with checkable evidence, acknowledge the rest. Stays in the duel transcript (tape, ledger, next round's exchange, judge input); **never appears in the answer deliverable** (`/report` answer views, final-answer excerpts). The stance-by-stance record it argues for is committed formally in the TRAILER, not in prose. |
| `=== POSITION ===` | required | required | One line: the stance. |
| `=== CLAIMS ===` | required | optional refresh | Per claim: `[id] (conf 0-1)` text · `evidence:` the named fact/source · `falsified-by:` what observation would prove it wrong. Generic conditions ("if evidence emerges") count as **missing** — calibration theater rule. |
| `=== ANSWER ===` | required | required | The full standalone answer — no reference to the other model, no critique content. Streams live; renders as the ANSWER block. |
| `=== ARTIFACT ===` | must be `none` | final round only | `none`, or one fenced self-contained HTML block (see Artifacts). |
| `=== TRAILER ===` | required, last | required, last | The JSON envelope below — including, rounds 1+, the **formal critique record**: a `SUPPORT / REFUTE / UNCERTAIN` stance on each opponent claim naming the fact it rests on, and evidence-gated `concessions`. |

Body-section tolerance: missing or renamed **body** sections never fail a round — council parses best-effort for tape niceties. Only the trailer is validated. A reply with no recognizable ANSWER at all still hits the existing dead-reply path (`_is_dead`), unchanged.

## Trailer schema

Two variants (round 0 has no stances/concessions). Round-1+ schema:

```json
{
  "type": "object",
  "required": ["position", "confidence"],
  "properties": {
    "position":   {"type": "string", "maxLength": 300},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "claims": {"type": "array", "items": {"type": "object",
      "required": ["id", "confidence"],
      "properties": {"id": {"type": "string"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                     "falsified_by": {"type": "string"}}}},
    "stances": {"type": "array", "items": {"type": "object",
      "required": ["on", "stance"],
      "properties": {"on": {"type": "string"},
                     "stance": {"enum": ["SUPPORT", "REFUTE", "UNCERTAIN"]},
                     "evidence": {"type": "string"}}}},
    "concessions": {"type": "array", "items": {"type": "object",
      "required": ["adopted"],
      "properties": {"adopted": {"type": "string"}, "evidence": {"type": "string"}}}}
  }
}
```

Judge trailer (only when judging is on):

```json
{"required": ["verdict", "escalated"],
 "properties": {"verdict": {"enum": ["agree", "differ", "ruling"]},
                "escalated": {"type": "boolean"},
                "ruling": {"type": "string"},
                "digest": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}
```

## Enforcement mechanics

The native schema flags (`claude -p --json-schema`, `codex exec --output-schema`) validate a **whole** output, so they cannot enforce a tail block mid-prose. Therefore:

1. **In-round**: council slices the `=== TRAILER ===` block off the tail and validates it locally against the JSON Schema. (Validation library choice = implementation detail.)
2. **On failure — one cheap retry** (owner decision): a follow-up call on the same head session — "emit only the corrected TRAILER JSON for the answer you just gave" — **with the native schema flag attached**; since the retry's whole output is the trailer, the flag now applies and the retry is shape-guaranteed (or errors cleanly). Resumed sessions make this a cents-level call.
3. **Still failing — degrade, never die**: keep the prose, store the trailer raw and unparsed, mark the ledger row (`contract: "unparsed"`), show a dim ⚠ in the tape. The duel continues; stance-dependent features skip that round honestly. Formatting never kills a debate.

## Contract injection — how heads learn it

One template inside council (three variants: round-0, round-N, judge) — **no skill installs, nothing written into the user's repo**:

- **claude**: `--append-system-prompt-file <generated tmp file>`; retry calls add `--json-schema`.
- **codex**: the contract block is **prepended to the composed message** council already builds (codex has no append-system-prompt flag, and generating `AGENTS.md` into the user's working directory would pollute their repo); retry calls add `--output-schema <tmp schema file>`. If a supported per-run instructions config key is confirmed later, it may replace the message prefix — upgrade path, not v1.
- Contract text is **not** part of the Preamble replay: it is re-injected fresh on every armed call, so it never competes with history for clip budget.
- Stub-head tests can assert the contract text reaches the subprocess argv/stdin verbatim.

## Artifacts

- **When**: final round only, in the standalone answer; `ARTIFACT: none` is the norm. A head fills it when the user asked for something visual/interactive, or the answer is inherently better shown than told (the contract text states this rule).
- **What**: exactly one **self-contained** HTML file — inline CSS/JS, no external requests, no build step (duel sandboxes are read-only; the npm-based artifact builders remain out of duel scope).
- **Where**: council saves it to `~/.council/artifacts/<run_id>/<slug>.html` (dir 0700, file 0600 — ledger conventions), writes a ledger row (path, title, head), and auto-opens it (`open` / `xdg-open`) on a TTY. Config: `artifact_open = true` off-switch.

## Interactions

- **Ledger (C1)**: parsed trailer fields and artifact records land as named row constructors once the Ledger row vocabulary refactor is in; row naming lives with that work, not this spec.
- **Tape**: CLAIMS render in the dim register with per-claim confidence; DELIBERATION streams in the thinking register while the head works and is excluded from deliverable surfaces (`/report` answer views, final-answer excerpts — ledger replay keeps it); the ANSWER block is unchanged; overall confidence shows on the ANSWER rule; ⚠ marks an unparsed trailer.
- **Debate techniques subsumed as clauses** (formal verdicts tracked separately): stance-commitment (GDP) and evidence-gated concession bind in the TRAILER schema, with the argument made in DELIBERATION; falsification conditions and stated confidence unchanged. The capitulation flag stays computable directly from trailers (position moved + no evidenced REFUTE/SUPPORT) — unaffected by the reclassification.
- **Shadow mode**: a `contract` on/off knob makes the whole contract A/B-able; prompt-variant experiments run on the contract's own variants.

## Deliberately deferred

- Artifact-craft skills (vendored `web-artifacts-builder` / `frontend-design`) — separate adopt decision.
- Quote verification of evidence spans — separate adopt decision.
- Ledger row shapes/naming — execution detail under the Ledger refactor.
- Judge jury variants and both-orders judging — separate adopt decisions; this spec only fixes the verdict trailer shape they would emit into.
