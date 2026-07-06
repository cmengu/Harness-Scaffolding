"""Every test runs hermetic: stub heads, a throwaway ledger, no ~/.council leakage.

The one trap this fixture exists for (further_steps step 0): `ledger._cfg` is
lru_cache'd, so a monkeypatched COUNCIL_LEDGER_PATH is invisible until the cache is
cleared — without the clears below, tests would write to the REAL ledger."""
from __future__ import annotations

from pathlib import Path

import pytest

from council import config as config_mod
from council import ledger

STUBS = Path(__file__).parent / "stubs"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Heads → canned shell scripts on the existing config seam; no keys, no network.
    monkeypatch.setenv("COUNCIL_CLAUDE_COMMAND", str(STUBS / "claude"))
    monkeypatch.setenv("COUNCIL_CODEX_COMMAND", str(STUBS / "codex"))
    # Ledger → per-test temp file (quarantine/ lands next to it).
    monkeypatch.setenv("COUNCIL_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    # The developer's real ~/.council/config.toml must not shape test behavior.
    monkeypatch.setattr(config_mod, "CONFIG_TOML", tmp_path / "config.toml")
    # Retry backoff in milliseconds, not seconds — tests exercise the loop, not the clock.
    monkeypatch.setenv("COUNCIL_RETRY_BASE_DELAY", "0.01")
    ledger._cfg.cache_clear()
    yield
    ledger._cfg.cache_clear()
