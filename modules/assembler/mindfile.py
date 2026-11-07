"""
MindFile brand assemblers (Applied Psychology / Human Behavior, see
users/mindfile/) -- the formats that need a thin new orchestrator on top of
shared engines already built for ResetWorth:

  mf5      "Two Minds"       -- reuses format11's two-voice TTS engine
                                 (modules.assembler.dialogue) with its own
                                 voices/labels/colors (IMPULSE vs REASON),
                                 a real video background, and tension music.
  mf6      "The Exchange"     -- reuses format10's chat-bubble engine
                                 (modules.assembler.chatfeed -- _build_bg/
                                 _render_thread/_chatfeed_cmd are already
                                 side-agnostic, only assemble_format10's OWN
                                 message-list construction hardcodes hook=
                                 left/body+cta=right) with a real alternating
                                 two-person exchange instead of one-sided
                                 chat, plus a distinct reveal card.
  mf_long  "MindFile Deep Dive" -- reuses long1's beat-rendering machinery
                                 (fetch real photos, Ken Burns clip, chapter
                                 cards, concat-demuxer stitching, music mux)
                                 but with a single synthesized TTS narrator
                                 voice (voice_bank archetype) instead of a
                                 real uploaded human recording -- same
                                 automation trade-off long3 already made
                                 relative to long1.

mf1/mf2/mf3 need no new code at all -- they dispatch straight to the
existing assemble_voice_format/assemble_format3a/assemble_format6 engines
(see modules/assembler/__init__.py's dispatchers dict), driven purely by
their own script_generator instructions.

(mf4 "Your Brain's Blind Spot" existed briefly as its own forked function --
a photo-backdrop fork of format8's pov_graph engine -- but was cut entirely
2026-07-28 per explicit user feedback ("j'ai pas compris l'objectif du
truc"). Don't re-add without asking first; if resurrected, note the fork
pattern is still the right one -- see modules/assembler/pov_graph.py for the
untouched ResetWorth original.)
"""

import logging
import shutil
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text, _FONT_REGULAR, _FONT_SEMIBOLD, _TEXT_COLOR,
    _NAIVE_COLOR, _REAL_COLOR, _loop_clips_to_duration,
)
from modules.assembler.chatfeed import _chatfeed_cmd
from modules.assembler.dialogue import build_dialogue_track, _render_turn, _build_bg
from modules.assembler.long1 import (
    _CHAPTER_CARD_DUR, _MAX_IMAGES_PER_BEAT, _MAX_IMAGE_HOLD,
    _concat_av_clips, _fetch_beat_images, _mux_with_music,
    _render_beat_clip, _render_chapter_card_clip,
)
from modules.assembler.voice_bank import select_voice
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)


# ── mf5 "Two Minds" ───────────────────────────────────────────────────────

_MF5_VOICES = {
    "impulse": "en-US-AriaNeural",   # quick, sharp, reactive register
    "reason":  "en-US-SteffanNeural",  # calm, measured register
}
_MF5_LABELS = {"impulse": "IMPULSE", "reason": "REASON"}
_MF5_COLORS = {"impulse": _NAIVE_COLOR, "reason": _REAL_COLOR}

_MF5_FALLBACK_KEYWORDS = ["person thinking pensive close up", "quiet room alone at night", "staring out window undecided"]
_MF5_BG_DARKEN = "eq=brightness=-0.35:contrast=1.05"   # keeps white text cards legible over real footage
_MF5_MUSIC_VOLUME = 0.15   # low and constant -- two voices carry the video, music is texture only


def _fetch_mf5_background(keywords: list[str], count: int, dest: Path) -> list[str]:
    """Real B-roll behind the dialogue cards. mf5 originally shared format11's
    flat charcoal-gradient background as-is (fine for ResetWorth's own
    format11, left completely untouched here) -- user feedback (2026-07-28)
    was blunt: a flat background reads as cheap for a video this length.
    Pexels only, no Drive/luxury-keyword logic -- that's ResetWorth's own
    library, deliberately not reused for MindFile (same reasoning as mf2)."""
    from modules.visuals import _fetch_pexels_videos
    clips = _fetch_pexels_videos(keywords or _MF5_FALLBACK_KEYWORDS, count, dest)
    if not clips:
        clips = _fetch_pexels_videos(_MF5_FALLBACK_KEYWORDS, count, dest)
    return clips


def _mix_background_music(dialogue_audio: Path, music_path: str | None, tmp_dir: Path) -> Path:
    """Low-volume tension music under the dialogue track -- mf5 had NO music
    at all before (format11's own design: "the two voices carry the whole
    audio track"), but user feedback (2026-07-28) asked for a little
    tension-building bed. Kept quiet enough to never compete with the two
    voices; falls back to the plain dialogue track if no music is available."""
    if not music_path:
        return dialogue_audio
    dur = probe_duration(str(dialogue_audio))
    fade_out = max(0.0, dur - 2.0)
    mixed = tmp_dir / "dialogue_with_music.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", str(dialogue_audio), "-stream_loop", "-1", "-i", music_path,
        "-filter_complex",
        f"[1:a]volume={_MF5_MUSIC_VOLUME},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.2f}:d=2[music];"
        f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "[aout]", "-t", f"{dur:.3f}",
        str(mixed),
    ]
    _run_ffmpeg(cmd)
    return mixed


def _mf5_video_cmd(
    bg_clips: list[str], overlay_specs: list[tuple[Path, float, float]],
    audio_path: Path, total_dur: float, output_path: str,
) -> list[str]:
    """Same per-turn overlay-timing pattern as dialogue.py's _dialogue_cmd,
    but input 0..N-1 is a darkened, looped REAL video background instead of
    one static flat-gradient PNG."""
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
    n_clips = len(bg_clips)
    cmd = ["ffmpeg", "-y"]
    for clip in bg_clips:
        if clip.lower().endswith(_IMAGE_EXTS):
            cmd += ["-loop", "1", "-i", clip]
        else:
            cmd += ["-i", clip]
    for png, _, _ in overlay_specs:
        cmd += ["-loop", "1", "-i", str(png)]
    audio_idx = n_clips + len(overlay_specs)
    cmd += ["-i", str(audio_path)]

    filters = []
    for i in range(n_clips):
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,{_MF5_BG_DARKEN}[s{i}]"
        )
    if n_clips == 1:
        filters.append(f"[s0]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[base]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(n_clips))
        filters.append(f"{concat_in}concat=n={n_clips}:v=1:a=0[cat]")
        filters.append(f"[cat]trim=duration={total_dur:.3f},setpts=PTS-STARTPTS[base]")

    prev = "base"
    for j, (_, start, end) in enumerate(overlay_specs):
        png_idx = n_clips + j
        lbl = "vout" if j == len(overlay_specs) - 1 else f"vo{j}"
        filters.append(f"[{png_idx}:v]format=rgba,fade=t=in:st={start:.3f}:d=0.35:alpha=1[fov{j}]")
        filters.append(f"[{prev}][fov{j}]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[{lbl}]")
        prev = lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]", "-map", f"{audio_idx}:a"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-t", str(total_dur),
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def assemble_mf5(script, visuals_result, voice_result, timestamps_result, music_path):
    """Two-voice "Impulse vs Reason" dialogue -- reuses format11's audio
    engine (build_dialogue_track/_render_turn) unchanged, but with its own
    real-video background + low-volume tension music instead of format11's
    flat gradient + no music (see module docstring: format11/ResetWorth
    itself is untouched). Ignores visuals_result/voice_result/timestamps_result,
    fetches its own B-roll and generates its own dialogue audio internally."""
    hook = (script.get("hook") or "").strip()
    body = [b.strip() for b in (script.get("body") or []) if b.strip()]
    cta  = (script.get("cta") or "").strip()

    lines = [hook] + body[:4] + ([cta] if cta else [])
    speakers = ["impulse", "reason", "impulse", "reason", "impulse", "reason"][:len(lines)]
    turns = list(zip(speakers, lines))

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_mf5"
    # rmtree, not a glob+unlink loop: bg_fetch (below) leaves a leftover
    # subdirectory behind, and unlink() on a directory raises
    # [WinError 5] Access is denied on Windows instead of cleaning it up.
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(exist_ok=True)

    audio_path, turn_durations = build_dialogue_track(turns, tmp_dir, _MF5_VOICES)
    total = probe_duration(str(audio_path))

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)
    mixed_audio = _mix_background_music(audio_path, music_path, tmp_dir)

    keywords = [k for k in (script.get("visual_keywords") or []) if k.strip()][:4]
    bg_session = tmp_dir / "bg_fetch"
    bg_session.mkdir(exist_ok=True)
    bg_clips = _fetch_mf5_background(keywords, 4, bg_session)
    if bg_clips:
        bg_clips = _loop_clips_to_duration(bg_clips, total)
    else:
        # Pexels came up completely empty even on the fallback keywords --
        # fall back to format11's own flat-gradient PNG rather than failing
        # the whole render over a background image.
        bg_png = tmp_dir / "bg.png"
        _build_bg(bg_png)
        bg_clips = [str(bg_png)]

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0
    for i, ((speaker, text), dur) in enumerate(zip(turns, turn_durations)):
        png = tmp_dir / f"turn_{i}.png"
        _render_turn(speaker, text, png, _MF5_LABELS, _MF5_COLORS)
        overlay_specs.append((png, t, t + dur))
        t += dur + 0.35

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_mf5_{slug}.mp4"

    cmd = _mf5_video_cmd(bg_clips, overlay_specs, mixed_audio, total, str(out))
    logger.info(f"Assembling mf5 (Two Minds) | turns={len(turns)} | total={total:.1f}s | bg_clips={len(bg_clips)}")
    _run_ffmpeg(cmd, cwd=str(tmp_dir))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


# ── mf6 "The Exchange" ─────────────────────────────────────────────────────
#
# Redone 2026-07-28 to actually read as an iPhone Messages screenshot (user
# feedback: the generic dark-chat look wasn't recognizable as texting) --
# true-black iOS dark-mode canvas, a stylized status bar (time/signal/wifi/
# battery), a generic circular-avatar contact header (no real photo -- this
# format's whole premise is a composite/generic exchange, never a real
# person), and actual iMessage-style bubbles (blue sent / dark-gray received,
# each with a small tail). This is its OWN rendering, not a reuse of
# chatfeed.py's _build_bg/_render_thread (format10/ResetWorth's generic dark
# chat mockup stays untouched) -- only _chatfeed_cmd (the generic ffmpeg
# assembly step, purely mechanical) is still shared.

# Per-message hold is ADAPTIVE (2026-07-28 user feedback: fixed 2.3s read as
# "too fast, not tied to how long a message actually takes to read"; a first
# adaptive pass was STILL too fast per a second round of feedback -- slowed
# down further below). See _mf6_reading_hold. _MF6_REVEAL_HOLD stays fixed
# (the payoff beat, always given a generous, consistent hold regardless of
# its own word count).
_MF6_READING_WPS = 2.8     # words/sec -- slowed down from an initial 4.0/sec, still too fast
_MF6_MIN_HOLD    = 2.0
_MF6_MAX_HOLD    = 5.5
_MF6_APPEAR_PAD  = 0.7     # extra beat so a bubble isn't judged "read" the instant it appears
_MF6_REVEAL_HOLD = 4.5   # the reveal card gets a longer beat -- it's the whole payoff
_MF6_DIM_ALPHA   = 0.82

# POV card hold is now driven by its own TTS narration length (2026-07-28
# user feedback: add a voice reading the POV line, and the old fixed 2.0s
# hold was too short anyway) -- see _synthesize_mf6_pov/assemble_mf6.
_MF6_POV_VOICE       = "en-US-JennyNeural"   # same default narrator voice as mf1, for a consistent MindFile "voice"
_MF6_POV_MIN_HOLD    = 1.8   # floor, in case TTS synthesis fails and falls back to silent
_MF6_POV_TAIL_PAD    = 0.5   # extra beat after the voice finishes before cutting to the phone
_MF6_MUSIC_VOLUME    = 0.18  # background music level under the POV narration + the whole video

_MF6_BG_COLOR       = (0, 0, 0)
_MF6_STATUS_H       = 60
_MF6_HEADER_H       = 150
_MF6_TOP_H          = _MF6_STATUS_H + _MF6_HEADER_H
_MF6_HOME_INDICATOR_H = 34   # reserved bottom strip for the input bar + home indicator
_MF6_BOTTOM_H       = 100    # input-bar row height
_MF6_SENT_FILL      = (10, 132, 255, 255)     # iMessage blue
_MF6_RECEIVED_FILL  = (38, 38, 42, 255)       # iOS dark-mode received gray
_MF6_BUBBLE_TEXT    = (255, 255, 255, 255)
_MF6_MUTED_TEXT     = (140, 140, 145, 255)
_MF6_BUBBLE_RADIUS  = 32
_MF6_BUBBLE_PAD_X   = 28
_MF6_BUBBLE_PAD_Y   = 20
_MF6_BUBBLE_GAP     = 18
_MF6_BUBBLE_MAX_W   = int(VIDEO_WIDTH * 0.72)
_MF6_SIDE_MARGIN    = 36
_MF6_TAIL_SIZE      = 16


def _mf6_reading_hold(text: str) -> float:
    """How long to hold a bubble before the next one sends -- proportional
    to how long it actually takes to read it, not a flat timer (2026-07-28
    user feedback: "dès que le lecteur finit un message, un autre s'envoie",
    not before, not long after)."""
    n_words = max(1, len(text.split()))
    dur = n_words / _MF6_READING_WPS + _MF6_APPEAR_PAD
    return max(_MF6_MIN_HOLD, min(_MF6_MAX_HOLD, dur))


def _draw_status_bar(draw: ImageDraw.ImageDraw) -> None:
    """Time + signal/wifi/battery glyphs -- the fastest visual cue that this
    is a phone screenshot, not a generic chat mockup. 9:41 is Apple's own
    long-standing demo-screenshot time, deliberately reused here."""
    font = ImageFont.truetype(_FONT_SEMIBOLD, size=30)
    draw.text((40, 16), "9:41", font=font, fill=(255, 255, 255, 255))

    # Signal bars (4, increasing height)
    bx = VIDEO_WIDTH - 168
    for i, h in enumerate((9, 13, 17, 21)):
        x0 = bx + i * 10
        y1 = 38
        draw.rounded_rectangle([x0, y1 - h, x0 + 6, y1], radius=2, fill=(255, 255, 255, 255))

    # Wifi (two nested arcs + dot)
    wx, wy = VIDEO_WIDTH - 116, 30
    draw.arc([wx - 15, wy - 15, wx + 15, wy + 15], start=225, end=315, fill=(255, 255, 255, 255), width=3)
    draw.arc([wx - 8, wy - 8, wx + 8, wy + 8], start=225, end=315, fill=(255, 255, 255, 255), width=3)
    draw.ellipse([wx - 2, wy - 2, wx + 2, wy + 2], fill=(255, 255, 255, 255))

    # Battery (outline + nub + ~80% fill)
    bx2, by2, bw2, bh2 = VIDEO_WIDTH - 84, 16, 48, 24
    draw.rounded_rectangle([bx2, by2, bx2 + bw2, by2 + bh2], radius=6, outline=(255, 255, 255, 255), width=2)
    draw.rounded_rectangle([bx2 + bw2 + 3, by2 + 7, bx2 + bw2 + 7, by2 + bh2 - 7], radius=2, fill=(255, 255, 255, 255))
    fill_w = int((bw2 - 6) * 0.8)
    draw.rounded_rectangle([bx2 + 3, by2 + 3, bx2 + 3 + fill_w, by2 + bh2 - 3], radius=3, fill=(255, 255, 255, 255))


def _draw_dynamic_island(draw: ImageDraw.ImageDraw) -> None:
    """The single most recognizable modern-iPhone UI element -- a black
    rounded pill centered at the very top of the screen."""
    w, h = 128, 36
    x0 = (VIDEO_WIDTH - w) // 2
    y0 = 10
    draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2, fill=(0, 0, 0, 255))


def _draw_back_chevron(draw: ImageDraw.ImageDraw, y: int) -> None:
    """Small "<" chevron -- iOS Messages' own back-navigation affordance,
    top-left of the contact header row."""
    x, cy = 34, y + 20
    draw.line([(x + 14, cy - 16), (x, cy), (x + 14, cy + 16)], fill=(10, 132, 255, 255), width=5, joint="curve")


def _draw_input_bar(draw: ImageDraw.ImageDraw) -> None:
    """The persistent message-compose bar at the bottom -- present in every
    real Messages screenshot/recording, one of the biggest tells that a
    mockup ISN'T actually an iPhone when it's missing."""
    bar_y0 = VIDEO_HEIGHT - _MF6_BOTTOM_H
    bar_y1 = VIDEO_HEIGHT - _MF6_HOME_INDICATOR_H - 14

    # "+" attachment button
    plus_cx, plus_cy = _MF6_SIDE_MARGIN + 20, (bar_y0 + bar_y1) // 2
    draw.ellipse([plus_cx - 20, plus_cy - 20, plus_cx + 20, plus_cy + 20], outline=(120, 120, 126, 255), width=3)
    draw.line([(plus_cx - 10, plus_cy), (plus_cx + 10, plus_cy)], fill=(120, 120, 126, 255), width=3)
    draw.line([(plus_cx, plus_cy - 10), (plus_cx, plus_cy + 10)], fill=(120, 120, 126, 255), width=3)

    # Text-field pill
    field_x0 = plus_cx + 32
    field_x1 = VIDEO_WIDTH - _MF6_SIDE_MARGIN - 50
    draw.rounded_rectangle([field_x0, bar_y0, field_x1, bar_y1], radius=(bar_y1 - bar_y0) // 2,
                           outline=(70, 70, 74, 255), width=2)
    placeholder_font = ImageFont.truetype(_FONT_REGULAR, size=26)
    draw.text((field_x0 + 26, (bar_y0 + bar_y1) // 2 - 16), "iMessage", font=placeholder_font, fill=(110, 110, 115, 255))

    # Mic button (right of the field)
    mic_cx, mic_cy = VIDEO_WIDTH - _MF6_SIDE_MARGIN - 20, (bar_y0 + bar_y1) // 2
    draw.rounded_rectangle([mic_cx - 12, mic_cy - 18, mic_cx + 12, mic_cy + 10], radius=12, outline=(120, 120, 126, 255), width=3)
    draw.line([(mic_cx, mic_cy + 10), (mic_cx, mic_cy + 20)], fill=(120, 120, 126, 255), width=3)
    draw.line([(mic_cx - 8, mic_cy + 20), (mic_cx + 8, mic_cy + 20)], fill=(120, 120, 126, 255), width=3)


def _draw_home_indicator(draw: ImageDraw.ImageDraw) -> None:
    w, h = 140, 5
    x0 = (VIDEO_WIDTH - w) // 2
    y0 = VIDEO_HEIGHT - 18
    draw.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=h // 2, fill=(255, 255, 255, 255))


def _make_generic_avatar(size: int) -> Image.Image:
    """Flat gray circular avatar with a generic person silhouette -- never a
    real photo, this format's contacts are always anonymous/composite."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    body = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.ellipse((0, 0, size, size), fill=(58, 58, 62, 255))
    bd.ellipse((size * 0.32, size * 0.16, size * 0.68, size * 0.52), fill=(148, 148, 152, 255))
    bd.ellipse((size * 0.10, size * 0.54, size * 0.90, size * 1.15), fill=(148, 148, 152, 255))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(body, (0, 0), mask)
    return out


def _mf6_build_bg(out_path: Path) -> None:
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), _MF6_BG_COLOR)
    draw = ImageDraw.Draw(img)
    _draw_status_bar(draw)
    _draw_dynamic_island(draw)
    _draw_back_chevron(draw, _MF6_STATUS_H)

    avatar = _make_generic_avatar(84)
    ax = (VIDEO_WIDTH - 84) // 2
    ay = _MF6_STATUS_H + 14
    img.paste(avatar, (ax, ay), avatar)

    name_font = ImageFont.truetype(_FONT_REGULAR, size=26)
    name_bbox = draw.textbbox((0, 0), "Unknown", font=name_font)
    nx = (VIDEO_WIDTH - (name_bbox[2] - name_bbox[0])) // 2
    ny = ay + 84 + 10
    draw.text((nx, ny), "Unknown", font=name_font, fill=_MF6_MUTED_TEXT[:3])
    draw.line([(0, _MF6_TOP_H), (VIDEO_WIDTH, _MF6_TOP_H)], fill=(28, 28, 30), width=2)

    draw.line([(0, VIDEO_HEIGHT - _MF6_BOTTOM_H), (VIDEO_WIDTH, VIDEO_HEIGHT - _MF6_BOTTOM_H)],
              fill=(28, 28, 30), width=2)
    _draw_input_bar(draw)
    _draw_home_indicator(draw)

    img.save(str(out_path), quality=92)


def _mf6_wrap_bubble_text(text: str, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> tuple[str, int, int]:
    max_text_w = _MF6_BUBBLE_MAX_W - 2 * _MF6_BUBBLE_PAD_X
    for width_chars in range(60, 8, -2):
        wrapped = textwrap.fill(text, width=width_chars, break_long_words=False)
        bbox = draw.textbbox((0, 0), wrapped, font=font, align="left", spacing=8)
        if bbox[2] - bbox[0] <= max_text_w:
            return wrapped, bbox[2] - bbox[0], bbox[3] - bbox[1]
    return text, max_text_w, font.size


def _draw_imessage_bubble(draw: ImageDraw.ImageDraw, side: str, x0: int, y0: int, bw: int, bh: int) -> None:
    """Rounded rect + a small tail on the OUTER bottom corner. Every message
    in this format alternates sides (no consecutive same-sender runs), so
    unlike real Messages (tail only on the last bubble of a run) every
    single bubble here gets its own tail -- visually correct for this case."""
    fill = _MF6_SENT_FILL if side == "right" else _MF6_RECEIVED_FILL
    draw.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=_MF6_BUBBLE_RADIUS, fill=fill)
    if side == "right":
        tail = [(x0 + bw - 22, y0 + bh - 8), (x0 + bw + _MF6_TAIL_SIZE, y0 + bh + _MF6_TAIL_SIZE),
                (x0 + bw - 4, y0 + bh - 2)]
    else:
        tail = [(x0 + 22, y0 + bh - 8), (x0 - _MF6_TAIL_SIZE, y0 + bh + _MF6_TAIL_SIZE), (x0 + 4, y0 + bh - 2)]
    draw.polygon(tail, fill=fill)


def _mf6_render_thread(messages: list[tuple[str, str]], out_path: Path) -> None:
    """Same cumulative-reveal idea as chatfeed._render_thread (drop oldest
    bubbles that would overflow), but iMessage bubble styling."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font = ImageFont.truetype(_FONT_REGULAR, size=34)

    max_bottom = VIDEO_HEIGHT - _MF6_BOTTOM_H - 20
    measured = []
    for side, text in messages:
        wrapped, tw, th = _mf6_wrap_bubble_text(text, font, draw)
        bh = th + 2 * _MF6_BUBBLE_PAD_Y
        measured.append((side, wrapped, tw, th, bh))

    visible = list(measured)
    total_h = sum(m[4] + _MF6_BUBBLE_GAP + _MF6_TAIL_SIZE for m in visible)
    while visible and total_h > (max_bottom - (_MF6_TOP_H + 30)):
        dropped = visible.pop(0)
        total_h -= (dropped[4] + _MF6_BUBBLE_GAP + _MF6_TAIL_SIZE)

    y = _MF6_TOP_H + 30
    for side, wrapped, tw, th, bh in visible:
        bw = tw + 2 * _MF6_BUBBLE_PAD_X
        x0 = _MF6_SIDE_MARGIN if side == "left" else VIDEO_WIDTH - _MF6_SIDE_MARGIN - bw
        _draw_imessage_bubble(draw, side, x0, y, bw, bh)
        _soft_text(overlay, (x0 + _MF6_BUBBLE_PAD_X, y + _MF6_BUBBLE_PAD_Y), wrapped, font,
                  _MF6_BUBBLE_TEXT, align="left", spacing=8)
        y += bh + _MF6_TAIL_SIZE + _MF6_BUBBLE_GAP

    if visible and visible[-1][0] == "right":
        delivered_font = ImageFont.truetype(_FONT_REGULAR, size=22)
        bbox = draw.textbbox((0, 0), "Delivered", font=delivered_font)
        dx = VIDEO_WIDTH - _MF6_SIDE_MARGIN - (bbox[2] - bbox[0])
        _soft_text(overlay, (dx, y - _MF6_BUBBLE_GAP + 4), "Delivered", delivered_font, _MF6_MUTED_TEXT,
                  multiline=False)

    overlay.save(str(out_path), format="PNG")


def _render_mf6_reveal(bg_png: Path, thread_png: Path, reveal_text: str, out_path: Path) -> None:
    """Distinct reveal beat -- dims the completed thread and names the real
    tactic in bold centered text, instead of just another chat bubble. This
    is the "screenshot this" payoff the whole exchange builds to."""
    bg = Image.open(bg_png).convert("RGBA")
    thread = Image.open(thread_png).convert("RGBA")
    frame = Image.alpha_composite(bg, thread)
    dim = Image.new("RGBA", frame.size, (2, 2, 3, int(_MF6_DIM_ALPHA * 255)))
    frame = Image.alpha_composite(frame, dim)

    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=58)
    except OSError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(frame)
    wrapped = textwrap.fill(reveal_text, width=26, break_long_words=False)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1]
    _soft_text(frame, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=2)

    frame.convert("RGB").save(str(out_path), quality=92)


def _mf6_blurred_thread_bg(bg_png: Path, full_thread_png: Path) -> Image.Image:
    """The COMPLETE conversation, blurred + dimmed, as the POV card's
    backdrop (2026-07-28 user feedback) -- a visual tease of the payoff to
    come instead of a plain black card, without spoiling it (blur keeps the
    actual message text unreadable)."""
    bg = Image.open(bg_png).convert("RGBA")
    thread = Image.open(full_thread_png).convert("RGBA")
    frame = Image.alpha_composite(bg, thread)
    frame = frame.filter(ImageFilter.GaussianBlur(22))
    dim = Image.new("RGBA", frame.size, (0, 0, 0, int(0.58 * 255)))
    return Image.alpha_composite(frame, dim)


def _render_mf6_pov_card(text: str, blurred_bg: Image.Image, out_path: Path) -> None:
    """Quick flash hook BEFORE the phone thread appears (2026-07-28 user
    feedback: "juste au debut faut faire un POV rapide accrocheur avant de
    mettre direct la conv"), now shown over the blurred full conversation
    (see _mf6_blurred_thread_bg) instead of a plain black card."""
    frame = blurred_bg.copy()
    try:
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=60)
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(frame)
    wrapped = textwrap.fill(text, width=22, break_long_words=False)
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (VIDEO_HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1]
    _soft_text(frame, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=2)
    frame.convert("RGB").save(str(out_path), quality=92)


def _synthesize_mf6_pov(text: str, out_path: Path) -> bool:
    """TTS for the POV card (2026-07-28 user feedback: add a voice reading
    it). Returns False on any failure -- narration is a polish layer here,
    never worth failing the whole render over (same philosophy as this
    codebase's other TTS/fetch fallbacks)."""
    import asyncio

    from modules.assembler.dialogue import _synthesize
    try:
        asyncio.run(_synthesize(text, str(out_path), _MF6_POV_VOICE))
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        logger.warning(f"mf6: POV narration synthesis failed ({e}); card will be silent")
        return False


def _mix_mf6_audio(pov_audio: Path | None, music_path: str | None, total_dur: float, tmp_dir: Path) -> str | None:
    """Background music for the whole video (quiet, constant) + the POV
    narration layered on top for its own opening segment. Falls back
    gracefully if either piece is missing rather than failing the render."""
    if not music_path and not pov_audio:
        return None
    if not music_path:
        return str(pov_audio)

    mixed = tmp_dir / "mf6_audio_mix.mp3"
    cmd = ["ffmpeg", "-y"]
    if pov_audio:
        cmd += ["-i", str(pov_audio)]
    cmd += ["-stream_loop", "-1", "-i", music_path]

    if pov_audio:
        filters = (
            f"[0:a]apad=whole_dur={total_dur:.3f}[narr];"
            f"[1:a]volume={_MF6_MUSIC_VOLUME},atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS[music];"
            f"[narr][music]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
    else:
        filters = f"[1:a]volume={_MF6_MUSIC_VOLUME},atrim=0:{total_dur:.3f},asetpts=PTS-STARTPTS[aout]"
    cmd += ["-filter_complex", filters, "-map", "[aout]", "-t", f"{total_dur:.3f}", str(mixed)]
    _run_ffmpeg(cmd)
    return str(mixed)


def assemble_mf6(script, visuals_result, voice_result, timestamps_result, music_path):
    """"The Exchange" -- forked from format10's chatfeed engine (see module
    docstring). Ignores visuals_result/voice_result/timestamps_result: no
    B-roll, synthesizes its own POV narration and picks its own background
    music internally same as format10."""
    pov_hook = (script.get("pov_hook") or "").strip() or "POV: you notice a pattern in their texts."
    hook = (script.get("hook") or "").strip()
    body = [b.strip() for b in (script.get("body") or []) if b.strip()][:6]
    reveal = (script.get("cta") or "").strip() or "Notice the pattern, not just the person."

    sides = ["right", "left"]
    messages = [("left", hook)] + [(sides[i % 2], b) for i, b in enumerate(body)]

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_mf6"
    # rmtree, not a glob+unlink loop -- unlink() can't remove a directory
    # (same bug hit mf5's bg_fetch subdir: [WinError 5] Access is denied).
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(exist_ok=True)

    bg_png = tmp_dir / "bg.png"
    _mf6_build_bg(bg_png)

    # Full conversation, rendered once up front -- reused BOTH as the blurred
    # teaser behind the POV card (2026-07-28 user feedback) AND as the final
    # cumulative-reveal stage in the loop below, no need to render it twice.
    full_thread_png = tmp_dir / "stage_full.png"
    _mf6_render_thread(messages, full_thread_png)
    blurred_bg = _mf6_blurred_thread_bg(bg_png, full_thread_png)

    pov_audio_path = tmp_dir / "pov_voice.mp3"
    has_pov_voice = _synthesize_mf6_pov(pov_hook, pov_audio_path)
    pov_hold = (probe_duration(str(pov_audio_path)) + _MF6_POV_TAIL_PAD) if has_pov_voice else _MF6_POV_MIN_HOLD
    pov_hold = max(_MF6_POV_MIN_HOLD, pov_hold)

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0

    pov_png = tmp_dir / "pov.png"
    _render_mf6_pov_card(pov_hook, blurred_bg, pov_png)
    overlay_specs.append((pov_png, t, t + pov_hold))
    t += pov_hold

    last_thread_png = None
    for i, (side, text) in enumerate(messages, start=1):
        png = full_thread_png if i == len(messages) else tmp_dir / f"stage_{i}.png"
        if i != len(messages):
            _mf6_render_thread(messages[:i], png)
        hold = _mf6_reading_hold(text)
        overlay_specs.append((png, t, t + hold))
        t += hold
        last_thread_png = png

    reveal_png = tmp_dir / "reveal.png"
    _render_mf6_reveal(bg_png, last_thread_png, reveal, reveal_png)
    overlay_specs.append((reveal_png, t, t + _MF6_REVEAL_HOLD))
    t += _MF6_REVEAL_HOLD
    total = t

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)
    mixed_audio = _mix_mf6_audio(pov_audio_path if has_pov_voice else None, music_path, total, tmp_dir)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_mf6_{slug}.mp4"

    cmd = _chatfeed_cmd(bg_png, overlay_specs, total, mixed_audio, str(out))
    logger.info(f"Assembling mf6 (The Exchange) | messages={len(messages)} | total={total:.1f}s | pov_voice={has_pov_voice}")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


# ── mf_long "MindFile Deep Dive" ──────────────────────────────────────────

_MF_LONG_SECTION_ORDER = ["setup", "what_happened", "why_it_happens", "what_it_means"]


def build_beats(script: dict) -> list[dict]:
    """Same flat-beat-list shape as long1/long3's own build_beats."""
    beats: list[dict] = []
    sections = script.get("sections") or {}
    for sec_id in _MF_LONG_SECTION_ORDER:
        sec = sections.get(sec_id) or {}
        for i, b in enumerate(sec.get("beats") or []):
            text = (b.get("text") or "").strip()
            if text:
                beats.append({
                    "section": sec_id, "index": i, "text": text,
                    "visual_query": (b.get("visual_query") or text).strip(),
                })
    return beats


def _synthesize_narrator(text: str, voice_id: str, out_path: Path) -> None:
    """edge-tts narration -> re-encoded to the .mp3 shape _render_beat_clip
    expects (it just calls ffprobe/ffmpeg on whatever file is handed to it,
    so a plain edge-tts .mp3 output is already usable directly)."""
    import asyncio

    from modules.assembler.dialogue import _synthesize
    asyncio.run(_synthesize(text, str(out_path), voice_id))


def assemble_mf_long(script, visuals_result, voice_result, timestamps_result, music_path):
    """MindFile Deep Dive -- chaptered documentary, single synthesized TTS
    narrator voice (voice_bank archetype, never a real person), real fetched
    photos per beat (Ken Burns), reusing long1's beat/chapter-card renderers
    and concat-demuxer stitching wholesale. Ignores visuals_result/
    voice_result/timestamps_result (synthesizes its own narration)."""
    subject = script.get("case_subject") or script["subject"]
    sections = script.get("sections") or {}
    beats = build_beats(script)
    if not beats:
        raise RuntimeError("mf_long: no usable narration beats in script")

    profile = script.get("voice") or {}
    voice_id = select_voice(
        gender=profile.get("gender", "male"), accent=profile.get("accent", "american"),
        age_register=profile.get("age_register", "mature"), tone=profile.get("tone") or ["measured", "credible"],
    )

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_mf_long"
    # rmtree, not a glob+unlink loop -- unlink() can't remove a directory
    # (same bug hit mf5's bg_fetch subdir: [WinError 5] Access is denied).
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(exist_ok=True)

    clips: list[Path] = []

    for sec_id in _MF_LONG_SECTION_ORDER:
        sec = sections.get(sec_id) or {}
        sec_beats = sec.get("beats") or []
        if not sec_beats:
            continue

        if sec_id != "setup":
            # Chapter card opens every section except the opening "setup",
            # which flows straight from nothing into content (same
            # convention as long1's hook section).
            chapter_fetched = _fetch_beat_images(sec_beats[0].get("visual_query", ""), 1, tmp_dir, f"{sec_id}_chapter")
            chapter_clip = tmp_dir / f"{sec_id}_chapter.mp4"
            _render_chapter_card_clip(sec.get("label", sec_id), chapter_fetched[0] if chapter_fetched else None,
                                       _CHAPTER_CARD_DUR, chapter_clip)
            clips.append(chapter_clip)

        for i, beat in enumerate(sec_beats):
            text = (beat.get("text") or "").strip()
            if not text:
                continue
            audio_path = tmp_dir / f"{sec_id}_beat_{i}.mp3"
            _synthesize_narrator(text, voice_id, audio_path)
            dur = probe_duration(str(audio_path))
            if dur <= 0:
                continue
            n_images = min(_MAX_IMAGES_PER_BEAT, max(1, -(-dur // _MAX_IMAGE_HOLD)))  # ceil(dur / hold)
            fetched = _fetch_beat_images(beat.get("visual_query", ""), int(n_images), tmp_dir, f"{sec_id}_img_{i}")
            clip_path = tmp_dir / f"{sec_id}_clip_{i}.mp4"
            _render_beat_clip(fetched, audio_path, dur, clip_path)
            clips.append(clip_path)

    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    av_concat = tmp_dir / "av_concat.mp4"
    _concat_av_clips(clips, av_concat)
    total_dur = probe_duration(str(av_concat))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(subject)
    out  = _VIDEOS_DIR / f"{ts}_mf_long_{slug}.mp4"

    _mux_with_music(av_concat, music_path, total_dur, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"mf_long: video ready | {subject!r} | {len(clips)} clips | "
                f"{total_dur / 60:.1f} min | {size_mb:.1f} MB -> {out}")
    return out
