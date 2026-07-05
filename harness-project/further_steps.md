# further_steps.md — wrapper → platform, step by step

The goal: graduate council from a tool you babysit to infrastructure someone could
trust without you in the room. Three trust gaps to close — *what happened?* (tracing),
*what happens when things break?* (failure semantics), *can someone else run it?*
(config + CI) — then four bonuses that mostly reuse what the top three force you to build.

Every step below is grounded in the actual code (file:line as of 5 Jul 2026), states
what already exists vs. what's missing, gives pseudocode, and ends with a checkable
"done when". Build order follows the dependency arrows, not the numbering of the
original list.

```
Step 1 (run IDs + metrics)  ──────►  Step 4 (council report / show)
Step 2 (tests + stubs + CI) ──────►  Step 3 (retries + quarantine, testable)
Step 2 (stubs)              ──────►  Step 5c (shadow mode, replayable)
Step 1 (metrics)            ──────►  Step 5a/5b (parallel number, budgets)
```

---

## Step 0 — audit: what exists vs. what's missing

Verified against the repo, not assumed:

| Piece | Status | Where |
|---|---|---|
| Single persistence seam (`record()`/`trace()`) | ✅ exists | `council/ledger.py:22,39` |
| Per-head timeout | ✅ exists | `backends.py:41` (`cfg.head_timeout`) |
| Graceful degrade (one dead head ≠ dead debate) | ✅ exists | `debate.py:69` (`_safe`) |
| Parallel fan-out | ✅ exists | `debate.py:59` (`ThreadPoolExecutor`) |
| Config file + env overrides | ✅ exists | `config.py:42` (`toml ← COUNCIL_*`) |
| Head binaries are config knobs (stub seam!) | ✅ exists | `config.py:25-26` |
| Code-mode cost/model capture | ✅ exists | `wrap/state.py` → `render.py:80` records `code_context` rows |
| No secrets in code (creds live in the CLIs) | ✅ exists | worth one README sentence |
| **Run ID stamped on every ledger row** | ✅ done 5 Jul | `ledger.py` `RUN_ID` module global |
| **Per-call latency / ask-mode cost** | ✅ done 5 Jul | `debate.py _safe` → `head_call` rows; `backends.py` → `head_cost` |
| **Retries with backoff + error classification** | ❌ missing | `_safe` catches once, never retries |
| **Quarantine / postmortem files** | ❌ missing | failures = one ledger row, easy to miss |
| **`tests/` directory** | ❌ missing | assertions live in shell history |
| **Stub head binaries** | ❌ missing | — |
| **CI workflow** | ❌ missing | no `.github/` |
| **`council report` / `council show`** | ✅ done 5 Jul | `report.py`; also in-REPL `/report` `/show` |

Two facts verified live (5 Jul 2026) that the plan depends on:
- `claude -p --output-format json` exists (single JSON result) — per-call cost is gettable.
- `codex exec --json` (JSONL events) and `codex exec -o FILE` (last message to file) exist.

Two traps found while auditing, encoded into the steps below:
- `total_cost_usd` from the statusLine is a **running session total** → a report must
  take **max per run**, never sum the rows (else you overcount by ~the number of turns).
- `ledger._cfg` is `lru_cache`'d (`ledger.py:16`) → tests that point `COUNCIL_LEDGER_PATH`
  at a temp dir **must call `_cfg.cache_clear()`** or they'll write to your real ledger.

---

## Step 1 — run IDs + per-call metrics  (~hours) — ✅ SHIPPED 5 Jul 2026

**Why:** the flight recorder. Turns "rows you correlate by squinting at timestamps"
into "threads you can filter, aggregate, and bill".

### 1a. Mint one run ID per invocation

Key insight from the audit: **one council process = one run**, and *every* `record()`
call already happens in the main process (the statusLine hack runs as a separate
process but only writes `context.json`; the main process's `render.py` reads that and
records). So the run ID can be a module global in `ledger.py` — no plumbing through
call sites at all.

```python
# ledger.py — 3 lines, zero call-site changes
import uuid
RUN_ID = uuid.uuid4().hex[:12]          # one per process = one per `council ask`/`code`

def record(event):
    row = {"ts": time.time(), "run_id": RUN_ID, **event}   # was: {"ts": ..., **event}
    ...
```

`trace()` already filters on arbitrary key equality (`ledger.py:46`), so
`trace(run_id="abc123")` works the moment rows carry the field. That one line is
what later makes `council show <run-id>` (and the whole "artifact store" bonus) free.

Also record an explicit start-of-run row so a run has a header even if it crashes
before doing anything:

```python
# cli.py, top of ask() and code(), after load_config():
record({"role": "run_start", "mode": "ask" | "code", "argv": sys.argv[1:]})
```

### 1b. Per-call latency

`_safe()` (`debate.py:69`) is the right place — it already knows the head label
(including `"judge"`), and it sees failures too, so error latency gets captured for free:

```python
def _safe(fn, msg, cfg, label):
    t0 = time.monotonic()
    try:
        out = fn(msg, cfg)
        if not out.strip(): raise ValueError("empty response")
        record({"role": "head_call", "head": label, "ok": True,
                "secs": round(time.monotonic() - t0, 2)})
        return out
    except Exception as e:
        record({"role": "head_call", "head": label, "ok": False,
                "secs": round(time.monotonic() - t0, 2), "error": str(e)})
        record({"role": "head_error", "head": label, "error": str(e)})   # keep: viewer tails this
        return f"_({label} unavailable: {e})_"
```

### 1c. Ask-mode cost (claude head first, codex optional)

Code mode already has cost (statusLine → `code_context` rows). Ask mode has none.
The claude head is one flag away:

```python
# backends.py — proposer() switches to JSON output
def proposer(message, cfg):
    raw = _run([cfg.claude_command, "-p", "--output-format", "json",
                "--allowedTools", ""], cfg, stdin=...)
    try:
        payload = json.loads(raw)
        record({"role": "head_cost", "head": "claude",
                "usd": payload.get("total_cost_usd")})       # ← field name: VERIFY once live
        return payload["result"].strip()                     # ← ditto
    except (json.JSONDecodeError, KeyError):
        return raw.strip()          # parse failure must NEVER kill the head — fall back to text
```

**First action of this step:** run `claude -p --output-format json "say hi"` once and
eyeball the real field names (`total_cost_usd`, `result` per docs — trust but verify,
that's the house rule). Codex cost: `codex exec --json` emits token-count events —
parse later if you care; skip for v1 rather than doubling the parsing surface.

**Done when:**
- [x] Every new ledger row carries `run_id`; `trace(run_id=...)` returns one coherent thread. *(verified: run 9a28af101d20)*
- [x] After one duel, the ledger holds a `head_call` row per head per round with real seconds. *(verified solo — 9.32s; the duel path shares `_safe`, so both heads record identically)*
- [x] One ask-mode question shows its cost in the ledger without opening the Claude console. *(verified: $0.0446 `head_cost` row; judge verdicts now also persist as `judge` rows — was in-memory only)*

---

## Step 2 — pytest + stub heads + CI smoke  (~a day)

**Why:** adoptability. This week's assertions live in throwaway shell history; a repo a
friend can clone-and-test is the difference between "works on my machine" and "works".

Do this **before** retries (step 3): retry/backoff logic is exactly the kind of code
you want to develop against a stub that fails on demand, not against real quota errors.

### 2a. Stub heads — zero code changes needed

The seam already exists: `cfg.claude_command` / `cfg.codex_command` are config knobs
with env overrides. A stub is just a shell script on that knob:

```sh
#!/bin/sh
# tests/stubs/claude — canned head. Contract with backends.py:
# prompt arrives on STDIN (must be swallowed), answer goes to stdout.
cat > /dev/null
echo "STUB CLAUDE: the moon is made of rock"
```

```sh
#!/bin/sh
# tests/stubs/codex — prompt arrives as argv (codex exec style), not stdin.
echo "STUB CODEX: disagree — the moon is made of cheese"
```

Failure-mode variants (these are what step 3's tests will feed on):

```sh
# tests/stubs/claude-flaky-quota:  echo "429 rate limit" >&2; exit 1
# tests/stubs/claude-slow:         sleep 10; echo "too late"   (test head_timeout)
# tests/stubs/claude-flaky-once:   fail on 1st call, succeed on 2nd (marker file in $TMPDIR)
```

`chmod +x` them and commit — git preserves the executable bit.

### 2b. Port the shell-history assertions to pytest

```
tests/
  conftest.py          # the fixture EVERY test needs (see trap below)
  stubs/               # claude, codex, claude-flaky-quota, claude-slow, claude-flaky-once
  test_config.py       # default < toml < env precedence; bad knob → default, no crash (config.py:81)
  test_ledger.py       # record→trace roundtrip; filter equality; 0o600 perms; session_start scoping
  test_debate.py       # the e2e: run() with stub heads
  test_backends.py     # JSON parse + text fallback (step 1c)
```

The fixture that avoids the audit trap (cached config → tests write your REAL ledger):

```python
# conftest.py
@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND",  str(STUBS / "codex"))
    ledger._cfg.cache_clear()        # ← the trap: lru_cache outlives monkeypatch
    yield
    ledger._cfg.cache_clear()
```

The one end-to-end test that proves the whole debate loop without an API key:

```python
def test_full_debate_with_stubs():
    cfg = load_config()                      # picks up stub env from the fixture
    result = debate.run("moon?", rounds=1, judge=None, cfg=cfg, console=Console(quiet=True))
    assert "STUB CLAUDE" in result.proposer_final
    assert "STUB CODEX" in result.adversary_final
    rows = trace(role="debate")
    assert {r["round"] for r in rows} <= {0, 1}
    assert all(r["run_id"] == rows[0]["run_id"] for r in rows)    # step 1 pays off here

def test_dead_head_degrades_not_dies():
    # point COUNCIL_CODEX_COMMAND at a stub that exits 1 →
    # run() still returns, adversary_final contains "unavailable",
    # ledger has a head_error row. This is _safe()'s contract, now enforced forever.
```

### 2c. CI

```yaml
# .github/workflows/ci.yml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: council --version          # install smoke: entry point resolves
      - run: pytest -q                  # full debate loop runs — stubs, no keys, no cost
```

Plus one pyproject line so `pip install -e ".[dev]"` works:

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
```

**Done when:**
- [ ] `pytest -q` green locally with no network and no `~/.claude` credentials.
- [ ] GitHub Actions badge green on push.
- [ ] The clone test, run literally: hand the repo to a friend, time `git clone` → green `pytest`. Target: under 5 minutes.
- [ ] README states the clean property you already have: *no secrets in this repo — credentials live inside the claude/codex CLIs.*

---

## Step 3 — retries + quarantine  (~a day)

**Why:** the line between script and system. You've lived the motivating example
(codex quota death mid-session). `_safe` already degrades; what's missing is *trying
again before giving up* and *leaving a readable corpse when it still fails*.

### 3a. Classify before retrying

Retrying a malformed flag three times is just failing slowly. Only transients earn retries:

```python
# backends.py
def _classify(exc) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "transient"
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "").lower()
        if any(m in stderr for m in ("429", "rate limit", "quota", "overloaded",
                                     "529", "503", "connection")):
            return "transient"                    # the world was flaky
        return "permanent"                        # WE are wrong (bad flag, bad auth) — fail fast
    return "permanent"
```

### 3b. Exponential backoff, idempotent by construction

Heads are stateless one-shot subprocesses — calling one twice has no side effects, so
the "did this already happen?" question every retry must ask is answered by design.
Wrap the retry around `fn` inside `_safe` (label + ledger access are already there):

```python
# config.py — two new knobs (the _apply coercion at config.py:62 handles int/float already)
head_retries: int = 2          # attempts AFTER the first try
retry_base_delay: float = 1.0  # 1s → 2s → 4s

# debate.py — inside _safe, replacing the bare fn(msg, cfg) call
for attempt in range(cfg.head_retries + 1):
    try:
        return fn(msg, cfg)          # (then the existing empty-check)
    except Exception as e:
        kind = _classify(e)
        record({"role": "head_retry", "head": label, "attempt": attempt,
                "kind": kind, "error": str(e)[:500]})
        if kind == "permanent" or attempt == cfg.head_retries:
            raise                    # falls into _safe's existing except → degrade + quarantine
        time.sleep(cfg.retry_base_delay * 2 ** attempt)
```

### 3c. Quarantine — a readable corpse, not a silent gap

On *final* failure (after retries), write a human-readable postmortem next to the ledger:

```python
# ledger.py (it's persistence — same file owns it, same 0o600 discipline as ledger rows)
def quarantine(head, exc, context: dict) -> Path:
    qdir = _cfg().ledger_path.parent / "quarantine"      # ~/.council/quarantine/
    qdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = qdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{RUN_ID}-{head}.md"
    path.write_text(f"""# head failure: {head}
run: {RUN_ID}   when: {time.ctime()}   class: {context['kind']}   attempts: {context['attempts']}

## what was asked (first 500 chars)
{context['question'][:500]}

## what stderr said
{context['stderr'][-2000:]}

## what to do
transient → the world was flaky, rerun when the provider recovers.
permanent → the command is wrong; check argv above against the CLI's --help.
""")
    os.chmod(path, 0o600)            # same privacy stance as the ledger (full prompt text inside)
    record({"role": "quarantined", "head": head, "path": str(path)})
    return path
```

Call it from `_safe`'s except block. A flaky-API day now yields a folder of postmortems
instead of silent gaps.

**Done when:**
- [ ] `claude-flaky-once` stub: debate succeeds on retry; ledger shows one `head_retry` row. (Step 2's stubs make this a pytest, not a prayer.)
- [ ] `claude-flaky-quota` stub with retries exhausted: debate degrades single-voice, a quarantine .md exists and reads like a postmortem, exit code 0.
- [ ] Permanent-class failure (bad flag): exactly one attempt — no slow-motion failing.
- [ ] A bad day requires **zero manual intervention** — and you can show the before/after failure rate, which is why step 1 had to exist first.

---

## Step 4 — `council report` + `council show`  (~half a day) — ✅ SHIPPED 5 Jul 2026 (out of order: pulled ahead of steps 2–3 with the REPL-interactivity work; `/report` `/show` also live inside the REPL)

**Why:** the payoff of step 1. Also where the "artifact store" bonus quietly gets
absorbed: run IDs already make every debate addressable, so `show` is a formatter,
not a storage system.

```python
# cli.py
@cli.command()
@click.option("--days", default=7)
def report(days):
    """The week in review: runs, cost, latency, failure rate."""
    rows = [r for r in trace() if r["ts"] > time.time() - days * 86400]
    by_run = group_by(rows, "run_id")

    calls   = [r for r in rows if r.get("role") == "head_call"]
    errors  = [c for c in calls if not c["ok"]]
    retries = [r for r in rows if r.get("role") == "head_retry"]
    lat     = sorted(c["secs"] for c in calls if c["ok"])

    # THE TRAP (step 0): code-mode total_cost_usd is a RUNNING session total.
    # Sum of per-run MAXes — never sum the rows.
    code_cost = sum(max(r["total_cost_usd"] for r in run if r.get("total_cost_usd"))
                    for run in by_run.values() if any(...))
    ask_cost  = sum(r["usd"] for r in rows if r.get("role") == "head_cost" and r["usd"])

    table("runs", len(by_run)); table("head calls", len(calls))
    table("failure rate", f"{len(errors)/max(len(calls),1):.0%} ({len(retries)} retried)")
    table("latency median/p95", f"{percentile(lat,50)}s / {percentile(lat,95)}s")
    table("cost (code + ask)", f"${code_cost + ask_cost:.2f}")
    # per-head split: claude vs codex vs judge — one groupby on c["head"]

@cli.command()
@click.argument("run_id")
def show(run_id):
    """Replay one run: rounds, verdicts, errors — the artifact, addressable."""
    rows = trace(run_id=run_id)     # free since step 1a
    if not rows: raise click.ClickException(f"no run {run_id}")
    # walk rows in order: user → debate rounds (reuse debate._present for the
    # two-column layout) → judge synthesis → head_errors/quarantine pointers
```

Add the last-line hint so IDs are discoverable without opening the ledger:
after a run ends, print `run a1b2c3 — council show a1b2c3 to replay`.

**Done when:**
- [x] `council report` answers, in one screen: how many runs this week, what it cost, median/worst latency, failure rate. *(verified: 3 runs, $0.04, 9.3s median, 0% failures)*
- [x] `council show <id>` replays any debate from the last month, including what failed. *(verified; unknown ID lists recent IDs instead of erroring)*
- [x] Both work on an empty ledger without stack-tracing (fresh-clone user, day one). *(verified against an empty temp ledger)*

---

## Step 5 — bonuses, strictly on demand

### 5a. The parallel number  (~30 min — cheapest item on this page)

Fan-out already exists (`debate.py:59`). Only the *number* is missing:

```python
# tests/test_parallel.py — stub heads that each sleep 3s
t0 = time.monotonic()
debate.run("q", rounds=0, judge=None, cfg=stub_cfg_with_slow_heads)
assert time.monotonic() - t0 < 4.5      # parallel ≈ 3s; serial would be ≥ 6s
```

Then measure once with real models and write the sentence in the README:
*"duels run ~2× faster than serial because both heads think concurrently."*

### 5b. Cost budget warning  (~1 hour, after step 1c)

```python
# config.py:  budget_usd: float = 0.0        # 0 = off
# render.py already reads context.json each status tick (render.py:81):
if cfg.budget_usd and cost > cfg.budget_usd:
    status_line += "  ⚠ over budget"        # in-session nag, red
# ask mode: same check where head_cost rows accumulate per run
```

Slack/email alerts: **not yet** — they earn their place the day unattended runs exist.

### 5c. Shadow mode  (~a day — heaviest, last, career-aligned)

The understudy pattern: same question through two configs, diff before switching.
It's an eval harness in miniature — the one bonus aligned with the own-the-evals
thesis — and it reuses step 2's stubs for cheap tests and step 1's run IDs for
addressable arms.

```python
@cli.command()
@click.option("-p", "--prompt", required=True)
@click.option("--set", "overrides", multiple=True)   # e.g. --set judge_style=reasoning
def shadow(prompt, overrides):
    """Run the question under config A (current) and config B (A + overrides), then diff."""
    cfg_a = load_config()
    cfg_b = apply_overrides(load_config(), overrides)    # reuse config._apply
    ra = debate.run(prompt, ..., cfg=cfg_a); record({"role": "shadow_arm", "arm": "A", ...})
    rb = debate.run(prompt, ..., cfg=cfg_b); record({"role": "shadow_arm", "arm": "B", ...})
    present_side_by_side(ra, rb)     # reuse the Table.grid pattern from debate._present
    # v2, only if v1 gets used: a third blind call judges which arm answered better —
    # reusing _synthesize's shuffle-and-strip-labels machinery
```

---

## Sequence, effort, and an honest note

| Order | Step | Effort | Unblocks |
|---|---|---|---|
| 1 ✅ | Run IDs + latency + ask-cost | hours | report, show, budgets, "show the failure rate" |
| 2 | pytest + stubs + CI | a day | safe retry development, shadow replay, the clone test |
| 3 | Retries + quarantine | a day | the zero-intervention bad day |
| 4 ✅ | `report` + `show` | half a day | the weekly answer to "what happened?" |
| 5a | Parallel number | 30 min | one README sentence |
| 5b | Budget warning | 1 hour | — |
| 5c | Shadow mode | a day | the eval-harness story |

The honest note, kept from the source discussion: for a solo tool, full failure rigor
exceeds actual need. The legitimate reason to build it anyway is that each "done when"
above is precisely a question a senior engineer asks to separate *wrote a wrapper* from
*built a platform* — and this repo is doing double duty as that evidence. Two weeks of
daily-driving council supplies the real runs, failures, and costs that make steps 1
and 4 report something true instead of something staged.
