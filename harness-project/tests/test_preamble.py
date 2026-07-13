"""The one owner of ask-mode recap: the clip window's edge cases + the single dead-marker
check. Chain-scope behavior is covered by test_debate/test_notes_arming/test_sessions."""
from __future__ import annotations

from council import preamble
from council.config import load_config


def test_clip_empty_history_is_empty():
    cfg = load_config()
    assert preamble.window([], cfg) == []
    assert preamble.clip([], cfg) == ""


def test_window_keeps_only_last_n_times_two_rows():
    cfg = load_config()                                  # history_turns default = 6 → window = 12
    rows = [f"turn {i}" for i in range(30)]
    kept = preamble.window(rows, cfg)
    assert kept == rows[-cfg.history_turns * 2:]         # exactly the last 12
    assert len(kept) == cfg.history_turns * 2


def test_window_shorter_than_cap_keeps_all():
    cfg = load_config()
    rows = ["only", "three", "turns"]                    # fewer than the window
    assert preamble.window(rows, cfg) == rows            # boundary: no over-slice, no padding


def test_clip_hard_caps_oversized_turns_at_window_chars():
    cfg = load_config()
    rows = ["X" * 5000, "Y" * 5000]                      # 2 turns, ~10k chars > 8k cap
    clipped = preamble.clip(rows, cfg)
    assert len(clipped) == preamble.WINDOW_CHARS         # capped to exactly the ceiling
    assert clipped.endswith("Y" * 100)                   # keeps the TAIL (most recent), drops the head


def test_clip_just_under_cap_is_untouched():
    cfg = load_config()
    rows = ["Z" * (preamble.WINDOW_CHARS - 1)]           # boundary: one below the cap
    assert preamble.clip(rows, cfg) == rows[0]           # nothing clipped


def test_preamble_caps_an_oversized_compact_summary(monkeypatch):
    from council import ledger
    ledger.start_session(summary="S" * 9000)             # a /compact summary far over the cap
    cfg = load_config()
    pre = preamble.preamble(cfg)
    assert "S" * preamble.SUMMARY_CHARS in pre            # the lead carries the capped summary
    assert "S" * (preamble.SUMMARY_CHARS + 1) not in pre  # …and no more than the cap


def test_is_dead_recognises_only_full_markers():
    assert preamble.is_dead("_(claude unavailable: boom)_")
    assert preamble.is_dead("_(codex cancelled)_")
    assert not preamble.is_dead("a normal answer")
    assert not preamble.is_dead("_(this trails off")     # opens like a marker but never closes
    assert not preamble.is_dead("trails off)_")           # closes like one but never opens
