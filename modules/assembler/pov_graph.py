"""
Format 8 assembler -- "POV : Realite vs Illusion" (motion graphics, no voice, no B-roll).

Full-video split-screen: two horizontal charts stacked (top = the naive/
illusion belief, a plain rising line; bottom = the real path, a smoothed
dip-then-rise curve through 4 labeled stages), both tracing left-to-right
IN SYNC for the whole video like a live oscilloscope/heartbeat-monitor
sweep -- not a sequence of full-screen phases popping in one at a time.
Ends on a short kinetic CTA card once both charts finish drawing.

Visual language tuned against reference clips (near-black background,
thin pale line graphs with a bright traveling dot, small inline labels
appearing as the line passes them, muted slate vs desaturated champagne
for the two curves). Rendered frame-by-frame with Pillow and piped to
ffmpeg as raw video (no PNG-sequence temp files) since the sweep needs a
unique frame every 1/30s, unlike the older stage-popup version this
replaces.
"""

import io
import logging
import math
import subprocess
import tempfile
import textwrap
from datetime import datetime

import numpy as np
import resvg_py
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, RESETWORTH_ASSETS_DIR
from modules.assembler import (
    _VIDEOS_DIR, _slugify, _soft_text,
    _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD,
    _TEXT_COLOR, _NAIVE_COLOR, _REAL_COLOR,
)

logger = logging.getLogger(__name__)

# ── Timeline (seconds) ──────────────────────────────────────────────────
# Headline/caption text is full-opacity from frame 0 (a hook has under 2s to stop the scroll,
# so it can't spend any of that fading up from a blank screen) -- only the chart/figure motion
# gets a brief anticipation delay before it starts, since the text is already on screen by then.
_T_START    = 0.4    # charts start drawing
_T_END      = 11.5   # both charts finish drawing
_T_HOLD_END = 12.0    # brief hold on the completed charts
_CTA_DUR    = 3.0
_TOTAL      = _T_HOLD_END + _CTA_DUR   # 15.0s
_HOOK_HOLD  = 4.0     # figure variant only: how long the hook caption + illusion labels stay
                      # fully visible before crossfading into the reality caption -- 2.2s still
                      # read as "too fast" to actually take in the hook line plus 2 floating words
_LABEL_FADE = 0.3     # stage-label fade-in as the dot passes each point

# ── Chart geometry ──────────────────────────────────────────────────────
_CHART_X0, _CHART_X1 = 60, VIDEO_WIDTH - 60

_TOP_HEADLINE_Y = 130
_TOP_BASE_Y     = 620
_TOP_LABEL_Y    = _TOP_BASE_Y + 50

_BOTTOM_HEADLINE_Y = 1000
_BOTTOM_BASE_Y     = 1560
_BOTTOM_LABEL_Y    = _BOTTOM_BASE_Y + 210

# Bottom curve control points: (x_frac, y_offset_px). y_offset > 0 = below
# baseline (worse), < 0 = above (better) -- dip through the "cost" stages
# then climb well past the top curve's own peak by the end.
_STAGE_X_FRACS   = [0.0, 0.22, 0.46, 0.72, 1.0]
_STAGE_Y_OFFSETS = [0, 70, 170, -80, -340]

_N_TOP_SAMPLES = 180
_SAMPLES_PER_SEG = 45   # bottom curve: 4 segments x 45 = 180 samples

# Several arrangements of the same "naive belief vs real path" idea. Only "curves" is in
# rotation for now -- "figure" and "3d" are built and working (see _render_figure/_render_3d
# below) but neither reached an acceptable match against their reference clip despite many
# verified rounds of side-by-side comparison and correction (proportions, pose, vector
# rendering via resvg, an independent agent's own 4-round refinement pass). Left in the code
# rather than deleted: re-enable by adding back to this tuple if a real source asset (Lottie/
# video) for the reference ever becomes available to adapt from instead of hand-tuning blind.
_VARIANTS = ("curves",)


def assemble_format8(script, visuals_result, voice_result, timestamps_result, music_path):
    """Pure motion-graphics assembler -- ignores visuals_result/voice_result/timestamps_result."""
    import random

    hook  = script.get("hook", "").strip() or "What you think it takes:"
    body  = [b.strip() for b in script.get("body", []) if b.strip()]
    cta   = (script.get("cta") or "Ready? Subscribe.").strip()

    reality_line = body[0] if body else "What it actually takes:"
    stage_labels = (body[1:5] if len(body) > 1 else ["Sacrifice", "Failure", "Pivot", "Discipline"])
    while len(stage_labels) < 4:
        stage_labels.append(["Sacrifice", "Failure", "Pivot", "Discipline"][len(stage_labels)])

    # Mirrors stage_labels but for the hook/naive side -- without these, the illusion half of
    # the screen was just the hook line and nothing else, next to a reality half carrying 4
    # labels, which read as if the illusion side had nothing on it at all.
    illusion_labels = [l.strip() for l in script.get("illusion_labels", []) if l.strip()][:2]
    while len(illusion_labels) < 2:
        illusion_labels.append(["Luck", "Overnight"][len(illusion_labels)])

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    variant = script.get("_pov_variant") or random.choice(_VARIANTS)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format8_{variant}_{slug}.mp4"

    logger.info(f"Assembling format8 (POV realite/illusion) | variant={variant} | total={_TOTAL:.1f}s")
    if variant == "figure":
        _render_figure(hook, reality_line, illusion_labels, stage_labels, cta, music_path, str(out))
    elif variant == "3d":
        _render_3d(hook, reality_line, illusion_labels, stage_labels, cta, music_path, str(out))
    else:
        _render_curves(hook, reality_line, illusion_labels, stage_labels, cta, music_path, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


# ── Curve construction ──────────────────────────────────────────────────

# Decaying oscillation (medium hump -> tallest peak -> progressively smaller swings settling
# near baseline) -- matches the reference's "Motivation" wave. A plain straight rising line
# read as static/lifeless next to it; this is what was actually wrong, not the sweep mechanic.
_ILLUSION_X_OFFSETS = [0.0, 0.12, 0.22, 0.35, 0.48, 0.62, 0.72, 0.85, 1.0]
_ILLUSION_Y_OFFSETS = [40, -180, 60, -280, 90, -140, 40, -90, -40]
_TOP_SAMPLES_PER_SEG = 22   # 8 segments x 22 = 176 samples, close to the old _N_TOP_SAMPLES


def _top_curve_points() -> list[tuple[float, float]]:
    ctrl = [
        (_CHART_X0 + f * (_CHART_X1 - _CHART_X0), _TOP_BASE_Y + off)
        for f, off in zip(_ILLUSION_X_OFFSETS, _ILLUSION_Y_OFFSETS)
    ]
    return _catmull_rom(ctrl, _TOP_SAMPLES_PER_SEG)


def _bottom_curve_points() -> tuple[list[tuple[float, float]], list[int]]:
    """Returns (smoothed sample points, sample-index of each of the 4 stage points)."""
    ctrl = [
        (_CHART_X0 + f * (_CHART_X1 - _CHART_X0), _BOTTOM_BASE_Y + off)
        for f, off in zip(_STAGE_X_FRACS, _STAGE_Y_OFFSETS)
    ]
    samples = _catmull_rom(ctrl, _SAMPLES_PER_SEG)
    # stage i (1..4 in ctrl) lands at sample index i*_SAMPLES_PER_SEG - 1 (segment boundary)
    stage_indices = [min(len(samples) - 1, i * _SAMPLES_PER_SEG - 1) for i in range(1, 5)]
    return samples, stage_indices


def _catmull_rom(points: list[tuple[float, float]], samples_per_seg: int) -> list[tuple[float, float]]:
    pts = [points[0]] + points + [points[-1]]
    out: list[tuple[float, float]] = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for j in range(samples_per_seg):
            t = j / samples_per_seg
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(pts[-2])
    return out


# ── Static assets (built once, reused every frame) ──────────────────────

def _build_bg() -> Image.Image:
    """Near-black vertical gradient -- matches the reference's flat, almost-pure-black look
    (measured ~(8,12,16) at the very top fading to (0,0,0)), a world apart from a lit
    charcoal-gray studio backdrop."""
    top    = np.array([10, 12, 16], dtype=np.float32)
    bottom = np.array([0, 0, 0], dtype=np.float32)
    frac = np.linspace(0, 1, VIDEO_HEIGHT, dtype=np.float32).reshape(-1, 1, 1)
    row = top * (1 - frac) + bottom * frac
    arr = np.broadcast_to(row, (VIDEO_HEIGHT, VIDEO_WIDTH, 3)).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB").convert("RGBA")


def _make_halo(color: tuple[int, int, int, int]) -> Image.Image:
    """Small blurred glow + bright core sprite for the traveling dot -- precomputed once
    per curve color instead of blurring the full frame every tick (would be far too slow
    at 450 frames)."""
    size, glow_r, core_r = 120, 34, 8
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([size / 2 - glow_r, size / 2 - glow_r, size / 2 + glow_r, size / 2 + glow_r],
                 fill=(color[0], color[1], color[2], 100))
    img = img.filter(ImageFilter.GaussianBlur(14))
    draw2 = ImageDraw.Draw(img)
    core = (min(255, color[0] + 35), min(255, color[1] + 35), min(255, color[2] + 35), 255)
    draw2.ellipse([size / 2 - core_r, size / 2 - core_r, size / 2 + core_r, size / 2 + core_r], fill=core)
    return img


def _text_sprite(text: str, font: ImageFont.FreeTypeFont, color: tuple[int, int, int, int],
                  align: str = "left", spacing: int = 10) -> Image.Image:
    """Renders soft-shadowed text onto a tightly-cropped transparent sprite so per-frame
    fade-in only has to alpha-scale a small image, not the full canvas."""
    probe = Image.new("RGBA", (VIDEO_WIDTH, 400), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(probe)
    bbox = pdraw.multiline_textbbox((0, 0), text, font=font, align=align, spacing=spacing)
    pad = 20
    w = int(bbox[2] - bbox[0]) + pad * 2
    h = int(bbox[3] - bbox[1]) + pad * 2
    sprite = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _soft_text(sprite, (pad - bbox[0], pad - bbox[1]), text, font, color, align=align, spacing=spacing)
    return sprite


def _paste_faded(base: Image.Image, sprite: Image.Image, pos: tuple[int, int], alpha: float) -> None:
    if alpha <= 0.0:
        return
    if alpha >= 0.999:
        base.alpha_composite(sprite, pos)
        return
    r, g, b, a = sprite.split()
    a = a.point(lambda v: int(v * alpha))
    base.alpha_composite(Image.merge("RGBA", (r, g, b, a)), pos)


# ── Shared: pipe Pillow frames straight into ffmpeg as rawvideo ─────────
# Both variants need a unique frame every 1/30s (a moving dot, a rising
# figure) -- too fine-grained for the older "N discrete PNG stages +
# ffmpeg overlay enable-windows" trick, so both encode this way instead.

def _encode_frames(frame_fn, n_frames: int, music_path: str | None, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "-r", str(VIDEO_FPS), "-i", "-",
    ]
    if music_path:
        cmd += ["-i", music_path]
    cmd += ["-filter_complex", "[0:v]format=yuv420p[v]", "-map", "[v]"]
    if music_path:
        fade_out = max(0, _TOTAL - 2)
        cmd += [
            "-map", "1:a",
            "-af", f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out:.2f}:d=2",
            "-shortest",
        ]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        # moov atom at the front -- without this, ffmpeg writes it after the video data
        # (mdat), which many players/previewers (Windows Photos, some browsers, QuickTime)
        # refuse to open rather than just being slow.
        "-movflags", "+faststart",
        output_path,
    ]

    # stderr/stdout go to a temp file, not PIPE -- with PIPE and nobody draining it
    # during the write loop below, ffmpeg blocks once the OS pipe buffer fills (a few
    # dozen KB), which then blocks our stdin writes too since ffmpeg stops consuming
    # them -- a classic deadlock that only shows up past a couple hundred frames.
    with tempfile.TemporaryFile() as log_f:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log_f, stderr=log_f)
        try:
            for i in range(n_frames):
                proc.stdin.write(frame_fn(i).tobytes())
            proc.stdin.close()
            proc.wait()
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()

        if proc.returncode != 0:
            log_f.seek(0)
            log = log_f.read().decode(errors="replace")
            tail = "\n".join(log.splitlines()[-20:])
            raise RuntimeError(f"FFmpeg failed (exit {proc.returncode}):\n{tail}")


# ── Variant A: split-screen dual-curve sweep ────────────────────────────

_ILLUSION_X_FRACS = [0.32, 0.68]   # where the 2 illusion_labels sit along the top line


def _render_curves(hook: str, reality_line: str, illusion_labels: list[str], stage_labels: list[str],
                    cta: str, music_path: str | None, output_path: str) -> None:
    bg = _build_bg()

    try:
        headline_font = ImageFont.truetype(_FONT_REGULAR, size=52)
        label_font    = ImageFont.truetype(_FONT_LIGHT, size=34)
        axis_font     = ImageFont.truetype(_FONT_LIGHT, size=38)
        cta_font      = ImageFont.truetype(_FONT_SEMIBOLD, size=84)
    except OSError:
        headline_font = label_font = axis_font = cta_font = ImageFont.load_default()

    top_pts = _top_curve_points()
    bottom_pts, stage_idx = _bottom_curve_points()

    top_headline_sprite = _text_sprite(textwrap.fill(hook, width=30, break_long_words=False),
                                        headline_font, _TEXT_COLOR)
    bottom_headline_sprite = _text_sprite(textwrap.fill(reality_line, width=30, break_long_words=False),
                                           headline_font, _TEXT_COLOR)
    top_axis_sprite = _text_sprite("Illusion", axis_font, _NAIVE_COLOR)
    bottom_axis_sprite = _text_sprite("Reality", axis_font, _REAL_COLOR)
    stage_sprites = [_text_sprite(lbl, label_font, _REAL_COLOR) for lbl in stage_labels]
    illusion_sprites = [_text_sprite(lbl, label_font, _NAIVE_COLOR) for lbl in illusion_labels]
    illusion_idx = [min(len(top_pts) - 1, int(f * (len(top_pts) - 1))) for f in _ILLUSION_X_FRACS]

    cta_sprite = _text_sprite(textwrap.fill(cta, width=18, break_long_words=False),
                               cta_font, _TEXT_COLOR, align="center", spacing=14)

    naive_halo = _make_halo(_NAIVE_COLOR)
    real_halo  = _make_halo(_REAL_COLOR)
    halo_half  = naive_halo.size[0] // 2

    def frame_fn(i: int) -> Image.Image:
        t = i / VIDEO_FPS
        frame = bg.copy()

        # Full opacity from frame 0 -- a hook has under 2s to stop the scroll, so it can't
        # spend even a fraction of a second fading up from a blank screen.
        text_alpha = 1.0
        _paste_faded(frame, top_headline_sprite, (80, _TOP_HEADLINE_Y), text_alpha)
        _paste_faded(frame, bottom_headline_sprite, (80, _BOTTOM_HEADLINE_Y), text_alpha)
        _paste_faded(frame, top_axis_sprite, (_CHART_X0, _TOP_LABEL_Y), text_alpha)
        _paste_faded(frame, bottom_axis_sprite, (_CHART_X0, _BOTTOM_LABEL_Y), text_alpha)

        frac = 0.0 if t < _T_START else min(1.0, (t - _T_START) / (_T_END - _T_START))

        draw = ImageDraw.Draw(frame)
        draw.line([(_CHART_X0, _TOP_BASE_Y), (_CHART_X1, _TOP_BASE_Y)],
                  fill=(90, 90, 94, 140), width=2)
        draw.line([(_CHART_X0, _BOTTOM_BASE_Y), (_CHART_X1, _BOTTOM_BASE_Y)],
                  fill=(90, 90, 94, 140), width=2)

        if frac > 0.0:
            top_n = max(1, int(frac * (len(top_pts) - 1)) + 1)
            draw.line(top_pts[:top_n], fill=_NAIVE_COLOR, width=4, joint="curve")
            tip = top_pts[top_n - 1]
            frame.alpha_composite(naive_halo, (int(tip[0] - halo_half), int(tip[1] - halo_half)))

            for j, idx in enumerate(illusion_idx):
                trigger_t = _T_START + (idx / (len(top_pts) - 1)) * (_T_END - _T_START)
                lbl_alpha = max(0.0, min(1.0, (t - trigger_t) / _LABEL_FADE))
                if lbl_alpha > 0.0:
                    px, py = top_pts[idx]
                    sprite = illusion_sprites[j]
                    lx = int(px) - 18 - sprite.size[0] if px + 18 + sprite.size[0] > VIDEO_WIDTH - 40 else int(px) + 18
                    ly = int(py) - sprite.size[1] // 2 - 30
                    _paste_faded(frame, sprite, (lx, ly), lbl_alpha)

            bot_n = max(1, int(frac * (len(bottom_pts) - 1)) + 1)
            draw.line(bottom_pts[:bot_n], fill=_REAL_COLOR, width=5, joint="curve")
            tip_b = bottom_pts[bot_n - 1]
            frame.alpha_composite(real_halo, (int(tip_b[0] - halo_half), int(tip_b[1] - halo_half)))

            for j, idx in enumerate(stage_idx):
                trigger_t = _T_START + (idx / (len(bottom_pts) - 1)) * (_T_END - _T_START)
                lbl_alpha = max(0.0, min(1.0, (t - trigger_t) / _LABEL_FADE))
                if lbl_alpha > 0.0:
                    px, py = bottom_pts[idx]
                    sprite = stage_sprites[j]
                    lx = int(px) - 18 - sprite.size[0] if px + 18 + sprite.size[0] > VIDEO_WIDTH - 40 else int(px) + 18
                    ly = int(py) - sprite.size[1] // 2
                    _paste_faded(frame, sprite, (lx, ly), lbl_alpha)

        _paste_cta(frame, t, cta_sprite)
        return frame

    n_frames = int(round(_TOTAL * VIDEO_FPS))
    _encode_frames(frame_fn, n_frames, music_path, output_path)


def _paste_cta(frame: Image.Image, t: float, cta_sprite: Image.Image) -> None:
    """Shared CTA card: dims whatever's already drawn on `frame` and fades the kinetic
    closing line in centered, once the video enters its last _CTA_DUR seconds."""
    if t < _T_HOLD_END:
        return
    cta_t = t - _T_HOLD_END
    dim_alpha = min(0.88, cta_t / 0.4 * 0.88)
    dim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (2, 2, 3, int(dim_alpha * 255)))
    frame.alpha_composite(dim)
    cta_alpha = max(0.0, min(1.0, (cta_t - 0.2) / 0.5))
    cx = (VIDEO_WIDTH - cta_sprite.size[0]) // 2
    cy = int(VIDEO_HEIGHT * 0.42)
    _paste_faded(frame, cta_sprite, (cx, cy), cta_alpha)


# ── Variant B: two thin stick figures (struggle cycle + running cycle) ──
# Rebuilt after pixel-diffing the actual reference (Recording 020042) frame-by-frame: it is
# NOT one figure slowly kneeling-to-standing. It's a thin (not blob-capsule) stick figure that
# cycles through several distinct struggle poses in quick succession (crouch, collapse, twist,
# all-fours) for the first few seconds under "Effort always goes unnoticed.", then a SECOND
# stick figure spawns and runs a continuous run-cycle under "Until you win." while the first
# stays down -- both visible together. The background is also a lighter charcoal gradient,
# measured directly off that reference (top ~(75,75,75) -> bottom ~(30,30,30)), not the
# near-black used by the curves variant's own separate references.

_FIG2_HEAD_R = 36
_FIG2_TORSO_W = 58
_FIG2_LIMB_W = 40
_FIG2_BOX_W, _FIG2_BOX_H = 340, 300
_FIG2_COLOR = (14, 14, 16, 255)
_FIG2_RIM_COLOR = (120, 120, 128, 130)
_FIG2_FLOOR_Y = 1460
_FIG2_LEFT_X = int(VIDEO_WIDTH * 0.32)
_FIG2_RIGHT_X = int(VIDEO_WIDTH * 0.70)
_FIG2_CAPTION_Y = 1560

_STRUGGLE_SEG_DUR = 1.05
_STRUGGLE_POSES = [
    {  # A: checkmark crouch -- a tucked-in ball, not an articulated stick figure with open
       # limbs. Both arms pulled IN against/over the torso (one forms a tight check-mark bend
       # right against the head, the other tucks behind the body almost hidden), both knees
       # pulled up close together near the hip so the lower body reads as one dense rounded
       # mass instead of two visibly separate legs.
        "head": (0.58, 0.30), "neck": (0.50, 0.42), "hip": (0.46, 0.78),
        "sh_l": (0.40, 0.42), "sh_r": (0.56, 0.40),
        "elbow_l": (0.36, 0.54), "elbow_r": (0.64, 0.30),
        "hand_l": (0.42, 0.62), "hand_r": (0.56, 0.22),
        "knee_l": (0.28, 0.76), "knee_r": (0.64, 0.76),
        "foot_l": (0.26, 0.98), "foot_r": (0.66, 0.98),
    },
    {  # B: collapsed sideways
        "head": (0.14, 0.72), "neck": (0.26, 0.70), "hip": (0.56, 0.76),
        "sh_l": (0.24, 0.62), "sh_r": (0.32, 0.76),
        "elbow_l": (0.12, 0.56), "elbow_r": (0.30, 0.86),
        "hand_l": (0.04, 0.52), "hand_r": (0.20, 0.92),
        "knee_l": (0.70, 0.70), "knee_r": (0.80, 0.82),
        "foot_l": (0.88, 0.62), "foot_r": (0.95, 0.86),
    },
    {  # C: twist, extended leg
        "head": (0.52, 0.12), "neck": (0.48, 0.28), "hip": (0.42, 0.58),
        "sh_l": (0.34, 0.30), "sh_r": (0.60, 0.32),
        "elbow_l": (0.20, 0.38), "elbow_r": (0.70, 0.42),
        "hand_l": (0.10, 0.46), "hand_r": (0.80, 0.50),
        "knee_l": (0.26, 0.68), "knee_r": (0.74, 0.60),
        "foot_l": (0.10, 0.90), "foot_r": (0.96, 0.62),
    },
    {  # D: all-fours
        "head": (0.72, 0.40), "neck": (0.62, 0.46), "hip": (0.38, 0.58),
        "sh_l": (0.60, 0.52), "sh_r": (0.66, 0.42),
        "elbow_l": (0.54, 0.64), "elbow_r": (0.62, 0.56),
        "hand_l": (0.50, 0.78), "hand_r": (0.58, 0.72),
        "knee_l": (0.30, 0.74), "knee_r": (0.38, 0.70),
        "foot_l": (0.22, 0.94), "foot_r": (0.32, 0.92),
    },
]

_RUN_SEG_DUR = 0.18
# A compact, coiled sprinter's-crouch cycle -- matching the reference's "Until you win" figure:
# one dense tucked mass (arms pulled in against the torso, knees close together) with only a
# small energetic jitter between keyframes, not an articulated stick figure with limbs spread
# out to the sides.
_RUN_POSES = [
    {  # A: coiled crouch, weight forward
        "head": (0.54, 0.26), "neck": (0.50, 0.38), "hip": (0.46, 0.72),
        "sh_l": (0.42, 0.42), "sh_r": (0.58, 0.40),
        "elbow_l": (0.38, 0.52), "elbow_r": (0.62, 0.32),
        "hand_l": (0.42, 0.60), "hand_r": (0.56, 0.26),
        "knee_l": (0.30, 0.72), "knee_r": (0.62, 0.70),
        "foot_l": (0.26, 0.96), "foot_r": (0.66, 0.94),
    },
    {  # B: weight shift, slight jitter
        "head": (0.56, 0.24), "neck": (0.52, 0.36), "hip": (0.48, 0.70),
        "sh_l": (0.44, 0.40), "sh_r": (0.60, 0.42),
        "elbow_l": (0.40, 0.50), "elbow_r": (0.66, 0.36),
        "hand_l": (0.46, 0.58), "hand_r": (0.60, 0.30),
        "knee_l": (0.32, 0.70), "knee_r": (0.64, 0.74),
        "foot_l": (0.28, 0.92), "foot_r": (0.68, 0.98),
    },
    {  # C: coiled crouch, mirrored weight
        "head": (0.52, 0.26), "neck": (0.48, 0.38), "hip": (0.44, 0.72),
        "sh_l": (0.40, 0.40), "sh_r": (0.56, 0.42),
        "elbow_l": (0.36, 0.32), "elbow_r": (0.60, 0.52),
        "hand_l": (0.42, 0.26), "hand_r": (0.56, 0.60),
        "knee_l": (0.26, 0.70), "knee_r": (0.60, 0.72),
        "foot_l": (0.22, 0.94), "foot_r": (0.64, 0.96),
    },
    {  # D: weight shift, slight jitter (mirror of B)
        "head": (0.50, 0.24), "neck": (0.46, 0.36), "hip": (0.42, 0.70),
        "sh_l": (0.38, 0.42), "sh_r": (0.54, 0.40),
        "elbow_l": (0.32, 0.36), "elbow_r": (0.58, 0.50),
        "hand_l": (0.38, 0.30), "hand_r": (0.52, 0.58),
        "knee_l": (0.24, 0.74), "knee_r": (0.58, 0.70),
        "foot_l": (0.20, 0.98), "foot_r": (0.62, 0.92),
    },
]


def _build_bg_gray() -> Image.Image:
    """Lighter charcoal gradient measured directly off the figure reference clip -- a world
    apart from the near-black bg the curves variant's own separate references call for."""
    top    = np.array([75, 75, 75], dtype=np.float32)
    bottom = np.array([30, 30, 30], dtype=np.float32)
    frac = np.linspace(0, 1, VIDEO_HEIGHT, dtype=np.float32).reshape(-1, 1, 1)
    row = top * (1 - frac) + bottom * frac
    arr = np.broadcast_to(row, (VIDEO_HEIGHT, VIDEO_WIDTH, 3)).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB").convert("RGBA")


def _pose_cycle_at(t: float, poses: list[dict], seg_dur: float) -> dict[str, tuple[float, float]]:
    """Holds each pose, then snaps to the next over a short window at the end of its segment --
    linearly interpolating joint POSITIONS across two very different poses for the whole segment
    stretches limbs like rubber hoses through the middle of the transition (limb length isn't
    preserved by lerping endpoints). A quick snap avoids that and reads more like the reference's
    own flipbook-style pose changes anyway, not a smoothly tweened rig."""
    n = len(poses)
    total = n * seg_dur
    local = t % total
    idx = int(local // seg_dur)
    raw_frac = (local - idx * seg_dur) / seg_dur
    frac = max(0.0, min(1.0, (raw_frac - 0.82) / 0.18))
    p0, p1 = poses[idx], poses[(idx + 1) % n]
    return {k: (p0[k][0] + (p1[k][0] - p0[k][0]) * frac, p0[k][1] + (p1[k][1] - p0[k][1]) * frac) for k in p0}


def _stick_px(pt: tuple[float, float], cx: float, floor_y: float) -> tuple[float, float]:
    box_top = floor_y - _FIG2_BOX_H
    return (cx + (pt[0] - 0.5) * _FIG2_BOX_W, box_top + pt[1] * _FIG2_BOX_H)


def _svg_color(c: tuple[int, int, int, int]) -> tuple[str, float]:
    return (f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}", c[3] / 255.0)


def _svg_tapered_limb(p0, r0: float, p1, r1: float, color_hex: str, opacity: float) -> str:
    """A smoothly tapered capsule (thick at one joint, thinner at the other) built from two
    circles plus the connecting quad -- true vector geometry via resvg, not a Pillow ellipse+
    line approximation. This is what actually separated the reference's clean vector rig from
    the earlier hand-drawn look: uniform-width capsules read as generic 'stick figure', tapered
    limbs read as an actual illustrated character."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy) or 1.0
    px_, py_ = -dy / length, dx / length
    a = (p0[0] + px_ * r0, p0[1] + py_ * r0)
    b = (p1[0] + px_ * r1, p1[1] + py_ * r1)
    c = (p1[0] - px_ * r1, p1[1] - py_ * r1)
    d = (p0[0] - px_ * r0, p0[1] - py_ * r0)
    op = f'{opacity:.3f}'
    return (
        f'<circle cx="{p0[0]:.1f}" cy="{p0[1]:.1f}" r="{r0:.1f}" fill="{color_hex}" fill-opacity="{op}"/>'
        f'<polygon points="{a[0]:.1f},{a[1]:.1f} {b[0]:.1f},{b[1]:.1f} {c[0]:.1f},{c[1]:.1f} {d[0]:.1f},{d[1]:.1f}" fill="{color_hex}" fill-opacity="{op}"/>'
        f'<circle cx="{p1[0]:.1f}" cy="{p1[1]:.1f}" r="{r1:.1f}" fill="{color_hex}" fill-opacity="{op}"/>'
    )


def _stick_svg_shapes(joints: dict, cx: float, floor_y: float, color_hex: str, opacity: float,
                       pad: float = 0.0) -> str:
    def j(name):
        return _stick_px(joints[name], cx, floor_y)

    L = _svg_tapered_limb
    parts = [
        L(j("neck"), _FIG2_TORSO_W / 2 + pad, j("hip"), _FIG2_TORSO_W / 2 - 4 + pad, color_hex, opacity),
        L(j("sh_l"), _FIG2_LIMB_W / 2 + pad, j("elbow_l"), _FIG2_LIMB_W / 2 - 3 + pad, color_hex, opacity),
        L(j("elbow_l"), _FIG2_LIMB_W / 2 - 3 + pad, j("hand_l"), _FIG2_LIMB_W / 2 - 6 + pad, color_hex, opacity),
        L(j("sh_r"), _FIG2_LIMB_W / 2 + pad, j("elbow_r"), _FIG2_LIMB_W / 2 - 3 + pad, color_hex, opacity),
        L(j("elbow_r"), _FIG2_LIMB_W / 2 - 3 + pad, j("hand_r"), _FIG2_LIMB_W / 2 - 6 + pad, color_hex, opacity),
        L(j("hip"), _FIG2_LIMB_W / 2 + 4 + pad, j("knee_l"), _FIG2_LIMB_W / 2 + pad, color_hex, opacity),
        L(j("knee_l"), _FIG2_LIMB_W / 2 + pad, j("foot_l"), _FIG2_LIMB_W / 2 - 4 + pad, color_hex, opacity),
        L(j("hip"), _FIG2_LIMB_W / 2 + 4 + pad, j("knee_r"), _FIG2_LIMB_W / 2 + pad, color_hex, opacity),
        L(j("knee_r"), _FIG2_LIMB_W / 2 + pad, j("foot_r"), _FIG2_LIMB_W / 2 - 4 + pad, color_hex, opacity),
    ]
    head = j("head")
    r = _FIG2_HEAD_R + pad
    parts.append(f'<circle cx="{head[0]:.1f}" cy="{head[1]:.1f}" r="{r:.1f}" fill="{color_hex}" fill-opacity="{opacity:.3f}"/>')
    return "".join(parts)


def _draw_stick_soft(frame: Image.Image, joints: dict, cx: float, floor_y: float,
                      swoosh: bool = False) -> None:
    """Soft blurred glow UNDER a sharp vector silhouette. Rendered as one SVG doc (glow shapes
    under an SVG feGaussianBlur filter, sharp shapes on top) and rasterized via resvg -- a real
    vector renderer, not Pillow's raster line/ellipse primitives upscaled-and-downsampled.
    swoosh=True adds a thin light curved motion-trail stroke near the forward hand, matching
    the reference's "Until you win" figure -- without it the running figure read as static/
    posed rather than mid-motion."""
    pad = 60
    x0 = int(cx - _FIG2_BOX_W - pad)
    x1 = int(cx + _FIG2_BOX_W + pad)
    y0 = int(floor_y - _FIG2_BOX_H - _FIG2_HEAD_R - pad)
    y1 = int(floor_y + pad)
    w, h = x1 - x0, y1 - y0

    # _stick_px maps joint fractions via cx/floor_y in FRAME coordinates -- shift so shapes land
    # correctly on a tile whose origin is (x0, y0) instead of the frame's own (0, 0).
    tile_cx, tile_floor_y = cx - x0, floor_y - y0

    glow_hex, glow_op = _svg_color(_FIG2_RIM_COLOR)
    sharp_hex, sharp_op = _svg_color(_FIG2_COLOR)
    glow_shapes = _stick_svg_shapes(joints, tile_cx, tile_floor_y, glow_hex, glow_op, pad=10)
    sharp_shapes = _stick_svg_shapes(joints, tile_cx, tile_floor_y, sharp_hex, sharp_op)

    swoosh_svg = ""
    if swoosh:
        hx, hy = _stick_px(joints["hand_r"], tile_cx, tile_floor_y)
        ex, ey = _stick_px(joints["elbow_r"], tile_cx, tile_floor_y)
        # A short curved stroke trailing behind the forward hand/arm.
        c1x, c1y = hx - 55, hy - 10
        c2x, c2y = hx - 90, hy + 25
        endx, endy = hx - 120, hy + 15
        swoosh_svg = (
            f'<path d="M {ex:.1f} {ey:.1f} Q {c1x:.1f} {c1y:.1f} {c2x:.1f} {c2y:.1f} T {endx:.1f} {endy:.1f}" '
            f'stroke="#cfcfd2" stroke-width="4" fill="none" stroke-linecap="round" opacity="0.85"/>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">'
        f'<defs><filter id="g" x="-50%" y="-50%" width="200%" height="200%">'
        f'<feGaussianBlur stdDeviation="7"/></filter></defs>'
        f'<g filter="url(#g)">{glow_shapes}</g>'
        f'{sharp_shapes}'
        f'{swoosh_svg}'
        f'</svg>'
    )
    png_bytes = bytes(resvg_py.svg_to_bytes(svg_string=svg))
    layer = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    frame.alpha_composite(layer, (x0, y0))


def _render_figure(hook: str, reality_line: str, illusion_labels: list[str], stage_labels: list[str],
                    cta: str, music_path: str | None, output_path: str) -> None:
    bg = _build_bg_gray()

    try:
        caption_font = ImageFont.truetype(_FONT_REGULAR, size=46)
        label_font   = ImageFont.truetype(_FONT_LIGHT, size=32)
        cta_font     = ImageFont.truetype(_FONT_SEMIBOLD, size=84)
    except OSError:
        caption_font = label_font = cta_font = ImageFont.load_default()

    hook_sprite = _text_sprite(textwrap.fill(hook, width=30, break_long_words=False),
                                caption_font, _TEXT_COLOR, align="center", spacing=10)
    reality_sprite = _text_sprite(textwrap.fill(reality_line, width=30, break_long_words=False),
                                   caption_font, _TEXT_COLOR, align="center", spacing=10)
    illusion_sprites = [_text_sprite(lbl, label_font, _NAIVE_COLOR) for lbl in illusion_labels]
    stage_sprites = [_text_sprite(lbl, label_font, _REAL_COLOR) for lbl in stage_labels]

    illusion_caption_l = _text_sprite("Effort goes unnoticed.", label_font, (170, 170, 175, 255))
    reality_caption_r = _text_sprite("Until you win.", label_font, _REAL_COLOR)

    cta_sprite = _text_sprite(textwrap.fill(cta, width=18, break_long_words=False),
                               cta_font, _TEXT_COLOR, align="center", spacing=14)

    _RUN_SPAWN = 4.5    # figure 1 has run one full struggle cycle by here; figure 2 spawns

    floor_glow_l = _make_floor_glow(_FIG2_LEFT_X, _FIG2_FLOOR_Y)
    floor_glow_r = _make_floor_glow(_FIG2_RIGHT_X, _FIG2_FLOOR_Y)

    illusion_triggers = [0.5, 1.8]
    stage_triggers = [_RUN_SPAWN + 0.6 + k * ((_T_HOLD_END - _RUN_SPAWN - 0.6) / max(1, len(stage_sprites)))
                      for k in range(len(stage_sprites))]
    stage_ys = [1180, 1080, 1180, 1080]

    def frame_fn(i: int) -> Image.Image:
        t = i / VIDEO_FPS
        frame = bg.copy()
        draw = ImageDraw.Draw(frame)

        # Fast, non-overlapping crossfade -- hook_alpha used to still be ~0.4 while reality's
        # text was already ~0.75, so both headlines were visibly double-exposed on screen at
        # once for a good half second.
        hook_alpha = 1.0 if t < _RUN_SPAWN else max(0.0, 1.0 - (t - _RUN_SPAWN) / 0.22)
        reality_text_alpha = max(0.0, min(1.0, (t - (_RUN_SPAWN + 0.22)) / 0.3))
        _paste_faded(frame, hook_sprite, ((VIDEO_WIDTH - hook_sprite.size[0]) // 2, 120), hook_alpha)
        for k, trig in enumerate(illusion_triggers):
            alpha = max(0.0, min(1.0, (t - trig) / _LABEL_FADE)) * hook_alpha
            if alpha > 0.0:
                sprite = illusion_sprites[k]
                lx = _FIG2_LEFT_X - 220 - sprite.size[0] if k == 0 else _FIG2_LEFT_X + 220
                _paste_faded(frame, sprite, (lx, 1000), alpha)

        # Figure 1: cycles through the struggle poses continuously up to _RUN_SPAWN, then
        # freezes in whatever pose it lands on (matching the reference: it stays down once the
        # second figure takes over, it doesn't keep looping forever).
        struggle_t = min(t, _RUN_SPAWN)
        joints1 = _pose_cycle_at(struggle_t, _STRUGGLE_POSES, _STRUGGLE_SEG_DUR)
        frame.alpha_composite(floor_glow_l)
        _draw_stick_soft(frame, joints1, _FIG2_LEFT_X, _FIG2_FLOOR_Y)

        if t >= _RUN_SPAWN:
            run_alpha = min(1.0, (t - _RUN_SPAWN) / 0.4)
            joints2 = _pose_cycle_at(t - _RUN_SPAWN, _RUN_POSES, _RUN_SEG_DUR)
            frame.alpha_composite(floor_glow_r)
            run_layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            _draw_stick_soft(run_layer, joints2, _FIG2_RIGHT_X, _FIG2_FLOOR_Y, swoosh=True)
            _paste_faded(frame, run_layer, (0, 0), run_alpha)

            _paste_faded(frame, reality_sprite, ((VIDEO_WIDTH - reality_sprite.size[0]) // 2, 120), reality_text_alpha)
            _paste_faded(frame, illusion_caption_l, (_FIG2_LEFT_X - illusion_caption_l.size[0] // 2, _FIG2_CAPTION_Y), 1.0)
            _paste_faded(frame, reality_caption_r, (_FIG2_RIGHT_X - reality_caption_r.size[0] // 2, _FIG2_CAPTION_Y), reality_text_alpha)

            for k, trig in enumerate(stage_triggers):
                alpha = max(0.0, min(1.0, (t - trig) / _LABEL_FADE))
                if alpha > 0.0:
                    sprite = stage_sprites[k]
                    lx = _FIG2_RIGHT_X - 210 - sprite.size[0] if k % 2 == 0 else _FIG2_RIGHT_X + 210
                    _paste_faded(frame, sprite, (lx, stage_ys[k]), alpha)
        else:
            _paste_faded(frame, illusion_caption_l, (_FIG2_LEFT_X - illusion_caption_l.size[0] // 2, _FIG2_CAPTION_Y), hook_alpha)

        _draw_effort_sparks(frame, t, _FIG2_LEFT_X, _FIG2_FLOOR_Y)
        if t >= _RUN_SPAWN:
            _draw_effort_sparks(frame, t - _RUN_SPAWN, _FIG2_RIGHT_X, _FIG2_FLOOR_Y)
        _paste_cta(frame, t, cta_sprite)
        return frame

    n_frames = int(round(_TOTAL * VIDEO_FPS))
    _encode_frames(frame_fn, n_frames, music_path, output_path)


def _make_floor_glow(cx: int, floor_y: int) -> Image.Image:
    glow = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([cx - 170, floor_y - 30, cx + 170, floor_y + 50], fill=(90, 90, 92, 90))
    return glow.filter(ImageFilter.GaussianBlur(28))


# Tiny sparks flicking off near a figure's hand/floor contact -- the reference has small dots
# right next to the collapsed figure, not big dots scattered across the whole sky (which is
# what the shared _draw_dust helper does for the other 2 variants).
_SPARK_OFFSETS = [(-20, -30, 0.7, 0.0), (10, -50, 0.9, 0.3), (-40, -10, 0.6, 0.6), (25, -70, 0.8, 0.5)]


def _draw_effort_sparks(frame: Image.Image, t: float, cx: int, floor_y: int) -> None:
    draw = ImageDraw.Draw(frame)
    for dx, dy, period, phase in _SPARK_OFFSETS:
        frac = ((t + phase * period) % period) / period
        x = cx + dx + int(frac * 14)
        y = floor_y + dy + int(frac * 40)
        a = int(130 * (1.0 - frac))
        draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(200, 200, 205, a))


# ── Variant C: real Blender-rendered 3D assets (figure + rolling ball) ──
# Uses the pre-rendered transparent-bg PNG sequences under assets/3d_library/ -- a real
# studio-lit floor + glossy geometry. Rebuilt after the first pass (bars/spikes growing then
# freezing) turned out to match NONE of the reference clips: pixel-diffing the actual reference
# (Recording 020042) frame-by-frame showed continuous real motion throughout -- a figure whose
# pose keeps changing for its full 3s, and a ball that genuinely rolls across the whole screen
# (confirmed via alpha-diff: near-zero for the first/last third of its 60-frame sequence,
# a sustained motion spike through the middle third where it's actually crossing on-screen).
# figure = the naive/illusion side (its pose barely changes at first, like the reference's
# collapsed "effort goes unnoticed" figure). ball = the reality side, LOOPED continuously for
# the whole reality window so it's never a single one-shot animation that ends in a freeze.
_3D_HOOK_DUR   = 3.6    # figure/ is naturally continuous for its own 3.0s; this just gives a
                        # beat of read time after it settles into its final pose
_3D_CROSS      = 0.4
_3D_BALL_LOOP  = 60 / VIDEO_FPS    # ball/ sequence length -- 2.0s per roll cycle
_3D_REALITY_DUR = _3D_BALL_LOOP * 4   # 4 full rolls -- 8.0s
_3D_CTA_DUR    = 3.0

_3D_HOOK_END = _3D_HOOK_DUR
_3D_REALITY_START = _3D_HOOK_END + _3D_CROSS
_3D_REALITY_END = _3D_REALITY_START + _3D_REALITY_DUR
_3D_CTA_START = _3D_REALITY_END + _3D_CROSS
_3D_TOTAL = _3D_CTA_START + _3D_CTA_DUR   # 15.0s

_asset_seq_cache: dict[str, list[Image.Image]] = {}


def _load_seq(name: str) -> list[Image.Image]:
    if name not in _asset_seq_cache:
        d = RESETWORTH_ASSETS_DIR / "3d_library" / name
        frames = sorted(d.glob("frame_*.png"))
        _asset_seq_cache[name] = [Image.open(f).convert("RGBA") for f in frames]
    return _asset_seq_cache[name]


# Continuous drifting dust, present for the ENTIRE video regardless of phase -- without this,
# each phase grows once then sits frozen for its hold, which is the specific thing that read as
# "nothing like your examples": every reference clip has constant subtle motion (falling
# particles, a pulsing glow) even during the stretches where the main subject barely changes.
_DUST_PARTICLES = [
    (140, 2.2, 0.0), (320, 2.8, 0.4), (500, 2.1, 0.8), (680, 3.1, 0.2),
    (860, 2.5, 0.6), (220, 3.4, 0.9), (760, 2.0, 0.3), (440, 2.7, 0.7),
]


def _draw_dust(frame: Image.Image, t: float) -> None:
    draw = ImageDraw.Draw(frame)
    for x, period, phase in _DUST_PARTICLES:
        frac = ((t + phase * period) % period) / period
        y = int(180 + frac * 520)
        a = int(140 * (1.0 - abs(frac - 0.5) * 2) + 20)   # fades in/out at loop boundaries
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(215, 215, 220, a))


def _render_3d(hook: str, reality_line: str, illusion_labels: list[str], stage_labels: list[str],
               cta: str, music_path: str | None, output_path: str) -> None:
    bg = _build_bg()
    spikes_seq = _load_seq("spikes")
    figure_seq = _load_seq("figure")
    ball_seq = _load_seq("ball")

    try:
        headline_font = ImageFont.truetype(_FONT_REGULAR, size=52)
        label_font    = ImageFont.truetype(_FONT_LIGHT, size=34)
        cta_font      = ImageFont.truetype(_FONT_SEMIBOLD, size=84)
    except OSError:
        headline_font = label_font = cta_font = ImageFont.load_default()

    hook_sprite = _text_sprite(textwrap.fill(hook, width=30, break_long_words=False),
                                headline_font, _TEXT_COLOR, align="center")
    reality_sprite = _text_sprite(textwrap.fill(reality_line, width=30, break_long_words=False),
                                   headline_font, _TEXT_COLOR, align="center")
    illusion_sprites = [_text_sprite(lbl, label_font, _NAIVE_COLOR) for lbl in illusion_labels]
    stage_sprites = [_text_sprite(lbl, label_font, _REAL_COLOR) for lbl in stage_labels]
    cta_sprite = _text_sprite(textwrap.fill(cta, width=18, break_long_words=False),
                               cta_font, _TEXT_COLOR, align="center", spacing=14)

    illusion_label_triggers = [0.5, 1.6]
    # One label per ball roll cycle -- appears as the ball is mid-crossing, not at a fixed x
    # (the ball itself only occupies roughly the middle third of each 2s loop on-screen).
    stage_label_triggers = [_3D_REALITY_START + (k + 0.5) * _3D_BALL_LOOP for k in range(len(stage_sprites))]
    stage_ys = [560, 460, 560, 460]

    def frame_fn(i: int) -> Image.Image:
        t = i / VIDEO_FPS
        frame = bg.copy()

        if t < _3D_HOOK_END:
            idx = min(len(figure_seq) - 1, int(t * VIDEO_FPS))
            frame.alpha_composite(figure_seq[idx])
            _paste_faded(frame, hook_sprite, ((VIDEO_WIDTH - hook_sprite.size[0]) // 2, 130), 1.0)
            for k, trig in enumerate(illusion_label_triggers):
                alpha = max(0.0, min(1.0, (t - trig) / _LABEL_FADE))
                if alpha > 0.0:
                    sprite = illusion_sprites[k]
                    lx = VIDEO_WIDTH // 2 - 300 - sprite.size[0] if k == 0 else VIDEO_WIDTH // 2 + 300
                    _paste_faded(frame, sprite, (lx, 520), alpha)

        elif t < _3D_REALITY_START:
            cross_a = max(0.0, min(1.0, (t - _3D_HOOK_END) / _3D_CROSS))
            layer_a = bg.copy()
            layer_a.alpha_composite(figure_seq[-1])
            layer_b = bg.copy()
            layer_b.alpha_composite(ball_seq[0])
            frame = Image.blend(layer_a, layer_b, cross_a)

        elif t < _3D_REALITY_END:
            phase_t = t - _3D_REALITY_START
            idx = int(phase_t * VIDEO_FPS) % len(ball_seq)
            frame.alpha_composite(ball_seq[idx])
            _paste_faded(frame, reality_sprite, ((VIDEO_WIDTH - reality_sprite.size[0]) // 2, 130), 1.0)
            for k, trig in enumerate(stage_label_triggers):
                alpha = max(0.0, min(1.0, (t - trig) / _LABEL_FADE))
                if alpha > 0.0:
                    sprite = stage_sprites[k]
                    lx = VIDEO_WIDTH // 2 - 300 - sprite.size[0] if k % 2 == 0 else VIDEO_WIDTH // 2 + 300
                    _paste_faded(frame, sprite, (lx, stage_ys[k]), alpha)

        elif t < _3D_CTA_START:
            cross_b = max(0.0, min(1.0, (t - _3D_REALITY_END) / _3D_CROSS))
            layer_a = bg.copy()
            layer_a.alpha_composite(ball_seq[(len(ball_seq) - 1)])
            layer_b = bg.copy()
            layer_b.alpha_composite(figure_seq[-1])
            frame = Image.blend(layer_a, layer_b, cross_b)

        else:
            frame.alpha_composite(figure_seq[-1])
            fig_t = t - _3D_CTA_START
            # Lighter dim than the other 2 variants' CTA -- this figure is a naturally light-gray
            # Blender render, not a dark silhouette, so an 0.85 dim crushed it to near-invisible.
            dim_alpha = min(0.5, fig_t / 0.5 * 0.5)
            dim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (2, 2, 3, int(dim_alpha * 255)))
            frame.alpha_composite(dim)
            cta_alpha = max(0.0, min(1.0, (fig_t - 0.3) / 0.5))
            cx = (VIDEO_WIDTH - cta_sprite.size[0]) // 2
            _paste_faded(frame, cta_sprite, (cx, int(VIDEO_HEIGHT * 0.2)), cta_alpha)

        _draw_dust(frame, t)
        return frame

    n_frames = int(round(_3D_TOTAL * VIDEO_FPS))
    _encode_frames(frame_fn, n_frames, music_path, output_path)
