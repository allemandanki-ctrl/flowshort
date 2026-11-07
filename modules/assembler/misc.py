"""
Format 4, 6 and helpers assembler.
"""

import logging
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageEnhance
from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, OUTPUT_DIR
from modules.assembler import _run_ffmpeg, _VIDEOS_DIR, _resize_crop, _darken

logger = logging.getLogger(__name__)

def assemble_format4(script, visuals_result, voice_result, timestamps_result, music_path):
    if not voice_result:
        raise ValueError("format4 requires voice_result")
    if not timestamps_result:
        raise ValueError("format4 requires timestamps_result")

    voice_path = voice_result["audio_path"]
    duration   = timestamps_result["duration"]

    from modules.subtitle_generator import generate_ass
    ass_path = generate_ass(timestamps_result)

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f4"
    tmp_dir.mkdir(exist_ok=True)
    bg_img_path = tmp_dir / "bg_f4.jpg"

    images = visuals_result.get("images", [])
    if images:
        _make_dark_bg(images[0], str(bg_img_path), darken_factor=0.35)
    else:
        _make_gradient_bg(str(bg_img_path))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format4_{slug}.mp4"

    cmd = _voice_image_cmd(str(bg_img_path), voice_path, ass_path, duration, str(out))
    logger.info(f"Assembling format4 | static bg | duration={duration:.1f}s | subs=yes")
    _run_ffmpeg(cmd, cwd=str(ass_path.parent))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out

def _make_dark_bg(img_path: str, out_path: str, darken_factor: float = 0.35) -> None:
    img = Image.open(img_path).convert("RGB")
    img = _resize_crop(img, VIDEO_WIDTH, VIDEO_HEIGHT)
    img = _darken(img, darken_factor)
    img.save(out_path, quality=88)

def _make_gradient_bg(out_path: str) -> None:
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (15, 15, 20))
    img.save(out_path, quality=88)

def _voice_image_cmd(bg_image, voice_path, ass_path, duration, output_path):
    cmd = ["ffmpeg", "-y"]
    cmd += ["-loop", "1", "-t", str(duration), "-i", bg_image]
    cmd += ["-i", voice_path]

    filter_str = f"[0:v]setsar=1,subtitles={ass_path.name}[vout]"

    cmd += ["-filter_complex", filter_str]
    cmd += ["-map", "[vout]", "-map", "1:a"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(duration),
        output_path,
    ]
    return cmd
