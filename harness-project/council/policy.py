"""council/policy.py — the blast-radius gate (ALLOW / ASK / DENY).  G5.

↔ omnigent inner/nessie/policies.py:346 (blast_radius) + helpers :134–:343.
This is the FIRST real council file (not a manuscript sketch): the helper bodies
are lifted VERBATIM from omnigent because they are the value — a single regex
re-introduces the bugs their comments document (split `rm -r -f`, sudo/CI= prefixes,
`+`/`:` refspecs). What council dropped is everything AROUND blast_radius: the three
multi-agent factories, the YAML registry, and the runner/server indirection.

PUBLIC SURFACE: evaluate(event) — called IN-PROCESS by G3 harness_status.
NOT A SECURITY BOUNDARY (omnigent :143): a safety net vs accidental/obvious damage.
It does NOT model subshells, command substitution, or eval. The real boundary is
sandboxing, which council `code` does not add. G5 is a SECOND layer: harness_status
maps ALLOW→None, so Claude Code's own consent gate still fires underneath.
"""
from __future__ import annotations

import re
import shlex

# A ready ALLOW (the common case — most tool calls pass).            ↔ :22
_ALLOW: dict = {"result": "ALLOW"}


def _decision(result: str, reason: str) -> dict:
    """Build a {result, reason} verdict. reason is surfaced to the human on ASK/DENY.  ↔ :25"""
    return {"result": result, "reason": reason}


def _tool_call(event: dict, tool_names: set[str]) -> dict | None:
    """Return the args dict of a matching tool_call event, else None (caller ALLOWs).  ↔ :39

    A V0 event is {"type": "tool_call", "data": {"name": ..., "arguments": {...}}}.
    G3 harness_status builds this from Claude's PreToolUse payload.
    """
    if event.get("type") != "tool_call":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("name") not in tool_names:
        return None
    args = data.get("arguments")
    return args if isinstance(args, dict) else {}


# ── Pattern / constant sets (verbatim from omnigent :64–:131) ────────────────
# Irreversible → DENY. rm + git push are NOT here (a single regex missed split/long
# flag forms, root children, and force/delete refspecs); they go through the helpers.
_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgit\b.*\breset\s+--hard\s+\w+/"),  # hard-reset to a remote ref
)
# Outward / destructive but recoverable → ASK the human first.
_ASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh\s+(pr\s+merge|release|repo\s+delete)\b"),
    re.compile(r"\b(kubectl|helm|terraform|databricks)\b.*\b(apply|deploy|destroy|delete)\b"),
)
# Recursive-force rm OF one of these dirs itself = catastrophic.
_RM_CRITICAL_DIRS: frozenset[str] = frozenset(
    {"/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var",
     "/boot", "/root", "/home", "/opt", "/dev", "/proc", "/sys"}
)
# Recursive-force rm of a path UNDER one of these = also catastrophic (system files).
# /home /opt /root excluded: a path under them is scoped/recoverable → ASK tier.
_RM_SYSTEM_PARENTS: frozenset[str] = frozenset(
    {"/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/boot", "/dev", "/proc", "/sys"}
)
# sudo options that consume the FOLLOWING argv token as their value.
_SUDO_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-U", "-u",
     "--chdir", "--chroot", "--close-from", "--command-timeout", "--group",
     "--host", "--other-user", "--prompt", "--role", "--type", "--user"}
)
# git GLOBAL options (before the subcommand) that consume the next token.
_GIT_GLOBAL_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
# git push short options that take an attached value (stop flag-scan in a bundle).
_PUSH_SHORT_VALUE_OPTS: frozenset[str] = frozenset({"o"})
# A leading FOO=bar shell env assignment (not the command itself).
_ENV_ASSIGNMENT_RE: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _shell_statements(command: str) -> list[list[str]]:
    """Best-effort split of a command line into per-statement token lists.  ↔ :134

    Splits on ; && || | newline, tokenizes each piece with shlex (whitespace
    fallback on a quoting error). Heuristic — deliberately does NOT model subshells
    / substitution / eval. e.g. "cd repo && rm -rf build" -> [["cd","repo"],["rm","-rf","build"]].
    """
    statements: list[list[str]] = []
    for piece in re.split(r"&&|\|\||[;|\n]", command):
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece)
        except ValueError:
            argv = piece.split()
        if argv:
            statements.append(argv)
    return statements


def _rm_target_is_catastrophic(target: str) -> bool:
    """Whether `rm -rf <target>` is catastrophic / irreversible.  ↔ :164

    Catastrophic = root, ~, a top-level critical dir itself, or any path under a
    system dir (/etc/...). A scoped path under /home /opt /tmp or a relative path
    is NOT catastrophic here (recoverable / the worker's own tree) → ASK tier.
    """
    norm = target.rstrip("/") or "/"
    if norm in ("~", "$HOME", "${HOME}"):
        return True
    if target == "/*" or target.startswith("/*"):
        return True
    if norm in _RM_CRITICAL_DIRS:
        return True
    if target.startswith("/"):
        top = "/" + target.lstrip("/").split("/", 1)[0]
        if top in _RM_SYSTEM_PARENTS:
            return True
    return False


def _skip_shell_assignments(argv: list[str], start: int) -> int:
    """First index after leading FOO=bar env assignments (e.g. `CI=1 git push`).  ↔ :192"""
    i = start
    while i < len(argv) and _ENV_ASSIGNMENT_RE.fullmatch(argv[i]):
        i += 1
    return i


def _command_index_after_shell_prefixes(argv: list[str]) -> int:
    """Index of the real command after env assignments and an optional `sudo ...`.  ↔ :210

    So `CI=1 sudo -n rm ...` and `sudo -u root rm ...` classify the same as bare
    `rm ...`. Walks sudo flags, consuming a value token for value-taking options.
    """
    i = _skip_shell_assignments(argv, 0)
    if i >= len(argv) or argv[i] != "sudo":
        return i
    i += 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return _skip_shell_assignments(argv, i + 1)
        if tok.startswith("--"):
            i += 2 if tok in _SUDO_VALUE_OPTS and "=" not in tok and i + 1 < len(argv) else 1
            continue
        if tok.startswith("-") and tok != "-":
            value_opt_pos = next(
                (pos for pos, opt in enumerate(tok[1:]) if f"-{opt}" in _SUDO_VALUE_OPTS),
                None,
            )
            if value_opt_pos is None:
                i += 1
                continue
            value_is_attached = value_opt_pos < len(tok[1:]) - 1
            i += 1 if value_is_attached else 2
            continue
        return _skip_shell_assignments(argv, i)
    return len(argv)


def _rm_severity(argv: list[str]) -> str | None:
    """Classify a single `rm` statement by blast radius (flag-form robust).  ↔ :247

    Detects recursion in any spelling (-rf, -Rf, -r, --recursive) and a sudo
    wrapper. Recursive rm of a catastrophic target → DENY; of any other target →
    ASK. Non-recursive rm (single-file delete) → None (ALLOW).
    """
    i = _command_index_after_shell_prefixes(argv)
    if i >= len(argv) or argv[i] != "rm":
        return None
    recursive = False
    targets: list[str] = []
    positional_only = False  # everything after a bare `--` is a filename, not a flag
    for tok in argv[i + 1:]:
        if positional_only:
            targets.append(tok)
        elif tok == "--":
            positional_only = True
        elif tok == "--force":
            continue
        elif tok == "--recursive":
            recursive = True
        elif tok.startswith("-") and len(tok) > 1 and not tok.startswith("--"):
            recursive = recursive or "r" in tok[1:] or "R" in tok[1:]
        elif not tok.startswith("-"):
            targets.append(tok)
    if not recursive:
        return None
    return "DENY" if any(_rm_target_is_catastrophic(t) for t in targets) else "ASK"


def _push_short_option_is_destructive(token: str) -> bool:
    """Whether a bundled `git push` short-option token force-pushes or deletes.  ↔ :287

    Handles combined shorts like -uf / -df. A value-taking short (-o) stops the
    scan so `-o=fast` isn't mistaken for a force/delete flag.
    """
    for opt in token[1:]:
        if opt in ("f", "d"):
            return True
        if opt in _PUSH_SHORT_VALUE_OPTS:
            return False
    return False


def _push_severity(argv: list[str]) -> str | None:
    """Classify a single `git push` statement by blast radius.  ↔ :307

    Force-push (--force / --force-with-lease / -f / +refspec / --mirror) or remote
    deletion (--delete / --prune / -d / :refspec) → DENY. Any other push → ASK. The
    `push` subcommand is resolved PAST global options (git -C <path> push ...) so a
    `push` appearing as an arg value isn't mistaken for the subcommand. Not a push → None.
    """
    i = _command_index_after_shell_prefixes(argv)
    if i >= len(argv) or argv[i] != "git":
        return None
    j = i + 1
    while j < len(argv) and argv[j].startswith("-"):
        j += 2 if argv[j] in _GIT_GLOBAL_VALUE_OPTS and j + 1 < len(argv) else 1
    if j >= len(argv) or argv[j] != "push":
        return None
    for tok in argv[j + 1:]:
        if tok.startswith("--force") or tok in ("--delete", "--mirror", "--prune"):
            return "DENY"
        if tok.startswith("-") and not tok.startswith("--") and _push_short_option_is_destructive(tok):
            return "DENY"
        if len(tok) > 1 and tok[0] in "+:":  # +refspec (force) / :refspec (delete)
            return "DENY"
    return "ASK"


def evaluate(event: dict, *, gate_pushes: bool = True,
             deny_reason: str = "Blocked by the blast-radius policy.") -> dict:
    """The ONE public entry (was blast_radius._evaluate @:368, un-nested from its factory).

    event = a V0 tool_call dict, built by G3 harness_status from Claude's PreToolUse
    payload. Returns the WORST verdict over all statements: {"result","reason"}.
    """
    args = _tool_call(event, {"Bash", "bash", "sys_os_shell"})   # both CLIs' shell tool  ↔ :383
    if args is None:
        return _ALLOW
    command = args.get("command")
    if not isinstance(command, str):                              # malformed → nothing to gate  ↔ :390
        return _ALLOW
    statements = _shell_statements(command)
    severities = {
        sev for stmt in statements for sev in (_rm_severity(stmt), _push_severity(stmt))
    }
    if "DENY" in severities or any(p.search(command) for p in _DENY_PATTERNS):
        return _decision("DENY", f"{deny_reason} (irreversible: {command!r})")
    if gate_pushes and ("ASK" in severities or any(p.search(command) for p in _ASK_PATTERNS)):
        return _decision("ASK", f"High-blast-radius command needs approval: {command!r}")
    return _ALLOW


# ── self-test: `python council/policy.py` ────────────────────────────────────
if __name__ == "__main__":
    def shell(cmd: str) -> dict:
        return evaluate({"type": "tool_call",
                         "data": {"name": "Bash", "arguments": {"command": cmd}}})

    cases = [
        # (command, expected result)
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
    ok = 0
    for cmd, expected in cases:
        got = evaluate({"type": "tool_call",
                        "data": {"name": "Bash", "arguments": {"command": cmd}}})["result"]
        mark = "✓" if got == expected else "✗"
        if got == expected:
            ok += 1
        print(f"  {mark}  {got:5}  (want {expected:5})  {cmd}")
    print(f"\n{ok}/{len(cases)} passed")
