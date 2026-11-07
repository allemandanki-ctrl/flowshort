"""
Persistent CTA rotation counter.
Tracks how many videos have been generated and decides if the current
one should include the book CTA (1 every N videos).
State stored in state/cta_state.json.
"""

import json
from pathlib import Path
from config import STATE_DIR, BOOK_CTA_EVERY_N

_STATE_FILE = STATE_DIR / "cta_state.json"


def _load() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    return {"total_generated": 0, "last_cta_at": -1}


def _save(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def should_include_book_cta() -> bool:
    """Returns True if this video should carry the book CTA."""
    state = _load()
    n = state["total_generated"]
    last = state["last_cta_at"]
    return (n - last) >= BOOK_CTA_EVERY_N


def increment(included_cta: bool) -> None:
    """Call after a script is generated to advance the counter."""
    state = _load()
    state["total_generated"] += 1
    if included_cta:
        state["last_cta_at"] = state["total_generated"] - 1
    _save(state)


def status() -> dict:
    return _load()
