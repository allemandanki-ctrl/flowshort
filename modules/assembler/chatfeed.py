"""
Format 10 assembler -- "Fake Conversation" (chat bubble mockup).

Cumulative reveal (not sequential full-screen replacement like format8/9):
each new bubble is added BELOW the previous ones, which stay visible --
building downward like a real chat thread, matches the "rupture de format"
brief (zero video, zero motion-graphics chart, pure UI mockup).

  bubble 0 (hook, LEFT/received) -> bubbles 1-4 (body, RIGHT/sent) ->
  bubble 5 (cta, RIGHT/sent, same style as body).

No voice, no B-roll -- same cheap production class as format8/format9.
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
    _FONT_REGULAR, _FONT_LIGHT, _TEXT_COLOR,
)

logger = logging.getLogger(__name__)

_BUBBLE_HOLD_DUR = 2.1     # how long each new bubble is the "latest" before the next appears
_CTA_HOLD_DUR    = 3.0     # last bubble (cta) holds longer, matches other formats' cta beat

_HEADER_H     = 160
_BUBBLE_PAD_X = 32
_BUBBLE_PAD_Y = 22
_BUBBLE_GAP   = 28
_BUBBLE_MAX_W = int(VIDEO_WIDTH * 0.72)
_SIDE_MARGIN  = 40
_RADIUS       = 34

_RECEIVED_FILL = (45, 46, 51, 255)     # left bubble -- neutral dark gray
_SENT_FILL     = (63, 52, 34, 255)     # right bubble -- dark champagne tint, matches brand accent
_HEADER_FILL   = (24, 24, 27, 255)


def assemble_format10(script, visuals_result, voice_result, timestamps_result, music_path):
    """Pure UI-mockup assembler -- ignores visuals_result/voice_result/timestamps_result."""
    hook = script.get("hook", "").strip()
    body = [b.strip() for b in script.get("body", []) if b.strip()]
    cta  = (script.get("cta") or "saving this for later.").strip()

    messages = [("left", hook)] + [("right", b) for b in body] + [("right", cta)]

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f10"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    bg_png = tmp_dir / "bg.png"
    _build_bg(bg_png)

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0
    for i in range(1, len(messages) + 1):
        png = tmp_dir / f"stage_{i}.png"
        _render_thread(messages[:i], png)
        is_last = i == len(messages)
        dur = _CTA_HOLD_DUR if is_last else _BUBBLE_HOLD_DUR
        overlay_specs.append((png, t, t + dur))
        t += dur
    total = t

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format10_{slug}.mp4"

    cmd = _chatfeed_cmd(bg_png, overlay_specs, total, music_path, str(out))
    logger.info(f"Assembling format10 (fake conversation) | bubbles={len(messages)} | total={total:.1f}s")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


def _build_bg(out_path: Path) -> None:
    """Same soft charcoal gradient as format8/9, plus a flat header bar (fake contact name)."""
    top, bottom = (58, 59, 63), (16, 16, 18)
    img  = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    draw.rectangle([0, 0, VIDEO_WIDTH, _HEADER_H], fill=_HEADER_FILL[:3])
    draw.line([(0, _HEADER_H), (VIDEO_WIDTH, _HEADER_H)], fill=(45, 46, 50), width=2)

    try:
        name_font = ImageFont.truetype(_FONT_REGULAR, size=40)
        status_font = ImageFont.truetype(_FONT_LIGHT, size=26)
    except OSError:
        name_font = status_font = ImageFont.load_default()
    draw.text((_SIDE_MARGIN, 52), "Anonymous", font=name_font, fill=(225, 225, 222))
    draw.text((_SIDE_MARGIN, 100), "online", font=status_font, fill=(120, 200, 140))

    img.save(str(out_path), quality=92)


def _wrap_bubble_text(text: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> tuple[str, int, int]:
    """Wrap to fit _BUBBLE_MAX_W minus padding, return (wrapped_text, w, h)."""
    max_text_w = _BUBBLE_MAX_W - 2 * _BUBBLE_PAD_X
    for width_chars in range(60, 8, -2):
        wrapped = textwrap.fill(text, width=width_chars, break_long_words=False)
        bbox = draw.textbbox((0, 0), wrapped, font=font, align="left", spacing=8)
        if bbox[2] - bbox[0] <= max_text_w:
            return wrapped, bbox[2] - bbox[0], bbox[3] - bbox[1]
    return text, max_text_w, font.size


def _render_thread(messages: list[tuple[str, str]], out_path: Path) -> None:
    """
    All messages up to this stage, stacked top-to-bottom starting just below
    the header, most recent at the bottom (like a real chat thread). Bubbles
    that no longer fit scroll off the top -- kept simple here as "drop the
    oldest if we'd overflow" rather than a real scroll animation.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype(_FONT_REGULAR, size=34)
    except OSError:
        font = ImageFont.load_default()

    # Pre-measure all bubbles, then drop the oldest ones that would overflow
    # the visible thread area (below header, above bottom margin).
    max_bottom = VIDEO_HEIGHT - 120
    measured = []
    for side, text in messages:
        wrapped, tw, th = _wrap_bubble_text(text, font, draw)
        bh = th + 2 * _BUBBLE_PAD_Y
        measured.append((side, wrapped, tw, th, bh))

    visible = list(measured)
    total_h = sum(m[4] + _BUBBLE_GAP for m in visible)
    while visible and total_h > (max_bottom - (_HEADER_H + 40)):
        dropped = visible.pop(0)
        total_h -= (dropped[4] + _BUBBLE_GAP)

    y = _HEADER_H + 40
    for side, wrapped, tw, th, bh in visible:
        bw = tw + 2 * _BUBBLE_PAD_X
        if side == "left":
            x0 = _SIDE_MARGIN
            fill = _RECEIVED_FILL
        else:
            x0 = VIDEO_WIDTH - _SIDE_MARGIN - bw
            fill = _SENT_FILL
        draw.rounded_rectangle([x0, y, x0 + bw, y + bh], radius=_RADIUS, fill=fill)
        _soft_text(overlay, (x0 + _BUBBLE_PAD_X, y + _BUBBLE_PAD_Y), wrapped, font,
                  _TEXT_COLOR, align="left", spacing=8)
        y += bh + _BUBBLE_GAP

    overlay.save(str(out_path), format="PNG")


def _chatfeed_cmd(
    bg_png: Path,
    overlay_specs: list[tuple[Path, float, float]],
    total_dur: float,
    music_path: str | None,
    output_path: str,
) -> list[str]:
    """Same looped-bg + per-stage cross-fade pattern as format8/9."""
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
