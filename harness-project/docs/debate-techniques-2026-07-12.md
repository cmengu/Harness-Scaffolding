# Debate techniques worth stealing — catalog for council (12 Jul 2026)

Companion to `docs/debate-harnesses-2026-07-12.md` (the survey; not repeated here). Every entry was verified against the primary source on 12 Jul 2026: arXiv API abstracts/full texts, raw files pulled from GitHub (`gh api … Accept: raw`). Entry format: **what it is** (implementable without the paper) · evidence · council landing · marginal cost · verdict = **STEAL NOW** / **A/B** (worth a shadow-mode test) / **SKIP**.

## A. Paper techniques (2024–2026 frontier)

### A1. Stance-commitment critique prompt (GDP) — STEAL NOW
[arXiv 2606.08457](https://arxiv.org/abs/2606.08457) "The Consistency Illusion" (Jun 2026). Finding: debate reduces *detectable* contradictions while *decreasing* semantic similarity of reasoning — agents "appear to agree more but reason less consistently" (the consistency illusion). Fix = Grounded Debate Protocol: a prompt-level rule requiring agents to (1) commit to **named facts** and (2) take an **explicit stance on each of the other agent's claims**. Evidence: alignment improvement Cohen's d +1.43 to +1.99, two datasets × two backbones, "without adding LLM calls."
**Council:** `debate.py:_CRIT_INSTR` — add: *"For each key claim in the other voice's answer, take an explicit stance — SUPPORT / REFUTE / UNCERTAIN — naming the specific fact your stance rests on."* Cost: 0 calls. Also makes A6's flag computable (stances are parseable).

### A2. Evidence-gated concession — STEAL NOW
[arXiv 2606.02866](https://arxiv.org/abs/2606.02866) "When Helping Hurts" (Jun 2026). Names **critique-induced confusion (CIC)**: "hallucinated Critic feedback that the Generator accepts uncritically" degrades generation across all 4 model families tested (−1.6 to −15.5pp) even while detection improves (+27.4pp F1). Their fixed configuration — separate critic with execution grounding + **evidence-gated generation** — is the first debate setup to beat single-agent on a generative task (+5.3pp, p<0.05). Debate-benefit condition: debate helps iff P(rescuing a wrong output) > P(destroying a correct one).
**Council:** `_CRIT_INSTR` — add: *"Adopt a criticism into your answer ONLY if it carries checkable evidence (a quote, a computation, a source); acknowledge unevidenced criticisms without adopting them."* Cost: 0 calls. This is the anti-capitulation guard the survey flagged as council's #1 gap, in prompt form.

### A3. Round-0 agreement routing (PAR) — STEAL NOW
[arXiv 2606.13197](https://arxiv.org/abs/2606.13197) ARMOR-MAD (Jun 2026), + full text. Mechanic: normalize round-0 answers (extract final answer / option letter), compute agreement = max share of any normalized answer; if ≥ τ=0.67 **skip the debate entirely** and aggregate. Evidence (gpt-4o-mini + deepseek-v3 + qwen-plus): 96.5% GSM8K vs 89.0% fixed-round debate, at 2,629 tokens/example vs ~25K on high-agreement tasks. Same instinct already in Du et al.'s code ([biography/gen_conversation.py](https://github.com/composable-models/llm_multiagent_debate): `if len(bullets) == 1: break` — "The LM just doesn't know this person so no need to create debates").
**Council (K=2):** `debate.py:run` after round 0 — if the two answers agree (extracted-answer match, else similarity ≥ threshold), record `{"event": "round0_agreed"}` and skip rounds ≥1. Saves 2 head calls on easy armed turns. Cost: 0 extra calls (one local similarity computation).

### A4. Cross-head agreement early-stop (EASE) — the `_moved` upgrade — STEAL NOW
Same source as A3. EASE recomputes the agreement share after every round and stops at φ=1.0 (all normalized answers identical), max 3 rounds. Key difference from council's `_moved`: it measures **agreement between agents**, not per-agent self-churn — `difflib` char-ratio can't distinguish "both stood firm apart" from "both converged," and two semantically identical prose answers can differ >0.10 textually.
**Council:** `debate.py:_moved`/`run` — keep the churn check, add a cross-head signal: extractable answers → normalized match; prose → cheap embedding cosine (or word-level Jaccard as zero-dep v1) between `a` and `b`; stop on (low churn) OR (high cross-agreement); at the rounds cap with high disagreement, write an `{"event": "unresolved"}` ledger row (alec's "persistent disagreement" breaker, C5) that `/judge`/gate can escalate on. Cost: 0 calls.

### A5. Both-orders judging — STEAL NOW (survey's #1, now with code specifics)
[Khan et al. 2402.06782](https://arxiv.org/abs/2402.06782); implementation visible in [ucl-dark/llm_debate](https://github.com/ucl-dark/llm_debate) `core/scoring/quotes.py`: every experiment dir carries paired `data0_judgement.csv` **and** `data0_swap_judgement.csv`, merged by `get_accuracy(df, swap=swap)` with the gold label remapped (`gold_label = "A" if not swap else "B"`).
**Council:** `debate.py:_synthesize` — second judge call with `pair` reversed; verdicts compared: agree → emit; disagree → that *is* a position-bias detection, record `{"role":"judge_order_conflict"}` and escalate or fall back to "differ". Cost: +1 judge call, only when judge is armed.

### A6. Capitulation flag: position-change-without-new-evidence — STEAL NOW
Operationalized rule found in [wan-huiyan/agent-review-panel SKILL.md](https://github.com/wan-huiyan/agent-review-panel) (fetched raw): *"Count position changes toward majority. If >50% lack new evidence → inject sycophancy alert into next round prompt for all reviewers"* (their "CONSENSAGENT" check; the cited paper itself UNVERIFIED — the rule as implemented is the verified artifact). Twin rule in [makinux/adversarial-panel SKILL.md](https://github.com/makinux/adversarial-panel): *"An unexplained full reversal is a sycophancy flag — ask that panelist for the grounds of its new agreement before accepting it."* Anthropic evidence base: [2310.13548](https://arxiv.org/abs/2310.13548) (sycophancy is preference-trained-in).
**Council (K=2):** post-round in `run()` — if head X's new answer is closer to the *other* head's previous answer than to its own (A4's similarity), AND its critique section carries no SUPPORT/REFUTE stance with named evidence (parseable after A1), record `{"role":"syco_flag","head":X}` and surface in `/report`. v1 = ledger heuristic (0 calls); v2 = one cheap probe call asking X for grounds.

### A7. Falsification conditions on claims — STEAL NOW
[adversarial-panel SKILL.md](https://github.com/makinux/adversarial-panel), verbatim: *"attach a confidence and a falsification condition (what observation would prove this wrong) to each key claim"*; generic conditions ("if evidence emerges to the contrary") are *"calibration theater and count as missing."*
**Council:** one clause in `_CRIT_INSTR` final-answer instructions + judge prompt in `_synthesize` ("prefer the answer whose claims carry checkable falsification conditions"). Cost: 0 calls.

### A8. Confidence-weighted verdicts, with recalibration — A/B
[ReConcile, 2309.13007](https://arxiv.org/abs/2309.13007) + [full text](https://ar5iv.labs.arxiv.org/html/2309.13007) + [code](https://github.com/dinobby/ReConcile). Mechanics: each round every agent outputs answer + confidence p∈[0,1]; discussion prompt shows grouped answers + confidences; team answer = argmax over answers of Σ_i f(p_i)·1[â_i = a], with the anti-overconfidence recalibration table f: 1.0→1.0, [0.9,1.0)→0.8, [0.8,0.9)→0.5, (0.6,0.8)→0.3, else 0.1. Stop when all agree, max 3 rounds. Evidence: +7.7% over debate baseline on StrategyQA (ChatGPT+Bard+Claude2); GPT-4 75.6→89.0%. Related: DebUnc [2407.06426](https://arxiv.org/abs/2407.06426) (conveying peer confidence textually or via attention helps).
**Council (K=2):** voting is degenerate, but (i) require both heads to state confidence in the ANSWER block; (ii) show the other's confidence in the round-1 critique message; (iii) hand recalibrated confidences to the judge; ledger records calibration over time. Cost: 0 extra calls, ~2 lines of prompt + a parse. Shadow-test: confidence display on/off (Du et al. showed prompt wording alone shifts consensus speed).

### A9. Jury of small judges (PoLL) — A/B (Phase-4 gate especially)
[Cohere, 2404.18796](https://arxiv.org/abs/2404.18796): a Panel of LLm evaluators — many *smaller* judges from **disjoint model families**, pooled by voting — "outperforms a single large judge, exhibits less intra-model bias… while being over seven times less expensive." Directly targets MAD's measured judge favoritism (survey: 120:77).
**Council:** `_synthesize` alternative style `judge="jury"`: 3 cheap judges (e.g. haiku + gpt-mini via codex + one more), each blind-graded both-orders, majority verdict; disagreement → ESCALATE. Cost: ~3–6 small calls, likely cheaper than 1 opus-class judge. For `council gate` where verdict reliability is the product.

### A10. Quote verification for evidence claims — A/B
[Khan et al.] repo mechanics: debater quotes are verified against the hidden passage; exact/normalized matches get `<v_quote>` tags, failures `<u_quote>` (`TranscriptParser.verify_strict`, `add_missing_quote_tags`, `normalize_text`, fuzzy `sim_values` per quote in `core/scoring/quotes.py`). The paper's core failure finding: without verified quotes, the dishonest side "could simply create an alternative narrative."
**Council:** duels already arm Read/Grep/WebSearch — add a post-round pass that extracts quoted spans from answers, greps them against the repo/fetched sources, and annotates ✓/✗ in the tape + ledger (`{"role":"quote_check"}`). ~50 lines, 0 LLM calls. Highest value in code-mode reviews and the CI gate; weak for open-ended prose.

### A11. Tool/faithfulness-scored judging (Tool-MAD) — A/B
[arXiv 2601.04742](https://arxiv.org/abs/2601.04742) (Jan 2026): agents get **distinct external tools** (search API vs RAG); adaptive query reformulation per round; judge integrates **Faithfulness and Answer Relevance scores** into the final decision, "up to 5.5%" accuracy gain on fact-verification benchmarks. Council already has heterogeneous native tooling (claude tools vs codex sandbox+web).
**Council:** judge prompt in `_synthesize` — before the verdict, score each answer 0–1 on (a) faithfulness to evidence it cites, (b) relevance to the question; verdict must be consistent with scores. Cost: 0 extra calls (same judge call, longer output).

### A12. Skip / validation notes (verified, deliberately not adopted)
| Paper | One-line takeaway | Verdict for council |
|---|---|---|
| [2502.08788](https://arxiv.org/abs/2502.08788) Stop Overvaluing MAD (Feb 2025) | 5 MAD methods × 9 benchmarks × 4 models: MAD "often fail to outperform" CoT/self-consistency; **model heterogeneity is "a universal antidote"** | Validates council's cross-family core; keep solo mode the default (MAD isn't free lunch) |
| [2311.17371](https://arxiv.org/abs/2311.17371) Should we be going MAD? | MAD ≤ self-consistency without tuning; "adjusting agent agreement levels" can beat all non-debate protocols | Mandate: tune disagreement intensity via `shadow.py`, don't hardcode (see A13) |
| [2406.11776](https://arxiv.org/abs/2406.11776) sparse topology; [2409.14051](https://arxiv.org/abs/2409.14051) GroupDebate (−51.7% tokens) | Neighbor-graphs / subgroup debates cut cost at N≥4 | SKIP — K=2 is already the minimal graph |
| ARMOR-MAD SOD (A3 source) | TF-IDF cosine trust score w_i, drop answers with 1−w_i>0.7 at aggregation | SKIP — needs K≥3 |
| [2409.16636](https://arxiv.org/abs/2409.16636) debate self-play RL; [2501.05707](https://arxiv.org/abs/2501.05707) Multiagent Finetuning; [2407.13692](https://arxiv.org/abs/2407.13692) Prover-Verifier Games; [2606.29425](https://arxiv.org/abs/2606.29425) Mixture of Debaters | Train-time debate: judge accuracy rises with debate-trained debaters (and NOT with consultancy-trained); legibility via checkability; MoE self-debate −87% tokens | SKIP — council orchestrates frozen CLIs; cite as direction, not technique |
| [2311.14125](https://arxiv.org/abs/2311.14125) doubly-efficient debate; [2506.13609](https://arxiv.org/abs/2506.13609) prover-estimator (avoids obfuscation); [2505.03989](https://arxiv.org/abs/2505.03989) debate safety case | Recursive/complexity-theoretic protocols with honesty-wins guarantees | SKIP — theory; no harness-shaped artifact |
| [2311.08702](https://arxiv.org/abs/2311.08702) Debate Helps Supervise Unreliable Experts; [2310.02170](https://arxiv.org/abs/2310.02170) DyLAN; [2403.08010](https://arxiv.org/abs/2403.08010) Debatrix; [2408.01419](https://arxiv.org/abs/2408.01419) DebateQA; [2312.01823](https://arxiv.org/abs/2312.01823) Exchange-of-Thought; [2312.04854](https://arxiv.org/abs/2312.04854) MADKE; [2507.03928](https://arxiv.org/abs/2507.03928) CortexDebate | Human-judge debate; dynamic agent teams; chronological debate judging; debatable-QA eval; communication topologies; shared evidence pool; sparse+equal debate vs "overconfidence dilemma" | SKIP for now — adjacent problems (long-debate judging, N≥3 team selection, eval datasets) |

### A13. Disagreement-intensity knob — A/B (the shadow-mode flagship)
Convergent evidence: MAD's "modest tit-for-tat beats forced max disagreement" (survey §1); 2311.17371's "adjusting agent agreement levels… can surpass all other protocols"; agent-review-panel parameterizes it per persona (*"your agreement intensity is {X}%. You don't disagree reflexively, but you hold a high evidence bar"*); council-review's V2 claims only a **dedicated devil's-advocate pass against the emerging consensus** reliably induces disagreement — "soft role-framing and 'please dissent' instructions test statistically indistinguishable from baseline" (its citations UNVERIFIED).
**Council:** `config.py` knob `disagree_level` ∈ {off, modest, forced} mapping to `_CRIT_INSTR` variants — modest = adversarial-panel's *"you disagree with at least one central claim; find it"*; forced = MAD-style must-oppose. Run all three through `shadow.py:run_shadow` on a fixed question set before defaulting. Cost: 0 calls per turn; a weekend of A/B runs.

## B. Code worth lifting (mechanics from the repos)

- **Du et al. debate prompt, verbatim** ([gsm/gen_gsm.py](https://github.com/composable-models/llm_multiagent_debate)): *"These are the solutions to the problem from other agents: … One agent solution: ``` {} ``` … Using the solutions from other agents as additional information, can you provide your answer…"* — note it frames peers as "additional information," not opponents (the agreeable end of A13's dial); last round switches to *"Closely examine your biography and the biography of other agents and provide an updated…"* Their answer-extraction (`\boxed{}` / bullets) is what makes A3/A4 agreement computable. **Landing:** prompt-variant corpus for `shadow.py`.
- **llm-council anonymization contract** ([backend/council.py](https://github.com/karpathy/llm-council)): labels `chr(65+i)`, `label_to_model` map kept host-side, and a hard parse contract — *"Start with the line 'FINAL RANKING:'… Each line should be… ONLY the response label."* Only stage 2 (peer ranking) is blinded; the chairman sees real names. **Landing:** council already blind-shuffles; steal the strict machine-parseable verdict epilogue for `_synthesize` (today's free-form verdict + `ESCALATE` prefix is the only contract). Cost: 0.
- **Khan tournament machinery** (`core/swiss_tournament.py`, `core/scoring/trueskill.py`, `++correct_debater.BoN=8` best-of-N sampling): Elo/TrueSkill over cross-play to measure debater persuasiveness. **Verdict: SKIP** — research eval apparatus; `shadow.py` A/B + ledger cost rows are council's proportionate equivalent.
- **ChatEval** — role diversity + one-by-one ordering already absorbed in survey; repo adds little liftable beyond a position-calibration script name. SKIP.

## C. Skill prompt lines (fetched raw; quote = incorporable as-is)

**[makinux/adversarial-panel](https://github.com/makinux/adversarial-panel/blob/main/SKILL.md)** — the best-written of the seven; **worth installing as-is** for ask-mode experiments, and strip-mining regardless:
- Forced disagreement: *"agreement without a new argument is a failed round"*; anti-sycophancy add-on: *"you disagree with at least one central claim; find it."*
- Reproduction over assertion: *"refute by reproduction, not by assertion… not 'I doubt this' but 'this fails on input X'."*
- Ghost-panelist validation gate (a status/error line "is NOT a contribution") — council's `_is_dead` already does this in code; the skill confirms the failure mode is universal.
- Triage honesty: the gate deciding *whether* to debate "is run by the same model whose blind spots the panel exists to catch" → stakes, not felt certainty, decide. Landing: wording for when council auto-suggests arming the duel.
- Synthesis rules: *"splitting the difference destroys the signal"*; convergence of a same-family panel = weak evidence (**diversity illusion**: *"A role name ('Red Team') does not decorrelate errors"*).

**[wan-huiyan/agent-review-panel](https://github.com/wan-huiyan/agent-review-panel)** (SKILL.md + references/prompt-templates.md) — **strip-mine only** (16-phase, $3–20/run):
- The A6 sycophancy counter, plus: *"Shared-artifact consensus… 2+ reviewers agree… by reading the same source lines without independent verification"* counts as **one** source (`[STATIC-INFERENCE-CONSENSUS]`) — directly relevant to council's judge weighing "where they agree."
- Blind finals: *"Each gives final score… Others do NOT see these"* — a K=2 analogue is A5's order-swap.
- Control-validation gate: run a **degenerate no-input control** through the same reviewers; *"a persona that rates the degenerate control as highly as the real one is non-discriminating sycophancy → drop it"* — a self-test council could run in CI against stub heads.
- Severity discipline: a P0 "that one cheap read-only command could falsify (with no agent having run it) is capped at P1 until verified."

**[ngmeyer/council-review](https://github.com/ngmeyer/council-review/blob/main/SKILL.md)** — strip-mine; its research citations are partly unverifiable (below):
- The anti-deference line for every advisor prompt: *"**Do not defer to any answer the framing seems to expect.** Reason from your method to wherever it actually leads… Hedging toward the obvious answer is the failure this council exists to prevent."* → drop-in for `_CRIT_INSTR`.
- `--measure-diversity`: *"score reasoning-footprint overlap… the council agreed despite different reasoning methods — that's a signal the consensus may be theatrical"* → prompt-level cousin of CARA (A1's source).
- `--adaptive`: *"halt when response distributions converge below epsilon for two consecutive rounds"* (KS-statistic) — council's `_moved` is the leaner K=2 version; A4 is the better upgrade.
- Scope validation: *"Do not spawn agents for trivial questions"* → same instinct as A3.

**[lemon03390](https://github.com/lemon03390/Claude-code-adversarial-review-skill/blob/main/SKILL.md)**: *"Forced disagreement — MUST challenge before conceding"*; the anti-sycophancy smell test — *"If the review would make the author say 'yeah, I knew all that' — dig deeper"*; confidence table that **suppresses** findings scored 4–6 to an appendix and discards 1–3 (*"force the model to commit to a priority order"*). Strip-mine.

**[alecnielsen/adversarial-review](https://github.com/alecnielsen/adversarial-review)** (prompts/ + README): cross-review verdict labels per finding — VALID / INVALID / FALSE POSITIVE / UNCLEAR, with *"Consider if the other agent has context you're missing"*; circuit breaker thresholds — no progress 3 iters, persistent disagreement 5+, same unfixable issue 3+. Strip-mine (labels → council's critique register; breaker → A4's `unresolved` marker).

**[robertoecf/adversarial-review](https://github.com/robertoecf/adversarial-review)**: host-routing rule — the host *"must not review your own work"* (council's two-binary design already embodies this); stance: *"Your job is to break confidence in the artifact, not to validate it"*; finding bar: *"Prefer one strong finding over several weak ones. If it looks safe, say so and return no findings."* Strip-mine.

**[YunseobShin/claude-skill-debate](https://github.com/YunseobShin/claude-skill-debate)**: context budget rule — rounds 1–2 pass full statements, *"rounds 3+: summarize each prior statement to 150 words max… total context ≤4,000 words per prompt"*; separate fact-check phase where each model checks the OTHER's claims before the report. Strip-mine (the cross-fact-check phase is A10's cheap cousin).

## Top-10 steal list (value ÷ effort, council-specific)

1. **GDP stance-commitment line** in `_CRIT_INSTR` (A1) — 0 calls, d≈+1.5 evidence, one string edit.
2. **Evidence-gated concession line** in `_CRIT_INSTR` (A2) — 0 calls; the capitulation guard, CIC-backed.
3. **Round-0 agreement router** in `run()` (A3) — saves 2 calls/turn on easy questions; ARMOR-MAD + Du-code precedent.
4. **Cross-head agreement early-stop + `unresolved` cap marker** upgrading `_moved` (A4, C5) — 0 calls; fixes the survey's "surface-textual" caveat and feeds escalation.
5. **Both-orders judging** in `_synthesize` (A5) — +1 judge call; order-conflict doubles as a bias detector; Khan's own CSV pattern as the ledger shape.
6. **Capitulation ledger flag** (A6) — 0 calls (heuristic over A4's similarity + A1's parsed stances); council's #1 missing guardrail becomes a `syco_flag` row.
7. **Falsification-condition clause** in `_CRIT_INSTR` + judge prompt (A7) — 0 calls; kills confidence theater.
8. **Disagreement-intensity knob run through `shadow.py`** (A13) — the literature says tune-don't-guess; three `_CRIT_INSTR` variants, one weekend of A/B.
9. **Stated-confidence + ReConcile recalibration table** (A8) — 0 extra calls; gives the judge and the ledger a calibration signal.
10. **PoLL jury judge style for `council gate`** (A9) — 3 small cross-family judges ≈ 7× cheaper than one big judge, less intra-model bias, natural ESCALATE-on-split.

(11th, larger build: A10 quote verifier for code-mode/CI duels.)

## Dropped as unverifiable / corrections

- **"M3MADBench 2026"** (council-review's headline citation): zero arXiv hits — could not verify existence. **"DMAD (ICLR 2025)"** with its "91% vs 82% GSM-8K" claim: no arXiv match found under acronym or title; possibly OpenReview-only — UNVERIFIED, quoted only as the skill's claim. Likewise its "Demystifying MAD 2026," "S2-MAD," "CONSENSAGENT," "Peacemaker-or-Troublemaker 2026," and the OpenReview-2026/IUI-2024 devil's-advocate studies: cited in skills, not independently fetched — the *implemented rules* (A6, A13) are what I verified, in the skills' own raw files.
- **MADKE** exists ([2312.04854](https://arxiv.org/abs/2312.04854), "Learning to Break") but its one-time-retrieval design is already superseded by Tool-MAD's adaptive retrieval (A11) per that paper's own comparison — catalogued via A11 instead.
- ChatEval position-calibration internals and Khan judge-prompt text: implied by filenames/configs in the repos, not read line-by-line — mechanics stated only to the depth verified.
