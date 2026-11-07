"""
Subtitle Generator — ASS format, TikTok style.
Input  : word_timestamps from transcriber
Output : .ass file saved to temp dir (no spaces in path → no FFmpeg escaping issue)

Style: large bold white text, black outline, positioned upper-center (avoids platform UI).
Groups words into 3-word chunks for readability.
"""

import tempfile
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# Subtitle position: upper-center zone (safe from TikTok/IG bottom UI)
# \an5 = center-aligned around \pos(x, y)
_SUB_X = 540   # horizontal center of 1080px
_SUB_Y = 700   # ~36% from top of 1920px

_WORDS_PER_CHUNK = 3

_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
Collisions: Normal
PlayResX: 1080
PlayResY: 1920
Timer: 100.0000

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,78,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,4,2,5,30,30,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def generate_ass(timestamps_result: dict) -> Path:
    """
    Generate ASS subtitle file from word timestamps.
    Returns path to .ass file in system temp dir (no spaces).
    """
    words = timestamps_result.get("word_timestamps", [])
    if not words:
        raise ValueError("No word timestamps to generate subtitles from")

    chunks = _group_words(words, _WORDS_PER_CHUNK)
    lines = [_ASS_HEADER]

    for chunk_text, start, end in chunks:
        # Slight gap: show subtitle until next chunk starts (max 0.3s gap)
        t_start = _ass_time(start)
        t_end   = _ass_time(end + 0.05)
        # \an5 = center-anchored, \pos sets exact screen position
        text = f"{{\\an5\\pos({_SUB_X},{_SUB_Y})}}{chunk_text}"
        lines.append(f"Dialogue: 0,{t_start},{t_end},Default,,0,0,0,,{text}")

    ass_content = "\n".join(lines)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    tmp = Path(tempfile.gettempdir()) / f"pipeline_subs_{ts}.ass"
    tmp.write_text(ass_content, encoding="utf-8-sig")  # utf-8-sig for ASS compatibility
    logger.info(f"ASS subtitles written: {tmp} ({len(chunks)} chunks)")
    return tmp


def _group_words(words: list[dict], n: int) -> list[tuple[str, float, float]]:
    """Group word timestamps into N-word chunks. Returns (text, start, end) tuples."""
    chunks = []
    for i in range(0, len(words), n):
        group = words[i:i + n]
        text  = " ".join(w["word"] for w in group)
        start = group[0]["start"]
        end   = group[-1]["end"]
        chunks.append((text, start, end))
    return chunks


def _ass_time(seconds: float) -> str:
    """Convert float seconds to ASS time format H:MM:SS.cs"""
    seconds = max(0.0, seconds)
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = seconds % 60
    cs = int(round((s % 1) * 100))
    s  = int(s)
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
