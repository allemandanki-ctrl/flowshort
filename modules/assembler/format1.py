"""
Format 1 assembler.
"""

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, OUTPUT_DIR
from modules.assembler import _run_ffmpeg, _loop_clips_to_duration, _F1_QUOTE_H, _F1_CLIP_H, _F1_CLIP_Y, _F1_RW_X, _F1_RW_Y, _render_quote_overlay

logger = logging.getLogger(__name__)

_VIDEOS_DIR = OUTPUT_DIR / "videos"
_FORMAT1_SEG_DUR = 7

def assemble_format1(script, visuals_result, voice_result, timestamps_result, music_path):
    """
    Quote-on-video: looping B-roll + Pillow-rendered transparent text overlays.
    """
    videos = visuals_result.get("videos", [])
    if not videos:
        raise ValueError("format1 needs video clips in visuals_result")

    lines = [script["hook"]] + list(script.get("body", []))
    if script.get("include_book_cta") and script.get("cta"):
        lines.append(script["cta"])

    n       = len(lines)
    seg_dur = _FORMAT1_SEG_DUR
    total   = n * seg_dur

    import tempfile
    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f1"
    tmp_dir.mkdir(exist_ok=True)
    overlay_pngs = []
    for i, line in enumerate(lines):
        out = str(tmp_dir / f"overlay_{i:02d}.png")
        _render_quote_overlay(line, out)
        overlay_pngs.append(out)
        logger.info(f"Overlay {i}: {line[:50]}")

    bg_clips = _loop_clips_to_duration(videos, total)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format1_{slug}.mp4"

    cmd = _format1_cmd(bg_clips, overlay_pngs, seg_dur, total, music_path, str(out))
    logger.info(f"Assembling format1 | segments={n} | total={total}s | bg_clips={len(bg_clips)}")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out

def _format1_cmd(bg_clips, overlay_pngs, seg_dur, total_dur, music_path, output_path, clip_dur=None):
    from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS
    
    n_clips = len(bg_clips)
    n_pngs  = len(overlay_pngs)
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

    cmd = ["ffmpeg", "-y"]
    for clip in bg_clips:
        if clip.lower().endswith(_IMAGE_EXTS):
            cmd += ["-loop", "1", "-i", clip]
        else:
            cmd += ["-i", clip]
    for png in overlay_pngs:
        cmd += ["-i", png]
    
    music_idx = n_clips + n_pngs
    if music_path:
        cmd += ["-i", music_path]

    filters = []
    for i in range(n_clips):
        d = None
        if isinstance(clip_dur, list):
            d = clip_dur[i % len(clip_dur)] if clip_dur else None
        elif clip_dur:
            d = clip_dur
        trim_step = f",trim=duration={d:.3f},setpts=PTS-STARTPTS" if d else ""
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1{trim_step}[s{i}]"
        )

    if n_clips == 1:
        filters.append(f"[s0]trim=duration={total_dur},setpts=PTS-STARTPTS[base]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(n_clips))
        filters.append(f"{concat_in}concat=n={n_clips}:v=1:a=0[cat]")
        filters.append(f"[cat]trim=duration={total_dur},setpts=PTS-STARTPTS[base]")

    prev = "base"
    for j in range(n_pngs):
        png_idx  = n_clips + j
        start    = j * seg_dur
        end      = (j + 1) * seg_dur
        out_lbl  = "vout" if j == n_pngs - 1 else f"vo{j}"
        filters.append(
            f"[{prev}][{png_idx}:v]overlay=0:0:enable='between(t,{start},{end})'[{out_lbl}]"
        )
        prev = out_lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]"]

    if music_path:
        fade_out = max(0, total_dur - 3)
        cmd += [
            "-map", f"{music_idx}:a",
            "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={fade_out}:d=3",
            "-shortest",
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-t", str(total_dur),
        output_path,
    ]
    return cmd
