"""
Step 2b - Transcriber (faster-whisper)
Input  : audio file path + original script dict
Output : timestamps dict saved to output/audio/

Output schema:
{
  "audio_path": str,
  "duration": float,           # seconds
  "word_timestamps": [
    {"word": str, "start": float, "end": float},
    ...
  ],
  "line_timestamps": [
    {"text": str, "start": float, "end": float, "type": "hook"|"body"|"cta"},
    ...
  ]
}

faster-whisper is lighter/faster than openai-whisper (CTranslate2 backend).
Model "base" is fast enough for short clips. Switch to "small" for better accuracy.
"""

import json
import logging
import re
from pathlib import Path

from faster_whisper import WhisperModel

from config import OUTPUT_DIR
from modules.voice import build_spoken_text, NO_VOICE_FORMATS

logger = logging.getLogger(__name__)

_AUDIO_DIR = OUTPUT_DIR / "audio"

# "base" = fast, good enough for clean TTS audio
# "small" = slower but more accurate (recommended for noisy audio)
WHISPER_MODEL = "base"
_model_cache: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model_cache
    if _model_cache is None:
        logger.info(f"Loading Whisper model '{WHISPER_MODEL}' (first run downloads it)...")
        _model_cache = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        logger.info("Whisper model loaded")
    return _model_cache


def transcribe(audio_path: str, script: dict) -> dict | None:
    """
    Transcribes audio and aligns word timestamps with script lines.

    Args:
        audio_path: path to .mp3 file
        script: original script dict (used to align line boundaries)

    Returns:
        timestamps dict, also saved as JSON next to the audio file
        None if format has no voice-over
    """
    fmt = script.get("format", "")
    if fmt in NO_VOICE_FORMATS:
        logger.info(f"Format {fmt} has no voice, skipping transcription")
        return None

    model = _get_model()

    logger.info(f"Transcribing {audio_path}")
    segments, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        language=None,  # auto-detect — works for fr and en
    )

    word_timestamps = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                word_timestamps.append({
                    "word": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

    duration = round(info.duration, 3)
    line_timestamps = _align_lines(word_timestamps, script)

    result = {
        "audio_path": audio_path,
        "duration": duration,
        "word_timestamps": word_timestamps,
        "line_timestamps": line_timestamps,
    }

    _save_timestamps(result, audio_path)
    logger.info(f"Transcription done | duration={duration}s | words={len(word_timestamps)}")

    return result


def transcribe_words(audio_path: str) -> list[dict]:
    """Word-level timestamps for ONE short standalone audio file -- no
    script/line-alignment needed. `transcribe()` above is built around a
    single long narration track matched back to a whole script's lines;
    formats that synthesize many short individual lines instead (format15's
    duel: one clip per round line) just need this line's own word timing,
    relative to that line's own t=0, to drive per-line animated captions."""
    model = _get_model()
    segments, _info = model.transcribe(audio_path, word_timestamps=True, language=None)
    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                words.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
    return words


def _align_lines(word_timestamps: list[dict], script: dict) -> list[dict]:
    """
    Groups word timestamps into line-level timestamps matching the script structure.
    Strategy: match words to lines by walking both sequences left-to-right.
    """
    lines = _extract_lines(script)
    if not lines or not word_timestamps:
        return []

    line_timestamps = []
    word_idx = 0
    total_words = len(word_timestamps)

    for line_text, line_type in lines:
        line_words = _tokenize(line_text)
        if not line_words:
            continue

        n = len(line_words)
        if word_idx >= total_words:
            break

        start = word_timestamps[word_idx]["start"]
        end_idx = min(word_idx + n - 1, total_words - 1)
        end = word_timestamps[end_idx]["end"]

        line_timestamps.append({
            "text": line_text,
            "start": round(start, 3),
            "end": round(end, 3),
            "type": line_type,
        })

        word_idx += n

    return line_timestamps


def _extract_lines(script: dict) -> list[tuple[str, str]]:
    """Returns (text, type) pairs from script in spoken order."""
    lines = []
    lines.append((script["hook"], "hook"))
    for line in script.get("body", []):
        lines.append((line, "body"))
    cta = script.get("cta")
    if cta and script.get("include_book_cta"):
        lines.append((cta, "cta"))
    return lines


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def _save_timestamps(result: dict, audio_path: str) -> Path:
    audio_p = Path(audio_path)
    ts_path = audio_p.with_suffix(".timestamps.json")
    ts_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Timestamps saved -> {ts_path}")
    return ts_path
