"""
Format 17 assembler -- "Weekly Finance & Markets Roundup". A hard-cut
slideshow: real news headline -> real fetched image (company logo or a
photo of the person the story is about) -> next headline. No voice-over --
on-screen phrase + real image per item, minimal background music picked by
the generic music_library flow (this format does NOT generate its own audio
track, unlike format9/11/14/15/16). No "Bons Plans" section (removed per
user feedback, 2026-07-27) -- news items only.

Content itself (the real headlines) comes from modules.script_generator's
_generate_format17_script, grounded in modules.news_fetcher's RSS digest --
this module only renders what's already in the script, it doesn't fetch news.
"""

import logging
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text,
    _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD, _TEXT_COLOR,
)
from modules.assembler.dilemma import _is_stock_agency_url, _has_visible_watermark, _is_relevant_image
from modules.assembler.red_phone import _cover_resize, _flat_gradient

logger = logging.getLogger(__name__)

_HOOK_DUR   = 3.0
_ITEM_DUR   = 5.5   # was 4.0 -- user feedback: slideshow moved too fast to read

_NEWS_ACCENT = (214, 184, 133, 255)   # champagne -- existing brand accent
_CARD_BG_DARK  = (18, 18, 20)
_CARD_BG_LIGHT = (54, 55, 60)

_IMG_MIN_SIZE = 300   # lowered from 400 -- clean logo images are often smaller than stock photos
_FEATURE_BOX_W_FRAC = 0.72
_FEATURE_BOX_H_FRAC = 0.40


def _fetch_item_image(query: str, out_path: Path) -> Path | None:
    """Real, non-watermarked image for one news/bons-plans item -- same
    stock-agency blocklist + vision watermark check as format9's pill photo
    pool (see modules.assembler.dilemma), just not cached into a permanent
    pool since every query here is different per video."""
    from io import BytesIO

    import requests
    from ddgs import DDGS

    if not query.strip():
        return None
    try:
        results = list(DDGS().images(query, max_results=10))
    except Exception as e:
        logger.warning(f"finance_roundup: image search failed for {query!r}: {e}")
        return None

    for r in results:
        url = r.get("image")
        source_url = r.get("url") or ""
        if not url or _is_stock_agency_url(url) or _is_stock_agency_url(source_url):
            continue
        try:
            w, h = int(r.get("width") or 0), int(r.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        if w < _IMG_MIN_SIZE or h < _IMG_MIN_SIZE:
            continue
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            Image.open(BytesIO(resp.content)).verify()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            img.save(out_path, format="JPEG", quality=90)
        except Exception as e:
            logger.debug(f"finance_roundup: skipping image candidate {url!r}: {e}")
            continue
        if _has_visible_watermark(out_path):
            out_path.unlink(missing_ok=True)
            continue
        if not _is_relevant_image(out_path, query):
            logger.debug(f"finance_roundup: rejecting off-topic image candidate {url!r} for query {query!r}")
            out_path.unlink(missing_ok=True)
            continue
        return out_path
    return None


def _blurred_backdrop(photo: Image.Image) -> Image.Image:
    """Full-bleed cover-cropped + heavily blurred + darkened copy of `photo` --
    a backdrop with the item's own colors/mood instead of an empty flat gradient,
    without literally cover-cropping (and mangling) a logo or a person's face."""
    bg = _cover_resize(photo, VIDEO_WIDTH, VIDEO_HEIGHT)
    bg = bg.filter(ImageFilter.GaussianBlur(46))
    arr = np.asarray(bg).astype(np.float32) * 0.42
    return Image.fromarray(arr.astype(np.uint8), mode="RGB")


def _contain_resize(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Scale (up or down) so the WHOLE image fits within max_w x max_h, preserving
    aspect ratio -- unlike _cover_resize, never crops. A cropped logo reads as
    broken; a contained one reads as designed."""
    ratio = min(max_w / img.width, max_h / img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    return img.resize((new_w, new_h))


def _paste_featured_image(base: Image.Image, photo: Image.Image, top_frac: float) -> None:
    """Contained image + soft drop shadow, centered horizontally, top edge at
    `top_frac` of the frame height -- the shared "featured logo/photo" treatment
    used by both the hook card and the news/bons-plans item cards."""
    box_w = int(VIDEO_WIDTH * _FEATURE_BOX_W_FRAC)
    box_h = int(VIDEO_HEIGHT * _FEATURE_BOX_H_FRAC)
    fg = _contain_resize(photo, box_w, box_h).convert("RGBA")

    cx = (VIDEO_WIDTH - fg.width) // 2
    cy = int(VIDEO_HEIGHT * top_frac)

    pad = 30
    shadow = Image.new("RGBA", (fg.width + pad * 2, fg.height + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad, fg.width + pad, fg.height + pad], radius=20, fill=(0, 0, 0, 150)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(20))
    base.alpha_composite(shadow, (cx - pad, cy - pad))
    base.alpha_composite(fg, (cx, cy))


def _render_hook_card(text: str, out_path: Path, image_path: Path | None = None) -> None:
    """Title card -- used for the opening hook and the 'Bons Plans' header.
    With a real fetched image (see hook_image_query in the script), shows it
    blurred full-bleed behind the text instead of an empty flat gradient."""
    if image_path and image_path.exists():
        try:
            photo = Image.open(image_path).convert("RGB")
            img = _blurred_backdrop(photo).convert("RGBA")
        except Exception:
            img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")
    else:
        img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=76)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=76)
    wrapped = textwrap.fill(text, width=18, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=3)
    img.convert("RGB").save(str(out_path), quality=92)


def _render_item_card(phrase: str, image_path: Path | None, accent: tuple, out_path: Path) -> None:
    """Blurred-backdrop + a CONTAINED (not cropped) real logo/photo featured near the
    top, then the on-screen phrase in a caption band at the bottom -- a cropped
    full-bleed logo read as broken/ugly, this keeps the whole image intact."""
    photo = None
    if image_path and image_path.exists():
        try:
            photo = Image.open(image_path).convert("RGB")
        except Exception:
            photo = None

    img = (_blurred_backdrop(photo) if photo else _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK)).convert("RGBA")
    if photo:
        _paste_featured_image(img, photo, top_frac=0.16)

    # Darkened band across the lower third -- caption legibility regardless of backdrop.
    scrim = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    scrim_top = int(VIDEO_HEIGHT * 0.66)
    for y in range(scrim_top, VIDEO_HEIGHT):
        f = (y - scrim_top) / (VIDEO_HEIGHT - scrim_top)
        sdraw.line([(0, y), (VIDEO_WIDTH, y)], fill=(6, 6, 8, int(225 * f)))
    img.alpha_composite(scrim)

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=52)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=52)
    wrapped = textwrap.fill(phrase, width=26, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="left", spacing=12)
    x = 64
    y = VIDEO_HEIGHT - (bbox[3] - bbox[1]) - 120

    draw.rectangle([x, y - 26, x + 64, y - 14], fill=accent)
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="left", spacing=12, stroke_width=2)

    # Points viewers to the description for the actual detail (numbers, source, why it
    # matters) -- the on-screen phrase is deliberately short, this is what tells the
    # viewer there's more and drives them to open/read the description.
    try:
        sub_font = ImageFont.truetype(_FONT_LIGHT, size=32)
    except OSError:
        sub_font = ImageFont.truetype(_FONT_REGULAR, size=32)
    sub_y = y + (bbox[3] - bbox[1]) + 16
    _soft_text(img, (x, sub_y), "(read description)", sub_font, (200, 200, 205, 255),
               multiline=False, stroke_width=1)

    img.convert("RGB").save(str(out_path), quality=92)


def _slideshow_cmd(card_durs: list[tuple[Path, float]], music_path: str | None, output_path: str) -> list[str]:
    """Plain hard-cut concat -- a fast-paced news rundown reads better as
    punchy cuts than soft fades between totally different real photos."""
    cmd = ["ffmpeg", "-y"]
    for png, dur in card_durs:
        cmd += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png)]
    music_idx = len(card_durs)
    if music_path:
        cmd += ["-i", music_path]

    filters = [
        f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,fps={VIDEO_FPS},format=yuv420p[s{i}]"
        for i in range(len(card_durs))
    ]
    concat_in = "".join(f"[s{i}]" for i in range(len(card_durs)))
    filters.append(f"{concat_in}concat=n={len(card_durs)}:v=1:a=0[vout]")

    cmd += ["-filter_complex", ";\n".join(filters), "-map", "[vout]"]

    total_dur = sum(d for _, d in card_durs)
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


def assemble_format17(script, visuals_result, voice_result, timestamps_result, music_path):
    """News-slideshow assembler -- ignores visuals_result/voice_result/timestamps_result
    (fetches its own real images per item, see _fetch_item_image)."""
    hook_text  = script.get("hook_text") or "This week's finance & markets news"
    hook_image_query = script.get("hook_image_query") or "New York Stock Exchange trading floor"
    news_items = script.get("news_items") or []

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f17"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    card_durs: list[tuple[Path, float]] = []

    hook_img_path = tmp_dir / "hook_img.jpg"
    hook_fetched = _fetch_item_image(hook_image_query, hook_img_path)
    hook_png = tmp_dir / "hook.png"
    _render_hook_card(hook_text, hook_png, image_path=hook_fetched)
    card_durs.append((hook_png, _HOOK_DUR))

    for i, item in enumerate(news_items):
        img_path = tmp_dir / f"news_img_{i}.jpg"
        fetched = _fetch_item_image(item.get("image_query") or item.get("phrase", ""), img_path)
        card_png = tmp_dir / f"news_card_{i}.png"
        _render_item_card(item.get("phrase", ""), fetched, _NEWS_ACCENT, card_png)
        card_durs.append((card_png, _ITEM_DUR))

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format17_{slug}.mp4"

    cmd = _slideshow_cmd(card_durs, music_path, str(out))
    total = sum(d for _, d in card_durs)
    logger.info(f"Assembling format17 (weekly finance roundup) | items={len(news_items)} "
                f"| total={total:.1f}s | music={'yes' if music_path else 'no'}")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out
