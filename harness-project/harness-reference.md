# Harness Reference

---

## 2. The two main jobs

1. **Codex commit gate** — every commit through Claude's Bash tool is reviewed by Codex (a *different* model) before it lands; **CRITICAL/HIGH blocks**; everything logged locally.
2. **Discipline framework** — RCA protocol, checklist/proof gates, assertion guard, optional multi-agent review workflows.

| Harness role | How the-harness does it |
|---|---|
| Constrain behavior | Hooks block bad commits, ungrounded claims, sloppy checklists |
| External safety check | Codex reviews what Claude is about to commit |
| Audit trail | Local `codex-review-audit.log` per repo |
| Structured protocols | `/rca`, `/disciplined`, `/deep-review` |

---

## 3. Practical workflow

- Work normally in Claude Code → hooks fire automatically → Codex reviews on commit → if blocked, **Claude usually fixes and retries** (you wait through each Codex call) → you're mostly watching unless you intervene.
- **Setup:** `install.sh` does **not** prompt for API keys. You authenticate Codex separately (`codex` / `codex login`) **before** install; `doctor.sh` verifies it works.

---

## 4. Commit gate mechanics (deeper than README)

- **No auto-retry loop in the harness** — one `git commit` = one Codex call. If blocked, *Claude* chooses to fix and retry; you wait through each attempt.
- `git add && git commit` in one Bash call is **blocked on purpose** — PreToolUse runs *before* bash executes, so the `add` wouldn't be in the staged diff Codex reviews.
- Same blocking for `git -C`, `cd other-repo && commit`, `git commit -a`, pathspec commits — all flagged as **wrong-tree / wrong-content** risks.
- **Large diffs aren't truncated** — over `COMMIT_DIFF_LINE_CAP` (default 8000 lines) the commit is **blocked, not silently cut**.
- **MEDIUM/LOW findings don't block** — only **CRITICAL/HIGH** stop the commit.

---

## 5. What gets stored vs sent

| Data | Where it goes |
|---|---|
| Full staged diff + commit message | **OpenAI** (Codex review) |
| Full Codex findings (`FINDINGS:` block) | **Claude session** (hook output) |
| Audit log line | **Local** `.git/codex-review-audit.log` — **metadata only** (verdict, `tree_sha`, subject), **not** the full review text |

- The audit log is **never pushed** with git. Teammates/CI don't see it unless they have your machine or you re-run review in CI.

---

## 6. Classifier internals (`REVIEW-AND-AUDIT.md`)

- Commit commands are parsed by **two independent parsers** (bashlex AST + hand-rolled shlex) — **most restrictive wins**.
- Parser disagreement is logged as `parser_disagree=true` in the audit line.

---

## 7. Override details (README glosses over)

- Raw override needs **both** `CODEX_REVIEW_OVERRIDE=1` **and** a non-empty `CODEX_REVIEW_OVERRIDE_REASON` — override alone is refused.
- Override-token path **still runs a live Codex re-audit**; the token is a *pointer*, not a trusted pass.
- `harness=true` lines in the audit log are **test-only** and never count as proof of review.

---

## 8. Hook behavior nuances

- **Fail-closed vs fail-open differs by hook:**
  - Commit gate → **fail-closed** (blocks if Codex/audit fails).
  - Assertion guard → **fail-open** on malformed input (warns, doesn't block).
- **RCA defer guard can false-positive** — e.g. writing "missing telemetry is by design" on a tracker file can trip it; docs note using `DEFER_OVERRIDE` for genuine value-choices.
- **Stop hook** blocks ending a turn if new commits aren't review-certified *or* if Claude makes ungrounded root-cause claims.

---

## 9. Architecture — three "roots" (`hooks/lib/common.sh`)

The harness always tracks three paths separately:
1. **Asset root** — where harness scripts live.
2. **Target repo** — the project you're committing in (from cwd).
3. **Git common dir** — where audit log / override token / stop baseline live.

> Machine-wide install still writes audit state **per target repo**, not into the harness checkout.

**Where state lives** (per repo, in its git common dir, shared across worktrees, never committed):
- Audit log: `$(git rev-parse --git-common-dir)/codex-review-audit.log`
- Override token: `codex-override-token.json`
- Stop baseline: `codex-review-stop-baseline`

---

## 10. Cost / ops reality

- **You pay** per Codex call (your ChatGPT or API key) — **not the harness author**.
- Default Codex timeout **600s**; hook timeout **960s** — a blocked-then-retry commit can mean **multiple minutes** sitting in session.
- Each gated commit = one Codex call, usually tens of seconds.
- **First-run baseline:** stop baseline set to current `HEAD` (**adopt-existing-history**) — old commits are warn-only; only **future** commits need certification.
  - `HARNESS_STOP_BASELINE_MODE=strict-from-root` (or `strict-from-origin-main`) for a stricter baseline.

---

## 11. Configuration

- `HARNESS_*` / `CODEX_REVIEW_*` knobs are **environment variables** the hooks read — set them in the env Claude Code runs hooks in (your shell before launching `claude`, or the `env` block of settings).
- Tuning knobs: `CODEX_REVIEW_MODEL`, `CODEX_REVIEW_TIMEOUT`, `COMMIT_DIFF_LINE_CAP` (default 8000).
- **Disabling a noisy hook:** opt-outs e.g. `HARNESS_RCA_ENFORCE=0`; per-commit `PROOF_BEFORE_CHECKMARK_OVERRIDE=1` / `CHECKLIST_GUARD_OVERRIDE=1` / `PLAN_ACCEPTANCE_OVERRIDE=1` (with a reason). Remove a hook entirely → delete its entry from your settings' `.hooks` block (or the plugin's `hooks/hooks.json`).
- **Uninstall:** Plugin → disable in `/plugin` (or drop the `--plugin-dir` / marketplace reference).

---

## 12. Install / wiring (from `install.sh`, not README)

- Skills/agents must land in `.claude/skills/` and `.claude/agents/` — Claude Code doesn't discover them inside the `the-harness/` subtree.
- Per-repo install defaults to `.claude/settings.local.json` (**gitignored**) because paths are machine-specific absolute paths.
- `--committed` flag exists but **warns loudly** — breaks for collaborators; **plugin is the team path**.
- Plugin install **namespaces** skills as `/the-harness:rca` etc.; `install.sh` uses `/rca` directly.
- `hooks.manifest.json` is the **single source of truth**; plugin + settings wiring are generated from it.
- **Workflows can't ship in the plugin** — plugins can't bundle workflows; you need `install.sh`.

---

## 13. Multi-agent workflows

- Rationale: a single LLM reviewing its own work is brittle — it shares the blind spots of the pass that produced it. Independent agents review from different angles; each finding is **adversarially verified by a separate agent**; only confirmed issues survive. Treat as the **default** way to review non-trivial work.
- (The pre-publish audit caught a **real proprietary-IP leak** in this repo that a single review pass had missed.)
- Installed into `.claude/workflows/` by default (`HARNESS_INSTALL_WORKFLOWS=1`; set `0` to skip), invoked as slash commands:
  - `/the-harness-ultra-review [commit-range]` — multi-perspective adversarial review: a Claude reviewer per dimension (correctness / security / tests / performance / maintainability) **plus a cross-model Codex pass**, every finding independently verified. Staged diff by default, or a commit range like `origin/main..HEAD`. Complements the single-Codex commit gate with an on-demand multi-agent panel.
  - `/the-harness-pre-publish-audit ["what counts as sensitive"]` — secrets / internal-leak / git-history / packaging scanners with adversarial verification → **GO/NO-GO**, before making a repo public or pushing to a new remote.
- **Runtime gate:** Claude Code's dynamic-workflows engine — requires Claude Code **≥ v2.1.154**, a **paid plan**, and "Dynamic workflows" enabled in `/config` (off by default on Pro). Plugins can't bundle workflows.

---

## 14. Enforcing it for a team

- An install (plugin or otherwise) is **opt-in per developer** — it gates the commits *that developer* makes through Claude Code on their machine. **Not server-side**, and it does **not** gate commits made outside Claude Code.
- For a policy a teammate cannot skip:
  - **Distribute as a plugin marketplace reference** committed to the repo, so every teammate enables the same plugin (portable `${CLAUDE_PLUGIN_ROOT}` paths).
  - **Add a CI / server-side check** as the enforceable layer. The audit log lives in each clone's `.git` and isn't pushed, so CI can't read a contributor's local log — instead have CI **re-review the pushed range** (`the-harness review diff origin/main..<pushed-head>` or `scripts/codex-review.sh diff …`) and fail on a CRITICAL/HIGH verdict. Certifies *content*, independent of who committed it or how.

**Recommended CLAUDE.md additions:** the gate blocks several command shapes by design (combined `git add && git commit`, `git -C` / `cd`-escape commits, heredocs in the commit command, undeclared worktree commits, `commit -a` / pathspec). Paste `docs/CLAUDE-MD-SNIPPET.md` into your project's CLAUDE.md (or the agent's system prompt) so the assistant works *with* the gate instead of repeatedly hitting blocks.

---

## 15. Repo structure (folder tour)

**Mental model:**
- `hooks/` = traffic cops (intercept Claude tool use)
- `scripts/` = engines + prompts (do the actual work)
- `skills/` = playbooks for the AI
- `workflows/` = multi-agent review orchestration
- `tests/` = prove the cops and engines can't be bypassed
- `docs/` = operator manual

### hooks/ — ~18 hook scripts + 1 Python classifier + shared `lib/common.sh`
Each script is one policy (split so you can disable one guard without touching others; each has focused tests). Each hook entry has a matcher (`Bash` vs `Edit|Write`), order, timeout, on/off flag.

| Category | Scripts |
|---|---|
| Commit gate | `codex-commit-review.sh`, `staged-diff-self-review.sh`, `codex-item-review.sh` |
| Wrong-tree / audit | `verify-reviewed-stop.sh`, `codex_commit_classifier.py` |
| Proof / checklist | `proof-before-checkmark.sh`, `checklist-guard.sh`, `plan-acceptance-gate.sh` |
| RCA discipline | `rca-guard.sh`, `no-defer-without-rca.sh`, `codex-rca-review.sh` |

### scripts/ — reusable engines the hooks call
| Script | Role |
|---|---|
| `codex-review.sh` | Core wrapper — calls `codex exec` for commit/plan/rca/item/diff reviews |
| `codex_prompts/*.md` | Prompt templates Codex reads (commit, plan, rca, item, etc.) |
| `work-item.sh` / `proof-item.sh` | Checklist workflow (`start` → capture proof → `finish`) |
| `verify-codex-reviewed.sh` | Prove commits were reviewed (audit log + tree SHA) |

**Single source of truth:** `hooks.manifest.json` lists every hook. `scripts/gen-distribution.sh` generates `hooks/hooks.json` (plugin wiring), `settings/settings.harness.json` (install wiring), and `.claude-plugin/plugin.json` — so hooks and settings never drift.

**Disciplined workflow** (in `COMMANDS.md` / skills, not README): `/disciplined` runs a Codex progress audit first (`codex-review.sh progress`) before touching checklist items. Manual scripts: `work-item.sh`, `proof-item.sh`, `verify_phase_gate.sh` — checklist → proof → Codex item review on finish. Default proof dir `build/proof-items`; checklists `docs/**/*checklist*.md`.

### skills/ — three slash-command playbooks Claude discovers
- `disciplined` — checklist → proof → verify workflow
- `rca` — 8-step root-cause analysis
- `deep-review` — adversarial review before landing docs/prompts

> Skills teach *how to work*; hooks enforce the same rules mechanically.

### workflows/ — JavaScript multi-agent orchestration
- `the-harness-ultra-review.js` — panel review (correctness, security, tests, … + Codex)
- `the-harness-pre-publish-audit.js` — secrets/leak scan before going public
- `.js` (not shell) because Claude Code workflows are JS modules with `agent()`, `parallel()`, etc.

### agents/
- `codex.md` — defines a Codex subagent Claude can delegate to.

### bin/
- `the-harness` — thin launcher exposing `doctor`, `review`, `verify`, `override-token`.

### settings/
- `settings.harness.json` — hook wiring template for `install.sh` (generated from manifest).

### .claude-plugin/
- Plugin metadata so you can enable the harness machine-wide via `claude --plugin-dir`.

### Top-level files
| File | Purpose |
|---|---|
| `harness.config.sh` | Default `HARNESS_*` knobs (overridable via env) |
| `install.sh` | Install into a repo, globally, or as plugin |
| `doctor.sh` | Preflight: codex, jq, bashlex, asset layout |
| `hooks.manifest.json` | Canonical hook list |
| `SYNC.md` | Provenance map from the original upstream repo (one-time port, not ongoing sync) |

**Why shell everywhere?** (1) Claude Code hooks expect shell commands — stdin JSON in, exit code + stderr out. (2) Portable — works in any git repo; no Node/Python runtime required for the gate itself (except the classifier).

### tests/
Tests **security-critical behavior** (commit blocking, wrong-repo detection, fail-closed paths). CI runs them with a **fake `codex` stub** so no real API is needed. Shared test harness `tmp-git-repo.sh` creates temp git repos, Codex stubs, and helpers to invoke hooks.

| Test | What it verifies |
|---|---|
| `test-classifier.sh` | Commit-command parser blocks bypass shapes (heredocs, etc.) |
| `test-target-git-common-dir.sh` | Audit log anchors to the target repo, not harness install dir |
| `test-linked-worktree.sh` | Commits from git worktrees still hit the right audit log |
| `test-certification.sh` | `verify-codex-reviewed.sh` |
| `test-bootstrap-review.sh` | Bootstrap review refuses empty stages, etc. |
| `test-override-token-installs.sh` | Override-token flow across install layouts |
| `test-install-layout-resolution.sh` | `install.sh` copies the right tree + skills/workflows paths |
| `test-distribution-parity.sh` | Generated `hooks.json` / settings match manifest |
| `test-config-complete.sh` | All config knobs documented |
| `test-workflows.sh` | Orchestrates the JS workflow tests |

- **JS tests** (`*.test.mjs`): workflow argument injection guards + orchestration logic for `workflows/*.js` (parsed/stubbed without a full Claude Code runtime).
- **Fixtures:** e.g. `fixtures/assertion-transcript.jsonl` — fake Claude transcript for assertion-guard tests.

---

## 16. Documentation map (`docs/`)

| Doc | Covers |
|---|---|
| `REVIEW-AND-AUDIT.md` | Gate, classifier, fail-closed shapes, audit log + certification, Stop quarantine, two override paths |
| `SCENARIOS.md` | What capability fires for which scenario (commit, checklist mark, deferral, session stop, …) |
| `HOOKS.md` | One entry per hook (trigger, matcher, what it blocks, config knobs, default-enabled) |
| `SKILLS.md` | The `disciplined`, `rca`, `deep-review` skills |
| `CONFIGURATION.md` | Every `HARNESS_*` knob, default, what it controls |
| `CODEX-SETUP.md` | Installing/authenticating the `codex` CLI |
| `codex-override-token.md` | The override-token runbook |
| `COMMANDS.md` | Manual command reference (launcher, reviewing artifacts, work-item workflow, override flows) |
| `WORKFLOWS.md` | Multi-agent workflows: what ships, runtime requirements, install scope, adding your own |
| `CLAUDE-MD-SNIPPET.md` | Ready-to-paste block for your project's CLAUDE.md |

---

## 17. The "README gaps" thesis (one-line takeaway)

The README sells the **what** (Codex gate + discipline). What it underplays:
- It's a **local Claude Code plugin, not agent infra**.
- **OpenAI sees your diffs**; the author doesn't.
- Install **won't set up Codex for you**.
- **Many commit shapes are deliberately blocked**.
- **Retries are Claude-driven, not harness-driven**.
- **Audit log is metadata-only and stays local**.

---

## 18. Gaps in THIS parse (what I don't yet have)

- The final screenshot starts a separate doc: **"Market and Open-Source Landscape Around the-harness" → "Executive summary"** — only the header is visible. **The entire market/landscape analysis is missing.**
- A few code-font strings were transcribed from photos and may have minor OCR error (verify against the actual repo before quoting verbatim).
