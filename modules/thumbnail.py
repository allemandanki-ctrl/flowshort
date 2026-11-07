"""
YouTube thumbnail generator for long-format compilations.

2-3 panel horizontal collage of darkened clip frames + a short bold white
headline derived from the title, landscape 1280x720 (YouTube standard) --
matches the reference style the user sent: darkened photo/video panels,
all-caps bold text, no logos/badges.
"""

import logging
import random
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

logger = logging.getLogger(__name__)

_WIDTH, _HEIGHT = 1280, 720
_FONT_IMPACT_PATH = r"C:\Windows\Fonts\impact.ttf"
_FONT_BOLD_PATH   = r"C:\Windows\Fonts\arialbd.ttf"

_DARKEN_FACTOR = 0.5   # 1.0 = untouched, lower = darker


def _generate_thumbnail_headline(case_subject: str, description: str) -> str:
    """One quick Claude call (the user's own subscription) for a punchy thumbnail
    headline -- more reliable than regex-extracting a number/phrase out of the
    case subject string, and cheap (single short call, small output)."""
    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude, ClaudeCliError
    prompt = (
        f"Case: {case_subject}\nContext: {description[:400]}\n\n"
        "Write a punchy YouTube thumbnail headline for this financial documentary video -- "
        "MAX 5 words, ALL CAPS, dramatic, a specific number/stat if one fits naturally "
        "(e.g. '$4.6 BILLION GONE', 'THE GENIUS TRAP', '44% LOST IN WEEKS'). "
        "Return ONLY the headline text, nothing else, no quotes."
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL, timeout=30)
        headline = raw.strip().strip('"').strip("'").upper()
        return headline[:40] if headline else case_subject.split("(")[0].strip().upper()[:40]
    except ClaudeCliError as e:
        logger.warning(f"thumbnail: headline generation failed ({e}), falling back to case subject")
        return case_subject.split("(")[0].strip().upper()[:40]


def build_long1_thumbnail(
    persona_name: str, case_subject: str, description: str, out_path: Path, tmp_dir: Path,
) -> Path | None:
    """
    long1 ("L'Autopsie Financiere") thumbnail: a real photo of the title-hook
    persona (see script_generator._generate_long1_script -- title-only, never
    the actual narration voice) on the right, a dark panel with a punchy
    headline on the left, landscape 1280x720. Falls back to a plain dark
    panel (no photo) if no usable photo of the persona was found -- never
    blocks thumbnail generation entirely.
    """
    from modules.assembler.character_image import fetch_character_image
    from modules.assembler.red_phone import _cover_resize

    tmp_dir.mkdir(parents=True, exist_ok=True)
    avatar_path = tmp_dir / "long1_thumb_persona.jpg"
    fetched = fetch_character_image(persona_name, avatar_path, name=persona_name) if persona_name else None

    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), (14, 14, 16))
    photo_w = int(_WIDTH * 0.56)
    if fetched:
        try:
            photo = Image.open(fetched).convert("RGB")
            photo = _cover_resize(photo, photo_w, _HEIGHT)
            photo = ImageEnhance.Contrast(photo).enhance(1.12)
            photo = ImageEnhance.Color(photo).enhance(1.08)
            canvas.paste(photo, (_WIDTH - photo_w, 0))
        except Exception as e:
            logger.warning(f"thumbnail: could not place persona photo ({e})")

    # Dark panel over the left ~64%, feathered into the photo's edge instead of a hard cut.
    overlay = Image.new("RGBA", (_WIDTH, _HEIGHT), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    photo_left = _WIDTH - photo_w
    feather_start = photo_left - 70
    feather_end = photo_left + 90
    for x in range(_WIDTH):
        if x <= feather_start:
            a = 235
        elif x >= feather_end:
            a = 0
        else:
            f = (x - feather_start) / (feather_end - feather_start)
            a = int(235 * (1 - f))
        odraw.line([(x, 0), (x, _HEIGHT)], fill=(6, 6, 8, a))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    headline = _generate_thumbnail_headline(case_subject, description)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(_FONT_IMPACT_PATH, size=92)
    except Exception:
        try:
            font = ImageFont.truetype(_FONT_BOLD_PATH, size=84)
        except Exception:
            font = ImageFont.load_default()

    wrapped = "\n".join(textwrap.wrap(headline, width=12))
    x, y = 60, 120
    outline_w = 6
    for dx in range(-outline_w, outline_w + 1, 3):
        for dy in range(-outline_w, outline_w + 1, 3):
            if dx == 0 and dy == 0:
                continue
            draw.multiline_text((x + dx, y + dy), wrapped, font=font, fill=(0, 0, 0), spacing=10)
    draw.multiline_text((x, y), wrapped, font=font, fill=(255, 255, 255), spacing=10)

    if persona_name:
        try:
            sub_font = ImageFont.truetype(_FONT_BOLD_PATH, size=34)
        except Exception:
            sub_font = ImageFont.load_default()
        sub_text = f"EXPLAINED BY {persona_name.upper()}"
        sub_y = _HEIGHT - 90
        for dx in range(-3, 4, 3):
            for dy in range(-3, 4, 3):
                if dx == 0 and dy == 0:
                    continue
                draw.text((x + dx, sub_y + dy), sub_text, font=sub_font, fill=(0, 0, 0))
        draw.text((x, sub_y), sub_text, font=sub_font, fill=(214, 184, 133))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), quality=92)
    logger.info(f"thumbnail: long1 thumbnail saved {out_path.name} (persona={persona_name!r}, headline={headline!r})")
    return out_path


def _extract_mid_frame(clip_path: Path, tmp_dir: Path, index: int) -> Path | None:
    dur_r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
        capture_output=True, text=True,
    )
    try:
        dur = float(dur_r.stdout.strip())
    except ValueError:
        dur = 2.0
    t = max(0.3, dur / 2)

    out = tmp_dir / f"thumb_frame_{index}.jpg"
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(clip_path),
         "-frames:v", "1", "-q:v", "3", str(out)],
        capture_output=True,
    )
    if r.returncode != 0 or not out.exists():
        logger.warning(f"thumbnail: frame extraction failed for {clip_path.name}")
        return None
    return out


def _cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Resize+crop to fill w x h exactly, cropping the overflow (no letterboxing)."""
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h = h
        new_w = int(h * src_ratio)
    else:
        new_w = w
        new_h = int(w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x0 = (new_w - w) // 2
    y0 = (new_h - h) // 2
    return img.crop((x0, y0, x0 + w, y0 + h))


def _headline_from_title(title: str) -> str:
    """
    Short punchy phrase for the overlay -- not the full title (matches the
    reference examples: single word/short phrase like "TRANSFORM." or
    "CONSISTENCY", not the whole video title crammed on the image).
    """
    words = [w.strip(".,!?:;\"'") for w in title.split() if w.strip(".,!?:;\"'")]
    if not words:
        return "WATCH THIS"
    # Prefer a short leading chunk (1-3 words) so it reads at thumbnail size.
    for n in (1, 2, 3):
        candidate = " ".join(words[:n]).upper()
        if len(candidate) >= 6 or n == len(words):
            break
    else:
        candidate = " ".join(words[:2]).upper()
    if not candidate.endswith((".", "!", "?")) and len(candidate.split()) == 1:
        candidate += "."
    return candidate


def build_thumbnail(
    clip_paths: list[Path], title: str, out_path: Path, tmp_dir: Path, headline: str | None = None,
) -> Path | None:
    """
    Compose a landscape thumbnail from 2-3 clip frames (darkened, side by
    side) with a bold white headline. Returns out_path on success, None if
    no usable frames were found (caller should skip Zernio thumbnail upload).

    `headline`, when given, is used verbatim instead of being derived from
    `title` via _headline_from_title -- that helper always collapses to just
    the title's leading word(s) (by design, for long1's single-word style
    like "TRANSFORM."), which reads as meaningless out of context for a
    multi-word phrase a caller already generated on purpose (confirmed live:
    white_card_compilation's own multi-word headline kept getting chopped
    back down to one word). `title` is otherwise unused when `headline` is
    passed -- existing callers that don't pass it are unaffected.
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    n_panels = random.choice([2, 3]) if len(clip_paths) >= 3 else min(2, len(clip_paths))
    if n_panels < 1:
        return None

    chosen = random.sample(clip_paths, n_panels)
    panel_w = _WIDTH // n_panels
    canvas = Image.new("RGB", (_WIDTH, _HEIGHT), (10, 10, 10))

    placed = 0
    for i, clip in enumerate(chosen):
        frame_path = _extract_mid_frame(clip, tmp_dir, i)
        if frame_path is None:
            continue
        try:
            frame = Image.open(frame_path).convert("RGB")
        except Exception as e:
            logger.warning(f"thumbnail: could not open frame {frame_path} ({e})")
            continue
        x0 = i * panel_w
        w  = panel_w if i < n_panels - 1 else (_WIDTH - x0)
        panel = _cover_crop(frame, w, _HEIGHT)
        panel = ImageEnhance.Brightness(panel).enhance(_DARKEN_FACTOR)
        panel = ImageEnhance.Contrast(panel).enhance(1.08)
        canvas.paste(panel, (x0, 0))
        placed += 1

    if placed == 0:
        logger.warning("thumbnail: no frames could be extracted, skipping thumbnail")
        return None

    draw = ImageDraw.Draw(canvas)
    headline = headline or _headline_from_title(title)
    try:
        font = ImageFont.truetype(_FONT_IMPACT_PATH, size=110)
    except Exception:
        try:
            font = ImageFont.truetype(_FONT_BOLD_PATH, size=100)
        except Exception:
            font = ImageFont.load_default()

    wrapped = "\n".join(textwrap.wrap(headline, width=14)) if len(headline) > 16 else headline
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (_WIDTH - tw) / 2 - bbox[0]
    y = (_HEIGHT - th) / 2 - bbox[1]

    # Thick black outline (multi-pass stroke) then white fill -- keeps
    # legibility across any panel brightness, matches the reference style.
    outline_w = 6
    for dx in range(-outline_w, outline_w + 1, 2):
        for dy in range(-outline_w, outline_w + 1, 2):
            if dx == 0 and dy == 0:
                continue
            draw.multiline_text((x + dx, y + dy), wrapped, font=font, fill=(0, 0, 0), align="center")
    draw.multiline_text((x, y), wrapped, font=font, fill=(255, 255, 255), align="center")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), quality=92)
    logger.info(f"thumbnail: saved {out_path.name} ({n_panels} panel(s), headline={headline!r})")
    return out_path
