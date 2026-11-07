"""
Format long3 assembler -- "Le Simulateur de Ruine" (10-11 minute ruin-and-
rebuild monologue). One well-known fictional character, voiced by a single
archetype TTS voice (see voice_bank -- NEVER a real actor/performer's own
voice), narrates first-person losing everything and rebuilding from 0 euros
in 2026. Fully automatable, single continuous voice -- no manual audio panel
needed, unlike long1.

Visual: a real fetched photo (per beat's own "visual_query", see
script_generator._generate_long3_script) as a blurred backdrop behind an
animated net-worth line chart that GROWS beat by beat across the whole video
(each beat's background PNG redraws the curve up to that beat's share of the
timeline, rather than a full 30fps frame-by-frame render for the whole video,
which would be both far slower to encode and unnecessary here -- the story
already provides the pacing). A signed-log transform keeps both the
multi-billion-dollar starting point and the thousands-scale recovery visible
on the same axis (this is an illustrative, stylized chart, not a literal
financial plot). A flat gradient is only ever a fallback when no photo could
be fetched for a beat (user feedback, 2026-07-27: an all-chart, no-photo
video "il n'y a pas d'image ou video en fond ... ca manque de fond"). Same
A/V-sync-robust per-clip-mux + concat-demuxer pattern as long1/long2.
"""

import logging
import math
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import LONG_FORMAT_HEIGHT, LONG_FORMAT_WIDTH
from modules.assembler import _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text, _FONT_LIGHT, _FONT_REGULAR, _FONT_SEMIBOLD, _TEXT_COLOR
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

_SECTION_ORDER = ["shock", "survival", "escalation", "lesson"]
_CHAPTER_CARD_DUR = 3.0
_ACCENT = (214, 184, 133, 255)   # champagne -- shared brand accent
_CARD_BG_DARK  = (10, 10, 12)
_CARD_BG_LIGHT = (46, 46, 52)

_CHART_X0, _CHART_X1 = 160, LONG_FORMAT_WIDTH - 160
_CHART_Y_TOP, _CHART_Y_BOTTOM = 240, LONG_FORMAT_HEIGHT - 280
_SAMPLES_PER_SEG = 30


def build_beats(script: dict) -> list[dict]:
    """Flat, ordered list of {section, index, text} across all 4 sections --
    single source of truth for both per-beat TTS synthesis and chart reveal
    fractions (this format's narration is fully synthesized, so there's no
    separate audio-matching step the way long1 needs one)."""
    beats: list[dict] = []
    sections = script.get("sections") or {}
    for sec_id in _SECTION_ORDER:
        sec = sections.get(sec_id) or {}
        for i, b in enumerate(sec.get("beats") or []):
            text = (b.get("text") or "").strip()
            if text:
                beats.append({
                    "section": sec_id, "index": i, "text": text,
                    "visual_query": (b.get("visual_query") or text).strip(),
                })
    return beats


def _visual_y(value: float) -> float:
    """Signed log transform -- keeps a multi-billion-dollar starting point and
    a thousands-scale recovery both legible on the same axis. Illustrative,
    not a literal financial plot."""
    sign = 1.0 if value >= 0 else -1.0
    return sign * math.log10(1 + abs(value))


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


def _build_chart_curve(milestones: list[dict]) -> tuple[list[tuple[float, float]], list[int]]:
    """Full smoothed curve through every milestone + the curve-sample index
    where each milestone lands (used to pick which label is "latest crossed"
    at a given reveal fraction)."""
    n = len(milestones)
    values = [_visual_y(float(m["value"])) for m in milestones]
    vmin, vmax = min(values), max(values)
    vrange = (vmax - vmin) or 1.0
    ctrl = []
    for i, v in enumerate(values):
        x = _CHART_X0 + (i / max(1, n - 1)) * (_CHART_X1 - _CHART_X0)
        yf = (v - vmin) / vrange
        y = _CHART_Y_BOTTOM - yf * (_CHART_Y_BOTTOM - _CHART_Y_TOP)
        ctrl.append((x, y))
    samples = _catmull_rom(ctrl, _SAMPLES_PER_SEG)
    milestone_idx = [min(len(samples) - 1, i * _SAMPLES_PER_SEG) for i in range(n)]
    return samples, milestone_idx


def _render_chart_beat(
    curve: list[tuple[float, float]], milestone_idx: list[int], milestones: list[dict],
    reveal_frac: float, character_name: str, avatar_path: Path | None, out_path: Path,
    bg_photo: Path | None = None,
) -> None:
    if bg_photo and Path(bg_photo).exists():
        try:
            img = _blurred_backdrop(Image.open(bg_photo).convert("RGB")).convert("RGBA")
        except Exception:
            img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")
    else:
        img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")
    draw = ImageDraw.Draw(img)

    draw.line([(_CHART_X0, _CHART_Y_BOTTOM), (_CHART_X1, _CHART_Y_BOTTOM)], fill=(90, 90, 94, 140), width=2)

    try:
        title_font = ImageFont.truetype(_FONT_SEMIBOLD, size=40)
        label_font = ImageFont.truetype(_FONT_LIGHT, size=34)
    except OSError:
        title_font = label_font = ImageFont.truetype(_FONT_REGULAR, size=40)

    _soft_text(img, (_CHART_X0, 100), "NET WORTH", title_font, _TEXT_COLOR, multiline=False)

    n_show = max(2, int(round(len(curve) * min(1.0, reveal_frac))))
    shown = curve[:n_show]
    if len(shown) > 1:
        draw.line(shown, fill=_ACCENT, width=5, joint="curve")
    tip = shown[-1]
    draw.ellipse([tip[0] - 11, tip[1] - 11, tip[0] + 11, tip[1] + 11], fill=_ACCENT)

    latest_label = None
    for i, idx in enumerate(milestone_idx):
        if idx <= n_show - 1:
            latest_label = milestones[i].get("label", "")
    if latest_label:
        wrapped = textwrap.fill(latest_label, width=26, break_long_words=False)
        lbbox = draw.textbbox((0, 0), wrapped, font=label_font, align="left", spacing=8)
        lx = min(max(_CHART_X0, tip[0] + 24), _CHART_X1 - (lbbox[2] - lbbox[0]))
        ly = tip[1] - (lbbox[3] - lbbox[1]) - 28 if tip[1] > _CHART_Y_TOP + 80 else tip[1] + 24
        _soft_text(img, (lx, ly), wrapped, label_font, _TEXT_COLOR, align="left", spacing=8, stroke_width=2)

    if avatar_path and Path(avatar_path).exists():
        try:
            photo = Image.open(avatar_path).convert("RGB")
            size = 150
            photo = _cover_resize(photo, size, size)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            ring = size + 10
            framed = Image.new("RGBA", (ring, ring), (0, 0, 0, 0))
            ImageDraw.Draw(framed).ellipse((0, 0, ring, ring), fill=(255, 255, 255, 230))
            framed.paste(photo, (5, 5), mask)
            img.alpha_composite(framed, (LONG_FORMAT_WIDTH - ring - 60, 60))
        except Exception:
            pass

    img.convert("RGB").save(str(out_path), quality=92)


def _render_chapter_card(label: str, avatar_path: Path | None, out_path: Path) -> Image.Image:
    if avatar_path and Path(avatar_path).exists():
        try:
            photo = Image.open(avatar_path).convert("RGB")
            img = _blurred_backdrop(photo).convert("RGBA")
        except Exception:
            img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")
    else:
        img = _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).convert("RGBA")

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=64)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=64)
    wrapped = textwrap.fill(label, width=30, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (LONG_FORMAT_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (LONG_FORMAT_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=3)
    return img.convert("RGB")


def assemble_long3(script, visuals_result, voice_result, timestamps_result, music_path):
    """Ruin-and-rebuild monologue assembler -- ignores visuals_result/voice_result/
    timestamps_result (synthesizes its own single-voice archetype TTS narration)."""
    character_name = script.get("character_name") or script["subject"]
    character_context = script.get("character_context", "")
    milestones = script.get("net_worth_milestones") or []
    beats = build_beats(script)
    if not beats:
        raise RuntimeError("long3: no usable narration beats in script")
    if len(milestones) < 2:
        raise RuntimeError("long3: fewer than 2 net_worth_milestones -- can't draw a chart")

    profile = script.get("voice") or {}
    voice_id = select_voice(
        gender=profile.get("gender", "male"), accent=profile.get("accent", ""),
        age_register=profile.get("age_register", ""), tone=profile.get("tone") or [],
    )

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_long3"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    avatar_path = tmp_dir / "avatar.jpg"
    # Defensive truncation -- the prompt asks for a short franchise name, but a
    # verbose model output here would otherwise turn the search query into a full
    # paragraph (observed 2026-07-27: killed every image search result for it).
    context_short = " ".join(character_context.split()[:6])
    query = f"{character_name} {context_short}".strip()
    avatar = avatar_path if fetch_character_image(query, avatar_path, name=character_name) else None

    curve, milestone_idx = _build_chart_curve(milestones)

    import asyncio

    clips: list[Path] = []
    total_beats = len(beats)
    beats_done = 0

    for sec_id in _SECTION_ORDER:
        sec_beats = [b for b in beats if b["section"] == sec_id]
        if not sec_beats:
            continue

        if sec_id != "shock":
            sec_label = (script.get("sections", {}).get(sec_id) or {}).get("label", sec_id)
            sec_photo_hits = _fetch_beat_images(sec_beats[0]["visual_query"], 1, tmp_dir, f"{sec_id}_chapter_bg")
            chapter_bg = sec_photo_hits[0] if sec_photo_hits else avatar
            chapter_img = _render_chapter_card(sec_label, chapter_bg, tmp_dir / f"{sec_id}_chapter_src.png")
            chapter_src = tmp_dir / f"{sec_id}_chapter_src.png"
            chapter_img.save(str(chapter_src), quality=92)
            # .wav, no codec override -- _AUDIO_ARGS forces AAC, and AAC-in-an-.mp3-container
            # is a codec/muxer mismatch ffmpeg rejects outright (observed 2026-07-27). This
            # file is only ever an INPUT to _render_zoom_image_clip below, which re-encodes it
            # to AAC itself when muxing into the final mp4, so no codec needs forcing here.
            silence_path = tmp_dir / f"{sec_id}_silence.wav"
            _run_ffmpeg([
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{_CHAPTER_CARD_DUR:.3f}", str(silence_path),
            ])
            chapter_clip = tmp_dir / f"{sec_id}_chapter.mp4"
            _render_zoom_image_clip(chapter_src, silence_path, _CHAPTER_CARD_DUR, chapter_clip)
            clips.append(chapter_clip)

        for b in sec_beats:
            audio_path = tmp_dir / f"{sec_id}_beat_{b['index']}.mp3"
            asyncio.run(_synthesize(b["text"], str(audio_path), voice_id))
            dur = probe_duration(str(audio_path))
            if dur <= 0:
                continue
            beats_done += 1
            reveal_frac = beats_done / total_beats
            beat_photo_hits = _fetch_beat_images(b["visual_query"], 1, tmp_dir, f"{sec_id}_bg_{b['index']}")
            bg_photo = beat_photo_hits[0] if beat_photo_hits else None
            chart_png = tmp_dir / f"{sec_id}_chart_{b['index']}.png"
            _render_chart_beat(curve, milestone_idx, milestones, reveal_frac, character_name, avatar, chart_png, bg_photo=bg_photo)
            clip_path = tmp_dir / f"{sec_id}_clip_{b['index']}.mp4"
            _render_zoom_image_clip(chart_png, audio_path, dur, clip_path)
            clips.append(clip_path)

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    av_concat = tmp_dir / "av_concat.mp4"
    _concat_av_clips(clips, av_concat)
    total_dur = probe_duration(str(av_concat))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_long3_{slug}.mp4"

    _mux_with_music(av_concat, music_path, total_dur, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"long3: video ready | {character_name!r} | {len(clips)} clips | "
                f"{total_dur / 60:.1f} min | {size_mb:.1f} MB -> {out}")
    return out
