"""
Format 6 assembler.
"""

import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, OUTPUT_DIR
from modules.assembler import _run_ffmpeg, _VIDEOS_DIR, _render_number_overlay

logger = logging.getLogger(__name__)

def assemble_format6(script, visuals_result, voice_result, timestamps_result, music_path):
    """
    Ranking list: dark B-roll per item + voice + word-synced subtitles +
    a big number overlay during each ranked item's segment.
    """
    if not voice_result:
        raise ValueError("format6 requires voice_result")
    if not timestamps_result:
        raise ValueError("format6 requires timestamps_result")

    videos = visuals_result.get("videos", [])
    if not videos:
        raise ValueError("format6 needs video clips in visuals_result")

    body = list(script.get("body", []))
    if not body:
        raise ValueError("format6 requires body (one line per ranked item)")

    words      = timestamps_result["words"]
    duration   = timestamps_result["duration"]
    voice_path = voice_result["audio_path"]

    item_starts = _find_body_segment_starts(words, body)
    bounds = [0.0] + item_starts + [duration]

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f6"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    seg_paths: list[Path] = []
    for i in range(len(bounds) - 1):
        seg_start, seg_end = bounds[i], bounds[i + 1]
        seg_dur = max(0.3, seg_end - seg_start)
        clip = videos[i % len(videos)]
        seg_out = tmp_dir / f"seg_{i:02d}.mp4"

        vf = (
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuv420p,fps={VIDEO_FPS}"
        )

        if i == 0:
            _run_ffmpeg([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", clip,
                "-t", str(seg_dur), "-vf", vf, "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                str(seg_out),
            ])
        else:
            item_number = _extract_item_number(body[i - 1], fallback=i)
            number_png = tmp_dir / f"num_{i:02d}.png"
            _render_number_overlay(item_number, str(number_png))
            _run_ffmpeg([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", clip, "-i", str(number_png),
                "-t", str(seg_dur),
                "-filter_complex", f"[0:v]{vf}[bg];[bg][1:v]overlay=0:0[vout]",
                "-map", "[vout]", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                str(seg_out),
            ])
        seg_paths.append(seg_out)

    concat_list = tmp_dir / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.name}'" for p in seg_paths), encoding="utf-8")
    video_only = tmp_dir / "video_only.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-c", "copy", "video_only.mp4",
    ], cwd=str(tmp_dir))

    from modules.subtitle_generator import generate_ass
    ass_path = generate_ass({"word_timestamps": words})

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format6_{slug}.mp4"

    local_video = ass_path.parent / "video_only.mp4"
    local_video.write_bytes(video_only.read_bytes())

    cmd = [
        "ffmpeg", "-y",
        "-i", "video_only.mp4", "-i", voice_path,
        "-filter_complex", f"[0:v]subtitles={ass_path.name}[vout]",
        "-map", "[vout]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(duration),
        str(out),
    ]
    logger.info(f"Assembling format6 | items={len(body)} | duration={duration:.1f}s")
    _run_ffmpeg(cmd, cwd=str(ass_path.parent))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out

def _normalize_word(w: str) -> str:
    return re.sub(r"[^\w]", "", w).lower()

_LEADING_NUMBER_RE = re.compile(r"^\s*number\s+\S+\s*:?\s*", re.IGNORECASE)
_LEADING_NUMBER_DIGIT_RE = re.compile(r"^\s*number\s+(\d+)\s*:?", re.IGNORECASE)

def _extract_item_number(line: str, fallback: int) -> int:
    """
    The overlay must show the SAME number the voice actually says for that
    item, not just its position in the video -- the script can rank items
    in descending order ("Number 7: ...", ..., "Number 1: ..."), in which
    case position 1 (spoken first) is "Number 7", not "1". Falls back to the
    segment position only if the line doesn't start with "Number <digit>:".
    """
    m = _LEADING_NUMBER_DIGIT_RE.match(line)
    return int(m.group(1)) if m else fallback

def _find_body_segment_starts(words: list[dict], body: list[str]) -> list[float]:
    starts: list[float] = []
    cursor = 0
    for line in body:
        label = _LEADING_NUMBER_RE.sub("", line)
        line_words = [_normalize_word(w) for w in label.split()[:4] if _normalize_word(w)]
        found_at = None
        for i in range(cursor, len(words)):
            candidate = [_normalize_word(words[j]["word"]) for j in range(i, min(i + len(line_words), len(words)))]
            if candidate == line_words[:len(candidate)] and len(candidate) == len(line_words):
                found_at = i
                break
        if found_at is None:
            fallback_frac = len(starts) / max(1, len(body))
            fallback_time = words[int(fallback_frac * (len(words) - 1))]["start"] if words else 0.0
            starts.append(fallback_time)
            logger.warning(f"format6: couldn't locate body line in transcript, using fallback timing: {line[:40]!r}")
            continue
        starts.append(words[found_at]["start"])
        cursor = found_at + 1
    return starts
