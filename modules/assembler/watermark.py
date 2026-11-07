"""
Blurs a rectangular corner region of a video -- built for stripping a CapCut
export watermark before publishing (format3 "Mode template" finish step, see
app.py's api_publish_upload template_pending_id branch). Position/size are
parameters, not hardcoded, because the exact watermark placement depends on
whatever CapCut template the user ends up using -- top-right is just today's
default guess.
"""

import logging
import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT

logger = logging.getLogger(__name__)

_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def blur_corner(
    video_path: Path,
    out_path: Path,
    corner: str = "top-right",
    w_pct: float = 0.25,
    h_pct: float = 0.10,
) -> Path:
    """
    Heavily blurs a `w_pct` x `h_pct` (fraction of VIDEO_WIDTH/VIDEO_HEIGHT)
    rectangle in the given corner, leaving the rest of the frame untouched.
    Re-encodes the whole video (no way around it -- the filter touches every
    frame), audio stream copied through unchanged.
    """
    if corner not in _CORNERS:
        raise ValueError(f"corner must be one of {_CORNERS}, got {corner!r}")

    w = max(2, round(VIDEO_WIDTH * w_pct))
    h = max(2, round(VIDEO_HEIGHT * h_pct))
    x = 0 if "left" in corner else VIDEO_WIDTH - w
    y = 0 if "top" in corner else VIDEO_HEIGHT - h

    filter_complex = (
        f"[0:v]split=2[base][region];"
        f"[region]crop={w}:{h}:{x}:{y},gblur=sigma=25[blurred];"
        f"[base][blurred]overlay={x}:{y}[vout]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-15:])
        raise RuntimeError(f"watermark blur failed:\n{tail}")

    logger.info(f"watermark blurred ({corner}, {w}x{h}px @ {x},{y}): {out_path.name}")
    return out_path
