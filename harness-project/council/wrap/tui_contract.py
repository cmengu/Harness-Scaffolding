"""council/wrap/tui_contract.py — H3: the ONE place Claude Code's screen layout is encoded.

Every camera check imports from here. When Anthropic changes the TUI it's a
one-line edit, not a hunt through bridge.py.

Values lifted from the vendored omnigent snapshot (claude_native_bridge.py:118-164),
which were themselves observed against a live Claude Code TUI — NOT the manuscript's
placeholders (it guessed "│ > "; the real input-box glyph is "❯").
"""

PROMPT_GLYPH = "❯"                        # the input-box marker _claude_prompt_rendered scans for
PASTED_PLACEHOLDER_PREFIX = "[Pasted text"    # claude shows this instead of a long pasted body
SCAN_TAIL_LINES = 5                       # bottom rows of capture-pane a prompt check reads
DRAFT_NEEDLE_MAX_CHARS = 24               # how much of the draft's first line to match verbatim

# H5 lives with its constant on purpose: council's OWN status bar must never
# contain PROMPT_GLYPH, or the camera would mistake council's status for a prompt.
