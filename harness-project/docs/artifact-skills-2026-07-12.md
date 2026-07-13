# Artifact & skill power for council heads — findings (12 Jul 2026)

Companion to `docs/debate-techniques-2026-07-12.md` (same register). Question: Claude Code's interactive sessions produce polished artifacts and lean on rich built-in skills, yet council's headless heads (`claude -p`, `codex exec`) feel limited — what exactly is the gap, and how much of it is portable? Every entry verified 12 Jul 2026 against primary sources: the installed binaries (claude **2.1.207** native install, codex-cli **0.142.5**), live headless probes (commands quoted inline), official docs (code.claude.com, learn.chatgpt.com, agentskills.io), and raw files via `gh api … Accept: raw`. Verdict per entry = **ADOPT** / **OPTION** / **SKIP**.

## A. How Claude Code makes artifacts — two layers, one portable

### A1. The Artifact tool is harness-side hosting; the *power* is model-side skills — finding
Interactive Claude Code artifacts = (1) an **Artifact tool** the harness provides — takes an HTML/Markdown file the model wrote, wraps it in a doctype/head/body skeleton, publishes it to claude.ai hosting with CSP, theming, and versioning — plus (2) **design skills** (`artifact-design`, `dataviz`) that shape *what the model writes* before the tool ever runs. The quality users perceive is mostly layer 2: the tool is a file-uploader; the design guidance is the craft.
**Probe:** `claude -p "list your tools + skills" --output-format json` (cost $0.19) returned the full headless roster: tools = `Agent, Bash, Edit, Read, ReportFindings, ScheduleWakeup, Skill, ToolSearch, Workflow, Write, Cron*, Task*, Monitor, NotebookEdit, SendMessage, WebFetch, WebSearch, …` — **no Artifact tool**. Skills = the user's `~/.claude/skills` entries **plus bundled skills including `dataviz`** — but **not `artifact-design`** (it travels with the Artifact tool). So: hosted artifacts are interactive-only; skills and file-writing are fully headless.
**Council:** an artifact in council = the model writes one self-contained HTML file; council owns the "publish" step locally (D4). Nothing about layer 2 requires the harness. **ADOPT** the framing.

### A2. Built-in skills live inside the 230 MB binary — not extractable, and don't need to be
`~/.local/bin/claude → ~/.local/share/claude/versions/2.1.207` (single 230 MB executable; no skill files on disk anywhere under `~/.claude` or the install dir). `rg -a` over the binary finds the bundled-skill roster as minified JS constants: `"artifact-design"`, `"artifact-capabilities"`, `"dataviz"`, `"code-review"`, `"code-walkthrough"`, `"pr-explainer"`, `"verify"`, `"simplify"`, `"commit"`… Scraping the full text out of a proprietary binary would be both brittle and unlicensed — and B4 makes it unnecessary. **SKIP** extraction.

## B. Skills — an open standard both heads already speak

### B2. Agent Skills is an open standard; Codex is on the supporters list — finding
[agentskills.io](https://agentskills.io): a skill = a folder with `SKILL.md` (frontmatter `name` + `description`, then instructions) plus optional `scripts/`, `references/`, `assets/`. Loading is **progressive disclosure** — agents see only name+description until a task matches, then read the body. "Originally developed by Anthropic, released as an open standard"; the client showcase lists Claude Code, **OpenAI Codex**, Gemini CLI, Cursor, Copilot/VS Code, Goose, OpenCode, and ~35 more. One skill folder is legible to both council heads.

### B2a. Claude Code loading rules ([code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills))
- Locations: personal `~/.claude/skills/<name>/SKILL.md` · project `.claude/skills/` (starting dir **and every parent up to repo root**, plus nested dirs on demand) · plugins (`<plugin>/skills/`, `--plugin-dir <path-or-zip>`, `--plugin-url`).
- `--add-dir` **loads that directory's `.claude/skills/` too** (documented exception — add-dirs otherwise grant file access only). Symlinked skill dirs are followed.
- Same-name precedence: enterprise > personal > project; any level **overrides a bundled skill of the same name**.
- Frontmatter worth knowing for a contract skill: `disable-model-invocation` (human-only), `allowed-tools`/`disallowed-tools` (tool pool while the skill is active), `context: fork` + `agent` (run in a subagent), `${CLAUDE_SKILL_DIR}` substitution for bundled scripts.
- Headless: bundled skills are "available in every session" unless `disableBundledSkills`; even `--bare` mode keeps them — its help text says "Skills still resolve via /skill-name". Confirmed by the A1 probe: user + bundled skills all present under `-p`.

### B2b. Codex loading rules + live probe — **skills work under `exec`**
Docs ([learn.chatgpt.com/docs/build-skills](https://learn.chatgpt.com/docs/build-skills), redirect target of developers.openai.com/codex/skills): scans `$CWD/.agents/skills`, `$CWD/../.agents/skills`, `$REPO_ROOT/.agents/skills`, `$HOME/.agents/skills`, `/etc/codex/skills`, plus system-bundled; same SKILL.md format, "build[s] on the open agent skills standard"; per-skill disable via `[[skills.config]]` in `config.toml`. The docs don't say whether `codex exec` (council's mode) loads them.
**Probe (12 Jul):** planted `~/.agents/skills/probe-marker/SKILL.md` whose body says to emit a token when skills are enumerated, then ran `codex exec --ephemeral -s read-only "list your skills"`. Output listed system skills (`imagegen, openai-docs, plugin-creator, skill-creator, skill-installer`), the planted skill, **and the token — the skill was discovered AND its instructions obeyed, non-interactively**. (Probe skill deleted after.)

### B4. `anthropics/skills` — the artifact-building skills, public and (mostly) Apache-2.0 — ADOPT
[github.com/anthropics/skills](https://github.com/anthropics/skills): official public repo, installable as a Claude Code plugin (`/plugin marketplace add anthropics/skills`). Contents include **`web-artifacts-builder`** — verified **Apache-2.0** from its `LICENSE.txt` — whose SKILL.md is the real claude.ai artifacts pipeline: init a React 18 + TypeScript + Vite + Tailwind + shadcn/ui project (`scripts/init-artifact.sh`, 40+ components pre-installed), develop, then **`scripts/bundle-artifact.sh` compiles everything into a single self-contained HTML file**. Also public: `frontend-design`, `canvas-design`, `theme-factory`, `algorithmic-art`, `mcp-builder`, `webapp-testing`, `skill-creator`. Document skills (`docx/pdf/pptx/xlsx`) are **source-available, not open source** — reference only. Repo disclaimer: shipped Claude implementations "may differ from what is shown in these skills" — so this is a sibling of the binary's `artifact-design`, not a byte-identical copy; it is the *licensed* version of the capability.
**Council landing:** this answers "can I have a copy of the skills Claude Code invokes" — yes, for the artifact/design family, legally, from Anthropic's own repo. Vendor or plugin-install; add NOTICE attribution alongside the existing omnigent entries.

## C. Structure enforcement — the primitives an output contract can stand on

### C1. Both heads accept a JSON Schema for their final answer — finding, load-bearing
- claude: `claude -p --json-schema '<schema>'` — help text verbatim: "JSON Schema for structured output validation. Example: {"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}". Print-mode only — exactly council's mode.
- codex: `codex exec --output-schema <FILE>` — "Path to a JSON Schema file describing the model's final response shape"; pairs with `--json` (JSONL events) and `-o/--output-last-message <FILE>` (clean final-answer channel).
**Council:** a duel output contract does not have to be prompt-and-pray. The machine-parseable block (stances, confidence, verdict epilogue) can be schema-*enforced* per head, per round. This is the enforcement half the debate-techniques catalog assumed would be parsing heuristics.

### C2. Per-run instruction injection, both heads — finding
- claude: `--append-system-prompt` / `--append-system-prompt-file` (contract text on top of the default prompt), `--system-prompt[-file]` (replace), `--settings <file-or-json>`, `--agents <json>`.
- codex: `AGENTS.md` is the project-instructions channel — discovery depth controlled by `project_doc_max_bytes`, alternate filenames via `project_doc_fallback_filenames` (config-advanced docs). Council can write a per-duel `AGENTS.md` (or fallback-named file) into the working dir. Reasoning knobs confirmed current at 0.142.5: `model_reasoning_effort` (values include `xhigh`), `model_reasoning_summary` (`none`…`detailed`), `model_verbosity` (Responses-API providers only). **Caveat:** legacy `[profiles.*]` TOML syntax was removed in codex 0.134.0+ — profiles are now separate `~/.codex/<name>.config.toml` files selected with `--profile`.

### C3. Sandbox asymmetry constrains who *builds* artifacts — finding
`web-artifacts-builder` runs npm installs and bundler scripts. Council duels run codex read-only (`-s read-only`); claude duel-arm allows `Read Grep Glob WebSearch WebFetch` only (no Bash). So under today's duel policy **neither head can execute the build scripts mid-duel**. Options, in effort order: (i) artifact = plain single-file HTML written by the model directly (no build step — covers most duel outputs; `dataviz`-style inline SVG/JS is headless-available today); (ii) arm the claude head with Bash scoped to the artifact workspace for an explicit "produce the artifact" final step; (iii) post-duel build pass outside the duel loop. The adversary can *critique* an artifact (read the HTML) under read-only regardless.

## D. Open-source artifact systems — the survey question

### D1. The artifacts skill itself is now open — headline answer
See B4. The strongest "open-source version of Claude Code's artifacts power" is Anthropic's own published skill family, Apache-2.0. Nothing third-party matches it *as a skill*, because the moat was never the renderer — it's the design guidance + bundling script.

### D2. e2b-dev/fragments — Apache-2.0, but cloud-sandbox-shaped — SKIP
[github.com/e2b-dev/fragments](https://github.com/e2b-dev/fragments) (~6.3k★, actively maintained): self-described "open-source version of apps like Anthropic's Claude Artifacts, Vercel v0, or GPT Engineer". Next.js template; multi-provider; renders by executing generated code in **E2B cloud sandboxes** (Python, Next.js, Vue, Streamlit, Gradio). Wrong shape for a local CLI: brings a hosted-sandbox dependency to solve a problem council doesn't have (untrusted multi-tenant execution).

### D3. LibreChat artifacts — Sandpack/iframe rendering — SKIP (pattern noted)
[librechat.ai/docs/features/artifacts](https://www.librechat.ai/docs/features/artifacts): open source; renders React/HTML/Mermaid via CodeSandbox's Sandpack in iframes (CSP `frame-src *.codesandbox.io`). Confirms the common OSS pattern: chat-panel artifacts = iframe + third-party bundler service. Council's terminal context makes a browser tab strictly simpler.

### D4. The council-shaped landing — local single-file HTML + `open` — ADOPT
The model (either head) writes **one self-contained HTML file**; council saves it under the run's directory (e.g. `~/.council/artifacts/<run_id>/<slug>.html`), records a ledger row, and opens it with the platform opener (`open` on macOS / `xdg-open` on Linux). A local `file://` page has no CSP, no hosting, no accounts, and diffs/replays with the ledger. Self-contained-single-file is exactly what `bundle-artifact.sh` produces and what the skill guidance teaches. ~30 lines of council code; the entire capability gap closes at layer 2 (skills), not layer 1 (hosting).

## E. Licensing / ToS summary

- `anthropics/skills` example skills (incl. `web-artifacts-builder`): **Apache-2.0, verified from the skill's own LICENSE.txt** — vendorable with attribution (NOTICE file precedent already exists in this repo). Document skills: source-available, reference only.
- Extracting `artifact-design`/`dataviz` text from the Claude Code binary: proprietary content, no license to redistribute — and redundant given the public repo. Don't.
- Agent Skills spec: open standard, open contribution ([github.com/agentskills/agentskills](https://github.com/agentskills/agentskills)).
- Planting skills/AGENTS.md for one's own authenticated CLIs is ordinary configuration of those products, same standing as the existing `~/.claude` / `~/.codex` config council already relies on.

## Top steals (value ÷ effort, council-specific)

1. **One shared contract skill, two installs** (B2, B2b) — author a single `duel-contract` skill folder; symlink/copy into `~/.claude/skills/` and `~/.agents/skills/` (or repo-local `.claude/skills/` + `.agents/skills/`). Both heads discover it by the same standard; the probe proves codex exec obeys it.
2. **Schema-enforced answer blocks** (C1) — `--json-schema` (claude) + `--output-schema` (codex) for the final-answer envelope: stance table, confidence, standalone answer. Turns the debate-techniques parsing steals (A1/A5/A8 there) from heuristics into validated structure.
3. **Local artifact publish step** (D4) — save + `open` a single-file HTML per duel when the contract requests an artifact; ledger row + replay for free.
4. **Vendor `web-artifacts-builder` + `frontend-design`** (B4) — the licensed copy of the artifact craft; arm on the claude head for artifact-producing turns (mind the C3 sandbox note).
5. **Per-run contract injection** (C2) — `--append-system-prompt-file` (claude) + generated `AGENTS.md` (codex) carry the *prose* half of the contract; the skill carries the reusable craft; the schema carries the machine half.

## Dropped as unverifiable / corrections

- **Whether binary `artifact-design` ≡ public `web-artifacts-builder`**: not claimed — the repo's own disclaimer says shipped behavior may differ. Treated as capability-equivalent, not text-identical.
- **Codex `exec` skills support**: absent from docs; asserted here **only** on the strength of the 12 Jul live probe (0.142.5). Re-probe on codex upgrades.
- **`model_verbosity`**: documented as Responses-API-only; not tested locally — quoted as the docs' claim.
- **Headless skill *auto-invocation* rate** (does `-p` reliably load a matching skill unprompted?): not measured — the probe verified discovery + listing + obedience when referenced, not spontaneous triggering frequency. A shadow-mode A/B (contract-skill on/off) is the cheap way to measure it if it matters.
