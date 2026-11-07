"""
Format 15 assembler -- "Duel Financier". Old-school fighting-game
structure: VS card -> 3 rounds (each opening with a "ROUND N / FIGHT!"
card, last one "FINAL ROUND / FIGHT!") where each character's line plays
over THEIR OWN real footage, then cuts to an arena shot of their
AI-generated fighting sprite landing (or missing) a hit with sound effects
and the HP bar updating live -> K.O. card -> winner/loser lines.

Three voice roles: "announcer" (fixed voice, no real person -- same
archetype-only reasoning as format14's interrogator), "a" and "b" (the two
characters, archetype-matched via voice_bank -- never the real actor's
voice, see BESOIN_TON_AVIS.md). The arena sprite is AI-generated art, not a
real photo -- the real photo is only used as the small HP-bar portrait.
"""

import asyncio
import logging
import math
import random
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, ASSETS_DIR, RESETWORTH_ASSETS_DIR
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text,
    _FONT_SEMIBOLD, _FONT_LIGHT, _TEXT_COLOR,
)
from modules.assembler.character_clip import fetch_character_clips
from modules.assembler.character_image import fetch_character_image
from modules.assembler.character_sprite import fetch_character_sprite, fetch_arena_backdrop
from modules.assembler.dialogue import _synthesize, _build_bg
from modules.assembler.sfx import (
    make_impact_sound, make_whoosh_sound, make_ko_sound,
    make_crowd_reaction_sound, make_crowd_boo_sound, mix_audio,
    stylize_announcer_audio, make_arena_music_bed,
)
from modules.assembler.voice_bank import select_voice
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

_ANNOUNCER_VOICE = "kokoro:am_adam"  # fixed brand voice, distinct from format14's interrogator (bm_george)
_VOICE_INPUT_ROOT = ASSETS_DIR / "voices" / "format15"

# User's own recorded announcer callouts ("Round 1, fight." / "Round 2,
# fight." / "Final round, fight.") -- real recordings of the user's own
# voice, pre-cut to clean segments (see assets/sfx/format15/). Used
# instead of the synthesized TTS line when present.
_SFX_DIR = ASSETS_DIR / "sfx" / "format15"
_ROUND_CALLOUTS = [_SFX_DIR / "round1_fight.mp3", _SFX_DIR / "round2_fight.mp3", _SFX_DIR / "final_round_fight.mp3"]
# User's own recorded hit sounds, rotated across landed hits for variety.
_USER_HIT_SOUNDS = [_SFX_DIR / "hit1.mp3", _SFX_DIR / "hit2.mp3"]
_HP_COLOR_A = (110, 210, 140, 255)
_HP_COLOR_B = (225, 95, 95, 255)
_HIT_DUR = 1.7  # full "hit-stop" segment length for ROUND 1 -- quick strike, then freeze on the impact
                # pose so it reads clearly. Was a flat 2.5s every single hit (~15s of frozen-pose time
                # cumulated across 6 hits/video) -- real retention data, 2026-08-26, showed viewers who
                # survived the intro still bailed hard, and repeated identical 2.5s freezes were a real
                # contributor. Later rounds shrink further -- see _hit_dur_for_round.
_HIT_DUR_MIN_FRACTION = 0.6  # the FINAL round's hit segments run at this fraction of _HIT_DUR
_IMPACT_AT_FRACTION = 0.2    # the punch always connects this fraction into whatever the hit duration is
_POV_CARD_DUR = 1.3  # opening flash card -- just long enough to register who's fighting, then move (see _render_pov_card)
_MUSIC_BED_GAIN_DB = -24.0  # heavily ducked -- present as background energy texture, never competing
                            # with dialogue. Real gap found 2026-08-26: assemble_format15 accepted a
                            # `music_path` argument and never used it anywhere -- the whole video ran on
                            # dead silence between lines/SFX.
_VS_DUR_FLOOR = 1.2         # was a flat 1.8s minimum regardless of how short the actual line was
_ROUND_DUR_FLOOR_FIRST = 1.1  # round 1's "ROUND 1 / FIGHT!" callout -- needs to register clearly
_ROUND_DUR_FLOOR_LATER = 0.7  # later rounds -- the audience already knows the drill, keep it snappy
                               # (was a flat 1.4s minimum on EVERY round card, announcer-only dead time)
_LANDED_LABELS = ["PUNCH!", "KICK!", "UPPERCUT!", "KNEE!", "THROW!"]
_MISSED_LABELS = ["BLOCKED!", "DODGED!", "MISSED!"]


def _clean(text: str) -> str:
    return re.sub(r"^\s*(announcer|character ?a|character ?b)\s*[:\-]\s*", "", (text or ""), flags=re.IGNORECASE).strip()


def build_turns(script: dict) -> list[tuple[str, str]]:
    """Speaker/text pairs for the lines that matter for MANUAL AUDIO
    matching/indexing (prepare_format15_voice.py, _find_external_audio) --
    announcer(pov_hook), 6 rounds of alternating a/b lines, announcer
    (winner_line), the LOSING character (loser_line). The VS/FIGHT lines
    are announcer-only fixed template lines synthesized separately and
    never manually recorded, so they're not part of this indexed list."""
    pov_hook = _clean(script.get("pov_hook"))
    rounds = script.get("rounds") or []
    winner = (script.get("winner") or "a").strip().lower()
    loser = "b" if winner == "a" else "a"
    winner_line = _clean(script.get("winner_line"))
    loser_line = _clean(script.get("loser_line"))

    turns: list[tuple[str, str]] = [("announcer", pov_hook)]
    for rnd in rounds:
        turns.append(("a", _clean(rnd.get("a_line"))))
        turns.append(("b", _clean(rnd.get("b_line"))))
    turns.append(("announcer", winner_line))
    turns.append((loser, loser_line))
    return turns


# ── Manual voice (same pattern as format14's interrogation.py) ─────────────

def voice_input_dir(subject: str) -> Path:
    return _VOICE_INPUT_ROOT / _slugify(subject)


_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def _resolve_combined_audio(folder: Path, tag: str, tmp_dir: Path) -> Path | None:
    """A character's audio may be spread across several separate uploads (see
    voice_split.find_numbered_recordings -- user feedback, 2026-07-27: "je
    prepare pas forcement tous les audios d'une traite") -- joins them into
    one continuous take before splitting."""
    from modules.assembler.voice_split import concat_recordings, find_numbered_recordings
    pieces = find_numbered_recordings(folder, tag)
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0]
    combined = tmp_dir / f"{tag}_combined_input.wav"
    concat_recordings(pieces, combined)
    logger.info(f"duel: joined {len(pieces)} recording piece(s) for character {tag!r}")
    return combined


def _find_external_audio(subject: str, turns: list[tuple[str, str]], tmp_dir: Path) -> dict[int, Path]:
    folder = voice_input_dir(subject)
    if not folder.is_dir():
        return {}

    found: dict[int, Path] = {}
    for tag in ("a", "b"):
        char_turns = [(i, text) for i, (speaker, text) in enumerate(turns) if speaker == tag]
        for i, text in char_turns:
            matches = [f for f in folder.glob(f"turn_{i}.*") if f.suffix.lower() != ".txt"]
            if not matches:
                continue
            manifest = folder / f"turn_{i}.txt"
            if manifest.exists() and manifest.read_text(encoding="utf-8").strip() != text.strip():
                logger.warning(f"turn_{i} audio in {folder} is stale for the current script -- falling back to TTS")
                continue
            found[i] = matches[0]

        missing = [(i, text) for i, text in char_turns if i not in found]
        if missing:
            combined = _resolve_combined_audio(folder, tag, tmp_dir)
            if combined:
                out_paths = [folder / f"turn_{i}.mp3" for i, _ in missing]
                from modules.assembler.voice_split import split_combined_audio
                success = split_combined_audio(combined, [text for _, text in missing], out_paths)
                if success:
                    for (i, _), out_path in zip(missing, out_paths):
                        found[i] = out_path

    for i, (speaker, text) in enumerate(turns):
        if i in found:
            (folder / f"turn_{i}.txt").write_text(text, encoding="utf-8")

    if found:
        logger.info(f"Using {len(found)} user-supplied audio file(s) from {folder}")
    return found


def _get_line_audio(index: int | None, text: str, voice: str, external_audio: dict[int, Path], tmp_dir: Path, tag: str) -> tuple[Path, float]:
    """Resolve one line's audio (external override if available, else TTS),
    returns (path, duration)."""
    out_path = tmp_dir / f"audio_{tag}.mp3"
    if index is not None and index in external_audio:
        _run_ffmpeg(["ffmpeg", "-y", "-i", str(external_audio[index]), "-ar", "24000", "-ac", "1", "-q:a", "9", str(out_path)])
    else:
        asyncio.run(_synthesize(text, str(out_path), voice))
    return out_path, probe_duration(str(out_path))


def _line_word_timestamps(audio_path: Path) -> list[dict]:
    """Per-word timing for one line's own audio, used to drive animated
    captions (see _burn_captions_filter) instead of a static whole-sentence
    block. Never raises -- a transcription hiccup just means that one line
    falls back to the old static-text overlay, not a broken render."""
    try:
        from modules.transcriber import transcribe_words
        return transcribe_words(str(audio_path))
    except Exception as e:
        logger.debug(f"Word-timestamp transcription failed for {audio_path}: {e}")
        return []


# ── Visual cards ─────────────────────────────────────────────────────────

def _fighting_font(size: int):
    try:
        return ImageFont.truetype(_FONT_SEMIBOLD, size=size)
    except OSError:
        return ImageFont.load_default()


_BAR_W, _BAR_H = int(VIDEO_WIDTH * 0.36), 22
_BAR_Y = 95
_BAR_AX = int(VIDEO_WIDTH * 0.03)
_BAR_BX = VIDEO_WIDTH - int(VIDEO_WIDTH * 0.03) - _BAR_W
_LOW_HP_THRESHOLD = 25


def _fit_name_font(text: str, max_width: int, start_size: int = 28, min_size: int = 16):
    """Shrink the font until `text` fits `max_width` -- a long name (e.g.
    "Krusty the Clown") used to get hard-truncated to 14 characters
    ("KRUSTY THE CLO", mid-word), which reads as a bug rather than a
    stylistic choice. Shrinking to fit keeps the full name legible."""
    size = start_size
    while size > min_size:
        font = _fighting_font(size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return _fighting_font(min_size)


def _draw_hp_bars(overlay: Image.Image, hp_a: int, hp_b: int, name_a: str, name_b: str, photo_a: Path | None, photo_b: Path | None) -> None:
    draw = ImageDraw.Draw(overlay)
    hp_font = _fighting_font(22)

    bar_w, bar_h = _BAR_W, _BAR_H
    y = _BAR_Y
    portrait = 64
    ax, bx = _BAR_AX, _BAR_BX

    for x, name, hp, color, photo, is_a in (
        (ax, name_a, hp_a, _HP_COLOR_A, photo_a, True),
        (bx, name_b, hp_b, _HP_COLOR_B, photo_b, False),
    ):
        if photo and Path(photo).exists():
            try:
                img = Image.open(photo).convert("RGB")
                w, h = img.size
                side = min(w, h)
                img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)).resize((portrait, portrait))
                mask = Image.new("L", (portrait, portrait), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, portrait, portrait), fill=255)
                px = x + bar_w // 2 - portrait // 2
                overlay.paste(img, (px, y + bar_h + 10), mask)
            except Exception:
                pass

        name_label = name.upper()
        name_font = _fit_name_font(name_label, bar_w)
        _soft_text(overlay, (x, y - 32), name_label, name_font, _TEXT_COLOR, multiline=False, stroke_width=2)
        draw.rounded_rectangle((x, y, x + bar_w, y + bar_h), radius=bar_h // 2, fill=(50, 50, 54, 230))
        frac = max(0.0, min(1.0, hp / 100.0))
        if frac > 0:
            fw = max(bar_h, int(bar_w * frac))
            fill_x = x if is_a else x + bar_w - fw
            draw.rounded_rectangle((fill_x, y, fill_x + fw, y + bar_h), radius=bar_h // 2, fill=color)
        hp_label = f"{int(hp)}"
        hbbox = draw.textbbox((0, 0), hp_label, font=hp_font)
        hx = x + bar_w // 2 - (hbbox[2] - hbbox[0]) // 2
        _soft_text(overlay, (hx, y + 1), hp_label, hp_font, (255, 255, 255, 255), multiline=False, stroke_width=2)

    vs_font = _fighting_font(28)
    vbbox = draw.textbbox((0, 0), "VS", font=vs_font)
    vx = (VIDEO_WIDTH - (vbbox[2] - vbbox[0])) // 2
    _soft_text(overlay, (vx, y - 6), "VS", vs_font, _TEXT_COLOR, multiline=False, stroke_width=2)


def _render_static_card(text: str, out_path: Path, size: int = 60, wrap_width: int = 24) -> None:
    """Plain centered title card on the shared gradient (the winner-line
    card uses this). Each line is wrapped to the frame width -- previously
    the raw text was drawn unwrapped, so anything longer than a couple of
    words ran off both edges of the frame and only the middle words were
    ever visible on screen."""
    import textwrap as _tw
    bg = Path(str(out_path) + "_bg.png")
    _build_bg(bg)
    img = Image.open(bg).convert("RGBA")
    draw = ImageDraw.Draw(img)
    font = _fighting_font(size)
    wrapped = "\n".join(
        _tw.fill(line, width=wrap_width, break_long_words=False) if line else line
        for line in text.split("\n")
    )
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center")
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, multiline=True, align="center", stroke_width=4)
    img.convert("RGB").save(str(out_path), quality=92)
    bg.unlink(missing_ok=True)


def _cover_crop(photo_path: Path, w: int, h: int) -> Image.Image | None:
    """Scale+crop `photo_path` to exactly fill a w x h box (cover, not
    contain) -- used to fill an entire half of the frame with a face."""
    try:
        img = Image.open(photo_path).convert("RGB")
    except Exception:
        return None
    pw, ph = img.size
    scale = max(w / pw, h / ph)
    new_w, new_h = max(1, round(pw * scale)), max(1, round(ph * scale))
    img = img.resize((new_w, new_h))
    cx, cy = new_w // 2, new_h // 2
    return img.crop((cx - w // 2, cy - h // 2, cx - w // 2 + w, cy - h // 2 + h))


def _render_pov_card(name_a: str, photo_a: Path | None, name_b: str, photo_b: Path | None, out_path: Path) -> None:
    """Opening flash card: the background itself is split top/bottom --
    character A's face fills the TOP half, character B's fills the BOTTOM
    half -- with VS on the divider and names above/below it. NO spoken hook
    line and no caption text anymore -- real retention data showed the old
    spoken "POV: ..." intro over this same frozen image was killing videos
    outright (94% of viewers gone by 0:02, before the fight even started).
    This card now plays silently for ~_POV_CARD_DUR seconds, just long
    enough to register who's fighting, then the video moves (explicit user
    call, 2026-08-26)."""
    half_h = VIDEO_HEIGHT // 2
    base = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (26, 26, 29))

    top = _cover_crop(Path(photo_a), VIDEO_WIDTH, half_h) if photo_a and Path(photo_a).exists() else None
    if top:
        base.paste(top, (0, 0))
    bottom = _cover_crop(Path(photo_b), VIDEO_WIDTH, VIDEO_HEIGHT - half_h) if photo_b and Path(photo_b).exists() else None
    if bottom:
        base.paste(bottom, (0, half_h))

    overlay = base.convert("RGBA")
    draw = ImageDraw.Draw(overlay)
    # Dim both halves a touch so white text/stroke reads over any photo,
    # plus a hard divider line across the middle.
    dim = Image.new("RGBA", overlay.size, (0, 0, 0, 70))
    overlay.alpha_composite(dim)
    draw.rectangle((0, half_h - 3, VIDEO_WIDTH, half_h + 3), fill=(0, 0, 0, 220))

    name_font = _fighting_font(36)
    vs_font = _fighting_font(88)

    nb1 = draw.textbbox((0, 0), name_a.upper(), font=name_font)
    _soft_text(overlay, (VIDEO_WIDTH // 2 - (nb1[2] - nb1[0]) // 2, 50), name_a.upper(), name_font, _HP_COLOR_A, multiline=False, stroke_width=3)

    vbbox = draw.textbbox((0, 0), "VS", font=vs_font)
    vx = VIDEO_WIDTH // 2 - (vbbox[2] - vbbox[0]) // 2
    vy = half_h - (vbbox[3] - vbbox[1]) // 2
    _soft_text(overlay, (vx, vy), "VS", vs_font, _TEXT_COLOR, multiline=False, stroke_width=6)

    nb2 = draw.textbbox((0, 0), name_b.upper(), font=name_font)
    _soft_text(overlay, (VIDEO_WIDTH // 2 - (nb2[2] - nb2[0]) // 2, half_h + 55), name_b.upper(), name_font, _HP_COLOR_B, multiline=False, stroke_width=3)

    overlay.convert("RGB").save(str(out_path), quality=92)


def _render_footage_overlay(speaker_name: str, text: str, color: tuple, out_path: Path, show_text: bool = True) -> None:
    """Transparent name+line overlay composited over that character's real
    footage clip (the clip itself is their "skin" during their own line).

    show_text: draw the line's full sentence as a static block (old
    behavior). Set False when the caller is instead burning real animated,
    word-by-word captions via _burn_captions_filter -- the whole-sentence
    block sitting frozen on screen for the entire line was itself a real
    retention issue (looks like "nothing is happening" even over a moving
    clip), and drawing both would just duplicate the text on screen."""
    import textwrap as _tw
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    label_font = _fighting_font(36)
    body_font  = _fighting_font(46)

    label = speaker_name.upper()
    lbbox = draw.textbbox((0, 0), label, font=label_font)
    lx = (VIDEO_WIDTH - (lbbox[2] - lbbox[0])) // 2
    ly = int(VIDEO_HEIGHT * 0.74)
    _soft_text(overlay, (lx, ly), label, label_font, color, multiline=False, stroke_width=3)

    if show_text:
        wrapped = _tw.fill(text, width=26, break_long_words=False)
        bbox = draw.textbbox((0, 0), wrapped, font=body_font, align="center", spacing=12)
        bx = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
        by = ly + (lbbox[3] - lbbox[1]) + 20
        _soft_text(overlay, (bx, by), wrapped, body_font, _TEXT_COLOR, align="center", spacing=12, stroke_width=3)

    overlay.save(str(out_path), format="PNG")


def _render_centered_text_overlay(text: str, out_path: Path, size: int = 42, wrap_width: int = 24) -> None:
    """Transparent centered-text overlay, NO speaker label -- for the
    announcer's moral-of-the-story line composited over the winner's real
    footage instead of a flat gradient card (a plain dark card right
    before the ending read as a dead spot)."""
    import textwrap as _tw
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _fighting_font(size)
    wrapped = "\n".join(
        _tw.fill(line, width=wrap_width, break_long_words=False) if line else line
        for line in text.split("\n")
    )
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center")
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    y = int(VIDEO_HEIGHT * 0.70) - (bbox[3] - bbox[1]) // 2
    _soft_text(overlay, (x, y), wrapped, font, _TEXT_COLOR, multiline=True, align="center", stroke_width=4)
    overlay.save(str(out_path), format="PNG")


# ── Segment builders (each is its own small, identically-encoded mp4) ──────

_ENC = ["-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(VIDEO_FPS)]
# Every segment's audio MUST be re-encoded to this exact sample rate/channel
# layout -- sources vary wildly (24kHz mono TTS, 48kHz mono procedural sfx,
# 44.1kHz stereo real recordings) and the `-f concat` demuxer used to join
# segments does NOT resample between them. Leaving each segment at its
# source's native format caused a real bug: audio played fine until the
# first segment with a different sample rate, then progressively
# desynced/dropped out for the rest of the video.
_AENC = ["-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "160k"]


def _exact_frame_dur(duration: float) -> float:
    """Quantize `duration` to a whole number of frames at VIDEO_FPS -- this
    is the REAL duration a segment's video track ends up with (you can't
    have a fractional frame), which is very rarely exactly equal to an
    audio track's natural (continuous-time) length. Concatenating ~20
    segments where each one's audio is even a few ms off from its own
    video's frame-quantized length accumulates into real, audible
    drift by the second half of the video (diagnosed 2026-07-23: ~48ms
    cumulative on a 62s video, entirely from this). Every segment's audio
    must be explicitly padded/trimmed to THIS exact value, not just
    "-shortest" against the source's raw length."""
    return round(duration * VIDEO_FPS) / VIDEO_FPS


def _hit_dur_for_round(r: int, n_rounds: int) -> float:
    """Progressive pacing: hit-stop segments get snappier round by round
    instead of repeating the exact same rhythm 6 times in a row (2 hits x 3
    identical rounds read as mechanical/stalled rather than escalating --
    real retention issue, see _HIT_DUR). Round 0 runs at the full _HIT_DUR,
    the last round runs at _HIT_DUR_MIN_FRACTION of it, linear in between."""
    if n_rounds <= 1:
        return _HIT_DUR
    frac = r / (n_rounds - 1)
    return _HIT_DUR * (1.0 - (1.0 - _HIT_DUR_MIN_FRACTION) * frac)


def _burn_captions_filter(prev_label: str, words: list[dict] | None) -> tuple[list[str], str, Path | None]:
    """Shared by every segment builder that can show spoken-line captions:
    turns per-word timestamps into an animated (word-group) subtitle burn
    instead of the old baked-in, whole-sentence-at-once text block that used
    to sit frozen on screen for the entire line (real retention issue --
    static text for 4+ seconds reads as "nothing is happening" even when a
    real clip is playing underneath). Reuses the same ASS pipeline already
    proven for format4/format6/format11's captions.

    Returns (extra_filter_lines, final_video_label, ass_path_to_cleanup).
    If `words` is empty/falsy or ASS generation fails, returns
    ([], prev_label, None) -- caller keeps its own static text overlay as
    the fallback, never ships a blank caption."""
    if not words:
        return [], prev_label, None
    try:
        from modules.subtitle_generator import generate_ass
        ass_path = generate_ass({"word_timestamps": words})
    except Exception as e:
        logger.warning(f"Animated caption generation failed, falling back to static text: {e}")
        return [], prev_label, None
    return [f"[{prev_label}]subtitles={ass_path.name}[capped]"], "capped", ass_path


def _seg_card(image_path: Path, audio_path: Path, duration: float, out_path: Path, music_bed: Path | None = None, words: list[dict] | None = None) -> None:
    exact_dur = _exact_frame_dur(duration)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(exact_dur), "-i", str(image_path), "-i", str(audio_path)]

    filters = [f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuv420p[base]"]
    cap_filters, video_label, ass_path = _burn_captions_filter("base", words)
    filters += cap_filters

    if music_bed and Path(music_bed).exists():
        cmd += ["-stream_loop", "-1", "-t", str(exact_dur), "-i", str(music_bed)]
        filters.append(f"[1:a]apad=whole_dur={exact_dur}[voice]")
        filters.append(f"[2:a]volume={_MUSIC_BED_GAIN_DB}dB[bed]")
        filters.append("[voice][bed]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append(f"[1:a]apad=whole_dur={exact_dur}[aout]")

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", "[aout]"]
    cmd += [*_ENC, *_AENC, "-t", str(exact_dur), str(out_path)]
    _run_ffmpeg(cmd, cwd=str(ass_path.parent) if ass_path else None)
    if ass_path:
        ass_path.unlink(missing_ok=True)


def _make_silence(duration: float, out_path: Path) -> None:
    """Plain silent audio track -- used for the opening flash card, which
    now plays with no voiceover at all (see _render_pov_card)."""
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(duration), "-q:a", "9", str(out_path),
    ])


def _seg_footage_line(clip_path: Path, overlay_png: Path, audio_path: Path, duration: float, out_path: Path, music_bed: Path | None = None, words: list[dict] | None = None) -> None:
    exact_dur = _exact_frame_dur(duration)
    cmd = [
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip_path),
        "-loop", "1", "-i", str(overlay_png), "-i", str(audio_path),
    ]
    filters = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,eq=brightness=-0.10:saturation=0.7,format=yuv420p[bg]",
        "[1:v]format=rgba[ov]",
        "[bg][ov]overlay=0:0[precap]",
    ]
    cap_filters, video_label, ass_path = _burn_captions_filter("precap", words)
    filters += cap_filters

    if music_bed and Path(music_bed).exists():
        cmd += ["-stream_loop", "-1", "-t", str(exact_dur), "-i", str(music_bed)]
        filters.append(f"[2:a]apad=whole_dur={exact_dur}[voice]")
        filters.append(f"[3:a]volume={_MUSIC_BED_GAIN_DB}dB[bed]")
        filters.append("[voice][bed]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append(f"[2:a]apad=whole_dur={exact_dur}[aout]")

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", "[aout]"]
    cmd += [*_ENC, *_AENC, "-t", str(exact_dur), str(out_path)]
    _run_ffmpeg(cmd, cwd=str(ass_path.parent) if ass_path else None)
    if ass_path:
        ass_path.unlink(missing_ok=True)


_SPRITE_W, _SPRITE_H = 380, 560
_GROUND_Y = 1480
_BASE_Y = _GROUND_Y - _SPRITE_H
_BASE_X_A = 70
_BASE_X_B = VIDEO_WIDTH - 70 - _SPRITE_W
_BOUNCE_Y = f"{_BASE_Y}+6*sin(2*PI*t/0.9)"  # continuous idle bob, always applied


def _make_fallen_sprite(sprite_path: Path, out_path: Path) -> Path:
    """Pre-rotate a sprite ~80 degrees for the K.O. moment in PIL (not
    ffmpeg) -- no time-varying-rotation risk. The sprite is already a real
    RGBA cutout (rembg), so rotated corners fill with actual transparency,
    not a sampled color match."""
    img = Image.open(sprite_path).convert("RGBA")
    rotated = img.rotate(-78, expand=True, resample=Image.BICUBIC)
    rotated.save(str(out_path), format="PNG")
    return out_path


def _color_hex(color: tuple) -> str:
    return f"0x{color[0]:02X}{color[1]:02X}{color[2]:02X}"


def _sprite_filter_block(in_label: str, out_label: str, flip: bool, outline_color: tuple, rotate_expr: str | None = None) -> str:
    """Full filter block (semicolon-separated, not just a comma-chain) that
    scales/pads an ALREADY-transparent (rembg) sprite cutout AND adds a
    solid-color outline (dilated alpha silhouette) behind it -- a dark
    backdrop area can otherwise swallow even a vividly colored sprite; the
    outline guarantees the silhouette always reads. No chroma-keying here
    -- the input's alpha channel is the real cutout, not a color guess."""
    flip_part = ",hflip" if flip else ""
    rotate_part = f",rotate=a='{rotate_expr}':ow=iw:oh=ih:c=none" if rotate_expr else ""
    base = f"{in_label}base"
    keep = f"{in_label}keep"
    edge = f"{in_label}edge"
    mask = f"{in_label}mask"
    fill = f"{in_label}fill"
    outline = f"{in_label}outline"
    oc = _color_hex(outline_color)
    return (
        f"[{in_label}]scale={_SPRITE_W}:{_SPRITE_H}:force_original_aspect_ratio=decrease,"
        f"format=rgba,"
        f"pad={_SPRITE_W}:{_SPRITE_H}:(ow-iw)/2:(oh-ih)/2:color=black@0{flip_part}{rotate_part}[{base}];"
        f"[{base}]split=2[{keep}][{edge}];"
        f"[{edge}]alphaextract,dilation=coordinates=255,dilation=coordinates=255[{mask}];"
        f"color=c={oc}:s={_SPRITE_W}x{_SPRITE_H}[{fill}];"
        f"[{fill}][{mask}]alphamerge[{outline}];"
        f"[{outline}][{keep}]overlay=0:0[{out_label}]"
    )


_IMPACT_BURST = RESETWORTH_ASSETS_DIR / "branding" / "impact_burst.png"


def _ensure_impact_burst() -> Path:
    """Generic radiating-spike impact graphic, drawn once and reused across
    EVERY duel video's landed hits (not tied to any character) -- gives a
    real "something connected" visual beyond just the sprite's own motion,
    matching the classic arcade-sprite-sheet impact-burst frame."""
    if _IMPACT_BURST.exists():
        return _IMPACT_BURST
    _IMPACT_BURST.parent.mkdir(parents=True, exist_ok=True)

    size = 260
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    n_spikes = 10
    outer_r, inner_r = size * 0.48, size * 0.18
    points = []
    for i in range(n_spikes * 2):
        angle = math.pi * i / n_spikes
        r = outer_r if i % 2 == 0 else inner_r
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(points, fill=(255, 220, 60, 235), outline=(255, 255, 255, 255))
    img.save(str(_IMPACT_BURST), format="PNG")
    return _IMPACT_BURST


_REACTION_STARS = RESETWORTH_ASSETS_DIR / "branding" / "reaction_stars.png"


def _ensure_reaction_stars() -> Path:
    """Generic comic-book "dizzy stars" reaction icon -- drawn once, reused
    over every landed hit's DEFENDER (not character-specific), a cheap
    reliable stand-in for "an emoji pops up" that doesn't depend on any
    system having a color-emoji font available."""
    if _REACTION_STARS.exists():
        return _REACTION_STARS
    _REACTION_STARS.parent.mkdir(parents=True, exist_ok=True)

    size = 150
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    star_specs = [(28, 65, 15), (66, 22, 12), (108, 58, 16), (48, 100, 11), (92, 98, 13)]
    for cx, cy, r_outer in star_specs:
        r_inner = r_outer * 0.42
        pts = []
        for i in range(10):
            angle = math.pi * i / 5 - math.pi / 2
            r = r_outer if i % 2 == 0 else r_inner
            pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(pts, fill=(255, 225, 50, 255), outline=(0, 0, 0, 255))
    img.save(str(_REACTION_STARS), format="PNG")
    return _REACTION_STARS


_BLOCK_SPARK = RESETWORTH_ASSETS_DIR / "branding" / "block_spark.png"


def _ensure_block_spark() -> Path:
    """Cyan/blue spark for a BLOCKED/MISSED swing -- deliberately a
    different color+shape family from the gold impact burst, so a landed
    hit and a whiffed one always read as visually distinct at a glance."""
    if _BLOCK_SPARK.exists():
        return _BLOCK_SPARK
    _BLOCK_SPARK.parent.mkdir(parents=True, exist_ok=True)

    size = 220
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2
    n_spikes = 6
    outer_r, inner_r = size * 0.46, size * 0.14
    points = []
    for i in range(n_spikes * 2):
        angle = math.pi * i / n_spikes
        r = outer_r if i % 2 == 0 else inner_r
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(points, fill=(80, 210, 245, 230), outline=(255, 255, 255, 255))
    img.save(str(_BLOCK_SPARK), format="PNG")
    return _BLOCK_SPARK


def _draw_speech_bubble(size_w: int, size_h: int) -> Image.Image:
    img = Image.new("RGBA", (size_w, size_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    body_h = int(size_h * 0.75)
    draw.rounded_rectangle((0, 0, size_w - 1, body_h), radius=18, fill=(255, 255, 255, 240), outline=(0, 0, 0, 255), width=3)
    tail = [(size_w * 0.30, body_h - 2), (size_w * 0.42, body_h - 2), (size_w * 0.22, size_h - 1)]
    draw.polygon(tail, fill=(255, 255, 255, 240), outline=(0, 0, 0, 255))
    return img


_CONFIDENT_ICON = RESETWORTH_ASSETS_DIR / "branding" / "reaction_confident.png"


def _ensure_confident_icon() -> Path:
    """Speech-bubble "showing off" reaction (sunglasses) for the ATTACKER
    when their swing LANDS -- generic/reusable, not character art."""
    if _CONFIDENT_ICON.exists():
        return _CONFIDENT_ICON
    _CONFIDENT_ICON.parent.mkdir(parents=True, exist_ok=True)

    w, h = 130, 100
    img = _draw_speech_bubble(w, h)
    draw = ImageDraw.Draw(img)
    black = (20, 20, 20, 255)
    draw.rounded_rectangle((w * 0.18, h * 0.30, w * 0.44, h * 0.48), radius=6, fill=black)
    draw.rounded_rectangle((w * 0.56, h * 0.30, w * 0.82, h * 0.48), radius=6, fill=black)
    draw.line((w * 0.44, h * 0.36, w * 0.56, h * 0.36), fill=black, width=4)
    draw.arc((w * 0.35, h * 0.42, w * 0.65, h * 0.62), start=20, end=160, fill=black, width=4)
    img.save(str(_CONFIDENT_ICON), format="PNG")
    return _CONFIDENT_ICON


_EMBARRASSED_ICON = RESETWORTH_ASSETS_DIR / "branding" / "reaction_embarrassed.png"


def _ensure_embarrassed_icon() -> Path:
    """Speech-bubble "shrinking/embarrassed" reaction (sweat drop + frown)
    for the ATTACKER when their swing MISSES/gets BLOCKED."""
    if _EMBARRASSED_ICON.exists():
        return _EMBARRASSED_ICON
    _EMBARRASSED_ICON.parent.mkdir(parents=True, exist_ok=True)

    w, h = 130, 100
    img = _draw_speech_bubble(w, h)
    draw = ImageDraw.Draw(img)
    blue = (90, 170, 230, 255)
    drop = [(w * 0.68, h * 0.24), (w * 0.78, h * 0.42), (w * 0.68, h * 0.50), (w * 0.58, h * 0.42)]
    draw.polygon(drop, fill=blue, outline=(0, 0, 0, 255))
    dark = (20, 20, 20, 255)
    draw.arc((w * 0.28, h * 0.50, w * 0.58, h * 0.68), start=200, end=340, fill=dark, width=4)
    img.save(str(_EMBARRASSED_ICON), format="PNG")
    return _EMBARRASSED_ICON


def _render_arena_overlay(hp_a: int, hp_b: int, names: dict, photos: dict, label: str | None, label_color: tuple) -> Path:
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    _draw_hp_bars(overlay, hp_a, hp_b, names["a"], names["b"], photos.get("a"), photos.get("b"))
    if label:
        multiline = "\n" in label
        draw = ImageDraw.Draw(overlay)
        font = _fighting_font(64)
        bbox = draw.textbbox((0, 0), label, font=font, align="center") if multiline else draw.textbbox((0, 0), label, font=font)
        lx = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
        ly = int(VIDEO_HEIGHT * 0.28)
        _soft_text(overlay, (lx, ly), label, font, label_color, multiline=multiline, align="center", stroke_width=4)
    return overlay


def _seg_arena(
    backdrop_path: Path,
    sprite_a: Path | None, x_expr_a: str,
    sprite_b: Path | None, x_expr_b: str,
    overlay_img: Image.Image, sfx_or_audio_path: Path, duration: float,
    flash: bool, out_path: Path,
    rot_a: str | None = None, rot_b: str | None = None,
    impact_effect: str | None = None,
    defender_reaction_side: str | None = None,
    attacker_reaction: tuple[str, str] | None = None,
    low_hp_sides: tuple[str, ...] = (),
    impact_at: float = 0.08,
    music_bed: Path | None = None,
) -> None:
    """Core arena composite: backdrop + both fighters (chroma-keyed sprites,
    positioned via time-varying x expressions -- bounce/lunge/recoil all
    happen here, y always bounces) + HP/label overlay + optional full-frame
    impact flash + audio. Both fighters are ALWAYS visible together (the
    actual gap the first version missed) -- never a solo character card.

    rot_a/rot_b: optional time-varying rotation expression (radians) -- the
    attacker leans/twists into the swing, giving real visible arm-and-torso
    motion instead of just the whole sprite sliding in place.
    impact_effect: "burst" (gold spikes, landed) or "spark" (cyan, missed/
    blocked) -- deliberately different assets so a hit vs a whiff always
    reads as visually distinct. defender_reaction_side ("a"/"b") pops the
    dizzy-stars icon above whoever just got hit. attacker_reaction is
    (side, "confident"|"embarrassed") -- the ATTACKER's own speech-bubble
    reaction to landing or whiffing their swing. All of these are generic,
    reusable assets, not character-specific art, and none depend on a
    system having a color-emoji font.
    impact_at: when (seconds into the clip) the punch actually connects --
    the burst/spark/flash/reaction overlays are timed around THIS moment,
    not the start of the clip. The hit-segment call site passes
    _IMPACT_AT_FRACTION of that round's hit duration here (the attacker's
    lunge, a sin(PI*t/duration) curve, peaks at exactly that instant) --
    previously these were hardcoded to fire right at t=0, so the impact
    flashed and vanished before the attacker's
    fist had even reached the opponent.
    music_bed: looped, heavily-ducked background energy track mixed under
    `sfx_or_audio_path` (see _MUSIC_BED_GAIN_DB) -- without it, every arena
    beat sits on dead silence between the SFX/voice lines."""
    overlay_png = out_path.with_suffix(".ov.png")
    overlay_img.save(str(overlay_png), format="PNG")

    effect_start = max(0.0, impact_at - 0.10)
    effect_end = impact_at + 0.30
    flash_start = max(0.0, impact_at - 0.03)
    flash_end = impact_at + 0.06
    react_start = impact_at + 0.05

    cmd = ["ffmpeg", "-y", "-loop", "1", "-t", str(duration), "-i", str(backdrop_path)]
    idx = 1
    a_idx = b_idx = effect_idx = defender_react_idx = attacker_react_idx = None
    if sprite_a and Path(sprite_a).exists():
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(sprite_a)]
        a_idx = idx; idx += 1
    if sprite_b and Path(sprite_b).exists():
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(sprite_b)]
        b_idx = idx; idx += 1
    if impact_effect == "burst":
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(_ensure_impact_burst())]
        effect_idx = idx; idx += 1
    elif impact_effect == "spark":
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(_ensure_block_spark())]
        effect_idx = idx; idx += 1
    if defender_reaction_side in ("a", "b"):
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(_ensure_reaction_stars())]
        defender_react_idx = idx; idx += 1
    if attacker_reaction is not None:
        _, kind = attacker_reaction
        icon = _ensure_confident_icon() if kind == "confident" else _ensure_embarrassed_icon()
        cmd += ["-loop", "1", "-t", str(duration), "-i", str(icon)]
        attacker_react_idx = idx; idx += 1
    overlay_idx = idx; idx += 1
    cmd += ["-loop", "1", "-t", str(duration), "-i", str(overlay_png)]
    audio_idx = idx; idx += 1
    cmd += ["-i", str(sfx_or_audio_path)]
    bed_idx = None
    if music_bed and Path(music_bed).exists():
        bed_idx = idx; idx += 1
        cmd += ["-stream_loop", "-1", "-t", str(duration), "-i", str(music_bed)]

    filters = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p[bg0]"
    ]
    prev = "bg0"
    if a_idx is not None:
        filters.append(_sprite_filter_block(f"{a_idx}:v", "spa", flip=False, outline_color=(0, 0, 0), rotate_expr=rot_a))
        filters.append(f"[{prev}][spa]overlay=x='{x_expr_a}':y='{_BOUNCE_Y}':format=auto[bgA]")
        prev = "bgA"
    if b_idx is not None:
        filters.append(_sprite_filter_block(f"{b_idx}:v", "spb", flip=True, outline_color=(0, 0, 0), rotate_expr=rot_b))
        filters.append(f"[{prev}][spb]overlay=x='{x_expr_b}':y='{_BOUNCE_Y}':format=auto[bgB]")
        prev = "bgB"
    if effect_idx is not None:
        mid_x = (_BASE_X_A + _BASE_X_B + _SPRITE_W) // 2 - 130
        mid_y = _BASE_Y + _SPRITE_H // 2 - 130
        filters.append(f"[{effect_idx}:v]format=rgba[effectimg]")
        filters.append(
            f"[{prev}][effectimg]overlay=x={mid_x}:y={mid_y}:"
            f"enable='between(t,{effect_start:.3f},{effect_end:.3f})'[bgEffect]"
        )
        prev = "bgEffect"
    if defender_react_idx is not None:
        rx = (_BASE_X_A if defender_reaction_side == "a" else _BASE_X_B) + _SPRITE_W // 2 - 75
        ry = _BASE_Y - 100
        filters.append(f"[{defender_react_idx}:v]format=rgba[reactimg]")
        filters.append(
            f"[{prev}][reactimg]overlay=x={rx}:y='{ry}+4*sin(2*PI*t/0.3)':"
            f"enable='between(t,{react_start:.3f},{duration})'[bgReact]"
        )
        prev = "bgReact"
    if attacker_react_idx is not None:
        att_side = attacker_reaction[0]
        ax = (_BASE_X_A if att_side == "a" else _BASE_X_B) + _SPRITE_W // 2 - 65
        ay = _BASE_Y - 95
        filters.append(f"[{attacker_react_idx}:v]format=rgba[attreactimg]")
        filters.append(
            f"[{prev}][attreactimg]overlay=x={ax}:y='{ay}+4*sin(2*PI*t/0.3)':"
            f"enable='between(t,{react_start:.3f},{duration})'[bgAttReact]"
        )
        prev = "bgAttReact"
    for side in low_hp_sides:
        bar_x = _BAR_AX if side == "a" else _BAR_BX
        label = f"lowhp{side}"
        filters.append(
            f"[{prev}]drawbox=x={bar_x - 3}:y={_BAR_Y - 3}:w={_BAR_W + 6}:h={_BAR_H + 6}:"
            f"color=red@0.85:t=4:enable='lt(mod(t\\,0.3)\\,0.15)'[{label}]"
        )
        prev = label
    if flash:
        # A full-screen white flash here made the fighters vanish right at
        # the moment they need to be visible -- much subtler edge vignette
        # instead, brief and low-opacity, that reads as "impact" without
        # hiding anyone.
        filters.append(
            f"[{prev}]drawbox=x=0:y=0:w={VIDEO_WIDTH}:h={VIDEO_HEIGHT}:color=white@0.18:t=fill:"
            f"enable='between(t,{flash_start:.3f},{flash_end:.3f})'[flashed]"
        )
        prev = "flashed"
    filters.append(f"[{overlay_idx}:v]format=rgba[ov]")
    filters.append(f"[{prev}][ov]overlay=0:0[vout]")

    exact_dur = _exact_frame_dur(duration)
    if bed_idx is not None:
        filters.append(f"[{audio_idx}:a]apad=whole_dur={exact_dur}[voice]")
        filters.append(f"[{bed_idx}:a]volume={_MUSIC_BED_GAIN_DB}dB[bed]")
        filters.append("[voice][bed]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append(f"[{audio_idx}:a]apad=whole_dur={exact_dur}[aout]")

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += [*_ENC, *_AENC, "-t", str(exact_dur), str(out_path)]
    _run_ffmpeg(cmd)
    overlay_png.unlink(missing_ok=True)


def _concat_segments(segments: list[Path], out_path: Path) -> None:
    """Every segment is already encoded with IDENTICAL params (_ENC/_AENC,
    same fps/sample-rate/channels), so this can be a lossless STREAM COPY
    instead of a full re-encode. That matters for more than speed: a
    re-encoding concat re-decodes and re-encodes audio+video across all ~20
    splice points, and that re-encode step has its own frame/sample
    rounding at each cut -- exactly the kind of small per-cut error that
    _exact_frame_dur's per-segment padding was designed to eliminate.
    Measured live: re-encoding here left ~43ms of cumulative AV drift on a
    65s video even with every individual segment already frame-matched;
    switching to stream copy is what actually gets rid of it, since
    nothing gets re-touched at the splice points at all."""
    concat_list = segments[0].parent / "concat_final.txt"
    concat_list.write_text("\n".join(f"file '{s.name}'" for s in segments), encoding="utf-8")
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list.name,
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ], cwd=str(segments[0].parent))


def _photo_sprite_fallback(photo_path: Path | None, out_path: Path) -> Path | None:
    """When the AI-generated pixel-art sprite fails vision verification on
    every attempt (fetch_character_sprite returns None), fall back to a
    real background-removed cutout of the character's own photo instead of
    leaving them invisible on the arena -- _seg_arena only draws a fighter
    when it's given a sprite path, so "no sprite" previously meant that
    whole side of the fight silently never appeared on screen at all,
    despite the code elsewhere insisting both fighters are always visible
    together. Not as "game-y" as a pixel-art sprite, but a real photo of
    the actual character beats a blank space."""
    if not photo_path or not Path(photo_path).exists():
        return None
    try:
        from rembg import remove
        cutout = remove(Image.open(photo_path).convert("RGB"))
        cutout.save(str(out_path), format="PNG")
        return out_path
    except Exception as e:
        logger.warning(f"Photo-to-sprite fallback failed: {e}")
        return None


def _fetch_photo_retrying(name: str, context: str, out_path: Path) -> Path | None:
    """Try several query phrasings in sequence until one actually returns a
    vision-verified photo -- leaving the POV half blank after a single
    failed query is NOT an acceptable outcome (explicit user correction:
    "ce n'est pas une solution de rien mettre, cherche jusqu'à trouver").
    Each attempt still goes through fetch_character_image's own relevance
    filtering + vision verification, so this never lowers the bar for
    accepting a wrong photo -- it just gives the search more chances to
    find a right one."""
    queries = [
        f"{name} {context}".strip(),
        name,
        f"{name} {context} actor".strip() if context else f"{name} actor",
        f"{name} character photo",
    ]
    for query in dict.fromkeys(q for q in queries if q):  # de-dupe, keep order
        if fetch_character_image(query, out_path, max_attempts=20, name=name):
            return out_path
    logger.warning(f"No photo found for {name!r} after trying {len(queries)} query variations")
    return None


def assemble_format15(script, visuals_result, voice_result, timestamps_result, music_path):
    """Old-school fighting-game duel: VS card -> FIGHT! -> per-round real
    footage + arena hit segments -> K.O. -> winner/loser lines."""
    rounds = script.get("rounds") or []
    winner = (script.get("winner") or "a").strip().lower()
    dec = 100.0 / max(1, len(rounds))

    a_profile = script.get("char_a_voice") or {}
    b_profile = script.get("char_b_voice") or {}
    voices = {
        "announcer": _ANNOUNCER_VOICE,
        "a": select_voice(gender=a_profile.get("gender", "male"), accent=a_profile.get("accent", ""),
                           age_register=a_profile.get("age_register", ""), tone=a_profile.get("tone") or []),
        "b": select_voice(gender=b_profile.get("gender", "male"), accent=b_profile.get("accent", ""),
                           age_register=b_profile.get("age_register", ""), tone=b_profile.get("tone") or []),
    }
    name_a = (script.get("char_a_name") or "A").strip()
    name_b = (script.get("char_b_name") or "B").strip()
    names  = {"a": name_a, "b": name_b}
    colors = {"a": _HP_COLOR_A, "b": _HP_COLOR_B}

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f15"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        # unlink() only handles files -- a stray subdirectory left behind
        # (e.g. by manual debugging/repair work poking at this same tmp_dir)
        # raises PermissionError and crashes the whole render before it
        # even starts. shutil.rmtree handles that case too.
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)

    turns = build_turns(script)
    external_audio = _find_external_audio(script["subject"], turns, tmp_dir)

    # Separate context fields (not parsed out of "subject") so character A's
    # searches never accidentally pick up character B's context -- subject
    # formatting ("vs" vs "vs.", stray punctuation) isn't reliable enough
    # to split on.
    context_a = (script.get("char_a_context") or "").strip()
    context_b = (script.get("char_b_context") or "").strip()
    query_a = f"{name_a} {context_a}".strip()
    query_b = f"{name_b} {context_b}".strip()

    photo_a_path = tmp_dir / "photo_a.jpg"
    photo_a = _fetch_photo_retrying(name_a, context_a, photo_a_path)
    photo_b_path = tmp_dir / "photo_b.jpg"
    photo_b = _fetch_photo_retrying(name_b, context_b, photo_b_path)
    photos = {"a": photo_a, "b": photo_b}

    sprite_a_path = tmp_dir / "sprite_a.png"
    sprite_a = sprite_a_path if fetch_character_sprite(name_a, context_a, sprite_a_path) else None
    if sprite_a is None:
        sprite_a = _photo_sprite_fallback(photo_a, tmp_dir / "sprite_a_fallback.png")
    sprite_b_path = tmp_dir / "sprite_b.png"
    sprite_b = sprite_b_path if fetch_character_sprite(name_b, context_b, sprite_b_path) else None
    if sprite_b is None:
        sprite_b = _photo_sprite_fallback(photo_b, tmp_dir / "sprite_b_fallback.png")
    sprites = {"a": sprite_a, "b": sprite_b}

    backdrop_path = tmp_dir / "backdrop.jpg"
    if not fetch_arena_backdrop(backdrop_path):
        _build_bg(backdrop_path)

    # count=4: 3 rounds each cycle through index r % len(pool), so with
    # exactly 3 rounds indices 0/1/2 all get used in the round loop -- the
    # 4th clip stays untouched there and is reserved for the loser's
    # closing card, so that final scene is never a repeat of one already
    # shown earlier in the video.
    clips_a = fetch_character_clips(query_a, tmp_dir, count=4, segment_duration=6.0, max_candidates=12, name=name_a, file_prefix="clip_a")
    clips_b = fetch_character_clips(query_b, tmp_dir, count=4, segment_duration=6.0, max_candidates=12, name=name_b, file_prefix="clip_b")
    clip_pools = {"a": clips_a, "b": clips_b}

    impact_sfx = tmp_dir / "sfx_impact.mp3"
    make_impact_sound(impact_sfx)
    whoosh_sfx = tmp_dir / "sfx_whoosh.mp3"
    make_whoosh_sound(whoosh_sfx)
    ko_sfx = tmp_dir / "sfx_ko.mp3"
    make_ko_sound(ko_sfx)
    music_bed_path = tmp_dir / "music_bed.mp3"
    make_arena_music_bed(music_bed_path)

    # Crowd reacts to the OUTCOME (cheers on a landed hit, boos on a miss/
    # block), mixed together with the hit/whoosh sound once -- both source
    # sounds are fixed per render, so re-mixing per-turn would just repeat
    # identical ffmpeg work every single hit.
    crowd_cheer_sfx = tmp_dir / "sfx_crowd_cheer.mp3"
    make_crowd_reaction_sound(crowd_cheer_sfx)
    crowd_boo_sfx = tmp_dir / "sfx_crowd_boo.mp3"
    make_crowd_boo_sound(crowd_boo_sfx)
    # The user's own recorded hit sounds (assets/sfx/format15/hit1.mp3,
    # hit2.mp3), rotated across landed hits for variety -- used instead of
    # the procedural impact thump when present.
    existing_user_hits = [p for p in _USER_HIT_SOUNDS if p.exists()]
    if existing_user_hits:
        landed_variants = []
        for i, hit_path in enumerate(existing_user_hits):
            mixed = tmp_dir / f"sfx_landed_mixed_{i}.mp3"
            mix_audio([hit_path, crowd_cheer_sfx], mixed, gain_db=-16.0)
            landed_variants.append(mixed)
    else:
        landed_sfx = tmp_dir / "sfx_landed_mixed.mp3"
        mix_audio([impact_sfx, crowd_cheer_sfx], landed_sfx)
        landed_variants = [landed_sfx]
    missed_sfx = tmp_dir / "sfx_missed_mixed.mp3"
    mix_audio([whoosh_sfx, crowd_boo_sfx], missed_sfx)

    segments: list[Path] = []
    seg_i = 0

    def next_seg(suffix: str) -> Path:
        nonlocal seg_i
        seg_i += 1
        return tmp_dir / f"seg_{seg_i:03d}_{suffix}.mp4"

    idle_x_a = str(_BASE_X_A)
    idle_x_b = str(_BASE_X_B)

    # -- Opening flash card: character A's photo on top, VS in the middle,
    #    character B's photo below -- SILENT, capped at _POV_CARD_DUR. Used
    #    to synthesize a spoken "POV: ..." line over this same frozen image,
    #    which killed retention outright (real data, 2026-08-26: 94% of
    #    viewers gone by 0:02, before the fight even started). turns[0]
    #    (pov_hook) is no longer voiced or shown here -- it stays in the
    #    script schema/turns list for format14/mindfile compatibility, just
    #    unused by this format now.
    pov_png = tmp_dir / "pov.png"
    _render_pov_card(name_a, photo_a, name_b, photo_b, pov_png)
    pov_silence = tmp_dir / "pov_silence.mp3"
    _make_silence(_POV_CARD_DUR, pov_silence)
    seg = next_seg("pov"); _seg_card(pov_png, pov_silence, _POV_CARD_DUR, seg, music_bed=music_bed_path); segments.append(seg)

    # -- VS card: both fighters already on the arena, idle bounce, big VS + names --
    vs_text = f"{name_a} versus {name_b}!"
    vs_audio, vs_dur = _get_line_audio(None, vs_text, voices["announcer"], {}, tmp_dir, "vs")
    vs_dur = max(vs_dur, _VS_DUR_FLOOR)
    vs_overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vs_overlay)
    name_font, vs_font = _fighting_font(38), _fighting_font(72)
    for nm, cx, col in ((name_a, _BASE_X_A + _SPRITE_W // 2, _HP_COLOR_A), (name_b, _BASE_X_B + _SPRITE_W // 2, _HP_COLOR_B)):
        bbox = vdraw.textbbox((0, 0), nm.upper(), font=name_font)
        _soft_text(vs_overlay, (cx - (bbox[2] - bbox[0]) // 2, 200), nm.upper(), name_font, col, multiline=False, stroke_width=3)
    vbbox = vdraw.textbbox((0, 0), "VS", font=vs_font)
    _soft_text(vs_overlay, (VIDEO_WIDTH // 2 - (vbbox[2] - vbbox[0]) // 2, 320), "VS", vs_font, _TEXT_COLOR, multiline=False, stroke_width=4)
    seg = next_seg("vs")
    _seg_arena(backdrop_path, sprite_a, idle_x_a, sprite_b, idle_x_b, vs_overlay, vs_audio, vs_dur, flash=False, out_path=seg, music_bed=music_bed_path)
    segments.append(seg)

    # -- rounds --
    hp = {"a": 100.0, "b": 100.0}
    last_landed_label = None
    last_landed_hit_idx = -1
    turn_idx = 1  # turns[0] was pov_hook; turns[1..] are the round lines
    n_rounds = len(rounds)
    for r, rnd in enumerate(rounds):
        round_winner = (rnd.get("round_winner") or "a").strip().lower()

        # -- "ROUND N / FIGHT!" card (last round is "FINAL ROUND / FIGHT!"
        #    instead of "ROUND <n>") -- announcer calls the round THEN
        #    fight, matching the classic arcade order (previously this was
        #    backwards: a standalone "FIGHT!" card played once right after
        #    VS, before "ROUND 1" -- so the callout read as "Fight...
        #    Round 1" instead of "Round 1... Fight!").
        round_label = "FINAL ROUND" if r == n_rounds - 1 else f"ROUND {r + 1}"
        # Progressive pacing: only round 1 needs the full floor to register
        # clearly -- every round after that repeats a callout the viewer
        # has already seen twice, so it can move fast (real retention
        # issue: a flat 1.4s floor on EVERY round card was pure
        # announcer-only dead time, identical 3 times in a row).
        round_dur_floor = _ROUND_DUR_FLOOR_FIRST if r == 0 else _ROUND_DUR_FLOOR_LATER
        real_callout = _ROUND_CALLOUTS[r] if r < len(_ROUND_CALLOUTS) else None
        if real_callout and real_callout.exists():
            # The user's own recorded voice for this exact callout --
            # used verbatim, no TTS/stylizing needed.
            round_audio = real_callout
            round_dur = max(probe_duration(str(round_audio)), round_dur_floor)
        else:
            round_audio, round_dur = _get_line_audio(None, f"{round_label}! Fight!", voices["announcer"], {}, tmp_dir, f"round{r}")
            styled_round_audio = tmp_dir / f"audio_round{r}_styled.mp3"
            stylize_announcer_audio(round_audio, styled_round_audio)
            round_audio = styled_round_audio
            round_dur = max(probe_duration(str(round_audio)), round_dur_floor)
        round_overlay = _render_arena_overlay(round(hp["a"]), round(hp["b"]), names, photos, f"{round_label}\nFIGHT!", _TEXT_COLOR)
        seg = next_seg(f"r{r}_card")
        _seg_arena(backdrop_path, sprite_a, idle_x_a, sprite_b, idle_x_b, round_overlay, round_audio, round_dur, flash=False, out_path=seg, music_bed=music_bed_path)
        segments.append(seg)

        for side in ("a", "b"):
            opponent = "b" if side == "a" else "a"
            text = _clean(rnd.get(f"{side}_line"))
            audio_path, dur = _get_line_audio(turn_idx, text, voices[side], external_audio, tmp_dir, f"r{r}{side}")
            turn_idx += 1
            words = _line_word_timestamps(audio_path)

            # 1) real footage while they speak -- arena NOT visible
            clip_pool = clip_pools[side]
            overlay_png = tmp_dir / f"overlay_r{r}{side}.png"
            _render_footage_overlay(names[side], text, colors[side], overlay_png, show_text=not words)
            seg = next_seg(f"r{r}{side}_line")
            if clip_pool:
                clip = clip_pool[r % len(clip_pool)]
                _seg_footage_line(clip, overlay_png, audio_path, dur, seg, music_bed=music_bed_path, words=words)
            else:
                fallback_bg = tmp_dir / f"fallback_r{r}{side}.png"
                _build_bg(fallback_bg)
                composed = Image.open(fallback_bg).convert("RGBA")
                composed.alpha_composite(Image.open(overlay_png).convert("RGBA"))
                composed.convert("RGB").save(str(overlay_png), quality=92)
                _seg_card(overlay_png, audio_path, dur, seg, music_bed=music_bed_path, words=words)
            segments.append(seg)

            # 2) hard cut back to the arena -- attacker lunges, defender
            #    recoils+flashes if it lands, HP updates
            landed = (side == round_winner)
            if landed:
                opponent_hp = opponent
                hp[opponent_hp] = max(0.0, hp[opponent_hp] - dec)
                choices = [l for l in _LANDED_LABELS if l != last_landed_label]
                label = random.choice(choices)
                last_landed_label = label
                last_landed_hit_idx = (last_landed_hit_idx + 1) % len(landed_variants)
                sfx = landed_variants[last_landed_hit_idx]
                label_color = (255, 210, 60, 255)
            else:
                label = random.choice(_MISSED_LABELS)
                sfx = missed_sfx
                label_color = (200, 200, 200, 255)

            lunge_amt = 60
            recoil_amt = 35
            rot_amt = 0.3  # radians -- attacker leans/twists into the swing
            rot_a_expr = rot_b_expr = None
            # "Hit-stop" style: a quick strike up to the impact moment, THEN
            # FREEZE at full extension for the rest of the segment -- the
            # punch itself stays snappy instead of playing out in unnatural
            # slow motion, but the impact POSE holds long enough to
            # actually see it, instead of the sprite swinging straight back
            # to idle right as the burst appears. hit_dur shrinks round by
            # round (see _hit_dur_for_round) -- a flat 2.5s freeze on all 6
            # hits/video was a real, measured retention contributor.
            hit_dur = _hit_dur_for_round(r, n_rounds)
            ia = hit_dur * _IMPACT_AT_FRACTION
            strike = f"sin(PI*t/(2*{ia}))"
            hold = f"if(lt(t,{ia}),{{amt}}*{strike},{{amt}})"
            if side == "a":
                x_a = f"{_BASE_X_A}+{hold.format(amt=lunge_amt)}"
                x_b = f"{_BASE_X_B}+{hold.format(amt=recoil_amt)}" if landed else idle_x_b
                rot_a_expr = hold.format(amt=rot_amt)
            else:
                x_b = f"{_BASE_X_B}-{hold.format(amt=lunge_amt)}"
                x_a = f"{_BASE_X_A}-{hold.format(amt=recoil_amt)}" if landed else idle_x_a
                rot_b_expr = f"-{hold.format(amt=rot_amt)}"

            hit_overlay = _render_arena_overlay(round(hp["a"]), round(hp["b"]), names, photos, label, label_color)
            low_hp_sides = tuple(s for s in ("a", "b") if hp[s] <= _LOW_HP_THRESHOLD)
            seg = next_seg(f"r{r}{side}_hit")
            _seg_arena(
                backdrop_path, sprite_a, x_a, sprite_b, x_b, hit_overlay, sfx, hit_dur,
                flash=landed, out_path=seg, rot_a=rot_a_expr, rot_b=rot_b_expr,
                impact_effect="burst" if landed else "spark",
                defender_reaction_side=opponent if landed else None,
                attacker_reaction=(side, "confident" if landed else "embarrassed"),
                low_hp_sides=low_hp_sides,
                impact_at=ia,
                music_bed=music_bed_path,
            )
            segments.append(seg)

    # -- K.O.: loser's sprite pre-rotated (fallen), winner still bouncing --
    final_loser = "b" if winner == "a" else "a"
    hp[final_loser] = 0.0
    fallen_path = tmp_dir / f"sprite_{final_loser}_fallen.png"
    fallen_sprite = _make_fallen_sprite(sprites[final_loser], fallen_path) if sprites[final_loser] else None
    ko_dur = max(probe_duration(str(ko_sfx)), 1.3)
    ko_overlay = _render_arena_overlay(round(hp["a"]), round(hp["b"]), names, photos, "K.O.", (240, 60, 60, 255))
    if final_loser == "a":
        sprite_fallen_a, x_fallen_a = fallen_sprite, str(_BASE_X_A)
        sprite_fallen_b, x_fallen_b = sprite_b, idle_x_b
    else:
        sprite_fallen_a, x_fallen_a = sprite_a, idle_x_a
        sprite_fallen_b, x_fallen_b = fallen_sprite, str(_BASE_X_B)
    seg = next_seg("ko")
    _seg_arena(backdrop_path, sprite_fallen_a, x_fallen_a, sprite_fallen_b, x_fallen_b, ko_overlay, ko_sfx, ko_dur, flash=True, out_path=seg, music_bed=music_bed_path)
    segments.append(seg)

    # -- winner_line (announcer, existing card style) + loser_line (loser's own footage) --
    winner_idx = len(turns) - 2
    loser_idx = len(turns) - 1
    winner_audio, winner_dur = _get_line_audio(winner_idx, turns[-2][1], voices["announcer"], external_audio, tmp_dir, "winner")
    winner_overlay_png = tmp_dir / "winner_overlay.png"
    _render_centered_text_overlay(turns[-2][1], winner_overlay_png, size=42)
    seg = next_seg("winner")
    # The moral/takeaway line plays over the WINNER's own real footage
    # (their own reserved spare clip, index -1 -- like the loser's closing
    # clip, never touched by the round loop, so it's not a repeat) instead
    # of a flat gradient card, which read as a dead spot right before the
    # ending.
    winner_clip_pool = clip_pools[winner]
    if winner_clip_pool:
        winner_clip = winner_clip_pool[-1] if len(winner_clip_pool) > n_rounds else winner_clip_pool[0]
        _seg_footage_line(winner_clip, winner_overlay_png, winner_audio, winner_dur, seg, music_bed=music_bed_path)
    else:
        winner_bg = tmp_dir / "winner_bg.png"
        _build_bg(winner_bg)
        composed = Image.open(winner_bg).convert("RGBA")
        composed.alpha_composite(Image.open(winner_overlay_png).convert("RGBA"))
        composed.convert("RGB").save(str(winner_overlay_png), quality=92)
        _seg_card(winner_overlay_png, winner_audio, winner_dur, seg, music_bed=music_bed_path)
    segments.append(seg)

    loser_audio, loser_dur = _get_line_audio(loser_idx, turns[-1][1], voices[final_loser], external_audio, tmp_dir, "loser")
    loser_words = _line_word_timestamps(loser_audio)
    loser_overlay_png = tmp_dir / "loser_overlay.png"
    _render_footage_overlay(names[final_loser], turns[-1][1], colors[final_loser], loser_overlay_png, show_text=not loser_words)
    seg = next_seg("loser")
    loser_clip_pool = clip_pools[final_loser]
    if loser_clip_pool:
        # index -1 is the spare 4th clip, never touched by the round loop
        # (which only ever indexes r % n_rounds) -- guarantees a scene the
        # viewer hasn't already seen earlier in this same video.
        loser_clip = loser_clip_pool[-1] if len(loser_clip_pool) > n_rounds else loser_clip_pool[0]
        _seg_footage_line(loser_clip, loser_overlay_png, loser_audio, loser_dur, seg, music_bed=music_bed_path, words=loser_words)
    else:
        loser_bg = tmp_dir / "loser_bg.png"
        _build_bg(loser_bg)
        composed = Image.open(loser_bg).convert("RGBA")
        composed.alpha_composite(Image.open(loser_overlay_png).convert("RGBA"))
        composed.convert("RGB").save(str(loser_overlay_png), quality=92)
        _seg_card(loser_overlay_png, loser_audio, loser_dur, seg, music_bed=music_bed_path, words=loser_words)
    segments.append(seg)

    # -- closing CTA: fixed engagement prompt, announcer voice, always
    #    English regardless of content language (a generic template line,
    #    not something that needs per-video generation) --
    cta_text = "Which duel do you want to see next?\nDrop it in the comments!"
    cta_audio, cta_dur = _get_line_audio(None, "Which duel do you want to see next? Drop it in the comments!", voices["announcer"], {}, tmp_dir, "cta")
    cta_dur = max(cta_dur, 1.6)
    cta_png = tmp_dir / "cta.png"
    _render_static_card(cta_text, cta_png, size=44)
    seg = next_seg("cta"); _seg_card(cta_png, cta_audio, cta_dur, seg, music_bed=music_bed_path); segments.append(seg)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format15_{slug}.mp4"
    _concat_segments(segments, out)

    total = probe_duration(str(out))
    logger.info(
        f"Assembling format15 (duel) | {name_a} vs {name_b} | winner={winner} "
        f"| segments={len(segments)} | total={total:.1f}s"
    )

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out
