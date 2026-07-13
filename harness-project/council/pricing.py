"""council/pricing.py — codex list prices, keyed by model.

Codex's CLI reports token usage but never a dollar figure (probes 11 Jul), so council
prices the tokens locally. This table is the single source of truth: /model codex <id>
sets cfg.codex_model, and codex_usd() resolves the rate from here — switch the model and
the cost re-prices with it. Claude needs no table; its CLI returns real dollars.

Rates = OpenAI list price, USD per 1M tokens, (input, cached_input, output). Verified
13 Jul 2026. Cached input is 90% off input across the line (OpenAI's cached-read discount),
so cached ≈ input / 10 where a separate figure isn't published. Edit a row when a price
moves; add a row when a model ships. A model not in the table falls back to DEFAULT and is
flagged as an assumed rate so a stale figure is never silently trusted.
"""
from __future__ import annotations

Rate = tuple[float, float, float]   # (input, cached_input, output) — USD per 1M tokens

CODEX_PRICES: dict[str, Rate] = {
    # codex-line models — the codex CLI pins one of these
    "gpt-5-codex":   (1.25, 0.125, 10.0),
    "gpt-5.1-codex": (1.25, 0.125, 10.0),
    "gpt-5.2-codex": (1.75, 0.175, 14.0),
    "gpt-5.3-codex": (1.75, 0.175, 14.0),
    # GPT-5.6 general tiers (Sol/Terra/Luna) — reachable with -m if you point codex at them
    "gpt-5.6-sol":   (5.00, 0.50, 30.0),
    "gpt-5.6-terra": (2.50, 0.25, 15.0),
    "gpt-5.6-luna":  (1.00, 0.10,  6.0),
}

# What the codex CLI resolves to when cfg.codex_model is None (probes 13 Jul: codex exec
# defaults to the gpt-5.3-codex line). Update alongside the CLI's own default.
DEFAULT_MODEL = "gpt-5.3-codex"


def codex_rate(model: str | None) -> tuple[Rate, str, bool]:
    """Resolve a model id to (rate, matched_model, exact). Exact ids win; otherwise a
    prefix match catches dated/suffixed ids ('gpt-5.3-codex-2026-07-…'). An unrecognized
    model falls back to DEFAULT_MODEL's rate with exact=False, so callers can show a
    'assumed rate' warning instead of a confidently-wrong number."""
    name = (model or DEFAULT_MODEL).strip().lower()
    if name in CODEX_PRICES:
        return CODEX_PRICES[name], name, True
    for key, rate in CODEX_PRICES.items():
        if name.startswith(key):
            return rate, key, True
    return CODEX_PRICES[DEFAULT_MODEL], DEFAULT_MODEL, False
