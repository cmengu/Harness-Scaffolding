"""council/report.py — the READ side of the ledger: the week in review + run replay.
Everything derives from trace() rows; nothing here writes. This is further_steps step 4:
run IDs made every run addressable, so 'show' is a formatter, not a storage system."""
from __future__ import annotations

import time

from rich.console import Console, Group
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .ledger import (cost_usd, is_answer, is_any_user, is_cancelled,
                     is_code_assistant, is_code_context, is_code_session,
                     is_code_tool, is_head_call, is_head_error, is_head_retry,
                     is_judge, is_quarantined, is_round0_agreed, is_run_start,
                     is_shadow_arm, is_syco_flag, is_unresolved, trace)


def summary(days: int = 7):
    """Aggregate the ledger into one screen: runs, cost, latency, failure rate."""
    cutoff = time.time() - days * 86400
    rows = [r for r in trace() if r.get("ts", 0) >= cutoff]
    if not rows:
        return Text(f"ledger has no rows in the last {days} day(s)", style="dim")
    runs: dict[str, list[dict]] = {}
    for r in rows:                                   # rows born before run IDs group as one bucket
        runs.setdefault(r.get("run_id", "pre-run-id"), []).append(r)

    calls = [r for r in rows if is_head_call(r)]
    fails = [c for c in calls if not c.get("ok") and not c.get("cancelled")]   # a ^C isn't a failure
    retries = sum(1 for r in rows if is_head_retry(r))
    lat = sorted(c.get("secs", 0.0) for c in calls if c.get("ok"))
    ask_usd = sum(cost_usd(r) for r in rows)             # cost_usd(codex) is now priced, not 0
    # code-mode cost: statusLine's total_cost_usd is a RUNNING session total —
    # take the max per run and sum those; summing rows would overcount by ~turns.
    code_usd = sum(m for m in (_code_total(rs) for rs in runs.values()) if m)

    top = Table(show_header=False, box=None, padding=(0, 2))
    top.add_row("runs", str(len(runs)))
    top.add_row("head calls", f"{len(calls)}"
                + (f"  ({len(fails)} failed · {len(fails) / len(calls):.0%}"
                   + (f" · {retries} retried" if retries else "") + ")" if calls else ""))
    if lat:
        top.add_row("latency", f"median {_pct(lat, 50):.1f}s · p95 {_pct(lat, 95):.1f}s · worst {lat[-1]:.1f}s")
    top.add_row("cost", f"${ask_usd + code_usd:.2f}  (ask ${ask_usd:.2f} · code ${code_usd:.2f})")
    sycos = sum(1 for r in rows if is_syco_flag(r))
    if sycos:                                        # capitulation is worth a headline, not a scroll
        top.add_row("sycophancy", f"{sycos} flag(s) — a head moved without evidence")

    per = Table(padding=(0, 2))
    for col in ("run", "started", "mode", "turns", "cost", "errors"):
        per.add_column(col, justify="right" if col in ("turns", "cost", "errors") else "left")
    for rid, rs in sorted(runs.items(), key=lambda kv: kv[1][0].get("ts", 0))[-15:]:
        errors = sum(1 for r in rs if is_head_error(r))
        cost = sum(cost_usd(r) for r in rs) + (_code_total(rs) or 0.0)
        per.add_row(rid, time.strftime("%d %b %H:%M", time.localtime(rs[0].get("ts", 0))), _mode(rs),
                    str(sum(1 for r in rs if is_any_user(r))),
                    f"${cost:.2f}" if cost else "—", str(errors) if errors else "—")
    return Group(Rule(f"last {days} day(s)", style="dim", align="left"), top, per)


def replay(run_id: str, console: Console) -> None:
    """Re-print one run from the ledger: questions, debate columns, verdicts, failures."""
    rows = trace(run_id=run_id)
    if not rows:
        ids = list(dict.fromkeys(r["run_id"] for r in trace() if r.get("run_id")))[-8:]
        console.print(f"[red]no rows for run {run_id!r}[/] — recent: " + (", ".join(ids) or "none"))
        return
    console.print(f"[bold]run {run_id}[/] · {_mode(rows)} · "
                  f"{time.strftime('%d %b %H:%M', time.localtime(rows[0].get('ts', 0)))}\n")
    render_rows(rows, console)


def render_rows(rows: list[dict], console: Console) -> None:
    """One ledger row → its terminal form. Shared by replay (a whole run) and ask-mode
    /history + /switch recap (the active chain). The `"proposer" in r` guard keeps event
    rows (converged/cancelled share role=debate) from rendering as empty Claude blocks."""
    from .config import load_config
    from .debate import _present                     # lazy: avoids a module cycle at import
    cfg = load_config()                              # glyphs only — no runtime knob reaches here
    for r in rows:
        if is_any_user(r):
            console.print(f"\n[bold]› {r.get('text', '')}[/]")
        elif is_cancelled(r):
            console.print("[yellow]✗ turn cancelled[/]")
        elif is_answer(r):
            if r.get("adversary"):
                _present(console, str(r.get("proposer", "")), str(r["adversary"]), cfg)
            else:                                        # single-voiced: still a deliverable view,
                from .contract import answer_of          # so strip a contract answer to its ANSWER
                console.print(f"[orange1]## {cfg.claude_glyph} Claude[/]\n"
                              f"{answer_of(str(r.get('proposer', '')))}")
        elif is_judge(r):
            console.print(f"\n[bold]## ⚖ Synthesis[/] ({r.get('style')})\n{r.get('text', '')}")
        elif is_code_assistant(r):
            console.print(f"[orange1]{r.get('text', '')}[/]")
        elif is_code_tool(r):
            console.print(f"[dim]⚙ {r.get('name')}  {r.get('summary', '')}[/]")
        elif is_head_retry(r):
            console.print(f"[dim]↻ {r.get('head')} retry {r.get('attempt', 0) + 1}"
                          f" ({r.get('kind')}): {str(r.get('error'))[:120]}[/]")
        elif is_head_error(r):
            console.print(f"[red]✗ {r.get('head')}: {str(r.get('error'))[:200]}[/]")
        elif is_quarantined(r):
            console.print(f"[red]☠ postmortem → {r.get('path')}[/]")
        elif is_round0_agreed(r):
            console.print(f"[dim]✓ heads agreed at round 0 — critique skipped[/]"
                          + (f" [dim]({r.get('position')})[/]" if r.get("position") else ""))
        elif is_unresolved(r):
            console.print(f"[yellow]⚠ unresolved — heads still disagreed at the round "
                          f"{r.get('round')} cap[/]")
        elif is_syco_flag(r):
            console.print(f"[red]⚠ syco_flag[/] — {r.get('head')} moved toward its opponent "
                          f"without evidence (round {r.get('round')})")
        elif is_shadow_arm(r):
            ovr = r.get("overrides") or []
            console.print(f"\n[bold]## arm {r.get('arm')}[/]"
                          + (f" [dim](+ {' '.join(ovr)})[/]" if ovr else " [dim](current config)[/]")
                          + f"\n{r.get('answer', '')}")


def _code_total(rs: list[dict]) -> float | None:
    totals = [r["total_cost_usd"] for r in rs if is_code_context(r)
              and isinstance(r.get("total_cost_usd"), (int, float))]
    return max(totals) if totals else None


def _mode(rs: list[dict]) -> str:
    for r in rs:
        if is_run_start(r):
            return r.get("mode", "ask")
    return "code" if any(is_code_session(r) for r in rs) else "ask"


def _pct(sorted_vals: list[float], p: int) -> float:
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100))]
