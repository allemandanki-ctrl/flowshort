"""
Transcribe a clip's audio using faster-whisper with word-level timestamps.

Uses faster-whisper (int8, CPU) — memory-efficient, unlike openai-whisper which
loads the whole model into RAM and can raise MemoryError on the 'small' model.

Model sizes (downloaded once, cached):
  base  — fast, decent accuracy
  small — recommended balance (default)
  medium — best quality, slower

Set WHISPER_MODEL=base in .env to override default.
"""

import logging
import os
from pathlib import Path

# Limit MKL/OMP thread arenas BEFORE ctranslate2 loads — cuts peak memory and
# avoids 'mkl_malloc: failed to allocate memory' when free RAM is low.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("MKL_DYNAMIC", "FALSE")

from config import WHISPER_MODEL

logger = logging.getLogger(__name__)

_model_cache = None
_cached_name: str | None = None


_FALLBACK = "base"   # memory-safe model when the requested one can't be allocated


def _get_model(name: str):
    global _model_cache, _cached_name
    if _model_cache is not None and _cached_name == name:
        return _model_cache

    from faster_whisper import WhisperModel
    for candidate in ([name] if name == _FALLBACK else [name, _FALLBACK]):
        try:
            logger.info(f"Loading faster-whisper model '{candidate}' (int8/cpu)...")
            _model_cache = WhisperModel(candidate, device="cpu", compute_type="int8", cpu_threads=1)
            _cached_name = candidate
            logger.info(f"Whisper model '{candidate}' loaded")
            return _model_cache
        except (MemoryError, RuntimeError) as e:
            logger.warning(f"Could not load '{candidate}' ({e}); "
                           f"{'falling back to ' + _FALLBACK if candidate != _FALLBACK else 'no smaller model left'}")
    raise RuntimeError("Whisper model could not be loaded (out of memory even for 'base')")


def transcribe_clip(clip_path: Path, model_name: str | None = None) -> dict:
    """
    Transcribe speech from clip_path.

    Returns:
        {
          "words":     [{"word": str, "start": float, "end": float}, ...],
          "full_text": str,
          "language":  str,
          "duration":  float,
        }
    """
    name  = model_name or WHISPER_MODEL
    model = _get_model(name)

    logger.info(f"Transcribing {clip_path.name}...")
    segments, info = model.transcribe(
        str(clip_path),
        word_timestamps=True,
        language=None,   # auto-detect
    )

    words: list[dict] = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                word = w.word.strip()
                if word:
                    words.append({
                        "word":  word,
                        "start": round(float(w.start), 3),
                        "end":   round(float(w.end), 3),
                    })

    duration  = round(float(info.duration), 3)
    full_text = " ".join(w["word"] for w in words)
    logger.info(f"Transcription: {len(words)} words | lang={info.language} | duration={duration:.1f}s")

    return {
        "words":     words,
        "full_text": full_text,
        "language":  info.language,
        "duration":  duration,
    }


_CLIMAX_MIN_DUR = 12.0    # never trim below this — a climax needs room to land
_CLIMAX_MAX_DUR = 45.0    # short-form ceiling; trims a long/rambling source down
_MIN_WORDS_FOR_CLIMAX = 20   # below this there's nothing meaningful to trim around


def find_climax_segment(words: list[dict], duration: float) -> tuple[float, float]:
    """
    Ask Claude which contiguous stretch of the transcript is the strongest
    excerpt -- skip any slow build-up or wind-down, go straight to (and
    through) the climax. Returns (start_sec, end_sec) to trim the clip to.

    Reference technique (viral clip-edit breakdown): "this speech was longer
    than the actual speech used in the video... try to avoid any buildups or
    any anticlimax parts, go straight to the climax cuz this is going to
    keep retention high." Word-index based (not raw seconds) because Claude
    reasons far more reliably over text boundaries than over a timestamp
    number it can't see the content of.

    Falls back to the full clip on any failure, or when the clip is already
    short enough that trimming would only lose content for no benefit.
    """
    if duration <= _CLIMAX_MIN_DUR or len(words) < _MIN_WORDS_FOR_CLIMAX:
        return 0.0, duration

    from config import CLAUDE_MODEL

    numbered = "\n".join(f"{i}: {w['word']}" for i, w in enumerate(words))
    prompt = (
        "This is a word-by-word transcript (index: word) of a speech clip being "
        "cut down for a short-form video. Find the single strongest CONTIGUOUS "
        f"excerpt -- between {_CLIMAX_MIN_DUR:.0f} and {_CLIMAX_MAX_DUR:.0f} seconds of "
        "speech. Skip any slow setup/preamble at the start and any trailing "
        "wind-down at the end -- start right where it gets compelling, and let it "
        "run through to its actual climax/punchline, not before.\n\n"
        f"{numbered}\n\n"
        'Answer with JSON only: {"start_index": N, "end_index": M} '
        "(both inclusive, referring to the word indices above)."
    )
    try:
        from modules.claude_cli import call_claude
        import json
        import re
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0)) if m else {}
        i0, i1 = int(result["start_index"]), int(result["end_index"])
        if not (0 <= i0 < i1 < len(words)):
            return 0.0, duration
        start = max(0.0, words[i0]["start"] - 0.15)
        end   = min(duration, words[i1]["end"] + 0.15)
        if end - start < _CLIMAX_MIN_DUR * 0.6:   # sanity floor against a bad answer
            return 0.0, duration
        logger.info(f"Climax segment: {start:.1f}s-{end:.1f}s "
                    f"(of {duration:.1f}s, words {i0}-{i1}/{len(words)})")
        return start, end
    except Exception as e:
        logger.warning(f"Climax segment detection failed ({e}); using full clip")
        return 0.0, duration
