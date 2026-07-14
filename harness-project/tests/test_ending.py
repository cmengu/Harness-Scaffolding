"""The duel ending (step 6, 11 Jul): one combined critique-and-final call per head per
round; critique = dim scratch work, only the standalone answer is the deliverable."""
from __future__ import annotations


from rich.console import Console

from council import debate
from council.config import load_config
from council.ledger import trace




def test_split_verdict_contract():
    crit, ans = debate._split_verdict("they are wrong ===ANSWER=== the moon is rock")
    assert crit == "they are wrong" and ans == "the moon is rock"
    crit, ans = debate._split_verdict("no marker at all")
    assert crit == "" and ans == "no marker at all"       # disobedience forfeits the dim register
    crit, ans = debate._split_verdict("only scratch ===ANSWER=== ")
    assert ans == "only scratch"                          # empty answer never silently dropped


def test_round_one_splits_critique_from_answer(monkeypatch):
    monkeypatch.setenv("COUNCIL_STUB_TEXT",
                       "your sources are stale ===ANSWER=== The moon is basalt.")
    # engine flow: assert the recorded rows, not printed text (the tape painting of the split is
    # a renderer concern — test_tape.test_tape_paints_the_critique_split).
    result = debate.run("moon?", rounds=1, judge=None, cfg=load_config(), console=Console(quiet=True))
    assert result.proposer_final == "The moon is basalt."   # deliverable = standalone answer
    assert "===ANSWER===" not in result.proposer_final
    row = [r for r in trace(role="debate") if r.get("round") == 1][0]
    assert row["proposer"] == "The moon is basalt."
    assert row["proposer_critique"] == "your sources are stale"
    assert row["adversary_critique"] == "your sources are stale"


def test_critique_instruction_reaches_the_heads(tmp_path, monkeypatch):
    capture = tmp_path / "stdin"
    monkeypatch.setenv("COUNCIL_STUB_CAPTURE", str(capture))
    debate.run("q", rounds=1, judge=None, cfg=load_config(),
               console=Console(quiet=True), sessions=debate.HeadSessions())
    last = capture.read_text()                             # the round-1 claude call
    assert "===ANSWER===" in last and "NEVER mentioning the other voice" in last


def test_round_zero_never_splits(monkeypatch):
    monkeypatch.setenv("COUNCIL_STUB_TEXT", "freak text with ===ANSWER=== inside")
    result = debate.run("q", rounds=0, judge=None, cfg=load_config(),
                        console=Console(quiet=True))
    assert result.proposer_final == "freak text with ===ANSWER=== inside"
    assert "proposer_critique" not in trace(role="debate")[-1]
