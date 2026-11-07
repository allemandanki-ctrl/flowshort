"""
Format long2 assembler -- "La Reunion Secrete" (15-18 minute mastermind
crossover). 4 well-known fictional characters debate around a dark cinematic
table, each voiced by an archetype TTS voice (see voice_bank -- NEVER a real
actor/performer's own voice), bookended by a fixed neutral narrator voice.
Fully automatable -- unlike long1, no real human voice is required.

Visual: a blurred real-photo backdrop (the active speaker's own photo for
debate turns -- reads like a spotlight on whoever's talking; one fetched
"secret meeting" mood photo, reused, for narrator turns) with 4 circular
real-photo portraits at fixed positions on top -- the currently-speaking
character is enlarged and ringed with a glow, the other three are
dimmed/desaturated (like waiting their turn at a real table). A flat gradient
is only ever a fallback when no photo could be fetched (user feedback,
2026-07-27: an all-gradient video "y'a pas d'fond ni rien"). Each turn's full
line is split into short sentence-grouped chunks (~<=22 words, see
_split_into_cards) and each chunk gets its OWN short clip instead of the
whole turn as one on-screen paragraph (same feedback: "des mega paves a
l'ecran c'est horrible").

Same A/V-sync-robust pattern as long1: each clip's video+audio are muxed
together in ONE ffmpeg call (with a slow Ken Burns zoom, see
long1._render_zoom_image_clip), then every clip is joined with the concat
DEMUXER (-c copy, identical codec params by construction) -- see long1.py's
module docstring for why this matters (a separately-concatenated video track
and audio track can silently drift apart over many segments).
"""

import logging
import re
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from config import LONG_FORMAT_HEIGHT, LONG_FORMAT_WIDTH
from modules.assembler import _VIDEOS_DIR, _slugify, _soft_text, _FONT_LIGHT, _FONT_REGULAR, _FONT_SEMIBOLD, _TEXT_COLOR
from modules.assembler.character_image import fetch_character_image
from modules.assembler.dialogue import _synthesize
from modules.assembler.long1 import (
    _blurred_backdrop, _concat_av_clips, _fetch_beat_images, _flat_gradient,
    _mux_with_music, _render_zoom_image_clip,
)
from modules.assembler.red_phone import _cover_resize
from modules.assembler.voice_bank import select_voice
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

_NARRATOR_VOICE = "kokoro:bm_lewis"   # fixed brand voice, distinct from format14's
                                      # interrogator (bm_george) and format15's announcer (am_adam)
_TABLE_POSITIONS = [(480, 340), (1440, 340), (480, 800), (1440, 800)]
_PORTRAIT_R_ACTIVE = 150
_PORTRAIT_R_DIM = 92
_ACCENT = (214, 184, 133, 255)   # champagne -- shared brand accent
_CARD_BG_DARK  = (10, 10, 12)
_CARD_BG_LIGHT = (46, 46, 52)

_SPEAKER_PREFIX_RE = re.compile(r"^\s*(narrator|char\s*_?\d)\s*[:\-]\s*", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_MAX_WORDS_PER_CARD = 22   # a whole round/narrator passage as one on-screen block read as a
                           # "wall of text" (user feedback, 2026-07-27: "des mega paves a
                           # l'ecran c'est horrible") -- split into sentence-grouped chunks
                           # short enough to read comfortably in one card each.


def _clean(text: str) -> str:
    return _SPEAKER_PREFIX_RE.sub("", text or "").strip()


def _split_into_cards(text: str) -> list[str]:
    """Group a turn's sentences into short chunks (~<= _MAX_WORDS_PER_CARD words each),
    never splitting mid-sentence, so each on-screen card shows a readable snippet instead
    of a whole multi-sentence paragraph at once."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for sent in sentences:
        n = len(sent.split())
        if current and current_words + n > _MAX_WORDS_PER_CARD:
            chunks.append(" ".join(current))
            current, current_words = [], 0
        current.append(sent)
        current_words += n
    if current:
        chunks.append(" ".join(current))
    return chunks


def build_turns(script: dict) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Speaker/text pairs in spoken order: narrator (intro), the debate rounds
    (matched to char_0..char_3 by name, case/whitespace-insensitive), narrator
    (conclusion). Returns (turns, name_to_key) -- name_to_key is also what the
    FlowShort panel needs to label each character's turns."""
    characters = script.get("characters") or []
    name_to_key = {c["name"].strip().lower(): f"char_{i}" for i, c in enumerate(characters)}

    turns: list[tuple[str, str]] = []
    intro = _clean(script.get("narrator_intro"))
    if intro:
        turns.append(("narrator", intro))

    for r in script.get("rounds") or []:
        speaker_name = (r.get("speaker") or "").strip().lower()
        key = name_to_key.get(speaker_name)
        if key is None:
            logger.warning(f"long2: round speaker {r.get('speaker')!r} doesn't match any of "
                            f"{[c['name'] for c in characters]} -- skipping this turn")
            continue
        text = _clean(r.get("text"))
        if text:
            turns.append((key, text))

    conclusion = _clean(script.get("narrator_conclusion"))
    if conclusion:
        turns.append(("narrator", conclusion))

    return turns, name_to_key


def _build_table_bg(bg_photo: Path | None = None) -> Image.Image:
    """Blurred real photo backdrop when one is available -- a flat gradient alone
    read as empty/lifeless (user feedback, 2026-07-27: "y'a pas d'fond ni rien").
    Falls back to the flat gradient only when no photo could be fetched."""
    if bg_photo and Path(bg_photo).exists():
        try:
            return _blurred_backdrop(Image.open(bg_photo).convert("RGB"))
        except Exception:
            pass
    return _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK)


def _paste_portrait(canvas: Image.Image, avatar_path: Path | None, cx: int, cy: int, active: bool) -> None:
    """Circular real photo, dimmed+desaturated unless this character is the
    one currently speaking -- reads as 'waiting their turn' at the table."""
    radius = _PORTRAIT_R_ACTIVE if active else _PORTRAIT_R_DIM
    img = None
    if avatar_path and Path(avatar_path).exists():
        try:
            img = Image.open(avatar_path).convert("RGB")
        except Exception:
            img = None
    if img is None:
        img = Image.new("RGB", (radius * 2, radius * 2), (36, 36, 40))
    else:
        img = _cover_resize(img, radius * 2, radius * 2)
        if not active:
            img = ImageEnhance.Brightness(img).enhance(0.42)
            img = ImageEnhance.Color(img).enhance(0.45)
        else:
            img = ImageEnhance.Contrast(img).enhance(1.08)

    mask = Image.new("L", (radius * 2, radius * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, radius * 2, radius * 2), fill=255)

    if active:
        ring = radius * 2 + 24
        glow = Image.new("RGBA", (ring, ring), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse((3, 3, ring - 3, ring - 3), outline=_ACCENT, width=6)
        canvas.alpha_composite(glow, (cx - ring // 2, cy - ring // 2))

    canvas.paste(img, (cx - radius, cy - radius), mask)


def _render_narrator_card(text: str, out_path: Path, audio_path: Path, duration: float, bg_photo: Path | None = None) -> None:
    img = _build_table_bg(bg_photo).convert("RGBA")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=54)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=54)
    wrapped = textwrap.fill(text, width=42, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (LONG_FORMAT_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (LONG_FORMAT_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=2)
    png = out_path.with_suffix(".png")
    img.convert("RGB").save(str(png), quality=92)
    _render_zoom_image_clip(png, audio_path, duration, out_path)
    png.unlink(missing_ok=True)


def _render_debate_card(
    speaker_key: str, speaker_name: str, text: str, characters: list[dict], avatars: dict[str, Path | None],
    out_path: Path, audio_path: Path, duration: float,
) -> None:
    # The currently-speaking character's own photo, blurred full-bleed, instead of a flat
    # gradient behind everyone -- reads like a spotlight on whoever's talking (2026-07-27
    # fix, see _build_table_bg).
    img = _build_table_bg(avatars.get(speaker_key)).convert("RGBA")
    for i in range(len(characters)):
        key = f"char_{i}"
        cx, cy = _TABLE_POSITIONS[i]
        _paste_portrait(img, avatars.get(key), cx, cy, active=(key == speaker_key))

    draw = ImageDraw.Draw(img)
    try:
        name_font = ImageFont.truetype(_FONT_SEMIBOLD, size=38)
        body_font = ImageFont.truetype(_FONT_LIGHT, size=42)
    except OSError:
        name_font = body_font = ImageFont.truetype(_FONT_REGULAR, size=38)

    label = speaker_name.upper()
    lbbox = draw.textbbox((0, 0), label, font=name_font)
    lx = (LONG_FORMAT_WIDTH - (lbbox[2] - lbbox[0])) // 2
    ly = int(LONG_FORMAT_HEIGHT * 0.56)
    _soft_text(img, (lx, ly), label, name_font, _ACCENT, multiline=False, stroke_width=2)

    wrapped = textwrap.fill(text, width=48, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=body_font, align="center", spacing=12)
    bx = (LONG_FORMAT_WIDTH - (bbox[2] - bbox[0])) // 2
    by = ly + (lbbox[3] - lbbox[1]) + 34
    _soft_text(img, (bx, by), wrapped, body_font, _TEXT_COLOR, align="center", spacing=12, stroke_width=2)

    png = out_path.with_suffix(".png")
    img.convert("RGB").save(str(png), quality=92)
    _render_zoom_image_clip(png, audio_path, duration, out_path)
    png.unlink(missing_ok=True)


def assemble_long2(script, visuals_result, voice_result, timestamps_result, music_path):
    """Roundtable-debate assembler -- ignores visuals_result/voice_result/timestamps_result
    (synthesizes its own per-turn archetype-TTS audio)."""
    characters = (script.get("characters") or [])[:4]
    turns, name_to_key = build_turns(script)
    if not turns:
        raise RuntimeError("long2: no usable turns in script (empty narrator lines and/or no rounds matched)")

    voices = {"narrator": _NARRATOR_VOICE}
    for i, c in enumerate(characters):
        profile = c.get("voice") or {}
        voices[f"char_{i}"] = select_voice(
            gender=profile.get("gender", "male"), accent=profile.get("accent", ""),
            age_register=profile.get("age_register", ""), tone=profile.get("tone") or [],
        )

    subject = script["subject"]
    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_long2"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    avatars: dict[str, Path | None] = {}
    for i, c in enumerate(characters):
        avatar_path = tmp_dir / f"char_{i}_avatar.jpg"
        query = f"{c.get('name', '')} {c.get('context', '')}".strip()
        avatars[f"char_{i}"] = avatar_path if fetch_character_image(query, avatar_path, name=c.get("name")) else None

    # One atmospheric backdrop for narrator turns, fetched once and reused -- narrator
    # lines aren't tied to a specific character's photo the way debate turns are.
    theme = script.get("theme", "")
    narrator_bg_hits = _fetch_beat_images(
        f"dark cinematic round table secret meeting {theme}".strip(), 1, tmp_dir, "narrator_bg",
    )
    narrator_bg = narrator_bg_hits[0] if narrator_bg_hits else None

    import asyncio

    clips: list[Path] = []
    clip_i = 0
    for speaker, text in turns:
        for chunk in _split_into_cards(text):
            audio_path = tmp_dir / f"turn_{clip_i}.mp3"
            asyncio.run(_synthesize(chunk, str(audio_path), voices[speaker]))
            dur = probe_duration(str(audio_path))
            if dur <= 0:
                continue
            clip_path = tmp_dir / f"clip_{clip_i}.mp4"
            if speaker == "narrator":
                _render_narrator_card(chunk, clip_path, audio_path, dur, bg_photo=narrator_bg)
            else:
                speaker_name = characters[int(speaker.split("_")[1])].get("name", speaker)
                _render_debate_card(speaker, speaker_name, chunk, characters, avatars, clip_path, audio_path, dur)
            clips.append(clip_path)
            clip_i += 1

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    av_concat = tmp_dir / "av_concat.mp4"
    _concat_av_clips(clips, av_concat)
    total_dur = probe_duration(str(av_concat))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(subject)
    out  = _VIDEOS_DIR / f"{ts}_long2_{slug}.mp4"

    _mux_with_music(av_concat, music_path, total_dur, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"long2: video ready | {[c.get('name') for c in characters]} | {len(clips)} turns | "
                f"{total_dur / 60:.1f} min | {size_mb:.1f} MB -> {out}")
    return out
