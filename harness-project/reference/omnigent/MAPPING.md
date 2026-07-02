# What council takes from omnigent — the matching map

Every row reads: **`council file (what you build)` ← `omnigent file:line (open it in this folder)` — what it does — verdict.**

- **omnigent** = the reference implementation (Databricks, Apache-2.0). It runs the real `claude`
  binary behind its *own* web UI for *many* coding agents and *many* users.
- **council** = your slimmed version: one agent (claude), one user, a Claude-Code plugin, branded
  skin. The pattern in every group is the same — **keep the mechanism, delete the multi-user server
  and the breadth.**
- **Line numbers** match THIS vendored snapshot. (Re-verify with `grep -n "def <name>" <file>`.)

## The headline — why ~30k lines becomes ~1,800

| layer | omnigent | council | why |
|---|---|---|---|
| coding agents | 11 (claude, codex, cursor, antigravity, opencode, hermes, kimi, goose, qwen, kiro, pi) | **claude only** | ~half the native subsystem gone outright |
| claude_* family | ~15,246 lines | **~1,500** | single-user subset |
| server/daemon/remote + `_post_external_*` | most of what remains | **0** | council renders locally → nothing to forward |
| G1 front + G2 debate | ~29k front-end + Debby | **~300** | thin command + a deterministic debate loop |

---

# G3 — WRAP (the part you couldn't reference before)

The `claude_native*` family = the machinery for running the **real `claude` binary hidden** behind
council's skin. One shared "mailbox" folder + four wires (inject in; transcript/deltas/cost out) +
one policy gate. Six council files, each below.

## `council/wrap/bridge.py` ← `omnigent/claude_native_bridge.py` (4,789 → ~600)
The tmux plumbing + transcript reading + hook-settings writer. **The crown jewel to lift verbatim.**

| council uses | ← omnigent fn:line | what it does (in / out) | verdict |
|---|---|---|---|
| the nouns | `ClaudeTranscriptItem`:280, `TranscriptReadResult`:300, `ClaudeHookRecord`:331, `ClaudeMessageDelta`:462 | dataclasses: one conv item / a read-bundle (items+cursors) / a hook record / a token chunk | KEEP (slim) |
| **inject (write wire)** | `inject_user_message`:2347 | in: bridge dir + text. out: none (raises if undelivered). Types text into the hidden pane with a **verified** bracketed paste | **LIFT verbatim** |
| inject helpers | `_run_tmux`:2716, `_capture_pane`:2746, `_claude_prompt_rendered`:2774, `_submit_needle`:2790, `_draft_in_input_box`:2820, `_wait_for_claude_prompt_ready`:2874, `_paste_payload_bytes`:2924, `_wait_for_tmux_info`:2965 | the send-keys / read-the-screen mechanics (encode the 16KB cap, the trailing-`\` bug, the coalesced-paste race) | **LIFT** |
| read (out wires) | `read_transcript_items_from_offset`:1778, `read_message_deltas_from_offset`:504, `_message_delta_from_jsonl_text`:538 | in: file + byte offset. out: new items/deltas + new offset (complete lines only) | KEEP |
| launch wiring | `prepare_bridge_dir`:735, `build_hook_settings`:1024, `augment_claude_args`:1279 | make the mailbox; build the hooks settings; stuff `--settings` into the launch args | KEEP / TRANSFORM |
| — | `start_tool_relay`:2990, `_serve_mcp`:3065, `_stdio_jsonrpc_loop`:3362, `display_cost_approval_popup`:2602, `post_tools_changed`:2675 | the MCP tool-relay server + server-side UI | **DROP** |

## `council/wrap/session.py` ← `claude_native.py` (4,404 → ~150) + `inner/claude_native_executor.py` (250 → ~15) + `inner/claude_native_harness.py` (30 → 0)
The conductor: launch the binary, run the inject + render pumps.

| council uses | ← omnigent fn:line | what it does | verdict |
|---|---|---|---|
| launch entry | `run_claude_native`:342 | in: kwargs (command, claude_args, use_claude_config…). out: none. preflight → resolve config (skipped when `use_claude_config=True`) → launch+attach | KEEP (swap the launch branch) |
| launch the pane | `_launch_claude_terminal`:3779, `_preflight_local_tools`:3696, `_strip_resume_from_claude_args`:1345 | start `claude` in tmux; fail early if tools missing; drop a stray `--resume` | KEEP-LITE |
| find the transcript | `_find_claude_transcript`:1256, `_claude_project_dir_for_cwd`:1287, `_sanitize_claude_project_name`:1301 | locate Claude's own JSONL log for the cwd | KEEP |
| what a "turn" is | `ClaudeNativeExecutor.run_turn`:99 (`supports_streaming`:64 → False) | a turn = just `inject_user_message`; output comes from the forwarder, not here | → ~15 lines |
| — | resume-workspace picker 577–1010; multi-provider/bedrock/ucode config 1382–1731; `_run_with_local_server`:1792 / `_run_with_remote_server`:2774 / daemon `_prepare_claude_terminal_via_daemon`:2585; cold-resume-from-server 3235–3593; `create_app`:22 (FastAPI harness) | the server ring + provider breadth + a TUI picker | **DROP** |

## `council/wrap/events.py` ← `claude_native_forwarder.py` (4,183 → ~200)
Tail the three out-channels → yield **local** render events. The deletions *are* the shrink.

| council uses | ← omnigent fn:line | what it does | verdict |
|---|---|---|---|
| the read loop | `forward_claude_transcript_to_session`:589 | poll transcript + deltas + status since offsets | KEEP the read half |
| read helpers | `_forward_available_items`:2683, `_forward_available_deltas`:3267, `_forward_available_status_events`:2376 | read new items/deltas/status records | KEEP |
| — | every `_post_external_*` (3099–3797), `supervise_forwarder`:1721, subagent forwarding 877–1452, session rotation 1822–2146 | POST everything to the omnigent server | **DROP all** |

## `council/wrap/render.py` ← `claude_native_message_display_hook.py` (144, lift whole)
| council uses | ← omnigent fn:line | what it does (in / out) | verdict |
|---|---|---|---|
| live-token hook | `main`:54, `_delta_record`:100 | in: a MessageDisplay payload on stdin. out: exit 0; appends `{message_id,index,final,delta}` to `message_deltas.jsonl` (stdlib-only, O_APPEND atomic) | **LIFT whole** |
| local painter | *(new council code)* | consume `events.read_events` → Rich `Live` block in council's skin; reconcile deltas vs final FIFO | NEW |

## `council/wrap/state.py` ← `claude_native_state.py` (279 → ~40) + `claude_native_status.py` (165, lift whole)
| council uses | ← omnigent fn:line | what it does (in / out) | verdict |
|---|---|---|---|
| cost/model capture | `main`:25, `_write_context_atomic`:65, `_chain`:135 | in: Claude's statusLine JSON on stdin. out: writes `context.json` (cost/model/context-window), then runs the user's original statusLine | **LIFT whole** |
| resume | (`claude_native_state.py` launch-cwd) | persist launch cwd so `--resume` reattaches the right project | KEEP ~40 |

## `council/wrap/harness_status.py` ← `native_policy_hook.py` (445 → ~150) + `claude_native_hook.py` (1,002 → ~120) + `runner/pending_approvals.py` (207, mostly drop)
The PreToolUse gate. Three **pure** translation functions (no I/O) → trivial to lift.

| council uses | ← omnigent fn:line | what it does (in / out) | verdict |
|---|---|---|---|
| payload → request | `hook_payload_to_evaluation_request`:91 | in: hook event + payload. out: a normalized eval request, or None | KEEP |
| verdict → output | `evaluation_response_to_hook_output`:171 | in: event + policy verdict. out: Claude hook JSON. **ALLOW→None** so the user's own consent gate still fires; DENY→"deny"; ASK→fail closed | KEEP |
| fail closed | `fail_closed_hook_output`:276 | in: event. out: PreToolUse→deny, others→None (phase-aware) | KEEP |
| dispatch | `claude_native_hook.main`:72, `_main_evaluate_policy`:803, `_main_permission_request`:658 | route a hook invocation to the right mode | KEEP (decision only) |
| — | `post_evaluate_with_retry`:319, `_post_hook_with_reattach`:565, rotation 240–492, `pending_approvals.py` | POST the decision to the server + server-side queue | **DROP** → call G5 `policy.py` in-process |

---

# G1 — FRONT ← `cli.py` / `chat.py` / `repl/_repl.py`
> **Note:** `groups.py`'s G1 `↔` numbers were written against an earlier clone. **Corrected anchors for THIS snapshot:**

| council | ← omnigent fn:line (this snapshot) | what it does |
|---|---|---|
| `cli.py` group + `main` | `cli()`:1161, `main()`:1241 | the Click group + entry; council keeps 3 commands, drops ~21 |
| `code` command | the `claude` command `@cli.command(`:4099 (+ `--use-native-config`/`use_claude_config` opt ~4136) | template for `council code` (the CODE seam) |
| (backend drag) | `_ensure_backend`:2411 | the one call that drags in omnigent's daemon ring — council does NOT take it |
| `banner.py` | `_StartupHeader`:248 (repl/_repl.py) | the startup banner council re-skins |
| `chat.py` loop | `_run_repl`:3805 (chat.py) | the turn-based repl loop → council's `run_loop` |

# G2 — DEBATE ← `examples/debby/`
| council | ← omnigent file:line | what it does |
|---|---|---|
| `debate.py` | `config.yaml`:47–55 (fan out), :82–97 (present side-by-side), :106–111 (stance) + `skills/debate/SKILL.md`:13–56 (the round loop) | the cross-critique debate council reimplements as a deterministic Python loop |
| `backends.py` | `agents/claude/config.yaml`:41–64 + `agents/gpt/config.yaml`:49–72 | the identical ANSWER/CRITIQUE head prompt |

# G5 — POLICY (next) ← `inner/nessie/policies.py`
| council | ← omnigent fn:line | what it does |
|---|---|---|
| `policy.py` `evaluate()` | `blast_radius`:346, `spawn_bounds`:408 | ALLOW/DENY/ASK on a tool call — what `wrap/harness_status.py`'s gate calls |
| (blast_radius internals) | `_shell_statements`:134, `_rm_target_is_catastrophic`:164, `_rm_severity`:247, `_push_severity`:307, `_tool_call`:39, `_decision`:25 | argv-parse (not regex) detection of `rm -rf /`, force-push, etc. |

---

# What council deliberately leaves behind
- The **10 other coding agents** (codex/cursor/antigravity/opencode/hermes/kimi/goose/qwen/kiro/pi).
- The **multi-user server / daemon / remote** + every `_post_external_*` forward (council renders local).
- The **MCP tool-relay** (`start_tool_relay` & friends) — MCP optional, much later.
- The **resume-workspace picker** (prompt_toolkit TUI) and **cold-resume-from-server**.
- The **FastAPI harness** (`create_app`) — there is no server to host.
