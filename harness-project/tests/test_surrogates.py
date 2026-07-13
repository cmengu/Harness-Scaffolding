"""Lone-surrogate paste hygiene (live crash 11 Jul: a huge paste split mid-character
across pt's input reads left \\udcXX surrogates in the buffer; the history file's strict
encode blew up the event loop, and the head's stdin encode killed the call).

Contract: text is scrubbed at the composer seam — split multibyte chars REASSEMBLE
(\\udce2\\udc80\\udc94 = the bytes of '—'), garbage becomes � — and the head subprocess
seam survives surrogates regardless (defense in depth)."""
from __future__ import annotations

import pytest

from council.backends import proposer
from council.config import load_config

DASH_AS_SURROGATES = "\udce2\udc80\udc94"        # the three escaped bytes of '—'


def test_scrub_reassembles_split_chars():
    composer = pytest.importorskip("council.composer")
    assert composer._scrub_surrogates(f"A{DASH_AS_SURROGATES}B") == "A—B"
    assert composer._scrub_surrogates("bad \udce9 byte") == "bad � byte"
    clean = "no surrogates — at all ⚔"
    assert composer._scrub_surrogates(clean) is clean          # fast path: untouched


def test_head_call_survives_surrogates():
    cfg = load_config()
    out = proposer(f"question with a torn dash {DASH_AS_SURROGATES} inside", cfg)
    assert "STUB CLAUDE" in out                  # answered, not '_(claude unavailable: …)_'


def test_history_write_survives_surrogates(tmp_path):
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.history import FileHistory
    from council.composer import _scrub_surrogates
    h = FileHistory(str(tmp_path / "history"))
    h.store_string(_scrub_surrogates(f"pasted {DASH_AS_SURROGATES} line"))   # must not raise
    assert "pasted — line" in (tmp_path / "history").read_text()
