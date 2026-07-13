"""Policy gate verdict tables (Phase 1.5 step 1, ticket #2): the blast-radius
gate's allow/deny/ask verdicts as data-driven cases, asserting only the external
verdict. First block = the module's own self-test ported verbatim; the rest cover
branches the self-test missed (event shapes, flag spellings, refspec forms,
gate_pushes, worst-verdict aggregation)."""
from __future__ import annotations

import pytest

from council.policy import evaluate


def shell(cmd: str, tool: str = "Bash") -> dict:
    return evaluate({"type": "tool_call",
                     "data": {"name": tool, "arguments": {"command": cmd}}})


# ── the __main__ self-test, ported verbatim ──────────────────────────────────
SELF_TEST = [
    ("ls -la && cat README.md",                 "ALLOW"),  # reads
    ("pytest -q",                               "ALLOW"),  # tests
    ("git commit -m 'wip'",                     "ALLOW"),  # local git
    ("rm build/app.o",                          "ALLOW"),  # non-recursive delete
    ("rm -rf build",                            "ASK"),    # recursive, scoped path
    ("cd repo && rm -rf node_modules",          "ASK"),    # chained; second stmt is the risk
    ("git push origin main",                    "ASK"),    # outward, recoverable
    ("gh pr merge 42 --squash",                 "ASK"),    # outward
    ("terraform apply",                         "ASK"),    # infra
    ("rm -rf /etc",                             "DENY"),   # catastrophic target
    ("CI=1 sudo -n rm -r -f /usr/local",        "DENY"),   # env + sudo + split flags + system path
    ("git push origin +main",                   "DENY"),   # +refspec = force
    ("git push --force-with-lease",             "DENY"),   # force
    ("git -C /repo push -df origin feature",    "DENY"),   # global -C, bundled -df = force+delete
    ("git reset --hard origin/main",            "DENY"),   # regex net
]


@pytest.mark.parametrize("cmd,expected", SELF_TEST)
def test_self_test_cases(cmd, expected):
    assert shell(cmd)["result"] == expected


# ── branches the self-test missed ─────────────────────────────────────────────
EXTRA = [
    # ALLOW: near-misses that must not trip the gate
    ("git pull origin main",                    "ALLOW"),  # git, not push
    ("rm --force notes.txt",                    "ALLOW"),  # force without recursion
    ("grep -r TODO .",                          "ALLOW"),  # -r on a non-rm command
    ("kubectl get pods",                        "ALLOW"),  # infra tool without a mutating verb
    # ASK: every recursive-rm spelling, scoped absolute paths, resolved subcommands
    ("rm -R build",                             "ASK"),    # capital-R recursion
    ("rm --recursive --force dist",             "ASK"),    # long-flag recursion
    ("rm -rf /home/user/project",               "ASK"),    # under /home = scoped, not system
    ("rm -rf /opt/app",                         "ASK"),    # /opt likewise excluded from DENY tier
    ("git -C /repo push origin main",           "ASK"),    # push resolved past global options
    ("git push -o=fast origin main",            "ASK"),    # value-short stops the flag scan
    ("gh release create v1.0",                  "ASK"),    # outward pattern
    ("helm delete myapp",                       "ASK"),    # infra pattern
    ("echo done | git push origin main",        "ASK"),    # pipe splits into statements
    # DENY: catastrophic rm targets, sudo wrappers, every push force/delete form
    ("rm -rf ~",                                "DENY"),   # home itself
    ("rm -rf /",                                "DENY"),   # root
    ("rm -rf /*",                               "DENY"),   # root glob
    ("rm -rf -- /etc",                          "DENY"),   # target after `--` still seen
    ("sudo -u root rm --recursive /var/log",    "DENY"),   # sudo value-opt walked, system path
    ("git push --delete origin feature",        "DENY"),   # remote delete
    ("git push origin :feature",                "DENY"),   # :refspec = delete
    ("git push --mirror",                       "DENY"),   # mirror
    ("git push --prune",                        "DENY"),   # prune
    ("git push -uf origin main",                "DENY"),   # force in a bundled short
    ("git push origin main && rm -rf /etc",     "DENY"),   # worst verdict wins over ASK
]


@pytest.mark.parametrize("cmd,expected", EXTRA)
def test_extra_verdict_cases(cmd, expected):
    assert shell(cmd)["result"] == expected


# ── event shapes: only well-formed shell tool_calls are gated ─────────────────
def test_non_tool_call_events_pass():
    assert evaluate({"type": "message"})["result"] == "ALLOW"


def test_non_shell_tools_pass():
    assert shell("rm -rf /etc", tool="Read")["result"] == "ALLOW"


@pytest.mark.parametrize("tool", ["Bash", "bash", "sys_os_shell"])
def test_both_clis_shell_tools_are_gated(tool):
    assert shell("rm -rf /etc", tool=tool)["result"] == "DENY"


@pytest.mark.parametrize("event", [
    {"type": "tool_call", "data": "not-a-dict"},
    {"type": "tool_call", "data": {"name": "Bash", "arguments": "not-a-dict"}},
    {"type": "tool_call", "data": {"name": "Bash", "arguments": {}}},
    {"type": "tool_call", "data": {"name": "Bash", "arguments": {"command": 42}}},
])
def test_malformed_payloads_pass(event):
    assert evaluate(event)["result"] == "ALLOW"


# ── gate_pushes flag: ASK tier optional, DENY tier never ──────────────────────
def test_gate_pushes_off_frees_the_ask_tier():
    ev = {"type": "tool_call",
          "data": {"name": "Bash", "arguments": {"command": "git push origin main"}}}
    assert evaluate(ev, gate_pushes=False)["result"] == "ALLOW"


def test_gate_pushes_off_still_denies_catastrophic():
    ev = {"type": "tool_call",
          "data": {"name": "Bash", "arguments": {"command": "rm -rf /etc"}}}
    assert evaluate(ev, gate_pushes=False)["result"] == "DENY"


# ── reasons: the human sees why on ASK/DENY ───────────────────────────────────
def test_ask_reason_names_the_command():
    verdict = shell("git push origin main")
    assert "git push origin main" in verdict["reason"]


def test_deny_reason_is_customizable():
    ev = {"type": "tool_call",
          "data": {"name": "Bash", "arguments": {"command": "rm -rf /etc"}}}
    verdict = evaluate(ev, deny_reason="Nope.")
    assert verdict["result"] == "DENY" and verdict["reason"].startswith("Nope.")
