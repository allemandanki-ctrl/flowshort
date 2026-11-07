"""
Corner watermark scanner for format18 (white card reaction) source clips.

Reposted clips (Snapchat Spotlight, TikTok reposts, ...) routinely carry the
ORIGINAL creator's own watermark/handle baked directly into the footage, on
top of whatever outer template the platform itself adds -- see
modules/assembler/white_card_reaction.py's docstring. A purely numeric CV
detector (frame-to-frame variance, edge-density) was tried against a real
example and turned out unreliable: on an action-heavy clip (arms, bottles,
glasses moving through frame) the genuine subject motion swamps the small
watermark's signal in exactly the corners where a watermark would sit, so
thresholds that catch the watermark also fire on ordinary motion, and
thresholds tuned to avoid that miss the watermark. There's no honest fully-
automatic threshold for this -- it needs an actual look.

So instead of a shaky heuristic, this builds one **contact sheet per corner**
(evenly spaced timestamped tiles, cropped to just that corner) so a single
glance -- by a human or by Claude's own vision reading the sheet -- tells you
exactly which corner(s) carry a watermark and the timestamps it spans. That
reading then becomes the `box`/`start`/`end` entries in white_card_reaction's
`redactions` config. This replaces manually scrubbing to random timestamps
and eyeballing single frames (slow, easy to miss the exact on/off point) with
one fast, repeatable step for any new source clip.
"""

import logging
import subprocess
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import BASE_DIR

logger = logging.getLogger(__name__)

# Scratch files fed to a Claude vision call (@path embeds) live INSIDE the
# project tree, never the OS temp folder -- confirmed live (2026-07-29) that
# the non-interactive `claude -p` CLI inconsistently refuses to read files
# under the system temp dir (e.g. AppData\Local\Temp on Windows): of 4
# corner contact sheets in one run, 3 failed outright with a sandbox/
# permission error and only 1 happened to succeed, with no code difference
# between them -- purely a function of where the file lived. A path under
# BASE_DIR is the one thing every other file this whole pipeline reads/
# writes already has in common, so it's the safe default here too.
_SCRATCH_DIR = BASE_DIR / "scratch" / "watermark_scan"

_FONT_PATH = r"C:\Windows\Fonts\arialbd.ttf"
_CORNER_FRAC = (0.34, 0.20)   # width, height -- fraction of the content box, generous enough
                              # to catch anything from a small avatar badge up to a full
                              # "Snapchat @handle" chip without needing to guess a size first
_STRIP_FRAC = 0.16            # height, FULL width -- catches a caption/handle centered along
                              # the top or bottom edge, which the 4 corner boxes above (each
                              # only 34% of the width) can miss entirely. Found live 2026-07-30:
                              # an "Edit_verse" watermark sat centered along the bottom edge of
                              # a Snapchat repost -- invisible to every corner scan, since it
                              # never touched the leftmost/rightmost 34% either side scans.
_TILE_SCALE = 3               # upscale each corner tile this much -- small watermarks (a
                              # ~30px avatar icon) are otherwise too tiny to read at native size
_COLS = 6


def build_corner_contact_sheets(
    video_path: str,
    box: tuple[int, int, int, int] | None = None,
    interval: float = 1.0,
    out_dir: str | None = None,
) -> dict:
    """
    Returns {"box": (x,y,w,h), "regions": {name: (x,y,w,h) in the SAME
    coordinate space white_card_reaction's `redactions` expects}, "sheets":
    {name: Path}} -- one contact-sheet PNG per region (corners tl/tr/bl/br
    PLUS full-width top_strip/bottom_strip -- see _STRIP_FRAC), each tile
    labeled with its timestamp so the on/off window reads directly off the
    sheet.
    """
    from modules.assembler import _duration_ffprobe
    from modules.assembler.white_card_reaction import detect_content_box

    if box is None:
        box = detect_content_box(video_path)
    x0, y0, cw, ch = box

    cw_px    = max(24, int(cw * _CORNER_FRAC[0]))
    ch_px    = max(24, int(ch * _CORNER_FRAC[1]))
    strip_h  = max(24, int(ch * _STRIP_FRAC))
    # Absolute (source-video) coords -- used to crop frames straight out of the
    # original file for the contact sheets below. Each region can have its OWN
    # width/height (corners are square-ish, strips are full-width/short).
    # "center" (2026-08-10): the 4 corners + 2 edge strips above leave a real
    # gap -- anything sitting in the vertical middle of the frame (not tucked
    # into a corner, not hugging the top/bottom edge) is invisible to all 6
    # of them. Confirmed live: a "Hyperblaxe" watermark sat left-of-center at
    # roughly the frame's vertical midpoint, well below top_strip's reach and
    # well above bl's, and outside every corner box too -- missed entirely,
    # rendered unblurred. This is a full-width band covering everything
    # between the two edge strips, closing that gap regardless of which side
    # (left/center/right) a mid-frame watermark sits on.
    mid_y = y0 + strip_h
    mid_h = max(24, ch - 2 * strip_h)
    abs_regions = {
        "tl":           (x0, y0, cw_px, ch_px),
        "tr":           (x0 + cw - cw_px, y0, cw_px, ch_px),
        "bl":           (x0, y0 + ch - ch_px, cw_px, ch_px),
        "br":           (x0 + cw - cw_px, y0 + ch - ch_px, cw_px, ch_px),
        "top_strip":    (x0, y0, cw, strip_h),
        "bottom_strip": (x0, y0 + ch - strip_h, cw, strip_h),
        "center":       (x0, mid_y, cw, mid_h),
    }
    # Crop-relative coords -- what white_card_reaction's `redactions` actually
    # wants, since its redaction step runs AFTER the content-box crop (origin
    # (0,0) there is this box's top-left, not the source frame's).
    regions = {name: (ax - x0, ay - y0, aw, ah) for name, (ax, ay, aw, ah) in abs_regions.items()}

    # A fresh, uniquely-named temp subdir when no out_dir is given -- NOT
    # derived from the source video's own filename. A real source clip's
    # name can itself contain misleading marketing text (a real example:
    # "...spotlight-snap-brought-to-you-by-snapchat..."), and a vision call
    # that sees that string in the file path it was handed can end up
    # reasoning from the NAME instead of the actual pixels -- confirmed live:
    # Claude hallucinated a "brought to you by" watermark in a clean corner
    # because that phrase happened to sit in the sheet's filename, while
    # missing the two watermarks that were actually visible. Neutral,
    # content-free filenames close that off entirely.
    out_dir = Path(out_dir) if out_dir else _SCRATCH_DIR / uuid.uuid4().hex[:12]
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        font = ImageFont.truetype(_FONT_PATH, size=16 * _TILE_SCALE // 3)
    except OSError:
        font = ImageFont.load_default()

    sheets = {}
    for name, region in abs_regions.items():
        out_path = _build_one_sheet(video_path, region, interval, out_dir, f"region_{name}", font)
        if out_path:
            sheets[name] = out_path

    return {"box": box, "regions": regions, "sheets": sheets}


def _build_one_sheet(
    video_path: str, region: tuple[int, int, int, int], interval: float,
    out_dir: Path, file_stem: str, font, start: float = 0.0, end: float | None = None,
) -> Path | None:
    """Shared tile-contact-sheet builder, factored out of
    build_corner_contact_sheets (2026-08-16) so a single AD-HOC region can
    get the same timestamped, upscaled treatment -- see
    build_one_region_contact_sheet below, used by white_card_reaction's
    coarse-then-zoom watermark pipeline to zoom into wherever a cheap
    full-frame first pass roughly pointed, instead of always building all 7
    fixed-region sheets up front.

    `start`/`end` (2026-08-16 3rd follow-up): optionally restricts sampling
    to a sub-window of the clip instead of always 0..duration. Added for the
    case where the user already TOLD us roughly when the watermark is on
    screen (auto_detect_redactions' explicit-timing-hint path) -- there's no
    reason to keep decoding and asking Claude to look at the other 90% of
    the clip once the window is already known, and a narrower sheet is both
    cheaper and less likely to confuse the read with unrelated footage.
    Defaults (0.0, None) reproduce the old always-full-video behavior
    exactly, so every other caller is unaffected."""
    from modules.assembler import _duration_ffprobe
    cx, cy, w, h = region
    dur = _duration_ffprobe(video_path)
    t0 = max(0.0, start)
    t1 = min(dur, end) if end is not None else dur
    span = max(0.0, t1 - t0)
    n_tiles = max(2, int(span / interval) + 1)
    timestamps = [round(t0 + i * interval, 2) for i in range(n_tiles) if t0 + i * interval <= t1]

    cmd = ["ffmpeg", "-v", "error"]
    if t0 > 0:
        cmd += ["-ss", f"{t0:.2f}"]
    cmd += ["-i", str(video_path)]
    if end is not None:
        cmd += ["-t", f"{span:.2f}"]
    cmd += ["-vf", f"crop={w}:{h}:{cx}:{cy},fps=1/{interval}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True)
    frame_size = w * h * 3
    n = len(r.stdout) // frame_size
    if n < 1:
        logger.warning(f"watermark_scan: no frames decoded for {file_stem}; skipping")
        return None
    frames = np.frombuffer(r.stdout[:n * frame_size], dtype=np.uint8).reshape(n, h, w, 3)

    tile_w, tile_h = w * _TILE_SCALE, h * _TILE_SCALE
    rows = (n + _COLS - 1) // _COLS
    sheet = Image.new("RGB", (tile_w * _COLS, tile_h * rows), (40, 40, 40))

    for i in range(n):
        tile = Image.fromarray(frames[i]).resize((tile_w, tile_h), Image.NEAREST)
        draw = ImageDraw.Draw(tile)
        t = timestamps[i] if i < len(timestamps) else i * interval
        label = f"{t:.1f}s"
        draw.rectangle([0, 0, draw.textlength(label, font=font) + 8, font.size + 6],
                       fill=(0, 0, 0))
        draw.text((4, 2), label, font=font, fill=(0, 255, 0))
        gx, gy = (i % _COLS) * tile_w, (i // _COLS) * tile_h
        sheet.paste(tile, (gx, gy))

    out_path = out_dir / f"{file_stem}.png"
    sheet.save(out_path)
    logger.info(f"watermark_scan: [{file_stem}] {n} tiles -> {out_path}")
    return out_path


def build_one_region_contact_sheet(
    video_path: str, region_box: tuple[int, int, int, int], interval: float = 1.0,
    out_dir: str | None = None, name: str = "adhoc",
    start: float = 0.0, end: float | None = None,
) -> Path | None:
    """Same tile-contact-sheet builder as build_corner_contact_sheets, for
    ONE ad-hoc region (not one of the 7 fixed named corner/strip/center
    zones) -- 2026-08-16, added for white_card_reaction's coarse-then-zoom
    watermark pipeline: a cheap first pass over a few full, un-zoomed frames
    roughly points at an area, and THIS builds the zoomed-in sheet for that
    area so the existing reliable grid-cell read (see
    _detect_one_corner_watermark) can locate it precisely -- without paying
    for all 7 fixed regions every time, most of which are empty on any given
    clip. `region_box` is in the SAME absolute source-video coordinate space
    as build_corner_contact_sheets' regions. `start`/`end` -- see
    _build_one_sheet's docstring -- optionally restrict sampling to a
    sub-window instead of the whole clip."""
    out_dir = Path(out_dir) if out_dir else _SCRATCH_DIR / uuid.uuid4().hex[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        font = ImageFont.truetype(_FONT_PATH, size=16 * _TILE_SCALE // 3)
    except OSError:
        font = ImageFont.load_default()
    return _build_one_sheet(video_path, region_box, interval, out_dir, name, font, start=start, end=end)
