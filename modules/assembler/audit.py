"""
Format 12 assembler -- "Audit Financier de Personnage" (character financial roast).

Purely textual: the character is referenced BY NAME in on-screen commentary
only -- no clip, no image, no cloned voice of them (nominative reference,
same as written parody/commentary). Report-card style: header (character
name) -> 3 "violation" cards revealed one at a time (X mark + the sin) ->
verdict/CTA card.

No voice, no B-roll -- same cheap production class as format8/9/10.
"""

import logging
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, OUTPUT_DIR
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text, _FADE_DUR,
    _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD,
    _TEXT_COLOR, _NAIVE_COLOR, _REAL_COLOR,
)

logger = logging.getLogger(__name__)

_HEADER_DUR   = 2.5
_VIOLATION_DUR = 3.0    # x3 violations
_CTA_DUR      = 3.5

_X_COLOR = (196, 108, 96, 255)   # muted red -- "violation" marker, still desaturated/on-brand


def assemble_format12(script, visuals_result, voice_result, timestamps_result, music_path):
    """Pure motion-graphics assembler -- ignores visuals_result/voice_result/timestamps_result."""
    hook = script.get("hook", "").strip()
    body = [b.strip() for b in script.get("body", []) if b.strip()][:3]
    cta  = (script.get("cta") or "Don't be like them. Subscribe.").strip()

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f12"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    bg_png = tmp_dir / "bg.png"
    _build_bg(bg_png)

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0

    header_png = tmp_dir / "header.png"
    _render_header(hook, header_png)
    overlay_specs.append((header_png, t, t + _HEADER_DUR))
    t += _HEADER_DUR

    for i in range(len(body)):
        png = tmp_dir / f"violation_{i}.png"
        _render_violations(hook, body[:i + 1], png)
        overlay_specs.append((png, t, t + _VIOLATION_DUR))
        t += _VIOLATION_DUR

    cta_png = tmp_dir / "cta.png"
    _render_cta(cta, cta_png)
    total = t + _CTA_DUR
    overlay_specs.append((cta_png, t, total))

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format12_{slug}.mp4"

    cmd = _audit_cmd(bg_png, overlay_specs, total, music_path, str(out))
    logger.info(f"Assembling format12 (audit financier) | violations={len(body)} | total={total:.1f}s")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


def _build_bg(out_path: Path) -> None:
    top, bottom = (58, 59, 63), (16, 16, 18)
    img  = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    img.save(str(out_path), quality=92)


def _render_header(hook: str, out_path: Path) -> None:
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=68)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=68)
    wrapped = textwrap.fill(hook, width=20, break_long_words=False)
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(overlay, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14)
    overlay.save(str(out_path), format="PNG")


def _draw_x_mark(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color, width: int = 6) -> None:
    """
    Hand-drawn X (two crossing lines) instead of the Unicode "✗" glyph --
    Segoe UI's cmap doesn't cover U+2717 on this system, so PIL rendered it
    as a hollow tofu box instead of an actual X. Drawing it manually has no
    font/glyph dependency at all.
    """
    draw.line([(cx - r, cy - r), (cx + r, cy + r)], fill=color, width=width)
    draw.line([(cx - r, cy + r), (cx + r, cy - r)], fill=color, width=width)


def _render_violations(hook: str, violations: list[str], out_path: Path) -> None:
    """Header line stays pinned at top (smaller), violations stack below it, each with an X mark."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        head_font = ImageFont.truetype(_FONT_REGULAR, size=44)
        item_font = ImageFont.truetype(_FONT_LIGHT, size=38)
    except OSError:
        head_font = item_font = ImageFont.load_default()

    wrapped_head = textwrap.fill(hook, width=32, break_long_words=False)
    _soft_text(overlay, (80, 140), wrapped_head, head_font, _TEXT_COLOR, spacing=10)
    hb = draw.textbbox((80, 140), wrapped_head, font=head_font, spacing=10)

    y = hb[3] + 70
    for v in violations:
        wrapped_v = textwrap.fill(v, width=34, break_long_words=False)
        vb = draw.textbbox((150, y - 2), wrapped_v, font=item_font, spacing=10)
        line_h = vb[3] - (y - 2)
        _draw_x_mark(draw, 95, y + line_h // 2 - 10, 16, _X_COLOR)
        _soft_text(overlay, (150, y - 2), wrapped_v, item_font, _TEXT_COLOR, spacing=10)
        y = vb[3] + 46

    overlay.save(str(out_path), format="PNG")


def _render_cta(text: str, out_path: Path) -> None:
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=72)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=72)
    wrapped = textwrap.fill(text, width=20, break_long_words=False)
    draw = ImageDraw.Draw(overlay)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(overlay, (x, y), wrapped, font, _REAL_COLOR, align="center", spacing=14)
    overlay.save(str(out_path), format="PNG")


def _audit_cmd(
    bg_png: Path,
    overlay_specs: list[tuple[Path, float, float]],
    total_dur: float,
    music_path: str | None,
    output_path: str,
) -> list[str]:
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(bg_png)]
    for png, _, _ in overlay_specs:
        cmd += ["-loop", "1", "-i", str(png)]
    music_idx = 1 + len(overlay_specs)
    if music_path:
        cmd += ["-i", music_path]

    filters = [f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p[base]"]
    prev = "base"
    for j, (_, start, end) in enumerate(overlay_specs):
        png_idx = 1 + j
        lbl = "vout" if j == len(overlay_specs) - 1 else f"vo{j}"
        filters.append(
            f"[{png_idx}:v]format=rgba,fade=t=in:st={start:.3f}:d={_FADE_DUR}:alpha=1[fov{j}]"
        )
        filters.append(
            f"[{prev}][fov{j}]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[{lbl}]"
        )
        prev = lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]"]

    if music_path:
        fade_out = max(0, total_dur - 2)
        cmd += [
            "-map", f"{music_idx}:a",
            "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.2f}:d=2",
            "-shortest",
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-t", str(total_dur),
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd
