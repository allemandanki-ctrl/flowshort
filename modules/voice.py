"""
Step 2a - Voice Generator (Edge-TTS)
Input  : script dict (from script_generator)
Output : audio .mp3 file path + spoken text used

Edge-TTS is free (Microsoft Azure TTS, no API key needed).
Format 3 (Luxury Aesthetic) has no voice-over -> returns None.
"""

import asyncio
import logging
import re
from pathlib import Path

import edge_tts

from config import OUTPUT_DIR, CONTENT_LANGUAGE

logger = logging.getLogger(__name__)

_AUDIO_DIR = OUTPUT_DIR / "audio"

# Voice map per language — override by setting VOICE_NAME in .env
_VOICE_MAP = {
    "fr": "fr-FR-HenriNeural",       # male, clear, energetic
    "en": "en-US-ChristopherNeural", # male, energetic
}

VOICE_NAME = _VOICE_MAP.get(CONTENT_LANGUAGE, "en-US-ChristopherNeural")

# Formats with no voice-over
NO_VOICE_FORMATS = {"format1", "format3"}


def build_spoken_text(script: dict) -> str | None:
    """
    Assembles the text to be spoken from a script dict.
    Returns None for formats that have no voice-over.
    """
    fmt = script.get("format", "")
    if fmt in NO_VOICE_FORMATS:
        return None

    parts = [script["hook"]]
    parts.extend(script.get("body", []))

    cta = script.get("cta")
    # format6's cta is a plain closing line (never a book plug -- its
    # include_book_cta is always False), so it's spoken whenever present,
    # unlike other formats where cta is gated behind the book-CTA toggle.
    if cta and (fmt == "format6" or script.get("include_book_cta")):
        parts.append(cta)

    return "\n".join(parts)


def generate_voice(script: dict, voice: str = VOICE_NAME) -> dict | None:
    """
    Generates TTS audio from script.

    Returns:
        dict with keys: audio_path, spoken_text, voice
        None if format has no voice-over (format3)
    """
    spoken_text = build_spoken_text(script)
    if spoken_text is None:
        logger.info(f"Format {script['format']} has no voice-over, skipping TTS")
        return None

    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    slug = _slugify(script["subject"])
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_path = _AUDIO_DIR / f"{ts}_{script['format']}_{slug}.mp3"

    logger.info(f"Generating TTS | voice={voice} | chars={len(spoken_text)}")

    asyncio.run(_synthesize(spoken_text, str(audio_path), voice))

    logger.info(f"Audio saved -> {audio_path}")

    return {
        "audio_path": str(audio_path),
        "spoken_text": spoken_text,
        "voice": voice,
    }


async def _synthesize(text: str, output_path: str, voice: str) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:40]
