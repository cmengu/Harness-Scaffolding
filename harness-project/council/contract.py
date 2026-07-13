"""council/contract.py — the output contract, taught by injection.

The single source of truth for contract *content* is `docs/output-contract.md`; this module
operationalizes it. One in-council template (three variants: round-0 · round-N · judge) is
generated fresh on every ARMED call and injected — never written into the user's repo, never
part of the Preamble replay (backends carry it: claude via --append-system-prompt-file, codex
as a prefix on the composed message). After the head answers, council slices the
`=== TRAILER ===` block off the tail and validates it here; the duel loop owns the retry/degrade.

Nothing here calls a model or touches the ledger — pure text in, structured data out — so the
template, the slicer, and the validator all unit-test as plain functions.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile

TRAILER_MARK = "=== TRAILER ==="

# ── the community prompt-line pack (docs/debate-techniques-2026-07-12.md), woven into every
#    critique-round variant: anti-deference, refute-by-reproduction, agreement-without-a-new-
#    argument-fails. Distilled to three lines so the contract IS the debate skill. ──────────
_PROMPT_PACK = """\
- Do NOT defer to the other voice because it sounds confident or authoritative — weigh the argument, not the tone.
- REFUTE BY REPRODUCTION: to reject a claim, reproduce the reasoning or name the fact that breaks it — never a bare "this is wrong".
- Agreement that adds no new argument does not count: only concede when you can name the checkable evidence that moved you."""

# The contract answer structure. Marker convention `=== NAME ===` extends the existing
# `===ANSWER===` family. Body sections are parsed best-effort (tape niceties); ONLY the trailer
# is validated. Emission order = the order below.
_SECTIONS_ROUND0 = """\
=== POSITION ===
One line: your stance.
=== CLAIMS ===
One block per claim: `[id] (conf 0-1) <text>` then `evidence:` the named fact or source and
`falsified-by:` the observation that would prove it wrong. A generic condition ("if new evidence
emerges") counts as MISSING — name a concrete one.
=== ANSWER ===
Your full standalone answer. No reference to the other voice; it must read on its own.
=== ARTIFACT ===
none
=== TRAILER ===
A single JSON object, LAST, nothing after it:
{"position": "<your stance>", "confidence": <0-1>,
 "claims": [{"id": "<id>", "confidence": <0-1>, "falsified_by": "<observation>"}]}"""

_SECTIONS_ROUNDN = """\
=== DELIBERATION ===
Your working — PROCESS, NOT PRODUCT. Engage each of the other voice's claims and every criticism
you received; refute by reproduction; adopt a criticism only with checkable evidence, and say so.
This streams as your visible thinking and NEVER appears in the answer deliverable.
=== POSITION ===
One line: your stance now.
=== CLAIMS ===
(Optional refresh, same shape as before.)
=== ANSWER ===
Your full standalone answer. No reference to the other voice or this debate; it must read on its own.
=== ARTIFACT ===
`none`, or — final round only, if the question is visual/interactive or better shown than told —
exactly one fenced ```html block that is fully self-contained (inline CSS/JS, no external requests).
=== TRAILER ===
A single JSON object, LAST, nothing after it. It carries the FORMAL critique record — the
stance-by-stance verdicts argued above are committed HERE, never in the answer:
{"position": "<your stance>", "confidence": <0-1>,
 "claims": [{"id": "<id>", "confidence": <0-1>, "falsified_by": "<observation>"}],
 "stances": [{"on": "<the other voice's claim>", "stance": "SUPPORT|REFUTE|UNCERTAIN", "evidence": "<fact>"}],
 "concessions": [{"adopted": "<what you adopted>", "evidence": "<checkable fact that moved you>"}]}"""

_JUDGE = """\
You are the judge of a duel. Read both answers blind and emit ONLY a JSON trailer, last:
{"verdict": "agree|differ|ruling", "escalated": <bool>, "ruling": "<if you ruled>",
 "digest": "<one-paragraph synthesis>", "confidence": <0-1>}"""


def injection(round_no: int, *, judge: bool = False, final_round: bool = False) -> str:
    """The contract text for one armed call. round 0 = opening shape (no deliberation, no
    stances, ARTIFACT none); round N = the critique shape with the prompt pack. The template is
    STATIC instructions only — the opponent's stated confidence is a dynamic per-turn fact, so it
    rides the round-N message (debate._opp_conf), not here; the prompt pack already carries the
    anti-deference rule that tells a head how to weigh it."""
    if judge:
        return _JUDGE
    head = ("You are one of two voices in a council duel. Answer in the sections below, IN ORDER, "
            "each on its own line beginning with its `=== NAME ===` marker. The trailer is the "
            "machine-authoritative copy: if prose and trailer ever disagree, the trailer wins.")
    if round_no <= 0:
        return f"{head}\n\n{_SECTIONS_ROUND0}"
    tail = "" if final_round else "\n\nThis is not the final round: keep ARTIFACT as `none`."
    return f"{head}\n\n{_PROMPT_PACK}\n\n{_SECTIONS_ROUNDN}{tail}"


# ── trailer slicing + validation ─────────────────────────────────────────────────────────
def split_trailer(text: str) -> tuple[str, str | None]:
    """(body, raw_trailer) — the `=== TRAILER ===` block sliced off the tail. Uses the LAST
    marker so a trailer that quotes the marker name in prose can't fool it. No marker → the
    whole text is body and there is no trailer (the degrade path handles that)."""
    if not text:
        return text, None
    idx = text.rfind(TRAILER_MARK)
    if idx == -1:
        return text, None
    body = text[:idx].rstrip()
    raw = text[idx + len(TRAILER_MARK):].strip()
    return body, (raw or None)


def _strip_fence(text: str) -> str:
    """Tolerate a ```json … ``` fence around the trailer JSON — models add one habitually."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _num01(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0


def parse_trailer(raw: str | None, round_no: int) -> dict | None:
    """Validate a raw trailer against the contract schema; return the parsed dict or None.
    Round 0 and round N share the required core (position + confidence in [0,1]); the optional
    arrays are shape-checked leniently and dropped if malformed rather than failing the whole
    trailer — the required core is the gate the retry/degrade path keys off."""
    if not raw:
        return None
    try:
        data = json.loads(_strip_fence(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    position = data.get("position")
    if not isinstance(position, str) or not position.strip():
        return None
    if not _num01(data.get("confidence")):
        return None
    out: dict = {"position": position.strip(), "confidence": float(data["confidence"])}
    claims = [c for c in data.get("claims", []) if isinstance(c, dict) and c.get("id")]
    if claims:
        out["claims"] = claims
    if round_no > 0:
        stances = [s for s in data.get("stances", [])
                   if isinstance(s, dict) and s.get("on")
                   and s.get("stance") in ("SUPPORT", "REFUTE", "UNCERTAIN")]
        if stances:
            out["stances"] = stances
        concessions = [c for c in data.get("concessions", [])
                       if isinstance(c, dict) and c.get("adopted")]
        if concessions:
            out["concessions"] = concessions
    return out


def confidence(parsed: dict | None) -> float | None:
    """The overall confidence a parsed trailer carries — None when there is no trailer."""
    return parsed.get("confidence") if isinstance(parsed, dict) else None


# ── body-section parsing (tape niceties + deliverable extraction) ───────────────────────────
# Best-effort only: missing or renamed body sections never fail a round (the contract validates
# the trailer, not the prose). The tape renders sections richly; the deliverable surfaces show
# only the ANSWER so DELIBERATION stays a thinking-register concern.
_SECTION_RE = re.compile(r"^===\s*([A-Za-z]+)\s*===\s*$", re.M)


def sections(text: str) -> dict[str, str]:
    """Split a contract body into its `=== NAME ===` sections, lowercased keys → stripped text.
    A body with no markers yields {} (free-form answers parse to nothing, unchanged)."""
    if not text:
        return {}
    parts = _SECTION_RE.split(text)     # [preamble, NAME, body, NAME, body, …]
    out: dict[str, str] = {}
    it = iter(parts[1:])
    for name, body in zip(it, it):
        out[name.lower()] = body.strip()
    return out


def answer_of(text: str) -> str:
    """The DELIVERABLE view of an answer: the `=== ANSWER ===` section alone, so DELIBERATION,
    CLAIMS, and the trailer never leak into /report answer views or final-answer excerpts. A
    free-form (non-contract) answer has no ANSWER marker → returned whole, unchanged."""
    body, _ = split_trailer(text)
    return sections(body).get("answer") or body.strip()


_HTML_FENCE = re.compile(r"```html\s*\n(.*?)```", re.S | re.I)


def artifact_html(section_text: str | None) -> str | None:
    """The self-contained HTML in an `=== ARTIFACT ===` section, or None when it is `none`
    / absent / not HTML. Prefers a fenced ```html block; falls back to raw markup if the
    section is bare HTML. The 'one self-contained file' rule is the contract's, enforced by
    the template — here we just lift what the head emitted."""
    if not section_text:
        return None
    s = section_text.strip()
    if not s or s.lower() == "none":
        return None
    m = _HTML_FENCE.search(s)
    if m:
        html = m.group(1).strip()
        return html or None
    return s if ("<" in s and ">" in s) else None


# ── the retry schema (native flag: claude --json-schema <inline> · codex --output-schema <file>) ──
def schema_json(round_no: int) -> str:
    """The JSON Schema string the corrective retry attaches so its whole output is
    shape-guaranteed. Two variants, as the contract spec states: round 0 has no stances or
    concessions; round N adds them. position + confidence are the required core in both."""
    props = {
        "position": {"type": "string", "maxLength": 300},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "claims": {"type": "array", "items": {"type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string"},
                           "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                           "falsified_by": {"type": "string"}}}},
    }
    if round_no > 0:
        props["stances"] = {"type": "array", "items": {"type": "object",
            "required": ["on", "stance"],
            "properties": {"on": {"type": "string"},
                           "stance": {"enum": ["SUPPORT", "REFUTE", "UNCERTAIN"]},
                           "evidence": {"type": "string"}}}}
        props["concessions"] = {"type": "array", "items": {"type": "object",
            "required": ["adopted"],
            "properties": {"adopted": {"type": "string"}, "evidence": {"type": "string"}}}}
    return json.dumps({"type": "object", "required": ["position", "confidence"],
                       "properties": props})


@contextlib.contextmanager
def system_prompt_file(text: str):
    """A throwaway 0600 tmp file holding the contract, for `--append-system-prompt-file`.
    A file (not a giant argv string) keeps the contract off `ps` and out of ARG_MAX; it is
    deleted the moment the call returns. Empty text → no file (the no-contract path)."""
    if not text:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="council-contract-", suffix=".txt")
    try:
        os.write(fd, text.encode())
        os.close(fd)
        os.chmod(path, 0o600)
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
