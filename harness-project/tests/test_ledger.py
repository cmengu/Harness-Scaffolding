"""The persistence seam: record→trace roundtrip, owner-only perms, session chains."""
from __future__ import annotations

from council.ledger import (RUN_ID, _cfg, chain_rows, quarantine, record,
                            sessions, start_session, trace)


def test_record_trace_roundtrip():
    record({"role": "probe", "value": 1})
    rows = trace(role="probe")
    assert len(rows) == 1
    assert rows[0]["value"] == 1
    assert rows[0]["run_id"] == RUN_ID       # step 1: every row is thread-addressable
    assert rows[0]["ts"] > 0


def test_trace_filters_on_every_key():
    record({"role": "head_call", "head": "claude", "ok": True})
    record({"role": "head_call", "head": "codex", "ok": False})
    assert len(trace(role="head_call")) == 2
    assert len(trace(role="head_call", head="codex")) == 1
    assert trace(role="nope") == []


def test_ledger_file_is_owner_only():
    record({"role": "probe"})
    assert (_cfg().ledger_path.stat().st_mode & 0o777) == 0o600


def test_session_chain_resume_splices_history():
    s1 = start_session()
    record({"role": "user", "text": "first thread"})
    record({"role": "debate", "round": 0, "proposer": "answer one", "adversary": None})
    start_session()                                      # an unrelated conversation between them
    record({"role": "user", "text": "noise"})
    start_session(resumes=s1)                            # /switch back to the first thread
    summary, rows = chain_rows()
    assert summary is None
    texts = [r.get("text") or r.get("proposer") for r in rows]
    assert "first thread" in texts and "answer one" in texts
    assert "noise" not in texts                          # the middle session is not in the chain


def test_compact_summary_caps_the_chain():
    start_session(summary="the gist of everything so far")
    summary, rows = chain_rows()
    assert summary == "the gist of everything so far"
    assert rows == []


def test_sessions_listed_in_file_order():
    a, b = start_session(), start_session(title="fork")
    segs = sessions()
    assert [s["sid"] for s in segs[-2:]] == [a, b]
    assert segs[-1]["start"]["title"] == "fork"


def test_quarantine_writes_a_readable_postmortem():
    path = quarantine("claude", RuntimeError("claude exited 1: 429 rate limit"),
                      {"kind": "transient", "attempts": 3, "question": "why is the sky blue?"})
    text = path.read_text()
    assert "head failure: claude" in text
    assert "429 rate limit" in text
    assert "why is the sky blue?" in text
    assert (path.stat().st_mode & 0o777) == 0o600        # full prompt text inside → owner-only
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    row = trace(role="quarantined")[0]
    assert row["path"] == str(path) and row["kind"] == "transient"
