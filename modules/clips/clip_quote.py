"""
Generate a short title for the format1 top band.
With speech: extract the most powerful existing line from the transcript (6-8 words).
Without: general wealth/mindset quote.
"""

import logging
import random

from config import CLAUDE_MODEL
from modules.claude_cli import call_claude

logger = logging.getLogger(__name__)

_FALLBACK_QUOTES = [
    "The players who move the markets.",
    "Money is a tool. Build with it.",
    "Most people work for money. A few don't.",
    "Your habits today are your net worth tomorrow.",
    "Stop spending. Start building.",
    "One decision separates winners from everyone else.",
    "Rich habits beat rich intentions.",
    "They all started with nothing. Most quit.",
    "The wealthy invest first. The rest spend first.",
    "Success is rented, not owned. Pay daily.",
]


def _load_past_quotes(n: int = 25) -> list[str]:
    """Return recent hooks from saved scripts to avoid repeating phrasing."""
    import json
    from config import OUTPUT_DIR
    scripts_dir = OUTPUT_DIR / "scripts"
    if not scripts_dir.exists():
        return []
    files = sorted(scripts_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    quotes: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            h = data.get("hook", "").strip()
            if h:
                quotes.append(h)
        except Exception:
            continue
    return quotes


def generate_quote(transcript: str | None = None) -> str:
    """Return a short title (max 8 words) for the top band."""
    past = _load_past_quotes()
    if transcript and len(transcript.split()) >= 8:
        return _quote_from_transcript(transcript, past_quotes=past)
    return _general_quote(past_quotes=past)


def _quote_from_transcript(transcript: str, past_quotes: list[str] | None = None) -> str:
    avoid_block = ""
    if past_quotes:
        lines = "\n".join(f"  - {q}" for q in past_quotes[:15])
        avoid_block = (
            f"\n\nALREADY USED — use a COMPLETELY DIFFERENT phrasing and angle:\n{lines}\n"
        )

    prompt = (
        "Read this speech transcript to understand its THEME (what topic/idea it's about). "
        "Then write ONE original motivational quote about wealth, money, or success "
        "that fits that same theme. "
        "The quote must NOT copy the transcript words — it should feel like a powerful "
        "caption that sets the tone for a video on that topic. "
        "6 to 8 words maximum, ONE LINE. "
        "Use a UNIQUE angle/phrasing completely different from past generations. "
        "Examples for a clip about market players: 'The players who move the markets.' "
        "Examples for a clip about starting something: 'The first step is the hardest. Take it.' "
        "No quote marks, no hashtags, no author. Just the line."
        f"{avoid_block}"
        f"\n\nTranscript: {transcript[:600]}"
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        result = raw.strip().strip('"').strip("'").rstrip(".")
        if not result or len(result.split()) > 12:
            return random.choice(_FALLBACK_QUOTES)
        return result
    except Exception as e:
        logger.warning(f"Quote from transcript failed ({e}), using fallback")
        return random.choice(_FALLBACK_QUOTES)


def _general_quote(past_quotes: list[str] | None = None) -> str:
    avoid_block = ""
    if past_quotes:
        lines = "\n".join(f"  - {q}" for q in past_quotes[:15])
        avoid_block = (
            f"\n\nALREADY USED — use a COMPLETELY DIFFERENT phrasing and angle:\n{lines}"
        )

    prompt = (
        "Write ONE punchy title (6-8 words, ONE LINE) about wealth, mindset, or money. "
        "Use a UNIQUE angle/phrasing — completely different from past generations. "
        "Examples: 'The players who move the markets' or 'Most people work for money. A few don't.' "
        "No quote marks, no hashtags, no author. Just the line."
        f"{avoid_block}"
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        return raw.strip().strip('"').strip("'").rstrip(".")
    except Exception as e:
        logger.warning(f"General quote failed ({e}), using fallback")
        return random.choice(_FALLBACK_QUOTES)
