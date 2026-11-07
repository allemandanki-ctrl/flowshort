"""
Voice-based assembler (Formats 2 & 5).
"""

import logging
from datetime import datetime
from pathlib import Path
from modules.assembler import _run_ffmpeg, _loop_clips_to_duration, _VIDEOS_DIR

logger = logging.getLogger(__name__)

def assemble_voice_format(script, visuals_result, voice_result, timestamps_result, music_path):
    """Shared assembler for format2 and format5 (video B-roll + voice + ASS subs)."""
    if not voice_result:
        raise ValueError(f"{script['format']} requires voice_result")
    if not timestamps_result:
        raise ValueError(f"{script['format']} requires timestamps_result")

    videos   = visuals_result.get("videos", [])
    if not videos:
        raise ValueError(f"{script['format']} needs video clips in visuals_result")

    voice_path = voice_result["audio_path"]
    duration   = timestamps_result["duration"]

    # generate_ass expects {"word_timestamps": [...]}, but transcribe_clip()
    # (this function's only caller, via pipeline.py's mf1/mf3-style branches)
    # returns {"words": [...], "duration": ..., ...} -- this mismatch was
    # latent and never exercised before: format2/format5 (this function's
    # original intended callers) were never wired into config.FORMATS or
    # pipeline.py's dispatch, so assemble_voice_format had no real caller
    # until MindFile's mf1 reused it (2026-07-28).
    from modules.subtitle_generator import generate_ass
    ass_path = generate_ass({"word_timestamps": timestamps_result.get("words", [])})

    # Repeat clips to cover full voice duration
    bg_clips = _loop_clips_to_duration(videos, duration)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    fmt  = script["format"]
    out  = _VIDEOS_DIR / f"{ts}_{fmt}_{slug}.mp4"

    cmd = _voice_video_cmd(bg_clips, voice_path, ass_path, duration, str(out))
    logger.info(f"Assembling {fmt} | clips={len(bg_clips)} | duration={duration:.1f}s | subs=yes")
    _run_ffmpeg(cmd, cwd=str(ass_path.parent))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out

def _voice_video_cmd(clips, voice_path, ass_path, duration, output_path):
    from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS
    n   = len(clips)
    cmd = ["ffmpeg", "-y"]

    for clip in clips:
        cmd += ["-i", clip]
    voice_idx = n
    cmd += ["-i", voice_path]

    filters = []
    for i in range(n):
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1[s{i}]"
        )

    if n == 1:
        filters.append(f"[s0]trim=duration={duration:.3f}[trimmed]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(n))
        filters.append(f"{concat_in}concat=n={n}:v=1:a=0[cat]")
        filters.append(f"[cat]trim=duration={duration:.3f}[trimmed]")

    filters.append(f"[trimmed]subtitles={ass_path.name}[vout]")

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]", "-map", f"{voice_idx}:a"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(duration),
        output_path,
    ]
    return cmd
