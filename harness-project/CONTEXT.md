# council — Glossary

Vocabulary for the council harness. Terms only — no implementation details.

## Head
One of the AI models taking part in a conversation. The **proposer** (Claude, 🟠) answers; the **adversary** (Codex, 🔵) challenges. A head is external to council — council talks to it, it is not part of council.

## Solo turn
A normal chat turn answered by the proposer alone. Fast, cheap, no tools.

## Duel
The adversarial mode: both heads answer, then critique each other across rounds, ending in two final standalone answers (and a verdict, if judging is on). Toggled on and off by the user ("armed" / "disarmed"); stays on until toggled off.

## Round
One exchange within a duel. **Round 0** is the research round: each head produces its first answer, and is the only round where tools may be used (unless per-head sessions carry tool memory forward). Later rounds are argument only.

## Briefing
The context pack handed to the adversary when the duel is armed mid-conversation. Written by the proposer, chosen by the human from a small set of options (proposer's summary / recent turns / full transcript) before round 0 starts. Nothing is sent to the adversary without this confirmation.

## Preamble
The replay of chat history handed to a head that has no memory of its own. The fallback memory mechanism when per-head sessions are unavailable.

## Head session
A head's own native "continue this conversation" state (resume). When available, replaces the preamble for rounds after 0.

## Note
A fact injected by the human via `/note <text>`, without firing any model. Notes queue up and ride into the context of the next turn, visible to all heads, and are to be treated as constraints from the boss — facts, not suggestions.

## Verdict
The judge's ruling at the end of a duel: agree / differ / a ruling, or an escalation to the human. Judging is off by default — a verdict only exists when the human turns it on, and is understood as a biased-but-useful opinion (the judge is also a debater); the human is always the final judge.

## Ledger
The append-only JSONL record of everything that happened. Single source of truth; every renderer (CLI, web) is a reader of it.
