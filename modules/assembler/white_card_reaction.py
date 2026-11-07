"""
Format 18 -- "White Card Reaction" (SHE:/THEM:/POV: meme repost style).

Standalone entry point -- see format18_white_card_reaction.py at the repo
root -- bypasses pipeline.run()/generate_script() entirely, same pattern as
format13 (modules/assembler/fakeout_drop.py): 100% JSON-config-driven.

Source material is a raw reaction/relatable clip (e.g. a Snapchat Spotlight
repost) that already comes baked onto a white template with the platform's
own logo and caption text. This format:
  1. detects the real footage's bounding box inside that white template
     (frame-to-frame pixel variance -- the static caption/logo pixels barely
     change between frames, the actual video content does) and crops to it,
     discarding the source's own white background/logo/caption entirely.
  2. re-composites that cropped footage onto OUR OWN white canvas with a
     thin black frame, our own black "THEM:"/"ME:"/"POV:" joke text on top,
     and a subtle ResetWorth watermark -- a finance/mindset punchline in the
     channel's own voice instead of whatever the original repost said.
  3. keeps the source clip's own audio untouched (no added music/VO -- the
     joke's punchline IS the clip, same as the reference format).

Reposted source clips very often carry the ORIGINAL creator's own
watermark/handle baked into the footage itself (not just Snapchat's outer
template) -- optional config["redactions"] heavily blurs a fixed box for a
given time window (coordinates in the CROPPED clip's own space, i.e. what
detect_content_box() returns / what you see cropping to `crop_box`) to
paper over exactly that, applied before the crop is scaled up so the
box/timing you eyeball off a same-resolution frame lines up directly:
  "redactions": [{"box": [x, y, w, h], "start": 5.5, "end": 9.0}, ...]

`build_white_card_reaction` above is the low-level renderer -- it takes
lines/redactions already decided. `auto_build_white_card_reaction` below is
the actual end-to-end entry point everything else should call (the FlowShort
panel, a pasted link handled directly): it decides those itself by having
Claude look at the clip -- generate_joke_lines writes the THEM:/ME:/POV:
caption from the actual footage (optionally steered by a one-line idea),
auto_detect_redactions reads watermark_scan's contact sheets to find and
redact any baked-in watermark. No manual label/text/box/timing input needed
(2026-07-29, explicit user ask: "je veux que ce soit toi qui le fasses tout
seul" -- pasting either the file or the link should be the whole job).
"""

import json
import logging
import random
import re
import subprocess
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from modules.assembler import _duration_ffprobe, _has_audio, _run_ffmpeg, _slugify, _VIDEOS_DIR

logger = logging.getLogger(__name__)

_FONT_BOLD_PATH = r"C:\Windows\Fonts\arialbd.ttf"

_BG_COLOR   = (255, 255, 255)
_TEXT_COLOR = (20, 20, 20, 255)
_FRAME_COLOR = (10, 10, 10, 255)
_FRAME_WIDTH = 3

_TEXT_TOP_Y     = 320   # pushed down from 130 (2026-07-29 user feedback: text sat right where
                        # TikTok/IG's own top UI chrome (profile pic, follow button, sound name)
                        # overlays the frame, making the joke hard to read there)
_SIDE_MARGIN    = 80
_TEXT_LINE_GAP  = 34    # vertical gap between successive lines/labels
_TEXT_MAX_SIZE  = 60
_TEXT_MIN_SIZE  = 34

_GAP_AFTER_TEXT = 70
_VIDEO_WIDTH_FRAC = 0.82   # clip box width, fraction of canvas width
_BOTTOM_SAFE_Y  = 1780     # leave room for platform caption/UI below the clip box

_WATERMARK_Y_FROM_BOTTOM = 60


def build_white_card_reaction(config: dict) -> Path:
    input_path = config["input_path"]
    lines = config["lines"]
    bottom_lines = config.get("bottom_lines")  # optional caption block rendered BELOW the clip box
    trim = config.get("trim") or {}
    manual_box = config.get("crop_box")
    keep_audio = config.get("keep_audio", True)
    redactions = config.get("redactions", [])
    teaser = config.get("teaser")  # optional {"start": float, "end": float}, source-clip time
    label_freeze = config.get("label_freeze")  # optional freeze+in-frame-labels beat, see _build_cmd

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f18_white_card"
    tmp_dir.mkdir(exist_ok=True)

    src_dur = _duration_ffprobe(input_path)
    t_start = float(trim.get("start", 0.0))
    t_end = float(trim.get("end") or src_dur)
    clip_dur = max(0.1, t_end - t_start)

    box = tuple(manual_box) if manual_box else detect_content_box(input_path)
    logger.info(f"format18: content box = {box} (source clip: {input_path})")
    crop_w, crop_h = box[2], box[3]

    video_w = round(VIDEO_WIDTH * _VIDEO_WIDTH_FRAC)
    video_h = round(video_w * crop_h / crop_w)

    text_png, text_bottom = _render_top_text(lines)
    video_y = text_bottom + _GAP_AFTER_TEXT
    if video_y + video_h > _BOTTOM_SAFE_Y:
        video_h = _BOTTOM_SAFE_Y - video_y
        video_w = round(video_h * crop_w / crop_h)
    video_x = (VIDEO_WIDTH - video_w) // 2

    if bottom_lines:
        # Same gap the top text keeps clear of the box (_GAP_AFTER_TEXT) --
        # note this shares _BOTTOM_SAFE_Y's reserved bottom margin with
        # platform UI chrome, so a very tall crop box leaves less room here.
        _render_bottom_text(text_png, bottom_lines, video_y + video_h + _GAP_AFTER_TEXT)

    frame_png = _render_frame_and_watermark(video_x, video_y, video_w, video_h)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(config.get("output_name", "white_card_reaction"))
    out = _VIDEOS_DIR / f"{ts}_format18_{slug}.mp4"
    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    cmd = _build_cmd(
        input_path=input_path, box=box, t_start=t_start, clip_dur=clip_dur,
        video_x=video_x, video_y=video_y, video_w=video_w, video_h=video_h,
        text_png=text_png, frame_png=frame_png, redactions=redactions,
        keep_audio=keep_audio and _has_audio(input_path),
        out_path=str(out), teaser=teaser, label_freeze=label_freeze,
    )
    logger.info(f"Assembling format18 (white card reaction) | clip={Path(input_path).name} "
                f"| box={box} | video_box={video_w}x{video_h}@({video_x},{video_y})"
                + (f" | teaser={teaser['start']:.1f}-{teaser['end']:.1f}s" if teaser else "")
                + (f" | label_freeze@{label_freeze['freeze_start']:.1f}s" if label_freeze else ""))
    _run_ffmpeg(cmd)

    _apply_uniqueness_pass(out)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


def _apply_uniqueness_pass(video_path: Path) -> None:
    """
    Thin format18-specific wrapper -- the actual transform now lives in
    modules/assembler/uniqueness.py (extracted 2026-08-13 so viral_remix.py
    can reuse the identical filter graph on an externally-sourced clip that
    keeps its own native framing instead of format18's white-card
    recomposition). Behavior here is unchanged from before the extraction:
    still re-detects the content box on THIS finished render (its position
    moves whenever the joke text's line count changes) and confines
    color/grain/zoom to it so the plain white card background never gets
    visibly tinted -- see uniqueness.apply_uniqueness_pass's docstring for
    why that split matters and the rest of the transform's rationale.

    Found live 2026-08-16, same incident as the content-box sliver bug: when
    detect_content_box can't find a solid block on THIS finished render
    (e.g. the recomposited video box itself still holds mostly-static/slow
    footage, same reason the original crop undershot), it falls back to the
    FULL FRAME -- not (0, 0, 0, 0), so the `box[2] <= 0` guard below never
    catches it. Passing that full-frame box straight into
    apply_uniqueness_pass(mirror=True) then mirrors the ENTIRE canvas,
    including our own white-card joke text sitting outside the real box --
    exactly the backwards-reading-text failure mode that function's own
    `mirror` docstring warns about, which format18's call site assumed could
    never happen here. Detected by comparing the returned box against the
    frame's real dimensions: a box that IS the full frame is `detect_content_box`
    giving up, not a genuinely full-bleed clip (format18's box always comes
    from a card with visible white margin), so treat it as "no reliable box"
    and keep mirror off rather than risk flipping the text.
    """
    box = detect_content_box(str(video_path))
    if box[2] <= 0 or box[3] <= 0:
        logger.warning("format18: uniqueness pass skipped, no content box detected")
        return
    from modules.assembler.uniqueness import apply_uniqueness_pass
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        pw, ph = (int(v) for v in probe.stdout.strip().split(","))
        gave_up = box == (0, 0, pw, ph)
    except Exception:
        gave_up = False
    if gave_up:
        logger.warning(
            "format18: uniqueness pass box-redetection fell back to the full frame -- "
            "disabling mirror so the card's own text can't get flipped"
        )
    apply_uniqueness_pass(video_path, box=box, mirror=not gave_up)


def detect_content_box(
    video_path: str, n_samples: int = 10, var_thresh: float = 15.0, solid_frac: float = 0.6,
) -> tuple[int, int, int, int]:
    """
    Finds the sub-rectangle of the frame that actually CHANGES over time
    (the real footage) as opposed to a static white background/logo/caption
    baked around it -- the trick a simple "crop the white" threshold can't
    pull off, since baked-in caption text is also non-white pixels on the
    same static canvas.

    Samples `n_samples` frames spread across the clip, takes per-pixel
    variance across them, then keeps only rows/columns where the SHARE of
    high-variance pixels exceeds `solid_frac` -- this is what separates a
    solid moving video block (every pixel in every row of the block changes)
    from sparse static text edges (compression noise gives a few pixels
    non-zero variance, but nowhere near a solid row/column).

    Falls back to the full frame if detection fails or looks degenerate
    (e.g. a source with no static template at all).
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path],
        capture_output=True, text=True,
    )
    try:
        w_str, h_str = probe.stdout.strip().split(",")
        w, h = int(w_str), int(h_str)
    except Exception:
        logger.warning(f"format18: could not probe dimensions of {video_path}; using no crop")
        return (0, 0, 0, 0)

    dur = _duration_ffprobe(video_path)
    frames = []
    for i in range(n_samples):
        t = dur * (i + 0.5) / n_samples
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path, "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True,
        )
        data = r.stdout
        if len(data) != w * h * 3:
            continue
        frames.append(np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3).astype(np.int16))

    if len(frames) < 2:
        logger.warning(f"format18: not enough sampled frames for {video_path}; using full frame")
        return (0, 0, w, h)

    var = np.stack(frames).var(axis=0).sum(axis=2)
    mask = var > var_thresh

    row_frac = mask.mean(axis=1)
    solid_rows = np.where(row_frac > solid_frac)[0]
    if solid_rows.size == 0:
        logger.warning(f"format18: no solid content block detected in {video_path}; using full frame")
        return (0, 0, w, h)

    y0, y1 = int(solid_rows.min()), int(solid_rows.max())
    col_frac = mask[y0:y1 + 1, :].mean(axis=0)
    solid_cols = np.where(col_frac > solid_frac)[0]
    if solid_cols.size == 0:
        return (0, 0, w, h)
    x0, x1 = int(solid_cols.min()), int(solid_cols.max())

    return (x0, y0, x1 - x0, y1 - y0)


def _locate_static_overlay(
    video_path: str, region_box: tuple[int, int, int, int], n_samples: int = 8,
) -> tuple[int, int, int, int] | None:
    """
    Precisely locates a STATIC watermark within `region_box` (absolute
    source-video coords) via temporal pixel variance -- the mirror image of
    detect_content_box's own trick above, but inverted: here it's the
    OVERLAY that barely changes across sampled frames while the underlying
    footage around/behind it does. 2026-08-10, explicit user point after two
    different vision-based localization attempts (continuous x/y/w/h
    fractions, then a coarse grid-cell selection) both consistently missed
    the same real "Hyperblaxe" watermark by a wide margin: "c'est statique,
    ça bouge même pas... tu fais aucun effort" -- a watermark confirmed to
    never move is exactly the case a purely numeric, pixel-exact check
    handles far better than a vision model's one-glance spatial guess at a
    small, low-contrast tile.

    Only meant to run on a region ALREADY flagged "present" by the vision
    scan (auto_detect_redactions calls this as a refinement, not a
    replacement for detecting presence in the first place) -- narrowing the
    search to that one region keeps this fast and avoids confusing a static
    watermark with an unrelated static patch of background elsewhere in the
    frame that vision never flagged.

    Returns a region-RELATIVE (x, y, w, h) box tight around the largest
    static blob found, or None if no clean blob stands out (a genuinely
    non-static watermark, hand-held/shaky footage with little real motion
    anywhere to contrast against, or a solid-color/empty region) -- callers
    should fall back to the vision-reported box in that case.
    """
    x0, y0, w, h = region_box
    # ffmpeg's crop filter silently rounds an ODD width/height down to the
    # nearest even number internally (chroma subsampling alignment, even for
    # an rgb24 rawvideo OUTPUT -- the decode/filter pipeline in between still
    # goes through a YUV-based format) -- found live 2026-08-10: a region
    # exactly 719px wide came back as 718px of actual data every single
    # time, and the strict `len(data) != w*h*3` check below then rejected
    # EVERY sampled frame as "wrong size", so this always returned None and
    # silently fell back to the less reliable grid-cell read. Round down to
    # even BEFORE building the crop filter so the requested and actual
    # output size always agree.
    w -= w % 2
    h -= h % 2
    if w < 8 or h < 8:
        return None
    dur = _duration_ffprobe(video_path)

    frames = []
    for i in range(n_samples):
        t = dur * (i + 0.5) / n_samples
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video_path,
             "-vf", f"crop={w}:{h}:{x0}:{y0}", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True,
        )
        data = r.stdout
        if len(data) != w * h * 3:
            continue
        frames.append(np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3).astype(np.float32))

    if len(frames) < 4:
        return None

    std = np.stack(frames).std(axis=0).mean(axis=2)   # per-pixel std-dev across samples, averaged over RGB
    overall = float(std.mean())
    if overall < 2.0:
        # Almost nothing in this region changes across samples at all -- no
        # usable contrast between "moving background" and "static overlay",
        # could just as easily be a mostly-static SCENE.
        return None

    static_mask = std < (overall * 0.35)
    if static_mask.sum() < 20:
        return None

    # Row-band projection, tried FIRST (2026-08-16, real user case: the
    # grid-cell fallback this whole function exists to avoid was ITSELF
    # blurring an oversized area -- 2 whole grid cells, roughly 2/3 of the
    # video's width and half its height -- because the blob approach further
    # below fragmented a long text line ("@the.porsche_hub") into several
    # small disconnected pieces (per-letter contrast against moving grass
    # behind it varies too much to stay one connected blob after dilation),
    # the plausibility floor further down correctly rejected the best
    # fragment as implausibly small, and the caller fell all the way back to
    # the coarse cell box -- correct location, comically oversized). Text has
    # a signature the blob approach doesn't exploit: nearly every ROW it
    # occupies has a LARGE share of its columns flagged static (many glyphs
    # spread across the row), while an unrelated static object (a rock, a
    # shadow) is comparatively narrow relative to the row's full width and
    # never clears this density bar. Find the contiguous row band with high
    # static density, keep ONLY that band's height (a text line is never as
    # tall as half a region), then take the full column span of static
    # pixels within just that band -- recovers the complete word even split
    # across several disconnected letter-groups, since this never depends on
    # blob connectivity the way the fallback below does.
    row_density = static_mask.mean(axis=1)
    text_rows = np.where(row_density >= max(0.12, float(row_density.mean()) * 2.5))[0]
    best_bbox = None
    if len(text_rows) > 0:
        gaps = np.where(np.diff(text_rows) > 2)[0]
        band = max(np.split(text_rows, gaps + 1), key=len)
        ty0, ty1 = int(band[0]), int(band[-1])
        cols = np.where(static_mask[ty0:ty1 + 1].any(axis=0))[0]
        if len(cols) >= 10:
            best_bbox = (int(cols.min()), ty0, int(cols.max()), ty1)

    # Blob detection, as a fallback for non-text watermarks (a square
    # badge/logo won't clear the row-density bar above -- it's compact, not
    # spread across its rows the way a word is).
    if best_bbox is None:
        from scipy import ndimage

        # Horizontal dilation BEFORE labeling -- individual letters in a word
        # are separate low-variance blobs with small gaps between them (spacing,
        # anti-aliasing), so labeling the raw mask fragments one watermark word
        # into several disconnected pieces; picking any single one (2026-08-10,
        # confirmed live: picked just "e" or similar, box came back far too
        # NARROW) misses the rest of the word. Dilating wider than tall merges
        # same-line letters into one connected blob while still keeping a
        # vertically-separate or spatially-distant element (e.g. an icon
        # graphic) its own blob, as long as the gap between them is bigger than
        # the dilation kernel -- structure size is proportional to the region's
        # own scale, not a fixed pixel count, so it scales sensibly whether the
        # sampled region is small or large.
        dilate_w = max(15, round(w * 0.07))
        structure = np.ones((3, dilate_w), dtype=bool)
        dilated_mask = ndimage.binary_dilation(static_mask, structure=structure)
        labeled, n_blobs = ndimage.label(dilated_mask)
        if n_blobs == 0:
            return None

        # Prefer a TEXT-shaped blob (wide, flat -- a watermark word/line) over
        # whichever blob happens to be biggest overall (2026-08-10, real user
        # complaint: an icon graphic and a text label sitting close together got
        # merged/conflated, producing a blur box far bigger than the text alone
        # needed -- "à peine plus grand que la taille du texte"). Only a
        # meaningfully sized blob is a real candidate (min_px filters out
        # compression-noise specks); among those, score by width/height ratio
        # and take the widest-relative-to-its-height one. Falls back to the
        # single biggest blob when nothing looks text-shaped (e.g. a square
        # badge/logo watermark, where "biggest" is the right call as before).
        # Final bbox comes from the ORIGINAL (non-dilated) mask's pixels within
        # that blob's label, not the dilated mask itself -- the dilation is only
        # a grouping aid, using its own (artificially widened) extent for the
        # box would over-pad on both sides.
        sizes = ndimage.sum(dilated_mask, labeled, range(1, n_blobs + 1))
        min_px = max(20, dilated_mask.sum() * 0.03)
        candidates = [i + 1 for i, s in enumerate(sizes) if s >= min_px]
        if not candidates:
            candidates = [int(np.argmax(sizes)) + 1]

        best_ratio = -1.0
        for label_id in candidates:
            ys, xs = np.where(static_mask & (labeled == label_id))
            if len(xs) < 20:
                continue
            bbw = xs.max() - xs.min() + 1
            bbh = ys.max() - ys.min() + 1
            ratio = bbw / max(1, bbh)   # >1 = wider than tall, text-line-like
            if ratio > best_ratio:
                best_ratio = ratio
                best_bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))

        if best_bbox is None:
            return None

        # Plausibility floor (2026-08-15, real user case: "@the.porsche_hub", a
        # long handle baked over busy grass footage, never formed one clean
        # static blob -- per-letter contrast against the moving grass behind it
        # fragmented the mask, and the "widest available" fragment that won the
        # loop above was a small near-square 27x26 sliver sitting on an unrelated
        # patch of background. It got blurred with full confidence while the
        # actual handle stayed fully readable. A real text line or logo badge is
        # either clearly wider than tall, or covers a real chunk of the searched
        # area -- a tiny, roughly-square scrap that's neither is far more likely
        # to be a stray static fragment (a rock, a shadow, compression noise)
        # than the watermark itself. Bail out to None here so the caller falls
        # back to its own (generous but correctly located) vision-reported cell
        # box instead of confidently blurring the wrong spot.
        bw0 = best_bbox[2] - best_bbox[0] + 1
        if best_ratio < 1.6 and bw0 < 0.35 * w:
            return None

    # Generous safety margin, not a tight fit (2026-08-10, explicit user
    # call after several tuning attempts never landed pixel-perfect on
    # every clip: "c'est pas possible de faire un truc qui marche pour
    # tout le monde" -- font/spacing/contrast varies too much per watermark
    # to calibrate one universal value. Scales with the blob's OWN size
    # (not a fixed pixel count) so a small badge and a wide text line both
    # get a proportionate cushion -- erring toward blurring a bit more
    # background is far cheaper than leaving a sliver of readable text.
    bw, bh = best_bbox[2] - best_bbox[0], best_bbox[3] - best_bbox[1]
    pad_x = max(10, round(bw * 0.25))
    pad_y = max(10, round(bh * 0.35))
    bx0 = max(0, best_bbox[0] - pad_x)
    by0 = max(0, best_bbox[1] - pad_y)
    bx1 = min(w, best_bbox[2] + pad_x)
    by1 = min(h, best_bbox[3] + pad_y)
    return (bx0, by0, bx1 - bx0, by1 - by0)


_CONTENT_BOX_SYSTEM_PROMPT = (
    "You inspect ONE frame from a video, with a RED rectangle overlay marking a candidate crop "
    "box that was picked automatically to isolate the real, moving video footage from a static "
    "template around it (a white card background, a platform logo, or -- the specific failure "
    "mode this checks for -- a baked-in caption/username CARD from an earlier repost of a "
    "repost, e.g. a Snapchat Spotlight clip that already carries its own caption card). Judge "
    "PURELY from the pixels, checking for TWO opposite failure modes:\n\n"
    "1. TOO BIG -- the red box still contains a full static CAPTION-CARD BAND: its own "
    "solid/blurred background block, usually spanning the box's full width at the top or bottom "
    "edge, clearly a separate template layer stacked on top of the real footage.\n\n"
    "2. TOO SMALL -- the numeric detector that drew this box only found a thin sliver of high-"
    "motion pixels (e.g. a mouth, a hand, a moving prop) instead of the WHOLE real-footage "
    "rectangle around it. This happens on slow/mostly-static footage (animation held on a nearly "
    "still frame, a talking-head clip, etc.) where much of the real video content barely changes "
    "between sampled frames. If you can see the red box sits INSIDE a visibly larger rectangle of "
    "real video content (same scene, same footage, continuing above/below/beside the box, not a "
    "white background and not a separate caption card), that whole surrounding rectangle is the "
    "real crop box, not just the sliver.\n\n"
    "Be conservative about #1 -- if the box already looks clean, say so; only correct it when a "
    "full card band like that is CLEARLY still inside the red box. Be more willing to correct #2: "
    "a real video's own frame is very rarely a thin strip, so a box far shorter or narrower than "
    "the obvious surrounding footage rectangle is usually a bug worth fixing.\n\n"
    "Do NOT flag a small username/handle watermark that sits ON TOP OF the moving footage "
    "itself (e.g. a corner tag like '@someuser' with no card background of its own, sitting "
    "directly over the video image) -- that is real footage with a watermark on it, not a "
    "separate static band, and a different step blurs it; cropping it out here would cut off "
    "real video content instead of just removing a template layer. When in doubt between the "
    "two, say the box is already clean."
)


def _verify_and_refine_content_box(
    video_path: str, box: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """
    Vision-side sanity check on detect_content_box's purely-numeric guess --
    the same class of fix, for the same reason, as watermark_scan.py's move
    away from a numeric-only detector (see that module's docstring). Checks
    for two opposite failure modes:

    TOO BIG -- found live 2026-07-30: a Snapchat Spotlight source clip turned
    out to be a REPOST OF A REPOST -- it already carried its own baked-in
    caption on its own white card. detect_content_box's frame-variance
    heuristic swallowed that caption band into the crop box, because its
    bold text under heavy re-compression flickered enough between frames to
    cross the "real motion" variance threshold -- so the box included the
    caption alongside the actual footage, and it ended up stacked underneath
    our own THEM:/ME: text in the final render.

    TOO SMALL -- found live 2026-08-16: a slow/mostly-static animated clip
    (long held frames, only a small detail like a mouth or hand actually
    moving) made detect_content_box's row/column variance thresholding pass
    for only a thin sliver of the real footage rectangle -- most rows of the
    real video content didn't change enough across the sampled frames to
    clear the "solid" threshold. This check used to only ever ask "is there
    still a caption band in here" (which can only ever shrink the box), so
    it rubber-stamped the sliver as "clean" and the final card rendered the
    whole real footage squeezed into a ~60px-tall strip -- a broken video
    already live-published to 5 platforms before this was caught (see git
    history around 2026-08-16 for the incident). The prompt now also asks
    Claude to expand the box when it's a sliver sitting inside a visibly
    larger rectangle of the same real footage.

    This looks at one real frame with the candidate box drawn on it and asks
    Claude to confirm or correct it. Fails soft in every direction: any
    decode/parse/vision-call problem, or a box Claude says already looks
    clean, returns the ORIGINAL box completely unchanged -- a missed
    correction just means the crop stays exactly as it always has, never
    worse than before this check existed.
    """
    x0, y0, cw, ch = box
    if cw <= 0 or ch <= 0:
        return box

    dur = _duration_ffprobe(video_path)
    from config import BASE_DIR
    tmp_dir = BASE_DIR / "scratch" / "white_card_reaction_boxcheck"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    frame_path = tmp_dir / "frame.jpg"
    cmd = ["ffmpeg", "-y", "-ss", f"{dur / 2:.2f}", "-i", str(video_path),
           "-frames:v", "1", str(frame_path)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not frame_path.exists():
        return box

    try:
        img = Image.open(frame_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.rectangle([x0, y0, x0 + cw, y0 + ch], outline=(255, 0, 0), width=max(2, img.width // 200))
        marked_path = tmp_dir / "frame_marked.jpg"
        img.save(marked_path, quality=90)
    except Exception as e:
        logger.warning(f"format18: content-box check couldn't render overlay ({e}), keeping original box")
        return box

    prompt = (
        f"@{marked_path}\n\n"
        f"The red box is at x={x0}, y={y0}, w={cw}, h={ch} in this {img.width}x{img.height} frame.\n\n"
        "Check both directions. TOO BIG: does the red box still include a static caption-card "
        "band (its own background block, not a small watermark sitting on the footage) at one of "
        "its edges? TOO SMALL: does the red box sit as a thin sliver INSIDE a visibly larger "
        "rectangle of the same real video content (not white background, not a separate caption "
        "card) -- i.e. does the real footage clearly continue above, below, or beside the box? "
        "If it's clean either way -- including if the only thing at an edge is a small corner "
        "username/handle watermark overlaid ON the footage -- say so. Only if one of those two "
        "problems is clearly present, give a corrected box (same coordinate space as the frame) "
        "that either excludes the caption-card band or expands to the full real-footage "
        "rectangle, as appropriate.\n\n"
        'Return JSON only: {"ok": true} OR {"ok": false, "x": <int>, "y": <int>, "w": <int>, "h": <int>}'
    )
    try:
        from config import CLAUDE_MODEL
        from modules.claude_cli import call_claude
        raw, _, _ = call_claude(_CONTENT_BOX_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            logger.warning(f"format18: content-box check got no JSON: {raw[:200]!r}")
            return box
        info = json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"format18: content-box verification failed ({e}), keeping original box")
        return box

    if info.get("ok", True):
        logger.info("format18: content box verified clean, no correction needed")
        return box

    try:
        nx, ny, nw, nh = int(info["x"]), int(info["y"]), int(info["w"]), int(info["h"])
    except Exception:
        logger.warning(f"format18: content-box correction malformed ({info!r}), keeping original box")
        return box
    if nw <= 0 or nh <= 0 or nx < 0 or ny < 0:
        logger.warning(f"format18: content-box correction out of range ({info!r}), keeping original box")
        return box

    logger.info(f"format18: content box corrected by vision check {box} -> {(nx, ny, nw, nh)}")
    return (nx, ny, nw, nh)


def _wrap_preserving_breaks(text: str, width: int) -> str:
    """textwrap.fill wraps the whole block as one paragraph and (via its
    default replace_whitespace=True) treats any embedded newline as just
    another whitespace character to reflow past -- wrong for a forced break
    a user explicitly asked for (see _extract_verbatim_caption's "\\n"
    marker). Wraps each paragraph split on real newlines independently and
    rejoins with real newlines, so an explicit break survives while text
    within a paragraph still auto-wraps normally. Shared with
    viral_remix.py's own caption renderer."""
    return "\n".join(
        textwrap.fill(p, width=width, break_long_words=False) if p else ""
        for p in text.split("\n")
    )


def _draw_text_lines(draw: ImageDraw.ImageDraw, lines: list[dict], start_y: int) -> int:
    """
    Shared rendering core for both the top card and the below-clip block
    (_render_top_text / _render_bottom_text, 2026-08-13 split): draws
    "LABEL: text" (or bare text when label is empty -- see
    generate_joke_lines' verbatim path) left-aligned in black, stacked top
    to bottom starting at `start_y`. A line with an empty/"..." text (the
    reference format's silent "ME: ..." beat, where the clip itself carries
    the punchline) still reserves its own row.

    Returns the y just past the last line, same convention _render_top_text
    always returned on its own.
    """
    safe_width = VIDEO_WIDTH - 2 * _SIDE_MARGIN

    y = start_y
    for entry in lines:
        label = entry.get("label", "").strip().upper()
        text = entry.get("text", "").strip()
        if not label:
            rendered = text
        elif label == "POV" or not text or text == "...":
            rendered = f"{label}: {text}".rstrip()
        else:
            rendered = f'{label}: "{text}"'

        size = _TEXT_MAX_SIZE
        while size >= _TEXT_MIN_SIZE:
            font = ImageFont.truetype(_FONT_BOLD_PATH, size=size)
            char_w = max(1, draw.textbbox((0, 0), "M", font=font)[2])
            wrap_width = max(3, int(safe_width / (char_w * 0.58)))
            wrapped = _wrap_preserving_breaks(rendered, wrap_width)
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="left", spacing=10)
            if bbox[2] - bbox[0] <= safe_width:
                break
            size -= 4
        else:
            size = _TEXT_MIN_SIZE
            font = ImageFont.truetype(_FONT_BOLD_PATH, size=size)
            wrapped = _wrap_preserving_breaks(rendered, max(3, int(safe_width / (size * 0.5))))
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="left", spacing=10)

        draw.multiline_text((_SIDE_MARGIN, y), wrapped, font=font, fill=_TEXT_COLOR,
                             align="left", spacing=10)
        block_h = bbox[3] - bbox[1]
        y += block_h + _TEXT_LINE_GAP

    return y - _TEXT_LINE_GAP


def _render_top_text(lines: list[dict]) -> tuple[Path, int]:
    """
    Renders `lines` (see _draw_text_lines) on a transparent 1080x1920 canvas
    starting at the fixed top margin.

    Returns (png_path, bottom_y) so the caller can place the video box right
    below the actual rendered text instead of at a fixed offset.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bottom_y = _draw_text_lines(draw, lines, _TEXT_TOP_Y)

    tmp = Path(tempfile.gettempdir()) / "vid_pipeline_f18_white_card"
    tmp.mkdir(exist_ok=True)
    out = tmp / "top_text.png"
    overlay.save(str(out), "PNG")
    return out, bottom_y


def _render_bottom_text(text_png: Path, lines: list[dict], start_y: int) -> Path:
    """
    Draws `lines` onto the EXISTING top-text overlay _render_top_text already
    produced, starting at `start_y` -- the renderer's only other text slot,
    for a caption that belongs BELOW the clip instead of stacked above it
    (2026-08-13, explicit user ask: a second verbatim line was always
    landing folded into the top card, because _render_top_text used to be
    the only place in this whole module that drew text at all). Reuses the
    SAME png file _build_cmd already overlays onto the render -- no new
    ffmpeg input stream needed, build_white_card_reaction just calls this
    AFTER the video box's own position/size is finalized (the below-clip
    start y depends on where the box ends up, which top text alone doesn't
    determine).
    """
    overlay = Image.open(text_png).convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    _draw_text_lines(draw, lines, start_y)
    overlay.save(str(text_png), "PNG")
    return text_png


def _render_frame_and_watermark(video_x: int, video_y: int, video_w: int, video_h: int) -> Path:
    """Thin black frame outline around the clip box -- no ResetWorth text
    watermark (removed 2026-08-10, explicit user ask: "je ne veux pas de
    watermark")."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw.rectangle(
        [video_x - _FRAME_WIDTH, video_y - _FRAME_WIDTH,
         video_x + video_w + _FRAME_WIDTH, video_y + video_h + _FRAME_WIDTH],
        outline=_FRAME_COLOR, width=_FRAME_WIDTH,
    )

    tmp = Path(tempfile.gettempdir()) / "vid_pipeline_f18_white_card"
    tmp.mkdir(exist_ok=True)
    out = tmp / "frame_watermark.png"
    overlay.save(str(out), "PNG")
    return out


def _apply_redactions(
    filters: list[str], prev: str, redactions: list[dict], seg_start: float, seg_end: float, tag: str,
) -> str:
    """Appends blur-overlay filters for every redaction that overlaps
    [seg_start, seg_end) (source-clip absolute time), retiming each one to
    the segment's OWN local clock (segments get their own setpts=PTS-STARTPTS,
    so `enable='between(t,...)'` must count from that segment's t=0, not the
    source clip's). `tag` keeps filter-graph labels unique across segments
    (main vs. teaser) sharing the same redaction index."""
    cur = prev
    for i, r in enumerate(redactions):
        start, end = float(r["start"]), float(r["end"])
        if end <= seg_start or start >= seg_end:
            continue
        local_start = max(0.0, start - seg_start)
        local_end = min(seg_end - seg_start, end - seg_start)
        rx, ry, rw, rh = r["box"]
        split_a, split_b = f"{tag}rdsrc{i}", f"{tag}rddup{i}"
        blurred, merged = f"{tag}rdblur{i}", f"{tag}rdout{i}"
        filters.append(f"[{cur}]split=2[{split_a}][{split_b}]")
        filters.append(f"[{split_b}]crop={rw}:{rh}:{rx}:{ry},avgblur=24[{blurred}]")
        filters.append(
            f"[{split_a}][{blurred}]overlay={rx}:{ry}:enable='between(t,{local_start},{local_end})'[{merged}]"
        )
        cur = merged
    return cur


def _build_cmd(
    input_path: str, box: tuple[int, int, int, int], t_start: float, clip_dur: float,
    video_x: int, video_y: int, video_w: int, video_h: int,
    text_png: Path, frame_png: Path, redactions: list[dict], keep_audio: bool, out_path: str,
    teaser: dict | None = None, label_freeze: dict | None = None,
) -> list[str]:
    """
    teaser (optional): {"start": float, "end": float} in the SOURCE clip's own
    absolute time (same coordinate space as `trim`/`redactions`) -- a 2-4s
    out-of-context flash of a later "twist" moment, prepended before the
    normal THEM:/ME: card (see detect_twist_moment). Rendered through the
    SAME crop/scale/frame treatment as the main card but with NO joke text
    overlay -- the point is to show the payoff's energy without explaining
    it yet, so the viewer keeps watching to find out why.

    label_freeze (optional): {"freeze_start": float, "freeze_duration": float,
    "labels": [{"text": str, "x": int, "y": int}, ...]} -- freezes the MAIN
    segment on the frame at `freeze_start` (source-clip absolute time) for
    `freeze_duration` seconds and draws each label at (x, y) IN THE CROPPED
    CLIP'S OWN PIXEL SPACE (same coordinate convention `redactions` already
    uses) for that whole freeze window, then resumes normal playback from
    `freeze_start` onward -- see plan_label_treatment. This is a direct,
    literal callout gag (e.g. "ME" on a person, "THE MARKET" on whatever's
    chasing/hitting them) that reads clearer as static labels on a held
    frame than as text that has to track moving subjects (tried and
    rejected live 2026-07-30: following labels frame-by-frame looked
    janky and didn't read as funnier than one clean freeze). The top
    THEM:/ME: card stays visible throughout regardless -- this ADDS a
    beat, it doesn't replace the card (explicit user ask: keep the quote).

    No input-level `-ss` here (unlike the pre-teaser version of this
    function): a teaser and the main segment need two different absolute
    start points cut out of the SAME decoded input, so both trims happen in
    the filter graph against the source's real timeline instead.
    """
    x0, y0, cw, ch = box
    crop_filter = f"crop={cw}:{ch}:{x0}:{y0}," if cw and ch else ""
    scale_x = video_w / cw if cw else 1.0
    scale_y = video_h / ch if ch else 1.0

    cmd = ["ffmpeg", "-y", "-i", str(input_path),
           "-loop", "1", "-i", str(text_png), "-loop", "1", "-i", str(frame_png)]

    filters = []

    def build_segment(seg_start: float, seg_dur: float, tag: str, with_text: bool) -> str:
        cropped = f"{tag}cropped"
        filters.append(
            f"[0:v]{crop_filter}trim=start={seg_start:.3f}:duration={seg_dur:.3f},"
            f"setpts=PTS-STARTPTS[{cropped}]"
        )
        prev = _apply_redactions(filters, cropped, redactions, seg_start, seg_start + seg_dur, tag)
        clip_tag = f"{tag}clip"
        filters.append(f"[{prev}]scale={video_w}:{video_h},setsar=1[{clip_tag}]")
        canvas_tag = f"{tag}canvas"
        filters.append(f"color=c=white:size={VIDEO_WIDTH}x{VIDEO_HEIGHT}:rate={VIDEO_FPS},format=yuv420p[{canvas_tag}]")
        with_clip = f"{tag}with_clip"
        filters.append(f"[{canvas_tag}][{clip_tag}]overlay={video_x}:{video_y}[{with_clip}]")
        if with_text:
            filters.append("[1:v]format=rgba[txt]")
            filters.append(f"[{with_clip}][txt]overlay=0:0[{tag}with_text]")
            with_clip = f"{tag}with_text"
        filters.append(f"[2:v]format=rgba[{tag}fw]")
        vout_raw = f"{tag}voutraw"
        filters.append(f"[{with_clip}][{tag}fw]overlay=0:0[{vout_raw}]")
        # The canvas/text/frame layers above are all INFINITE sources (a
        # `color=` generator, two `-loop 1` PNGs) -- overlay's default
        # eof_action is to keep repeating the shorter (clip) input's last
        # frame rather than end the stream, so `vout_raw` never reaches EOF
        # on its own even though the clip itself is trimmed to `seg_dur`.
        # With a single segment that's harmless (the global `-t` flag below
        # hard-cuts the final output regardless), but `concat` needs each
        # segment to actually END on its own to know when to switch to the
        # next one -- without this explicit trim, concat hangs forever on
        # the first (teaser) segment, its own EOF never arriving.
        vout = f"{tag}vout"
        filters.append(f"[{vout_raw}]trim=duration={seg_dur:.3f},setpts=PTS-STARTPTS[{vout}]")
        return vout

    main_audio_override = None  # set below only when label_freeze inserts a freeze (silence) beat

    if not label_freeze:
        main_vout = build_segment(t_start, clip_dur, "m", with_text=True)
        main_dur = clip_dur
    else:
        f_start = float(label_freeze["freeze_start"])
        f_dur = float(label_freeze.get("freeze_duration", 1.8))
        pre_dur = max(0.0, f_start - t_start)
        post_dur = max(0.0, (t_start + clip_dur) - f_start)

        v_parts, a_parts = [], []
        if pre_dur > 0.05:
            v_parts.append(build_segment(t_start, pre_dur, "mpre", with_text=True))
            if keep_audio:
                filters.append(f"[0:a]atrim=start={t_start:.3f}:duration={pre_dur:.3f},asetpts=PTS-STARTPTS[mprea]")
                a_parts.append("mprea")

        # Frozen beat: a couple of real source frames at f_start (never a
        # single frame alone -- see build_segment's own comment on why a
        # degenerate near-zero trim can misbehave), held via tpad for the
        # rest of freeze_duration, with each label drawn on top for the
        # WHOLE held duration (no per-frame tracking -- tried and rejected
        # live 2026-07-30, see this function's docstring).
        freeze_src_dur = min(2.0 / VIDEO_FPS, f_dur)
        hold_dur = max(0.0, f_dur - freeze_src_dur)
        frozen_raw = build_segment(f_start, freeze_src_dur, "mfz", with_text=True)
        frozen = frozen_raw
        if hold_dur > 0:
            frozen = "mfz_held"
            filters.append(f"[{frozen_raw}]tpad=stop_mode=clone:stop_duration={hold_dur:.3f}[{frozen}]")
        for i, lbl in enumerate(label_freeze.get("labels", [])):
            cx = video_x + float(lbl["x"]) * scale_x
            cy = video_y + float(lbl["y"]) * scale_y
            safe_text = str(lbl["text"]).replace("\\", "").replace("'", "").replace(":", "\\:")
            # Backslash-escape the drive-letter colon instead of relying on the
            # surrounding quotes to protect it -- single-quoting alone does NOT
            # stop ffmpeg's own colon-separated option parser from splitting on
            # "C:" here (confirmed live: quoted fontfile='C:/...' still errored
            # with "No option name near ...", the same escape style already
            # used successfully elsewhere fixes it).
            font_path = _FONT_BOLD_PATH.replace("\\", "/").replace(":", "\\:")
            out_lbl = f"mlbl{i}"
            filters.append(
                f"[{frozen}]drawtext=fontfile='{font_path}':text='{safe_text}':"
                f"fontcolor=black:fontsize=42:box=1:boxcolor=white@0.9:boxborderw=10:"
                f"x={cx:.0f}:y={cy:.0f}[{out_lbl}]"
            )
            frozen = out_lbl
        v_parts.append(frozen)
        if keep_audio:
            filters.append(f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=duration={f_dur:.3f}[mfza]")
            a_parts.append("mfza")

        if post_dur > 0.05:
            v_parts.append(build_segment(f_start, post_dur, "mpost", with_text=True))
            if keep_audio:
                filters.append(f"[0:a]atrim=start={f_start:.3f}:duration={post_dur:.3f},asetpts=PTS-STARTPTS[mposta]")
                a_parts.append("mposta")

        if len(v_parts) == 1:
            main_vout = v_parts[0]
        else:
            main_vout = "mjoined"
            filters.append("".join(f"[{v}]" for v in v_parts) + f"concat=n={len(v_parts)}:v=1:a=0[{main_vout}]")
        if keep_audio:
            if len(a_parts) == 1:
                main_audio_override = a_parts[0]
            else:
                main_audio_override = "majoined"
                filters.append("".join(f"[{a}]" for a in a_parts) + f"concat=n={len(a_parts)}:v=0:a=1[{main_audio_override}]")
        main_dur = pre_dur + f_dur + post_dur

    if teaser:
        t0, t1 = float(teaser["start"]), float(teaser["end"])
        t_dur = max(0.3, t1 - t0)
        teaser_vout = build_segment(t0, t_dur, "t", with_text=False)
        if keep_audio:
            filters.append(f"[0:a]atrim=start={t0:.3f}:duration={t_dur:.3f},asetpts=PTS-STARTPTS[ta]")
            if main_audio_override:
                ma = main_audio_override
            else:
                filters.append(f"[0:a]atrim=start={t_start:.3f}:duration={clip_dur:.3f},asetpts=PTS-STARTPTS[ma]")
                ma = "ma"
            filters.append(f"[{teaser_vout}][ta][{main_vout}][{ma}]concat=n=2:v=1:a=1[vout][aout]")
        else:
            filters.append(f"[{teaser_vout}][{main_vout}]concat=n=2:v=1:a=0[vout]")
        total_dur = t_dur + main_dur
    else:
        filters.append(f"[{main_vout}]null[vout]")
        if keep_audio and main_audio_override:
            filters.append(f"[{main_audio_override}]anull[aout]")
        total_dur = main_dur

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]"]
    if keep_audio:
        if teaser or main_audio_override:
            cmd += ["-map", "[aout]"]
        else:
            cmd += ["-map", "0:a"]
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd


# ── Fully automatic pipeline (no manual label/text/redaction input) ──────────

def _extract_preview_frames(video_path: str, box: tuple[int, int, int, int], n: int = 5) -> list[Path]:
    """
    n evenly-spaced frames from the CROPPED clip content (the actual footage,
    not the source's outer white template/logo) -- what generate_joke_lines
    needs to see to judge the clip's mood/arc, same frames a human would look
    at to write the same joke by hand.
    """
    x0, y0, cw, ch = box
    dur = _duration_ffprobe(video_path)
    # INSIDE the project tree, not the OS temp folder -- these get fed to a
    # Claude vision call (@path), and the non-interactive `claude -p` CLI has
    # been observed to inconsistently refuse reads outside BASE_DIR (see
    # modules/clips/watermark_scan.py's _SCRATCH_DIR for the same fix and the
    # concrete failure that prompted it).
    from config import BASE_DIR
    tmp_dir = BASE_DIR / "scratch" / "white_card_reaction_preview"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for f in tmp_dir.glob("*.jpg"):
        f.unlink(missing_ok=True)

    crop_filter = f"crop={cw}:{ch}:{x0}:{y0}," if cw and ch else ""
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        out = tmp_dir / f"frame_{i}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video_path), "-frames:v", "1",
               "-vf", f"{crop_filter}scale=480:-2", "-q:v", "4", str(out)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and out.exists():
            frames.append(out)
    return frames


_JOKE_SYSTEM_PROMPT = (
    "You are a social media copywriter for ResetWorth, a wealth/investing/money-mindset "
    "channel. You write short 'reaction meme' captions in the classic "
    "THEM/SHE/HE: \"...\" then ME: ... (or a standalone POV: ...) format -- the kind reposted "
    "from Snapchat Spotlight or TikTok, where a short line sets up a question or jab and the "
    "clip's own visual reaction IS the punchline (a silent 'ME: ...' beat, or a POV caption "
    "with no reply line at all). Direct, punchy, aspirational, a little dry/deadpan. "
    "The channel's niche is broader than literal market/investing talk -- money, mindset, "
    "discipline, motivation, and hustle/grinding to make money are all equally on-brand angles. "
    "Pick whichever of these actually fits the SPECIFIC clip best; don't default to a stock-market "
    "line just because that's the obvious ResetWorth cliche if the footage itself suggests a "
    "different angle (a slow/lazy moment reads as motivation or discipline, a chaotic/frantic "
    "moment can read as hustle, etc.). No em dashes."
)


_VERBATIM_TRIGGERS = (
    "exactement cette phrase", "mot pour mot", "verbatim",
    "word for word", "exact phrase", "exactly this",
    # 2026-08-16 real user report: "écris exactement: <caption>" fell
    # through to the LLM-authored path below because none of the triggers
    # above actually match that phrasing -- "exactement" only matched
    # bundled with "cette phrase" right after it. The LLM then paraphrased
    # a close-but-not-identical caption instead of using the given text
    # as-is. Added the verb + "exactement"/"exactly" combos people actually
    # type, accents optional since French input doesn't always include them.
    "écris exactement", "ecris exactement", "dis exactement",
    "mets exactement", "note exactement",
    # 2026-08-23 real user report: typed "écrit exactement:" (3rd-person/
    # past-participle spelling of écrire, easily confused with the "écris"
    # imperative above -- a genuinely common French mix-up, not a rare typo)
    # and it fell through to the LLM-authored path again, same failure mode
    # as the 2026-08-16 fix above. Added the "-t" spelling alongside "-s".
    "écrit exactement", "ecrit exactement",
    "write exactly", "say exactly", "type exactly",
)


def _extract_verbatim_caption(idea: str) -> str | None:
    """
    If `idea` explicitly asks for its own text to be used AS-IS (2026-08-10,
    real user report: the LLM was paraphrasing the given idea into its own
    words and dropping the user's own quote marks, even when they wrote "je
    veux exactement cette phrase") -- returns the literal caption text to
    render, verbatim, quote marks and all. Returns None when no such trigger
    is present, so generate_joke_lines' normal LLM-authored path runs
    unchanged for a plain steering idea like "make it about crypto crashing".

    Strips out only the trigger phrase itself (+ everything up to the next
    ':' if there is one, e.g. "Je veux exactement cette phrase: <caption>").

    2026-08-16 correction: this used to ALSO strip any "(...)" parenthetical
    out of the caption, on the theory that parens are asides/notes TO the
    app (e.g. "(also there's a watermark to blur)"), not caption text. Real
    user report: they explicitly asked for a caption whose own wording
    included a trailing parenthetical -- "(I don't know if she's stupid or
    what.)" -- as part of the joke, and got it silently deleted. "Verbatim"
    means verbatim, full stop; the app has no reliable way to tell an
    in-quote parenthetical from an app note, so it no longer guesses --
    nothing here is lost for real app notes (like a watermark hint) either,
    since _parse_watermark_time_hint/_parse_watermark_location_hint already
    read the raw, un-stripped `idea` independently of this function.
    """
    lowered = idea.lower()
    if not any(t in lowered for t in _VERBATIM_TRIGGERS):
        return None

    text = idea
    for t in _VERBATIM_TRIGGERS:
        i = text.lower().find(t)
        if i != -1:
            rest = text[i + len(t):]
            colon = rest.find(":")
            text = rest[colon + 1:] if colon != -1 else rest
            break
    text = text.strip()
    # 2026-08-21 real user question: the idea field is a single-line <input>,
    # so there's no way to actually type Enter into it for a forced line
    # break. Users who want one type a literal two-character marker instead
    # (mnemonic, and never collides with real caption wording) -- translate
    # it into a real newline here so _wrap_preserving_breaks (the renderers
    # this feeds) honors it instead of textwrap.fill silently discarding it
    # as just more whitespace to reflow past. Accepts both "\n" and "/n":
    # the FIRST live test used "/n" (plain slash), not "\n" (backslash) as
    # instructed, because backslash is an awkward AltGr combo on an AZERTY
    # keyboard -- rather than relitigate which slash people "should" type,
    # both are honored.
    text = text.replace("\\n", "\n").replace("/n", "\n")
    return text or None


_BELOW_VIDEO_TRIGGERS = (
    "en bas de la vidéo", "en bas de la video", "en dessous de la vidéo",
    "en dessous de la video", "sous la vidéo", "sous la video", "en bas du clip",
    "below the video", "below the clip", "under the video", "underneath the video",
    "beneath the video",
)


def _split_below_video_text(text: str) -> tuple[str, str | None]:
    """
    Splits an already-verbatim-extracted caption (generate_joke_lines' own
    `verbatim`) into (top, bottom) when it contains a second instruction
    marking part of it for placement BELOW the clip instead of stacked above
    it -- 2026-08-13, explicit user ask: "Puis en bas de la vidéo tu écris
    exactement ca: ..." used to just fold straight into the SAME top block,
    since _extract_verbatim_caption only ever captured one contiguous blob
    and _render_top_text was the only place in this module that drew text at
    all. Same "everything after the next ':' if there is one" convention as
    _extract_verbatim_caption uses for its own trigger.

    Deliberately its OWN function, not folded into _extract_verbatim_caption
    -- that function's exact name/signature/behavior is shared with
    viral_remix.py's own verbatim path (`from ...white_card_reaction import
    _extract_verbatim_caption`) and must not change. This one only ever runs
    on the text _extract_verbatim_caption already returned, never on the raw
    idea, and only format18 calls it.

    Returns (text, None) unchanged when no below-video trigger is present --
    the common case, a single top-only verbatim caption.
    """
    lowered = text.lower()
    for t in _BELOW_VIDEO_TRIGGERS:
        i = lowered.find(t)
        if i == -1:
            continue
        # Cut at the nearest sentence boundary BEFORE the trigger, not right
        # at the trigger itself -- a connector word introducing it ("Puis
        # en bas de la vidéo...", "Then below the video...") would otherwise
        # dangle at the end of the top text. Falls back to cutting exactly
        # at the trigger when there's no earlier boundary to use (e.g. the
        # below-video instruction is the very first thing in the text).
        head = text[:i]
        boundary = max(head.rfind("."), head.rfind("!"), head.rfind("?"), head.rfind("\n"))
        top = (head[:boundary + 1] if boundary != -1 else head).strip()
        rest = text[i + len(t):]
        colon = rest.find(":")
        bottom = (rest[colon + 1:] if colon != -1 else rest).strip()
        return top, (bottom or None)
    return text, None


def generate_joke_lines(
    video_path: str, box: tuple[int, int, int, int], idea: str | None = None,
) -> tuple[list[dict], list[dict] | None]:
    """
    Looks at the actual cropped footage (not the source's outer template) and
    writes a THEM:/ME: (or POV:) joke fitted to what's actually happening --
    the same judgment call made by hand for the first format18 videos, now
    automated. Raises RuntimeError if Claude's response can't be parsed --
    callers should surface that rather than silently falling back to a
    generic line, since a mismatched joke is worse than a failed build here.

    `idea` is an optional steer ("make it about crypto crashing", "checking a
    401k") -- the joke is still written to match the CLIP's own visual arc,
    not generic copy, whether or not one is given. EXCEPTION: if `idea`
    explicitly asks for its exact wording to be used (see
    _extract_verbatim_caption), that text is rendered completely unchanged --
    no vision call, no LLM rewrite, no risk of the quote marks or phrasing
    getting altered, and (2026-08-13 fix) no label of ANY kind added on top
    of it either: a verbatim request means "use exactly this", full stop --
    if the user wanted a "POV:" prefix they'd have typed it themselves, and
    the auto/LLM path below already owns the job of choosing its own label
    when no exact wording was given.

    Returns (lines, bottom_lines) -- bottom_lines is a second block meant to
    render BELOW the clip instead of stacked above it (2026-08-13, explicit
    user ask: a verbatim idea marking part of its text "en bas de la vidéo"/
    "below the video" used to have nowhere to go but the SAME top block, see
    _split_below_video_text), or None when nothing was marked for there --
    the overwhelming majority of calls, including the entire LLM-authored
    path below, which never produces a bottom line at all.
    """
    verbatim = _extract_verbatim_caption(idea) if idea else None
    if verbatim:
        top_text, bottom_text = _split_below_video_text(verbatim)
        lines = [{"label": "", "text": top_text}]
        bottom_lines = [{"label": "", "text": bottom_text}] if bottom_text else None
        logger.info(f"format18: verbatim caption requested -> top={lines} bottom={bottom_lines}")
        return lines, bottom_lines

    frames = _extract_preview_frames(video_path, box)
    if not frames:
        raise RuntimeError("generate_joke_lines: could not extract any preview frames")

    idea_line = (
        f'\nThe user wants the joke built around this idea/angle: "{idea}"' if idea else
        "\nNo specific idea was given -- invent whatever angle best matches what's actually "
        "happening in the clip."
    )

    prompt = (
        "\n".join(f"@{f}" for f in frames)
        + "\n\nThese frames are sampled evenly across one short reaction/relatable video clip "
          "(no audio available to you -- judge by the visuals only: mood, body language, props, "
          "the arc from the first frame to the last)."
        + idea_line
        + "\n\nWrite ONE caption. Return JSON only:\n"
          '{"label1": "THEM" or "SHE" or "HE" or "", "text1": "<short question/jab, PLAIN TEXT '
          'with no quote marks of your own -- the renderer adds the quotation marks around it, '
          'or empty if label2 is POV>", "label2": "ME" or "POV", '
          '"text2": "<exactly \'...\' if label2 is ME, or a short direct caption if label2 is POV>"}\n\n'
          "Rules:\n"
          "- text1 (when used) must be a SHORT, natural-sounding line, under 12 words, and must NOT "
          "start or end with a quote/quotation-mark character -- write the words only\n"
          "- if label2 is \"ME\", text2 MUST be exactly \"...\" -- the clip itself is the punchline\n"
          "- if label2 is \"POV\", label1/text1 should be empty strings and text2 carries the whole caption\n"
          "- must clearly connect to ResetWorth's niche -- money, investing, wealth, mindset, "
          "discipline, motivation, or hustle/grinding to make money -- pick whichever ONE of "
          "these actually fits the SPECIFIC clip, not just market/investing by default\n"
          "- must match the SPECIFIC visual arc in these frames, not a generic template line\n"
          "- No em dashes anywhere"
    )

    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude
    raw, _, _ = call_claude(_JOKE_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError(f"generate_joke_lines: no JSON in Claude's response: {raw[:200]!r}")
    parsed = json.loads(m.group(0))

    lines = []
    label1 = str(parsed.get("label1", "")).strip()
    # Defensive strip: _render_top_text wraps text1 in its OWN quote marks
    # (f'{label}: "{text}"') -- if Claude includes a quote/curly-quote
    # character at either end anyway (seen in practice despite the prompt
    # asking it not to), that would double up into `""like this""`.
    text1 = str(parsed.get("text1", "")).strip().strip("\"'“”‘’").strip()
    if label1 or text1:
        lines.append({"label": label1, "text": text1})
    label2 = str(parsed.get("label2", "ME")).strip() or "ME"
    text2 = str(parsed.get("text2", "...")).strip() or "..."
    lines.append({"label": label2, "text": text2})

    logger.info(f"format18: auto-generated joke -> {lines}")
    return lines, None


_GRID_COLS = 6   # was 3x2 (see _detect_one_corner_watermark's docstring) -- 6x4
_GRID_ROWS = 4   # quarters the worst-case fallback blur area for the same
                 # reliable discrete-cell-counting task, just finer-grained.

_REDACTION_TRIGGERS = (
    "flout", "blur", "watermark", "filigrane", "censur", "redact",
    "cache le pseudo", "masque le pseudo", "cache le watermark",
    "efface le watermark", "enlève le watermark", "enleve le watermark",
    "supprime le watermark", "retire le watermark",
)
_REDACTION_NEGATIONS = ("pas de", "pas ", "sans ", "aucun", "aucune", "jamais", "no ", "don't", "not ")


def _wants_watermark_redaction(idea: str | None) -> bool:
    """Gates the entire watermark scan (coarse pass + zoomed precise pass,
    see auto_detect_redactions) behind an explicit request in `idea`
    (2026-08-16, explicit user ask: "ça c'est seulement si je te dis y'a un
    watermark qui faut que t'enlèves, sinon tu le fais pas"). Mirrors
    _wants_label_treatment's exact opt-in pattern and reasoning: scanning on
    every clip whether or not it's needed both costs real time (several
    Claude calls) and reads as an unpredictable default rather than a tool
    reached for on purpose. Same negation guard as _wants_label_treatment so
    "pas de watermark à flouter" doesn't accidentally opt in. Trigger words
    are deliberately specific to this feature (unlike generic English words
    such as "label"/"freeze" that _wants_label_treatment had to guard
    against) -- "flout"/"blur"/"watermark"/"filigrane" don't show up in
    ordinary joke/caption text by coincidence."""
    if not idea:
        return False
    lowered = idea.lower()
    for t in _REDACTION_TRIGGERS:
        i = lowered.find(t)
        if i == -1:
            continue
        preceding = lowered[max(0, i - 20):i]
        if any(neg in preceding for neg in _REDACTION_NEGATIONS):
            continue
        return True
    return False


_WATERMARK_SYSTEM_PROMPT = (
    "You inspect ONE timestamped contact sheet -- a grid of small tiles, one per second, all "
    "cropped to either a single corner OR a full-width top/bottom edge strip of a video frame -- "
    "to spot a baked-in watermark, logo, username handle, or app UI chrome overlaid on the "
    "footage -- NOT part of the natural scene (not furniture, not a person's actual clothing/"
    "accessories, not lighting/reflections). Judge PURELY from the pixels you can see in the "
    "tiles themselves. Ignore any file name, path, or other text metadata you might be given "
    "about the image or the source video -- it is unrelated to what is actually drawn on screen "
    "and must never influence your answer. Be conservative: only report present if you can "
    "clearly see a recognizable logo/icon/text overlay repeating across multiple tiles."
)


def _detect_one_corner_watermark(name: str, sheet_path: Path, hint: str | None = None) -> dict | None:
    """One independent vision call per region -- NOT a single combined call
    across all of them. A combined call was tried first and produced a
    hallucination: the model built one narrative across all 4 corner images
    and let a misleading detail (the sheet's file NAME, at the time still
    derived from the source video's own name) contaminate its read of a
    totally unrelated corner, while missing the two watermarks that were
    actually visible. Splitting into one-region-at-a-time calls removes that
    cross-talk -- each call has nothing to reason across, only pixels to
    describe. Returns None on any failure (missing JSON, vision-call error)
    -- treated as "not present" by the caller, logged as a warning either way.

    hint (2026-08-10, explicit user ask -- see auto_detect_redactions'
    docstring): the user's raw "idea/angle" text, passed through AS-IS
    whether or not it says anything about a watermark at all. Free-text --
    Claude picks out whatever's relevant (e.g. "there's a static watermark
    in the middle saying X") and ignores the rest (a caption instruction
    sharing the same text box) itself, no brittle parsing needed on our end.
    Purely a hint, never overrides what's actually visible in the pixels.

    Location is asked as a coarse GRID CELL selection, not continuous x/y/w/h
    fractions -- confirmed live 2026-08-10: asking for precise numeric
    fractions from a single glance at a tile produced a real, visible
    mismatch (a "Hyperblaxe" text watermark sitting left-of-center
    consistently got a box reported to its right, over blank background --
    stayed fully readable, unblurred, across a real render). A column/row
    judgment per cell is a much easier, more reliable read than a continuous
    coordinate estimate.

    Grid is 6 columns x 4 rows (_GRID_COLS/_GRID_ROWS), not the original 3x2
    (2026-08-16, real user case: "@the.porsche_hub" over grass footage with a
    diagonal transition effect that broke the numeric static-pixel refiner's
    core assumption for this clip -- see _locate_static_overlay's docstring --
    so the raw 3x2 cell union became the actual blur box, and 3x2 cells are
    HUGE (up to a third of the region's width, half its height): the result
    was a correctly-located but comically oversized blur block, "le floutage
    est trop grand". Quadrupling the grid (twice the columns, twice the rows)
    quarters that worst-case area while staying a simple discrete counting
    task, not a continuous-coordinate guess -- the thing that was confirmed
    unreliable, not "any grid finer than 3x2" specifically."""
    hint_line = f'\n\nThe user also gave this note, which may describe a watermark to look for (ignore anything in it unrelated to a watermark): "{hint}"' if hint else ""
    n_cells = _GRID_COLS * _GRID_ROWS
    prompt = (
        f"@{sheet_path}\n\n"
        "This is a contact sheet for ONE region of a video frame (a corner, a full-width "
        "top/bottom edge strip, or the full-width middle band between those two strips) -- "
        "tiles are labeled top-left with their timestamp in seconds."
        + hint_line +
        "\n\nIs a watermark/logo/username/app-UI overlay visible in ANY tile? If so, give the first "
        "and last timestamp it appears at (it may span only part of the sheet, or all of it).\n\n"
        f"Also locate it: mentally divide ONE tile into a {_GRID_COLS}x{_GRID_ROWS} grid "
        f"({_GRID_COLS} columns left-to-right, {_GRID_ROWS} rows top-to-bottom), giving {n_cells} cells "
        "numbered left-to-right then top-to-bottom starting at 0 in the top-left corner "
        f"(so cell index c means column c%{_GRID_COLS}, row c//{_GRID_COLS}). List EVERY cell number the "
        "watermark touches, even partially -- e.g. a small badge fully inside one cell is just that one "
        "number, a wider watermark spanning several cells is all of them. Err on the side of including a "
        "cell if you're unsure it touches it -- missing part of it leaves real text exposed, which is "
        "worse than blurring a little extra background.\n\n"
        'Return JSON only: {"present": true/false, "start": <number or null>, "end": <number or null>, '
        f'"cells": <list of integers 0-{n_cells - 1}, or empty list if not present>}}'
    )
    try:
        from config import CLAUDE_MODEL
        from modules.claude_cli import call_claude
        raw, _, _ = call_claude(_WATERMARK_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            logger.warning(f"auto_detect_redactions: [{name}] no JSON in Claude's response: {raw[:200]!r}")
            return None
        return json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"auto_detect_redactions: [{name}] vision call failed ({e})")
        return None


def _strip_accents(s: str) -> str:
    """Lowercase + accent-stripped normalization, shared by the two hint
    parsers below -- lets a single pattern match "2eme"/"2ème"/"2éme" etc.
    without enumerating every accented variant by hand."""
    import unicodedata
    norm = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


def _parse_watermark_time_hint(idea: str | None) -> tuple[float, float] | None:
    """Extracts an EXPLICIT start/end second range the user gave directly in
    the idea text (2026-08-16 3rd follow-up -- explicit user ask after two
    live tests in a row where the coarse pass's own timing guess landed on
    the wrong window entirely: "je peux te dire quand le watermark [...] à
    quelles secondes"). When this returns non-None, auto_detect_redactions
    skips the coarse guess ENTIRELY -- the coarse pass exists only to guess
    roughly WHEN and WHERE from a handful of sampled frames, and a
    user-given number is ground truth, not another guess to double-check
    against a vision call that already proved unreliable twice on the same
    clip.

    Handles (accent/case-insensitive, see _strip_accents): "entre la 2eme et
    3eme seconde", "entre 2 et 3 secondes", "de 2 a 3 secondes",
    "2-3 secondes", "a 2 secondes" / "vers 2s" (single instant, padded to a
    +/-1s window). Returns None if no such explicit number is found --
    caller falls back to the coarse-pass guess as before.
    """
    if not idea:
        return None
    norm = _strip_accents(idea)
    num = r"(\d+(?:[.,]\d+)?)"
    m = re.search(rf"entre\s+(?:la\s+)?{num}\s*(?:eme|e)?\s+et\s+(?:la\s+)?{num}\s*(?:eme|e)?\s+seconde", norm)
    if not m:
        m = re.search(rf"{num}\s*(?:s|sec|secondes?)?\s*(?:a|-)\s*{num}\s*(?:s|sec|secondes?)", norm)
    if m:
        a, b = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        return (min(a, b), max(a, b))
    m = re.search(rf"(?:a|vers)\s+{num}\s*(?:s|sec|secondes?)", norm)
    if m:
        t = float(m.group(1).replace(",", "."))
        return (max(0.0, t - 1.0), t + 1.0)
    return None


def _parse_watermark_location_hint(idea: str | None) -> tuple[float, float, float, float] | None:
    """Extracts a rough on-screen location described in plain French
    (2026-08-16 3rd follow-up, same user ask as _parse_watermark_time_hint)
    -- e.g. "en bas au milieu", "en haut a droite" -- as content-box
    FRACTIONS (x0, y0, x1, y1). Each axis is independent: a direction given
    on only ONE axis (e.g. just "en bas", no left/right/center) spans the
    FULL range on the other axis rather than narrowing to a guess -- "en
    bas" alone means "somewhere along the bottom edge", not "bottom-center
    specifically". Returns None if the idea has no recognizable direction
    word at all, letting the caller scan the whole content box instead."""
    if not idea:
        return None
    norm = _strip_accents(idea)

    vert = None
    if re.search(r"\ben\s+bas\b|\bbas\b", norm):
        vert = "bottom"
    elif re.search(r"\ben\s+haut\b|\bhaut\b", norm):
        vert = "top"

    horiz = None
    if re.search(r"\bgauche\b", norm):
        horiz = "left"
    elif re.search(r"\bdroite\b", norm):
        horiz = "right"
    elif "milieu" in norm or "centre" in norm:
        horiz = "center"

    if vert is None and horiz is None:
        return None

    y0, y1 = {"top": (0.0, 0.4), "bottom": (0.6, 1.0)}.get(vert, (0.0, 1.0))
    x0, x1 = {"left": (0.0, 0.4), "right": (0.6, 1.0), "center": (0.25, 0.75)}.get(horiz, (0.0, 1.0))
    return (x0, y0, x1, y1)


_END_CUT_NUM = r"(\d+(?:[.,]\d+)?)"
_END_CUT_PATTERNS = [
    rf"\bcoupe\w*\s+(?:la\s+video\s+|le\s+clip\s+)?a\s+{_END_CUT_NUM}\s*(?:s|sec|secondes?)\b",
    rf"\bgarde\w*\s+(?:les\s+)?{_END_CUT_NUM}\s*premi\w*\s*(?:s|sec|secondes?)\b",
    rf"\b(?:termine|arrete|stoppe)\w*\s+a\s+{_END_CUT_NUM}\s*(?:s|sec|secondes?)\b",
    rf"\braccourci\w*\s+(?:la\s+video\s+|le\s+clip\s+)?a\s+{_END_CUT_NUM}\s*(?:s|sec|secondes?)\b",
    rf"\b(?:max|maximum)\w*\s+{_END_CUT_NUM}\s*(?:s|sec|secondes?)\b",
    rf"\bjusqu[\w']*\s+{_END_CUT_NUM}\s*(?:s|sec|secondes?)\b",
]


def _parse_end_cut_hint(idea: str | None) -> float | None:
    """Extracts an EXPLICIT "shorten the clip to N seconds" instruction from
    the idea text (2026-08-20 user ask: typing e.g. "coupe a 12 secondes"
    into the idea field should actually drop everything in the source clip
    past the 12s mark, not just steer the joke text -- until this, nothing
    in the idea field could touch trim.end at all -- before this, there was
    no automatic trim mechanism at all).

    Unlike _parse_watermark_time_hint's start/end WINDOW (a region to scan),
    this is a single cutoff: "coupe a 12 secondes" means "keep 0-12s of the
    clip", full stop. Deliberately narrow triggers (coupe/garde/termine/
    arrete/raccourci/max/jusqu'a + a number of seconds) so ordinary idea
    text ("montre-le en train de couper des legumes") can't accidentally
    fire this -- each pattern requires the trigger verb to be immediately
    followed by "a <number> sec...", not just co-occurring somewhere in the
    sentence. Returns None if no such explicit number is found, or if the
    number is nonsensically small (<1s, almost certainly a mis-parse).
    """
    if not idea:
        return None
    norm = _strip_accents(idea)
    for pat in _END_CUT_PATTERNS:
        m = re.search(pat, norm)
        if m:
            t = float(m.group(1).replace(",", "."))
            return t if t >= 1.0 else None
    return None


def _strip_end_cut_phrase(idea: str) -> str:
    """Removes an end-cut phrase ("coupe a 11 secondes", see
    _parse_end_cut_hint) from `idea` before it reaches the watermark hint
    parsers (2026-08-24, real live case: combining an end-cut with a
    watermark-blur request in the same idea field -- "coupe a 11 secondes.
    flou le watermark..." -- made _parse_watermark_time_hint's own "a <N>
    secondes" pattern match the END-CUT's number too, misreading it as
    "the watermark appears around 11s" and shrinking the redaction's
    start/end window down to ~9-13s on an 11s clip instead of the whole
    video, leaving the watermark exposed everywhere outside that sliver).
    Both parsers share the same loose "at N seconds" phrasing by design (French
    has no cleaner way to say either one), so the fix is to make sure the
    end-cut's OWN number never reaches the watermark parsers at all rather
    than trying to make the regexes tell the two apart. Matches on the same
    accent-stripped normalization _parse_end_cut_hint uses -- safe to slice
    back into the original string since accent-stripping never changes
    character COUNT (see _strip_accents), only which codepoints survive."""
    norm = _strip_accents(idea)
    for pat in _END_CUT_PATTERNS:
        m = re.search(pat, norm)
        if m:
            return idea[:m.start()] + idea[m.end():]
    return idea


def auto_detect_redactions(video_path: str, box: tuple[int, int, int, int], hint: str | None = None) -> list[dict]:
    """
    Watermark pipeline (2026-08-16, 4th follow-up -- see below for the full
    history; this is the version actually reachable today, the coarse-guess
    machinery it replaced is gone, not just disabled):

      1. GATE (_wants_watermark_redaction): does nothing at all unless
         `hint` (the idea text) explicitly asks for a watermark to be
         removed. The old version ran unconditionally on every single
         format18 build, most of which have no watermark to find -- pure
         wasted time.
      2. TIMING is always DETERMINISTIC now, never guessed by a vision
         call -- two live tests in a row on the same hard clip (a busy
         multi-scene compilation) showed the coarse pass's own timing guess
         (both a classic 3-fixed-frame version and a dense full-duration
         version, both since removed) landing on the wrong window entirely.
         Explicit user rule: "si je dis une duree c'est un watermark qui
         bouge [apparait puis disparait au meme endroit] [...] si je donne
         pas de duree c'est un watermark sur toute la video". So:
           - _parse_watermark_time_hint finds an explicit second-accurate
             range in the idea (e.g. "entre la 2eme et 3eme seconde") ->
             that EXACT range is used as final start/end, full stop.
           - no such range -> start/end default to the WHOLE video
             (0..duration) -- assumed constantly present rather than
             guessed at.
         Either way, _parse_watermark_location_hint can ALSO narrow WHERE
         to look (e.g. "en bas au milieu") as content-box fractions; with
         no location hint the zoom pass below just scans the whole content
         box directly.
      3. ZOOM+PRECISE: ONE Claude call for the exact grid-cell fit over the
         (possibly location-narrowed) box (_detect_one_corner_watermark,
         reused as-is), then the existing numeric pixel-variance refinement
         (_locate_static_overlay) for the tightest box that still safely
         covers the whole thing.
      4. ZOOM2, conditional (2026-08-16 2nd follow-up): if step 3's grid
         read still lands on MORE THAN ONE cell (a block, not a single
         tight cell), zoom into just that block and ask the same question
         again over a fresh 6x4 grid on top of it -- ONE more Claude call,
         but ONLY when actually needed. Real user case that motivated this:
         a small text watermark got a 4-cell block covering ~80% empty
         space around it, and step 3's numeric refiner also failed on that
         same clip (diagonal scene-transition footage breaks its "moving
         background vs static overlay" assumption), so the oversized block
         would otherwise have shipped as the final blur box untouched.

    One Claude call total in the common case (down from the old design's up
    to 7 -- one per fixed region, scanned every time regardless of whether
    anything was there), two when step 4 fires. The tradeoff:
    only ONE watermark location is chased per video -- fine for the real
    case this was built for (one handle/logo baked into the footage), but
    two SEPARATE, spatially unrelated overlays on the same clip would only
    get the first one found. Rare enough in practice not to be worth the
    extra calls the old exhaustive scan cost on every build.

    Returns a redactions list ready for build_white_card_reaction, already
    padded by one sampling interval on each side (a watermark can start/stop
    mid-interval, so the raw reported timestamps would otherwise risk
    leaving a sliver exposed right at the edge). Fails soft (returns []) on
    any detection-side error or when nothing is found -- a missed redaction
    is a real risk the caller should be told about via the log, but it's
    better than blocking the whole build over a vision-call hiccup.
    """
    if not _wants_watermark_redaction(hint):
        logger.info("format18: no watermark-removal request in the idea, skipping redaction scan")
        return []

    # Strip any "coupe a Ns" end-cut phrase out before the time/location hint
    # parsers below ever see it -- see _strip_end_cut_phrase's docstring for
    # the real collision this guards against (an end-cut's own number getting
    # misread as a watermark timing hint when both are typed in one idea).
    hint = _strip_end_cut_phrase(hint) if hint else hint

    x0, y0, cw, ch = box
    n_cells = _GRID_COLS * _GRID_ROWS

    from modules.clips.watermark_scan import build_one_region_contact_sheet
    interval = 1.0

    def _zoom_pass(abs_box_in: tuple[int, int, int, int], pass_name: str,
                    t_start: float | None = None, t_end: float | None = None):
        """One zoom-into-region pass, factored out (2026-08-16 2nd
        follow-up) so it can be chained a second time on a tighter box
        instead of only ever running once. Builds a contact sheet for
        abs_box_in (absolute source-video coords), asks Claude for
        presence/timing/cells over a FRESH 6x4 grid laid on top of THAT
        box specifically, and returns (info, abs_cell_box, n_valid_cells)
        or None on any failure / "not present". abs_cell_box is in the
        same absolute coordinate space as abs_box_in, so it can be fed
        straight back in as the next pass's abs_box_in.

        t_start/t_end (2026-08-16 3rd follow-up): when the user already
        gave an explicit timing hint, restricts the contact sheet to that
        window (see build_one_region_contact_sheet) instead of the whole
        clip -- None (the coarse-guess path's default) reproduces the old
        always-full-video sampling."""
        try:
            sheet_path = build_one_region_contact_sheet(
                video_path, abs_box_in, interval=interval, name=pass_name,
                start=t_start or 0.0, end=t_end,
            )
        except Exception as e:
            logger.warning(f"auto_detect_redactions: [{pass_name}] sheet build failed ({e})")
            return None
        if not sheet_path:
            logger.warning(f"auto_detect_redactions: [{pass_name}] sheet build produced nothing")
            return None
        info = _detect_one_corner_watermark(pass_name, sheet_path, hint=hint)
        if not info or not info.get("present"):
            return None
        rw, rh = abs_box_in[2], abs_box_in[3]
        fine_cells = info.get("cells")
        valid_cells = [c for c in (fine_cells or []) if isinstance(c, int) and 0 <= c < n_cells]
        if valid_cells:
            cw2, ch2 = rw / _GRID_COLS, rh / _GRID_ROWS
            fx0 = min((c % _GRID_COLS) * cw2 for c in valid_cells)
            fy0 = min((c // _GRID_COLS) * ch2 for c in valid_cells)
            fx1 = max((c % _GRID_COLS) * cw2 + cw2 for c in valid_cells)
            fy1 = max((c // _GRID_COLS) * ch2 + ch2 for c in valid_cells)
            cell_box = (round(fx0), round(fy0), max(4, round(fx1 - fx0)), max(4, round(fy1 - fy0)))
        else:
            cell_box = (0, 0, rw, rh)
        abs_cell_box = (abs_box_in[0] + cell_box[0], abs_box_in[1] + cell_box[1], cell_box[2], cell_box[3])
        return info, abs_cell_box, len(valid_cells)

    # PATH A (2026-08-16 3rd follow-up): the user gave an EXPLICIT timing
    # hint in the idea text -- skips the coarse guess entirely and goes
    # straight to the zoom pass, optionally ALSO restricted to a described
    # location. Explicit user ask after two live tests in a row where the
    # coarse pass's own guess (both the classic 3-frame version and the
    # dense full-duration version) landed on the wrong window/area on the
    # same hard clip (a busy multi-scene compilation): "je peux te dire
    # quand le watermark [...] à quelles secondes". A user-given number is
    # ground truth -- trusted directly for the final start/end below,
    # never overwritten by whatever the zoom pass's own vision call reports.
    time_hint = _parse_watermark_time_hint(hint)
    if time_hint:
        t_start, t_end = time_hint
        logger.info(f"format18: explicit timing hint detected ({t_start:.1f}-{t_end:.1f}s), skipping the coarse guess")
        loc_frac = _parse_watermark_location_hint(hint)
        if loc_frac:
            fx0, fy0, fx1, fy1 = loc_frac
            abs_region = (
                x0 + round(fx0 * cw), y0 + round(fy0 * ch),
                round((fx1 - fx0) * cw), round((fy1 - fy0) * ch),
            )
            logger.info(f"format18: location hint also detected, restricting scan to {abs_region}")
        else:
            abs_region = box
        # Generous time pad around the user's own numbers -- an eyeballed
        # "entre la 2eme et 3eme seconde" is a human estimate, not a frame-
        # accurate cut point, so the window fed to the zoom pass needs a
        # little slack on both sides even though the FINAL start/end below
        # stays the user's own numbers.
        win_pad = 1.5
        result = _zoom_pass(abs_region, "zoom", t_start=max(0.0, t_start - win_pad), t_end=t_end + win_pad)
        if not result:
            logger.info("format18: zoom pass didn't confirm the user-given watermark timing/location, skipping redactions")
            return []
        info, abs_box, n_valid = result
        start, end = t_start, t_end
    else:
        # PATH C (2026-08-16 4th follow-up, explicit user rule): no
        # explicit timing hint at all means the watermark is assumed
        # present for the WHOLE video -- "si je donne pas de duree c'est
        # que c'est un watermark sur toute la video". Replaces the old
        # coarse-guessing fallback entirely (both the classic 3-frame guess
        # and the dense full-duration guess, now removed): every live test
        # of the coarse pass's own TIMING guess landed on the wrong window
        # on the same hard clip, so guessing WHEN is no longer attempted at
        # all -- it's either given explicitly (PATH A above) or assumed
        # constant. Only WHERE still needs a vision pass: a location hint
        # narrows the search the same way PATH A does; with no location
        # hint either, the zoom pass scans the WHOLE content box directly
        # (still just one Claude call -- no coarse pre-pass wasted on a
        # rough area guess that kept proving unreliable anyway).
        loc_frac = _parse_watermark_location_hint(hint)
        if loc_frac:
            fx0, fy0, fx1, fy1 = loc_frac
            abs_region = (
                x0 + round(fx0 * cw), y0 + round(fy0 * ch),
                round((fx1 - fx0) * cw), round((fy1 - fy0) * ch),
            )
            logger.info(f"format18: no timing hint given -- assuming the watermark spans the whole video; "
                        f"location hint restricts the scan to {abs_region}")
        else:
            abs_region = box
            logger.info("format18: no timing or location hint given -- assuming the watermark spans the "
                        "whole video, scanning the whole content box for it")

        result = _zoom_pass(abs_region, "zoom")
        if not result:
            logger.info("format18: zoom pass found nothing, skipping redactions")
            return []
        info, abs_box, n_valid = result
        start, end = 0.0, _duration_ffprobe(video_path)

    # SECOND zoom pass, conditional (2026-08-16 2nd follow-up, explicit user
    # ask after a live test: a small text watermark got a 4-cell block that
    # left ~80% empty space around it, AND the numeric refiner just below
    # failed on that same clip -- diagonal scene-transition footage breaks
    # its "moving background vs static overlay" assumption -- so the
    # oversized 4-cell block would have shipped as the final blur box
    # untouched). Only pays for the extra Claude call when the first zoom
    # actually still landed on a multi-cell block (n_valid > 1) -- a
    # single-cell read is already about as tight as one grid affords, so
    # there's nothing to gain zooming again. Padding here is intentionally
    # much lighter than the coarse->zoom step's: this box is a real
    # localization now, not a rough first guess. Stays within the same
    # start/end window computed above (whether that came from PATH A's
    # explicit hint or PATH C's whole-video default) rather than widening
    # back out to scan time it doesn't need to -- and (2026-08-16 4th
    # follow-up) never lets THIS pass's own vision-reported timing override
    # start/end either: timing is always deterministic now (explicit or
    # whole-video), never taken from a vision guess, on either zoom pass.
    if n_valid > 1:
        bx, by, bw, bh = abs_box
        pad_x2, pad_y2 = bw * 0.15, bh * 0.15
        inner_box = (
            max(0, round(bx - pad_x2)), max(0, round(by - pad_y2)),
            max(8, round(bw + 2 * pad_x2)), max(8, round(bh + 2 * pad_y2)),
        )
        inner_result = _zoom_pass(inner_box, "zoom2", t_start=max(0.0, start - 1.5), t_end=end + 1.5)
        if inner_result:
            _, abs_box, _ = inner_result
            logger.info(f"format18: [zoom2] refined {n_valid}-cell block down to {abs_box}")
        else:
            logger.info("format18: [zoom2] refinement pass found nothing, keeping the first zoom's box")

    # Numeric refinement, bounded to abs_box (never a wider crop) -- see
    # _locate_static_overlay's own plausibility floor for why a bad numeric
    # read bails out to None instead of confidently blurring the wrong spot.
    # HEIGHT ONLY now (2026-08-16, real user case: "@the.porsche_hub" over
    # grass -- the numeric row-band width came back ~137px, half the real
    # ~250px word, because the LEFT half of the word had weaker per-frame
    # contrast against the grass behind it and never cleared the
    # static-pixel threshold at any of the 8 sampled instants -- rendered
    # with half the handle still fully readable. A text line's HEIGHT is a
    # far more robust numeric signal than its width: every row within the
    # real band has SOME static content regardless of which letters are
    # low-contrast, so height stays trustworthy even when width doesn't.
    # WIDTH always comes from the vision grid-cell read instead, which saw
    # the whole word directly rather than inferring it from frame-to-frame
    # pixel variance -- erring toward a little extra background blurred is
    # far cheaper than leaving half a handle exposed again.
    # Height-undershoot floor (2026-08-20, real user case: a watermark sitting
    # directly over a talking character's mouth -- the row-band/blob detection
    # inside _locate_static_overlay has NO plausibility check on HEIGHT at all
    # (only the blob path's width/ratio floor above), so when the watermark's
    # lower rows overlap fast-changing background (mouth opening/closing,
    # teeth), those rows show real frame-to-frame variance even though the
    # overlay itself never moves -- semi-transparent/anti-aliased overlay
    # pixels blend with whatever's behind them, so a highly dynamic background
    # defeats the "static == watermark" assumption right where it overlaps.
    # That silently excluded the bottom of the text from the detected row
    # band, shipping a confidently too-short blur box and leaving the lower
    # half of the watermark readable. Guard the same way the width path
    # already guards itself: never accept a refined height that shrinks
    # below the vision-reported box's own height by much (85% floor).
    #
    # This alone is NOT enough, though -- checked against this exact real
    # failure's own log: the numeric band's reported bottom edge (392, in
    # content-box rows) landed EXACTLY on abs_box's own bottom edge (392),
    # i.e. the numeric pass wasn't freely choosing to stop short, it was
    # capped by zoom2's OWN vision-reported window, which pixel-measurement
    # on the actual rendered frame showed was ITSELF already ~8px short of
    # the true watermark bottom (needed ~400, window only reached 392) --
    # the same low-contrast-against-busy-background problem that fools the
    # numeric pass also makes vision under-select cells at the boundary
    # where the (semi-transparent) watermark blends into a highly detailed
    # background. So a fixed safety pad is added to whichever height wins
    # above, REGARDLESS of source -- bottom-biased since both real cases
    # found so far cut off the bottom, not the top.
    static_box = _locate_static_overlay(video_path, abs_box)
    min_h = abs_box[3] * 0.85
    if static_box and static_box[3] >= min_h:
        sy, sh = static_box[1], static_box[3]
        ry0 = abs_box[1] - y0 + sy
        logger.info("format18: [zoom] height refined via static-pixel variance, width kept from the vision grid read")
    else:
        if static_box:
            logger.info(f"format18: [zoom] static-pixel height refinement ({static_box[3]}px) implausibly short "
                        f"vs the vision box ({abs_box[3]}px) -- keeping the vision-reported height instead")
        ry0, sh = abs_box[1] - y0, abs_box[3]

    pad = max(4, round(sh * 0.3))
    pad_top, pad_bottom = pad // 3, pad - pad // 3   # bottom-biased, see docstring above

    # Width safety pad (2026-08-24, real live case: a "VOID BUFFET" badge --
    # width came from the vision grid read same as always, per the comment
    # above, but that read itself undershot the true badge by roughly a
    # third, same failure mode as the old "@the.porsche_hub" case except
    # THIS time vision's own box was the one too narrow, not the numeric
    # refiner -- live check on the rendered output showed the right two
    # thirds of the badge, "ID BUFFET" and most of the hexagon outline,
    # completely exposed while only the leftmost sliver got blurred).
    # _locate_static_overlay's numeric column-span can't catch this on its
    # own -- it only ever scans PIXELS INSIDE abs_box, so it's structurally
    # incapable of reporting anything wider than vision's own box. Same
    # fix as height's own undershoot floor above: a fixed safety pad on
    # whichever width vision reported, split evenly left/right since
    # (unlike height's bottom-biased case) nothing so far points to one
    # side undershooting more often than the other.
    pad_w = max(4, round(abs_box[2] * 0.3))
    pad_left, pad_right = pad_w // 2, pad_w - pad_w // 2
    fx0 = max(0, (abs_box[0] - x0) - pad_left)
    fw = min(cw - fx0, abs_box[2] + pad_left + pad_right)
    final_box = [fx0, max(0, ry0 - pad_top), fw, sh + pad_top + pad_bottom]

    padded_start = max(0.0, float(start) - interval)
    padded_end = float(end) + interval
    redactions = [{"box": final_box, "start": padded_start, "end": padded_end}]
    logger.info(f"format18: auto-redaction [zoom] {final_box} {padded_start:.1f}-{padded_end:.1f}s")
    return redactions


_MIN_DURATION_FOR_TEASER = 8.0   # below this there's no room for a separate
                                  # "teaser vs. full story" structure -- skip
_TEASER_MIN_START = 2.0          # a twist reported inside the first ~2s isn't
                                  # a twist, it's just the clip's own opening

_TWIST_SYSTEM_PROMPT = (
    "You inspect ONE contact sheet -- a grid of timestamped tiles sampled evenly across one "
    "continuous short reaction/relatable video clip -- to judge whether the clip has a genuine "
    "'twist': a clear, sudden shift in mood/energy/action partway through (e.g. a deadpan/static "
    "opening that flips into a big laugh, a reveal, a punchline beat) that reads as visually "
    "distinct from the tiles before it. Judge PURELY from the pixels in the tiles. Be "
    "conservative: many clips have no such twist -- gradual motion, a person just talking, or a "
    "consistently-toned clip do NOT count. Only report present if a human glancing at the sheet "
    "would clearly point to one specific stretch of tiles and say 'that's the moment it flips'."
)


def detect_twist_moment(video_path: str, box: tuple[int, int, int, int]) -> dict | None:
    """
    Looks for a short, out-of-context-worthy "twist" moment later in the clip
    (a mood/energy flip distinct from the opening) that could be flashed at
    the very start of the video as a foreshadowing teaser -- 2026-07-30 user
    idea: showing 2-3s of the payoff BEFORE the normal THEM:/ME: card gives
    viewers a reason to keep watching ("why is this guy laughing?") instead of
    bouncing at 0:01 off a flat/misleading opening frame (see the
    market_crashed_unbothered post-mortem this was built for -- the clip
    opens on a slumped, defeated-looking pose that reads as the OPPOSITE of
    the "unbothered" punchline, which only lands ~15-24s in).

    Returns None -- meaning "no teaser, build normally" -- whenever:
      - the clip is too short for a separate teaser to make narrative sense
        (_MIN_DURATION_FOR_TEASER)
      - Claude reports no clear twist (the common case -- most clips don't
        have one, e.g. a single consistent reaction shot)
      - the reported window is unusably close to the very start, too long,
        or otherwise malformed
      - anything about the detection call itself fails

    This is deliberately fail-soft/conservative in the SAME direction as
    auto_detect_redactions: a missed teaser opportunity just means the video
    renders exactly as it always has, which is a safe, known-working
    fallback -- never risk building an awkward teaser onto a clip that
    doesn't actually have one.
    """
    dur = _duration_ffprobe(video_path)
    if dur < _MIN_DURATION_FOR_TEASER:
        return None

    sheet = _build_arc_contact_sheet(video_path, box)
    if sheet is None:
        return None

    prompt = (
        f"@{sheet}\n\n"
        f"This clip is {dur:.1f}s long; each tile is labeled with its timestamp.\n\n"
        "Is there a genuine twist -- a clear mood/energy shift partway through, distinct from "
        "the clip's own opening tiles? If so, give a tight 2-3 second window (start, end) that "
        "best captures the moment it flips -- not the whole aftermath, just the flip itself or "
        "its clearest beat.\n\n"
        'Return JSON only: {"present": true/false, "start": <number or null>, "end": <number or null>}'
    )
    try:
        from config import CLAUDE_MODEL
        from modules.claude_cli import call_claude
        raw, _, _ = call_claude(_TWIST_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            logger.warning(f"detect_twist_moment: no JSON in Claude's response: {raw[:200]!r}")
            return None
        info = json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"detect_twist_moment: vision call failed ({e})")
        return None

    if not info.get("present"):
        logger.info("format18: no twist detected, skipping foreshadowing teaser")
        return None

    start, end = info.get("start"), info.get("end")
    if start is None or end is None:
        return None
    start, end = float(start), float(end)
    if start < _TEASER_MIN_START or end <= start or end > dur or (end - start) > 4.0:
        logger.warning(f"format18: twist window {start:.1f}-{end:.1f}s looks malformed, skipping teaser")
        return None

    logger.info(f"format18: twist detected at {start:.1f}-{end:.1f}s -> using as foreshadowing teaser")
    return {"start": start, "end": end}


def _build_arc_contact_sheet(
    video_path: str, box: tuple[int, int, int, int], n_tiles: int = 12, cols: int = 4,
) -> Path | None:
    """One contact sheet, full cropped frame per tile (not a corner -- see
    watermark_scan.build_corner_contact_sheets for that variant), each tile
    timestamp-labeled, spread evenly across the whole clip -- what
    detect_twist_moment needs to point at a specific timestamp range rather
    than just describe the clip's general mood."""
    x0, y0, cw, ch = box
    dur = _duration_ffprobe(video_path)
    tile_w, tile_h = 220, round(220 * ch / cw) if cw else 220
    crop_filter = f"crop={cw}:{ch}:{x0}:{y0}," if cw and ch else ""

    from config import BASE_DIR
    out_dir = BASE_DIR / "scratch" / "white_card_reaction_arc"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.jpg"):
        f.unlink(missing_ok=True)

    tiles = []
    for i in range(n_tiles):
        t = dur * (i + 0.5) / n_tiles
        frame_out = out_dir / f"tile_{i}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video_path), "-frames:v", "1",
               "-vf", f"{crop_filter}scale={tile_w}:{tile_h}", "-q:v", "4", str(frame_out)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and frame_out.exists():
            tiles.append((t, frame_out))

    if len(tiles) < 4:
        logger.warning("format18: not enough tiles decoded for arc contact sheet, skipping twist detection")
        return None

    font = ImageFont.truetype(_FONT_BOLD_PATH, size=18)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (tile_w * cols, tile_h * rows), (40, 40, 40))
    for i, (t, frame_path) in enumerate(tiles):
        tile = Image.open(frame_path).convert("RGB")
        draw = ImageDraw.Draw(tile)
        label = f"{t:.1f}s"
        draw.rectangle([0, 0, draw.textlength(label, font=font) + 8, font.size + 6], fill=(0, 0, 0))
        draw.text((4, 2), label, font=font, fill=(0, 255, 0))
        gx, gy = (i % cols) * tile_w, (i // cols) * tile_h
        sheet.paste(tile, (gx, gy))

    out = out_dir / "arc_sheet.jpg"
    sheet.save(out, quality=90)
    return out


_LABEL_TREATMENT_SYSTEM_PROMPT = (
    "You inspect several timestamped frames sampled across one short reaction/relatable video "
    "clip, all at the SAME pixel resolution and coordinate space, plus the caption-card joke "
    "already written for it (a THEM:/ME: or POV: line). Your job is a CLARITY check, not a "
    "prettiness check: if this clip has two distinct subjects (e.g. a person AND a separate "
    "thing/animal/object visibly acting on them), would a viewer watching it with ONLY the "
    "caption card -- no other labels -- be genuinely unsure which subject is 'ME' and what the "
    "other one is supposed to represent? A silent 'ME: ...' beat over a clip with a second, "
    "unlabeled subject often reads as unclear/not-funny for exactly this reason: the joke's "
    "metaphor (the OTHER subject standing in for the joke's real topic) never actually lands "
    "because nothing on screen ties that subject to the topic. When that ambiguity is real, "
    "direct in-frame text labels (a meme callout -- 'ME' on the person, a short label matching "
    "the joke's own topic on the other subject) fix it and should be used. Be conservative about "
    "clips that DON'T have this problem: a single person alone, or a joke whose connection to the "
    "visual is already obvious without labels, do NOT need labels -- only add them when their "
    "absence would genuinely leave the viewer confused about who's who."
)


_LABEL_TREATMENT_TRIGGERS = (
    "arrêt sur image", "arret sur image", "figé", "fige",
    "gèle l'image", "gele l'image", "geler l'image", "image gelée", "image gelee",
    "freeze frame", "freeze-frame", "in-frame label", "in frame label",
    "ajoute des labels", "ajoute un label", "mets des labels", "mets un label",
    "add labels", "add a label",
)

_LABEL_TREATMENT_NEGATIONS = ("pas de", "pas ", "sans ", "aucun", "aucune", "jamais", "no ", "don't", "not ")


def _wants_label_treatment(idea: str | None) -> bool:
    """
    plan_label_treatment used to run on every clip as a per-clip judgment
    call (see its own docstring) -- 2026-08-12 explicit user complaint: it
    fired often enough ("me", "my savings"-style in-frame labels showing up
    unasked) to read as an automatic default rather than the rare tool it
    was designed to be. Now it only runs when `idea` explicitly asks for
    it (a "label"/"freeze"/"arrêt sur image" style mention); everything
    else renders the plain top-card version only.

    2026-08-13 follow-up: it STILL fired unasked after the fix above -- the
    trigger list mixed in bare, generic single words ("label", "freeze")
    that can show up anywhere in ordinary caption/idea text with zero
    relation to this feature, and there was no defense against a NEGATED
    mention ("pas de freeze-frame", "sans les labels") still counting as a
    request. Triggers are now multi-word phrases only (a bare "label" or
    "freeze" no longer matches on its own), and a trigger closely preceded
    by a negation word is treated as explicitly declined, not requested.
    Callers should also pass an `idea` with any verbatim caption text
    already stripped out of it (see auto_build_white_card_reaction) so a
    caption's own wording can never accidentally opt into this on its own.
    """
    if not idea:
        return False
    lowered = idea.lower()
    for t in _LABEL_TREATMENT_TRIGGERS:
        i = lowered.find(t)
        if i == -1:
            continue
        preceding = lowered[max(0, i - 20):i]
        if any(neg in preceding for neg in _LABEL_TREATMENT_NEGATIONS):
            continue
        return True
    return False


def plan_label_treatment(
    video_path: str, box: tuple[int, int, int, int], joke_lines: list[dict],
) -> dict | None:
    """
    Decides whether THIS clip has a clean, literal two-subject visual (one
    thing visibly acting on/chasing/affecting another -- e.g. a bird
    attacking someone) that reads funnier as a held freeze-frame with direct
    in-frame text labels than the standard top card alone -- 2026-07-30 user
    idea (the "market" duck-chase clip), validated live through direct
    testing: a STATIC freeze-frame with labels landed; per-frame label
    TRACKING that followed the moving subjects was tried and explicitly
    rejected as less funny/harder to read, and the FIRST freeze attempted
    (right at the collision, subjects overlapping) was also rejected as too
    late/unclear -- the winning version froze earlier, while the two
    subjects were still visually separated. This does NOT replace the top
    THEM:/ME: card -- when used, it adds one extra beat on top of it
    (build_white_card_reaction's `label_freeze`), the card stays exactly as
    generate_joke_lines wrote it (explicit user ask: keep the quote).

    Deliberately conservative like the rest of this module's judgment calls
    (auto_detect_redactions, detect_twist_moment) -- and explicitly NOT meant
    to fire on every clip (2026-07-30 user note: this is one tool in the box,
    not a rule to apply every time). Forcing labels onto a clip that isn't a
    literal one-thing-acts-on-another moment would look forced, not funny.
    Returns None (skip -- render the plain top-card version exactly as
    before) on: no clear pairing, any parse/vision-call failure, or a
    returned window/position that doesn't check out.
    """
    x0, y0, cw, ch = box
    if cw <= 0 or ch <= 0 or _duration_ffprobe(video_path) < 3.0:
        return None
    dur = _duration_ffprobe(video_path)

    from config import BASE_DIR
    out_dir = BASE_DIR / "scratch" / "white_card_reaction_labelplan"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.jpg"):
        f.unlink(missing_ok=True)

    crop_filter = f"crop={cw}:{ch}:{x0}:{y0}"
    n = 8
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        out = out_dir / f"f_{i}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video_path), "-frames:v", "1",
               "-vf", crop_filter, "-q:v", "3", str(out)]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and out.exists():
            frames.append((t, out))
    if len(frames) < 4:
        return None

    joke_desc = "; ".join(f"{l.get('label', '')}: {l.get('text', '')}" for l in joke_lines)
    frame_list = "\n".join(f"@{p} (t={t:.2f}s)" for t, p in frames)
    prompt = (
        f"{frame_list}\n\n"
        f"All frames are {cw}x{ch} pixels, same crop, same coordinate space (0,0 = top-left).\n"
        f"The joke already written for this clip: {joke_desc}\n\n"
        "Would a viewer seeing only this caption card over this footage -- no other text -- be "
        "genuinely unsure which subject is \"ME\" and what the other subject is supposed to mean "
        "in relation to the joke? If that ambiguity is real, pick the ONE timestamp among these "
        "frames (or close to one) where the two subjects are most clearly SEPARATED (not "
        "overlapping/tangled), and give a short label + pixel center (x,y) for each -- the person "
        "is usually \"ME\"; the other label should name whatever the joke's OTHER subject stands "
        "in for (it does not have to be \"the market\").\n\n"
        'Return JSON only: {"use_labels": true/false, "freeze_time": <number or null>, '
        '"labels": [{"text": "...", "x": <int>, "y": <int>}, {"text": "...", "x": <int>, '
        '"y": <int>}] or []}'
    )
    try:
        from config import CLAUDE_MODEL
        from modules.claude_cli import call_claude
        raw, _, _ = call_claude(_LABEL_TREATMENT_SYSTEM_PROMPT, prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            logger.warning(f"format18: label-treatment planning got no JSON: {raw[:200]!r}")
            return None
        info = json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"format18: label-treatment planning failed ({e}), using plain card")
        return None

    if not info.get("use_labels"):
        logger.info("format18: no clean two-subject visual, using plain top card")
        return None

    freeze_time = info.get("freeze_time")
    labels = info.get("labels") or []
    if freeze_time is None or len(labels) < 2:
        return None
    try:
        freeze_time = float(freeze_time)
        clean_labels = [
            {"text": str(lbl["text"])[:20], "x": max(0, min(cw, int(lbl["x"]))),
             "y": max(0, min(ch, int(lbl["y"])))}
            for lbl in labels[:2]
        ]
    except Exception:
        logger.warning(f"format18: label-treatment output malformed ({info!r}), using plain card")
        return None
    if not (0 <= freeze_time <= dur):
        return None

    logger.info(f"format18: label treatment planned -- freeze@{freeze_time:.1f}s labels={clean_labels}")
    return {"freeze_start": freeze_time, "freeze_duration": 1.8, "labels": clean_labels}


# (2026-08-21, removed: `plan_pacing`, a "dead opening" auto-trim judgment
# call that ran unconditionally on every format18 render. It fired on a
# clip the user gave no cut instruction for at all and silently shortened a
# 7s source down to ~3.4s -- see auto_build_white_card_reaction's docstring
# NOTE for the full incident. Removed outright per explicit user ask: the
# clip's start is never trimmed automatically anymore, only an explicit
# "coupe a Ns" end-cut (_parse_end_cut_hint) can shorten anything at all.
# `_build_arc_contact_sheet` above is still used by `plan_label_treatment`
# and `detect_twist_moment`; only the pacing beat and its system prompt are
# gone.)


def _joke_summary(lines: list[dict]) -> str:
    """Human-readable version of the THEM:/ME:/POV: lines generate_joke_lines
    already wrote for this specific clip -- e.g. 'POV: they tell you "..."'
    or 'THEM: ... / ME'. Becomes this video's real subject/hook for
    downstream title/description generation (see auto_build_white_card_reaction's
    _save_format18_script), instead of the empty/generic stub every format18
    video used to leave behind (2026-08-10 user report: this is why every
    White Card post read like Claude had never actually seen the clip)."""
    parts = []
    for entry in lines:
        label = (entry.get("label") or "").strip()
        text = (entry.get("text") or "").strip()
        if not text or text == "...":
            if label:
                parts.append(label)
            continue
        parts.append(f"{label}: {text}" if label else text)
    return " / ".join(parts) if parts else "reaction video"


def _save_format18_script(
    video_path: Path, lines: list[dict], ctx=None, bottom_lines: list[dict] | None = None,
) -> None:
    """
    Persists the ACTUAL joke text this clip's card carries as a script JSON
    next to the video, using the video's OWN filename stem (not a fresh
    timestamp like script_generator._save_script -- every later lookup,
    app.py's `ts_prefix = video_path.stem[:15]` glob included, keys off the
    VIDEO's timestamp, so the script must share it exactly).

    Without this, content_meta.generate_meta() had nothing but an empty
    hook/body and a generic "white_card_reaction" subject to write a
    caption from -- Claude was blind to what the video actually said
    (2026-08-10 fix). ctx (modules.users.UserContext), when given and not
    the default brand, scopes the save to that brand's own output/scripts/
    tree instead of the shared default one -- same reasoning as
    users.tag_and_relocate for format15/16 (see its docstring).

    bottom_lines (2026-08-13): the below-clip block, when the caption used
    one -- folded into the same summary string so downstream caption/title
    generation still sees the whole joke, not just its top half.
    """
    from config import OUTPUT_DIR

    summary = _joke_summary(lines)
    if bottom_lines:
        summary = f"{summary} / {_joke_summary(bottom_lines)}"
    script = {"format": "format18", "subject": summary, "hook": summary, "body": []}

    scripts_dir = (ctx.root / "output" / "scripts") if (ctx is not None and not ctx.is_default) else (OUTPUT_DIR / "scripts")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / f"{video_path.stem}.json").write_text(
        json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(f"format18: script saved for {video_path.name} -- subject={summary!r}")


def auto_build_white_card_reaction(
    input_path: str, idea: str | None = None, output_name: str | None = None, keep_audio: bool = True,
    allow_teaser: bool = True, ctx=None,
) -> Path:
    """
    Fully automatic format18 pipeline -- the actual entry point the FlowShort
    panel and a pasted link should call. No manual label/text/box/timing
    input: detects the real footage's crop box (detect_content_box), has
    Claude double-check that box for a leftover static caption/logo band the
    numeric detector alone can misread as content (_verify_and_refine_content_box
    -- see its docstring for the repost-of-a-repost case that prompted this),
    has Claude read the clip's own visuals to write the THEM:/ME:/POV: joke
    (generate_joke_lines, optionally steered by `idea`, now free to reach for
    mindset/motivation/hustle angles and not just literal market/investing --
    2026-07-30 user note), has Claude read the corner contact sheets to find
    and redact any baked-in watermark (auto_detect_redactions), looks for a
    foreshadowing teaser moment (detect_twist_moment) -- unless allow_teaser
    is False, see below --, and, ONLY when `idea` explicitly asks for it (see
    _wants_label_treatment -- 2026-08-12: this used to run as a per-clip
    judgment call on every video and fired often enough to feel like an
    unwanted default, e.g. "me"/"my savings" style in-frame labels showing
    up unasked), judges whether this specific clip has a clean two-subject
    visual worth an in-frame label freeze-frame on top of the normal card
    (plan_label_treatment), then renders through the same
    build_white_card_reaction used by the manual/config-driven path, and
    finally persists the joke lines as a real script (see
    _save_format18_script) and relocates the video to `ctx`'s own brand
    folder when it isn't the default brand (same pattern users.run_generator
    applies to every other format -- format18 used to always render into
    the shared default output tree regardless of brand).

    allow_teaser (2026-08-10, explicit user ask): the FlowShort White Card
    panel's own checkbox for the "shows a later moment before the start"
    beat. Judgment stays the default (True lets detect_twist_moment decide
    per-clip as before, may still skip on its own); False skips the
    detection call entirely -- for when the user knows ahead of time they
    don't want that mechanism on a specific clip, not a per-clip judgment
    override.

    ctx (modules.users.UserContext, optional, 2026-08-10): brand this video
    belongs to. None keeps the old brand-agnostic behavior (default output
    tree, no script saved under a brand-scoped path).

    `idea` can also carry an EXPLICIT end-cut ("coupe a 12 secondes", see
    _parse_end_cut_hint -- 2026-08-20 user ask). Unlike every other
    idea-driven beat above, this one is a hard command, not a judgment call:
    if the number is there, the clip plays from its own true start (t=0)
    and stops at that mark, full stop -- which also means it OVERRIDES
    (rather than stacks with) the teaser beat above, since it would
    otherwise reorder or shift where the clip appears to start; see the
    end_cut block below for why that surprised the user on the first live
    test of this feature.

    NOTE (2026-08-21 user report): this module used to also run a
    `plan_pacing` beat that judged, per-clip and entirely on its own
    initiative, whether the clip's OPENING looked "dead" and trimmed it if
    so -- with no way to opt out short of typing an explicit end-cut (which
    only suppressed it as a side effect). It fired on a clip the user had
    given no cut instruction for at all, silently shortening a 7s source to
    ~3.4s, which read as "you shortened my video and nowhere does it say
    that's allowed." Removed outright per explicit user ask -- the clip's
    start is never touched automatically anymore, only `_parse_end_cut_hint`
    (an explicit ask) can trim anything at all.
    """
    box = detect_content_box(input_path)
    box = _verify_and_refine_content_box(input_path, box)
    redactions = auto_detect_redactions(input_path, box, hint=idea)
    lines, bottom_lines = generate_joke_lines(input_path, box, idea)

    # Explicit "coupe a Ns" ask in the idea text (2026-08-20 user ask, and a
    # same-day correction after the first live test: the user's mental model
    # is "play my clip from its own true start, stop it at N" -- NOT "keep
    # 0-Ns of source but still let other beats reorder/shift that window".
    # A hard user-given end boundary therefore also suppresses the teaser
    # beat (it PREPENDS a preview snippet pulled from later in the window,
    # which visibly delays the clip's real opening even though no frames
    # are technically dropped -- the first live test showed exactly this
    # and read to the user as "you shortened the start"). That's a judgment
    # call this module makes FOR the user; an explicit cut is the user
    # making the call themselves, so it wins outright rather than stacking
    # with it -- see _parse_end_cut_hint's docstring.
    end_cut = _parse_end_cut_hint(idea)
    if end_cut is not None:
        teaser = None
        logger.info("format18: explicit end-cut requested -- skipping the teaser so the "
                     "clip plays from its own true start")
    else:
        teaser = detect_twist_moment(input_path, box) if allow_teaser else None

    # Isolate the label-freeze trigger check from a verbatim caption's own
    # wording (2026-08-13 fix, see _wants_label_treatment's docstring) -- a
    # verbatim caption's own text must never be able to accidentally opt
    # into this beat just by coincidentally containing one of its trigger
    # phrases. No-op (label_idea stays == idea) whenever verbatim mode
    # wasn't used, i.e. the entire LLM-authored path below.
    verbatim_text = _extract_verbatim_caption(idea) if idea else None
    label_idea = idea.replace(verbatim_text, "") if verbatim_text else idea
    label_freeze = plan_label_treatment(input_path, box, lines) if _wants_label_treatment(label_idea) else None

    trim_end = None
    if end_cut is not None:
        src_dur = _duration_ffprobe(input_path)
        if end_cut < src_dur - 0.1:
            trim_end = max(end_cut, 1.0)
            logger.info(f"format18: explicit end-cut requested, keeping 0-{trim_end:.1f}s")
            if label_freeze and label_freeze["freeze_start"] > trim_end:
                logger.info("format18: dropping label freeze -- its moment falls past the requested cut")
                label_freeze = None
        else:
            logger.info(
                f"format18: requested end-cut ({end_cut:.1f}s) is at/after the clip's own "
                f"length ({src_dur:.1f}s), ignoring"
            )

    build_config = {
        "output_name": output_name or "white_card_reaction",
        "input_path": input_path,
        "crop_box": list(box),
        "keep_audio": keep_audio,
        "lines": lines,
        "bottom_lines": bottom_lines,
        "redactions": redactions,
        "teaser": teaser,
        "label_freeze": label_freeze,
    }
    trim_cfg = {}
    if trim_end is not None:
        trim_cfg["end"] = trim_end
    if trim_cfg:
        build_config["trim"] = trim_cfg
    path = build_white_card_reaction(build_config)

    if ctx is not None and not ctx.is_default:
        from modules.users import tag_and_relocate
        path = tag_and_relocate(path, ctx)
    _save_format18_script(path, lines, ctx, bottom_lines)

    return path
