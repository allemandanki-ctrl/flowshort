"""
Assembler package entry point + shared helpers.

format3 : Pillow renders text on images -> FFmpeg slideshow (no voice)
format2 : B-roll video clips + voice + ASS subtitles
format4 : static image bg + voice + ASS subtitles (minimal bg, advice-driven)
format5 : narrative B-roll + voice + ASS subtitles (same as format2)
format1 : clip edit (assemble_clip_edit) -- dispatched directly by pipeline.py,
          NOT through assemble() below.
format7 : clip edit 2.0 (assemble_clip_edit_v2) -- same, direct dispatch only.

This module (__init__.py) holds every helper shared across the per-format
submodules (format1.py/format3.py/format6.py/misc.py/voice.py) plus the
format1/format7 clip-edit code and the hook-transition assembler, which
don't have a per-format submodule of their own. Shared names MUST be defined
here before the per-format submodules are imported at the bottom of this
file, since those submodules do `from modules.assembler import _run_ffmpeg`
etc. at their own top level.
"""

import json
import logging
import math
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter

from config import OUTPUT_DIR, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH

logger = logging.getLogger(__name__)

_VIDEOS_DIR = OUTPUT_DIR / "videos"

# Windows font paths (regular Python paths, no FFmpeg escaping needed here)
_FONT_BOLD_PATH   = r"C:\Windows\Fonts\arialbd.ttf"
_FONT_REG_PATH    = r"C:\Windows\Fonts\arial.ttf"
_FONT_IMPACT_PATH = r"C:\Windows\Fonts\impact.ttf"   # format6 number overlay

# Layout 1080x1920 (format1 clip edit, reference: style fxe_capital TikTok):
#   y=   0-660  : zone noire haut - titre LEFT-ALIGNED pres du bas (y=~610)
#   y= 660-1268 : clip 16:9 natural (1080x608, aucun crop)
#   y=1268-1920 : zone noire bas - ResetWorth discret en bas a gauche
_F1_QUOTE_H  = 660   # grande zone noire haut (34% de 1920)
_F1_CLIP_H   = 608   # clip 16:9 naturel (1080x608, no crop)
_F1_CLIP_Y   = _F1_QUOTE_H                     # 660
_F1_BOTTOM_H = 652   # zone noire bas (1920 - 660 - 608)
_F1_RW_X     = 20    # watermark bas-gauche dans zone noire bas
_F1_RW_Y     = 1840  # 80px avant bas de video
_F1_SUB_FONT = 52    # taille sous-titres - medium, style TikTok reference

# Plain white, black outline -- matches the color of every other caption in
# the video (grouped style, source clips' own burned-in captions) so the
# B-roll burst's word captions don't stand out as a different color.
_F1_WORD_BODY_COLOR    = "&H00FFFFFF"   # white
_F1_WORD_OUTLINE_COLOR = "&H00000000"   # black

# Loudness normalization targets (LUFS) for format1's speech/music mix -- see
# _clip_edit_cmd. Music sits a fixed 10 LU under speech regardless of how the
# source clip or the royalty-free track was originally mastered.
_SPEECH_TARGET_LUFS = -14.0
_MUSIC_TARGET_LUFS  = -24.0

# ── Shared "minimal motion graphics" visual language (format8, format9, ...) ──
# Charcoal gradient + thin Segoe UI + soft drop shadows + muted accents,
# tuned against a dark-3D-minimal reference. Shared across every no-voice/
# no-B-roll motion-graphics format so the channel reads as one visual system
# instead of each new format inventing its own look.
_FONT_REGULAR  = r"C:\Windows\Fonts\segoeui.ttf"     # headline
_FONT_LIGHT    = r"C:\Windows\Fonts\segoeuil.ttf"    # labels -- airier, thinner
_FONT_SEMIBOLD = r"C:\Windows\Fonts\segoeuib.ttf"    # CTA / emphasis -- more presence

_FADE_DUR = 0.35   # ffmpeg cross-fade duration between stages, this family of formats

_TEXT_COLOR  = (230, 230, 228, 255)   # soft off-white, not stark #fff
_NAIVE_COLOR = (128, 134, 146, 255)   # muted slate -- "the naive/wrong option"
_REAL_COLOR  = (214, 184, 133, 255)   # desaturated champagne -- "the real/right option"


def _slugify(text: str, max_len: int = 30) -> str:
    """
    Filesystem-safe filename slug. The older `text[:30].replace(" ", "_")`
    pattern used throughout this package leaves punctuation untouched --
    harmless for auto-generated wealth-niche subjects, but a subject like
    "Bruce Wayne / Batman" (format12's character-name subjects) turns the
    "/" into a literal path separator and ffmpeg fails with "No such file
    or directory". Strips anything that isn't a word char/space/hyphen
    instead of passing it through.
    """
    text = re.sub(r"[^\w\s-]", "_", text.strip())
    text = re.sub(r"[\s_-]+", "_", text)
    return text[:max_len].strip("_") or "video"


def _soft_text(overlay, xy, text, font, fill, align="left", spacing=10, multiline=True,
                stroke_width=0, stroke_fill=(0, 0, 0, 235)):
    """
    Soft blurred drop shadow + flat fill -- reads as elegant/quiet rather
    than punchy, matching the shared reference's thin type over a mid-gray
    backdrop (a hard black stroke barely shows on charcoal anyway -- it
    needs a near-black background to read).

    stroke_width>0 adds a hard outline on top of the soft shadow -- needed
    when the backdrop is real busy footage instead of a flat gradient (see
    format14), where the soft shadow alone doesn't guarantee contrast.
    """
    shadow = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    draw_fn = sd.multiline_text if multiline else sd.text
    kwargs = {"spacing": spacing, "align": align} if multiline else {}
    draw_fn((xy[0] + 3, xy[1] + 5), text, font=font, fill=(0, 0, 0, 130), **kwargs)
    shadow = shadow.filter(ImageFilter.GaussianBlur(6))
    overlay.alpha_composite(shadow)

    draw = ImageDraw.Draw(overlay)
    draw_fn = draw.multiline_text if multiline else draw.text
    stroke_kwargs = {"stroke_width": stroke_width, "stroke_fill": stroke_fill} if stroke_width else {}
    draw_fn(xy, text, font=font, fill=fill, **kwargs, **stroke_kwargs)


def assemble(
    script: dict,
    visuals_result: dict,
    voice_result: dict | None = None,
    timestamps_result: dict | None = None,
    music_path: str | None = None,
) -> Path:
    """Main entry. Returns path to final .mp4."""
    from modules.assembler.format1 import assemble_format1
    from modules.assembler.voice import assemble_voice_format
    from modules.assembler.format3 import assemble_format3, assemble_format3a
    from modules.assembler.misc import assemble_format4
    from modules.assembler.format6 import assemble_format6
    from modules.assembler.pov_graph import assemble_format8
    from modules.assembler.dilemma import assemble_format9
    from modules.assembler.chatfeed import assemble_format10
    from modules.assembler.dialogue import assemble_format11
    from modules.assembler.audit import assemble_format12
    from modules.assembler.interrogation import assemble_format14
    from modules.assembler.duel import assemble_format15
    from modules.assembler.red_phone import assemble_format16
    from modules.assembler.finance_roundup import assemble_format17
    from modules.assembler.long1 import assemble_long1
    from modules.assembler.long2 import assemble_long2
    from modules.assembler.long3 import assemble_long3
    from modules.assembler.mindfile import assemble_mf5, assemble_mf6, assemble_mf_long

    fmt = script["format"]
    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    # format8/9/10/12 (motion-graphics/UI-mockup-only formats) pick their own
    # track directly, skip the generic per-clip music_library lookup.
    # format11/14/15/16 generate their own multi-voice dialogue audio internally
    # and never touch music_path at all. mf5 (Two Minds, forked from format11's
    # engine, own video backdrop + tension music) and mf6 (The Exchange, forked
    # from format10's engine, own quiet tension bed) follow suit -- both pick
    # their own track internally, same pattern format8/format10 themselves use.
    # (mf4 was cut entirely 2026-07-28, see config.py.)
    _NO_STOCK_MUSIC = ("format8", "format9", "format10", "format11", "format12", "format14", "format15", "format16", "mf5", "mf6")
    if music_path is None and fmt not in _NO_STOCK_MUSIC:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    def _assemble_format3_plain(script, visuals_result, voice_result, timestamps_result, music_path):
        # Plain (no hook, no beat-match) format3 path -- forced single_quote
        # +darken like the hook and beat-match paths already are, so all 3
        # format3 modes agree on the ~8.5s single-quote spec instead of this
        # one leftover branch still rendering the old 4-line/16s slideshow.
        return assemble_format3a(script, visuals_result, voice_result, timestamps_result, music_path,
                                  single_quote=True, darken=True)

    dispatchers = {
        "format1":  assemble_format1,
        "format2":  assemble_voice_format,
        "format3":  _assemble_format3_plain,
        "format3a": _assemble_format3_plain,   # accepted alias, same merged renderer as format3
        "format3b": _assemble_format3_plain,   # accepted alias, same merged renderer as format3
        "format4":  assemble_format4,
        "format5":  assemble_voice_format,
        "format6":  assemble_format6,
        "format8":  assemble_format8,
        "format9":  assemble_format9,
        "format10": assemble_format10,
        "format11": assemble_format11,
        "format12": assemble_format12,
        "format14": assemble_format14,
        "format15": assemble_format15,
        "format16": assemble_format16,
        "format17": assemble_format17,
        "long1":    assemble_long1,
        "long2":    assemble_long2,
        "long3":    assemble_long3,
        # MindFile brand (see users/mindfile/) -- mf1-mf3 reuse existing engines
        # directly (assemble_voice_format/assemble_format3a/assemble_format6, all
        # already imported above). mf5/mf6/mf_long are forked orchestrators in
        # modules/assembler/mindfile.py -- mf5/mf6 reuse format11's/format10's
        # rendering machinery as imports but are their OWN functions so
        # format11/format10 themselves stay byte-for-byte unchanged for
        # ResetWorth. (mf4 was cut entirely 2026-07-28, see config.py.)
        "mf1":      assemble_voice_format,
        "mf2":      assemble_format3a,
        "mf3":      assemble_format6,
        "mf5":      assemble_mf5,
        "mf6":      assemble_mf6,
        "mf_long":  assemble_mf_long,
    }

    fn = dispatchers.get(fmt)
    if not fn:
        raise NotImplementedError(f"Assembly for {fmt} not yet implemented")
    # format3/3a/3b's single-persistent-quote-card + darken behavior is now
    # forced inside _assemble_format3_plain itself (see above) -- this used
    # to also re-pass single_quote=True, darken=True here on top of that,
    # which broke the moment _assemble_format3_plain's signature stopped
    # accepting those kwargs (9c9e832): "got an unexpected keyword argument
    # 'single_quote'" on every plain format3 render and on hook-recut's
    # empty-hook-bank fallback (both go through this generic dispatcher).
    return fn(script, visuals_result, voice_result, timestamps_result, music_path)


# ── Shared Pillow overlay renderers ───────────────────────────────────────────

def _render_quote_overlay(text: str, out_path: str) -> None:
    """
    1080x1920 RGBA PNG: transparent bg + bold white text (stroked).
    Overlaid on top of video -- video shows through the transparent areas.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype(_FONT_BOLD_PATH, size=76)
    except OSError:
        font = ImageFont.load_default()

    wrapped = textwrap.fill(text, width=18, break_long_words=False)

    bbox   = draw.textbbox((0, 0), wrapped, font=font, align="center")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (VIDEO_WIDTH  - text_w) // 2
    y = (VIDEO_HEIGHT - text_h) // 2

    draw.multiline_text(
        (x, y), wrapped, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=4, stroke_fill=(0, 0, 0, 255),
        align="center",
    )

    overlay.save(out_path, format="PNG")


def _render_pov_overlay(text: str, out_path: str) -> None:
    """
    1080x1920 RGBA PNG: bold POV caption near the TOP of frame (not centered
    like _render_quote_overlay), stroke+shadow for legibility over any footage.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype(_FONT_BOLD_PATH, size=64)
    except OSError:
        font = ImageFont.load_default()

    wrapped = textwrap.fill(text, width=24, break_long_words=False)

    bbox   = draw.textbbox((0, 0), wrapped, font=font, align="center")
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (VIDEO_WIDTH - text_w) // 2
    y = 160   # high position, clear of platform UI chrome

    shadow_layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).multiline_text(
        (x + 3, y + 4), wrapped, font=font, fill=(0, 0, 0, 160), align="center",
    )
    overlay = Image.alpha_composite(overlay, shadow_layer)
    draw = ImageDraw.Draw(overlay)
    draw.multiline_text(
        (x, y), wrapped, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=4, stroke_fill=(0, 0, 0, 255),
        align="center", spacing=8,
    )

    overlay.save(out_path, format="PNG")


def _render_number_overlay(number: int, out_path: str) -> None:
    """
    1080x1920 RGBA PNG: big bold rank number near the top of frame.
    format6 only. Rendered via Pillow (not FFmpeg drawtext) because drawtext
    with a Windows drive-letter fontfile path crashes FFmpeg's filter parser
    on this box (fontconfig error) -- PNG overlay sidesteps it entirely.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype(_FONT_IMPACT_PATH, size=340)
    except OSError:
        font = ImageFont.truetype(_FONT_BOLD_PATH, size=300)

    text   = str(number)
    bbox   = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (VIDEO_WIDTH - text_w) // 2
    y = 140

    draw.text(
        (x, y), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=14, stroke_fill=(0, 0, 0, 220),
    )

    overlay.save(out_path, format="PNG")


def _resize_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale to fill then center-crop to exact w x h."""
    img_ratio    = img.width / img.height
    target_ratio = w / h

    if img_ratio > target_ratio:
        new_h = h
        new_w = int(img.width * h / img.height)
    else:
        new_w = w
        new_h = int(img.height * w / img.width)

    img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - w) // 2
    top  = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _darken(img: Image.Image, factor: float = 0.65) -> Image.Image:
    """Darken image by factor (0=black, 1=original)."""
    return ImageEnhance.Brightness(img).enhance(factor)


# ── Shared FFmpeg helpers ─────────────────────────────────────────────────────

def _loop_clips_to_duration(
    clips: list[str], target_duration: float, clip_dur: float | list[float] | None = None,
) -> list[str]:
    """
    Repeat clip list until we have enough to cover target_duration (approx).

    clip_dur, if given, assumes every entry contributes a fixed duration
    (ignoring each source clip's real length) -- used together with
    _format1_cmd's own clip_dur trim so a fast-cut montage with MANY short
    entries accumulates correctly. A float means every entry is the same
    length; a list needs exactly len(clip_dur) entries -- one clip per
    beat-grid slot, not a duration-summed count.

    Shuffles the clip order on each pass through the pool so a small Drive
    folder repeating multiple times doesn't play back in the exact same
    predictable sequence every lap.
    """
    if not clips:
        return clips

    if clip_dur:
        n_needed = (len(clip_dur) if isinstance(clip_dur, list)
                   else max(1, math.ceil(target_duration / clip_dur)))
        result: list[str] = []
        pool = list(clips)
        while len(result) < n_needed:
            random.shuffle(pool)
            result.extend(pool)
        return result[:n_needed]

    result = []
    total  = 0.0
    clip_durations = []

    for c in clips:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", c],
                capture_output=True, text=True,
            )
            d = float(r.stdout.strip())
        except Exception:
            d = 10.0  # fallback estimate
        clip_durations.append(d)

    if not clip_durations:
        return clips

    i = 0
    while total < target_duration:
        result.append(clips[i % len(clips)])
        total += clip_durations[i % len(clip_durations)]
        i += 1
        if i > 100:  # safety cap
            break

    return result


def _format1_cmd(
    bg_clips: list[str],
    overlay_pngs: list[str],
    seg_dur: int,
    total_dur: int,
    music_path: str | None,
    output_path: str,
    clip_dur: float | list[float] | None = None,
    darken: bool = False,
) -> list[str]:
    """
    FFmpeg command: loop video clips + chain transparent PNG overlays (timed).

    clip_dur, if given, trims EACH background clip before concat -- a
    fast-cut montage (many quick cuts) instead of each source clip playing
    out its own full native length. A single float applies the same
    duration to every clip; a list gives each clip its OWN duration
    (index i). None preserves the original behavior (clips play full
    length, only the final concatenated sequence is trimmed to total_dur).

    darken: dims/desaturates the background slightly (mood-quote-page look,
    text pops more) before the text overlay is composited on top.
    """
    n_clips = len(bg_clips)
    n_pngs  = len(overlay_pngs)
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

    cmd = ["ffmpeg", "-y"]
    for clip in bg_clips:
        if clip.lower().endswith(_IMAGE_EXTS):
            cmd += ["-loop", "1", "-i", clip]
        else:
            cmd += ["-i", clip]
    for png in overlay_pngs:
        cmd += ["-i", png]
    music_idx = n_clips + n_pngs
    if music_path:
        cmd += ["-i", music_path]

    filters = []
    for i in range(n_clips):
        d = None
        if isinstance(clip_dur, list):
            d = clip_dur[i % len(clip_dur)] if clip_dur else None
        elif clip_dur:
            d = clip_dur
        trim_step = f",trim=duration={d:.3f},setpts=PTS-STARTPTS" if d else ""
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1{trim_step}[s{i}]"
        )

    base_lbl = "basedim" if darken else "base"
    if n_clips == 1:
        filters.append(f"[s0]trim=duration={total_dur},setpts=PTS-STARTPTS[{base_lbl}]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(n_clips))
        filters.append(f"{concat_in}concat=n={n_clips}:v=1:a=0[cat]")
        filters.append(f"[cat]trim=duration={total_dur},setpts=PTS-STARTPTS[{base_lbl}]")
    if darken:
        filters.append(f"[{base_lbl}]eq=brightness=-0.15:saturation=0.80:contrast=1.08[base]")

    prev = "base"
    for j in range(n_pngs):
        png_idx  = n_clips + j
        start    = j * seg_dur
        end      = (j + 1) * seg_dur
        out_lbl  = "vout" if j == n_pngs - 1 else f"vo{j}"
        filters.append(
            f"[{prev}][{png_idx}:v]overlay=0:0:enable='between(t,{start},{end})'[{out_lbl}]"
        )
        prev = out_lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]"]

    if music_path:
        fade_out = max(0, total_dur - 3)
        cmd += [
            "-map", f"{music_idx}:a",
            "-af", f"afade=t=in:st=0:d=2,afade=t=out:st={fade_out}:d=3",
            "-shortest",
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-t", str(total_dur),
        output_path,
    ]
    return cmd


def _concat_raw_clips_cmd(clip_paths: list[str], output_path: str) -> list[str]:
    """
    Plain silent concat of 2+ video clips, normalized to VIDEO_WIDTH x
    VIDEO_HEIGHT, no text overlay/music/darken -- raw B-roll handoff, each
    clip playing out at its own native length (no trim). Used by format3
    "Mode template"'s extra raw-clip deliverables (see
    modules/assembler/format3.py's _build_template_extras and
    modules/publish/template_parts.py), not the main quote-card renderer.
    """
    cmd = ["ffmpeg", "-y"]
    for clip in clip_paths:
        cmd += ["-i", clip]
    filters = []
    for i in range(len(clip_paths)):
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1[s{i}]"
        )
    concat_in = "".join(f"[s{i}]" for i in range(len(clip_paths)))
    filters.append(f"{concat_in}concat=n={len(clip_paths)}:v=1:a=0[vout]")
    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += [
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        output_path,
    ]
    return cmd


def _duration_ffprobe(path: str) -> float:
    from modules.transitions import probe_duration
    return probe_duration(path)


def _has_audio(path: str) -> bool:
    """True if the media file has at least one audio stream."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        return "audio" in r.stdout
    except Exception:
        return False


def _find_music() -> str | None:
    from config import ASSETS_DIR
    music_dir = ASSETS_DIR / "music"
    files = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.m4a"))
    if not files:
        logger.warning("No music in assets/music/ — video will be silent. Add .mp3 files there.")
        return None
    chosen = random.choice(files)
    logger.info(f"Music: {chosen.name}")
    return str(chosen)


def _ts_ass(seconds: float) -> str:
    """Seconds (float) -> ASS timestamp H:MM:SS.CC."""
    seconds = max(0.0, seconds)
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = seconds % 60
    cs = min(99, round((s - int(s)) * 100))
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def _run_ffmpeg(cmd: list[str], cwd: str | None = None) -> None:
    logger.debug(f"FFmpeg: {' '.join(cmd[:8])}...")
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-20:])
        raise RuntimeError(f"FFmpeg failed (exit {result.returncode}):\n{tail}")


# ── Hook-transition assembler (format3/3a/3b prefixed with a transitional hook) ──

def assemble_with_hook(
    script: dict,
    visuals_result: dict,
    transition: str = "zoom_punch",
    transition_duration: float | None = None,
    music_bleed_in: float = 0.0,
    speed_ramp: bool = True,
    ramp_duration: float | None = None,
    ramp_factor: float | None = None,
    cut_style: str = "flash",   # "flash" | "glitch" | "none"
    cut_accent_duration: float | None = None,
    impact_duration: float | None = None,
) -> Path:
    """
    format3 (mixed image/video luxury slideshow) prefixed with a transitional
    hook:
      hook (own audio, speed-ramped tail, "POV: ..." overlay, visual cut
      accent + percussive impact hit ON the cut) -> synced transition ->
      luxury content, with music starting exactly at the cut (or
      `music_bleed_in` seconds early), trimmed to its first detected beat so
      the beat lands on the transition. The content montage itself is always
      cut on the music's real beat grid, regardless of whether the pool ends
      up all-video, all-image, or mixed.

    Falls back to the plain (no-hook) assembler if the hook bank is empty.
    """
    from modules.assembler.format3 import assemble_format3a, _SINGLE_QUOTE_DUR
    from modules.hooks import pick_hook_and_music, generate_pov_text
    from modules.transitions import (
        detect_first_beat, DEFAULT_TRANSITION_DURATION,
        DEFAULT_RAMP_DURATION, DEFAULT_RAMP_FACTOR,
        DEFAULT_FLASH_DURATION, DEFAULT_GLITCH_DURATION, DEFAULT_IMPACT_DURATION,
    )

    hook, music_path = pick_hook_and_music(script)
    if hook is None:
        logger.warning("assemble_with_hook: hook bank empty, falling back to plain assembly")
        return assemble(script, visuals_result, music_path=None)

    fmt = script["format"]

    music_offset = 0.0
    if music_path:
        music_offset = detect_first_beat(music_path)

    # Was a stale 16.0 (leftover from before the 2026-08-01 single-quote
    # redesign shortened content to ~8.5s) -- the beat-duration list this
    # feeds into becomes clip_cut_dur below, and since assemble_format3a
    # sums a list clip_cut_dur as the content's ACTUAL total duration
    # (single_quote_dur is only the fallback for a non-list clip_cut_dur),
    # that stale 16.0 was silently doubling every hook video's content
    # length back to ~16s regardless of single_quote_dur.
    _CONTENT_TARGET_DUR = _SINGLE_QUOTE_DUR
    from modules.transitions import detect_tempo, detect_beat_grid
    clip_cut_dur: float | list[float] = 1.2
    if music_path:
        beat_durations = detect_beat_grid(music_path, start_offset=music_offset)
        if beat_durations:
            truncated, running = [], 0.0
            for d in beat_durations:
                if running >= _CONTENT_TARGET_DUR:
                    break
                truncated.append(d)
                running += d
            clip_cut_dur = truncated or beat_durations[:1]
        else:
            bpm = detect_tempo(music_path)
            if bpm > 0:
                clip_cut_dur = max(0.4, min(2.0, (60.0 / bpm) * 2))

    content_path = assemble_format3a(script, visuals_result, None, None, None,
                                     fast_cut=True, clip_cut_dur=clip_cut_dur,
                                     single_quote=True)

    hook_path = hook["_path"]
    hook_dur = float(hook.get("cut_time") or hook.get("duration") or 0.0)
    if hook_dur <= 0:
        hook_dur = _duration_ffprobe(hook_path)
    hook_has_audio = _has_audio(hook_path)

    from modules.transitions import TRANSITION_TYPES
    transition = transition if transition in TRANSITION_TYPES else "zoom_punch"
    cut_style = cut_style if cut_style in ("flash", "glitch", "none") else "flash"
    t_dur = transition_duration or DEFAULT_TRANSITION_DURATION
    ramp_duration   = ramp_duration if ramp_duration is not None else DEFAULT_RAMP_DURATION
    ramp_factor     = ramp_factor if ramp_factor is not None else DEFAULT_RAMP_FACTOR
    impact_duration = impact_duration if impact_duration is not None else DEFAULT_IMPACT_DURATION
    if cut_accent_duration is None:
        cut_accent_duration = DEFAULT_FLASH_DURATION if cut_style == "flash" else DEFAULT_GLITCH_DURATION

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_hook"
    tmp_dir.mkdir(exist_ok=True)

    pov_text = generate_pov_text(hook, script)
    pov_png  = tmp_dir / "pov_overlay.png"
    _render_pov_overlay(pov_text, str(pov_png))

    impact_path = None
    if cut_style != "none":
        from modules.transitions import synthesize_impact_hit
        impact_path = tmp_dir / "impact.wav"
        synthesize_impact_hit(impact_duration, str(impact_path))

    logger.info(f"Hook: {hook['id']} ({hook_dur:.2f}s, {hook.get('ambiance')}) | "
                f"transition={transition} | ramp={speed_ramp} | cut_style={cut_style} | pov={pov_text!r}")

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_{fmt}_hook_{slug}.mp4"

    cmd = _hook_join_cmd(
        hook_path=hook_path, hook_dur=hook_dur, hook_has_audio=hook_has_audio,
        content_path=str(content_path), pov_png=str(pov_png),
        music_path=music_path, music_offset=music_offset,
        music_bleed_in=max(0.0, music_bleed_in), transition=transition, t_dur=t_dur,
        speed_ramp=speed_ramp, ramp_duration=ramp_duration, ramp_factor=ramp_factor,
        cut_style=cut_style, cut_accent_duration=cut_accent_duration,
        impact_path=str(impact_path) if impact_path else None, impact_duration=impact_duration,
        output_path=str(out),
    )
    _run_ffmpeg(cmd, cwd=str(tmp_dir))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Hook video ready: {out} ({size_mb:.1f} MB)")
    return out


def _hook_join_cmd(
    hook_path: str, hook_dur: float, hook_has_audio: bool, content_path: str,
    pov_png: str, music_path: str | None, music_offset: float, music_bleed_in: float,
    transition: str, t_dur: float, output_path: str,
    speed_ramp: bool = False, ramp_duration: float = 0.0, ramp_factor: float = 1.0,
    cut_style: str = "none", cut_accent_duration: float = 0.0,
    impact_path: str | None = None, impact_duration: float = 0.0,
) -> list[str]:
    """
    Inputs: [0] hook  [1] content (silent)  [2] POV PNG  [+impact hit?]  [+music?]
    Video : hook (optionally speed-ramped tail, optional flash/glitch accent
            on its final `cut_accent_duration` seconds) with POV overlay for
            [0, effective_hook_dur) -> transition -> content.
    Audio : hook's own audio (or silence) for [0, effective_hook_dur -
            bleed_in), then music onward, with an optional short percussive
            impact hit layered exactly at that cut point.
    """
    from modules.transitions import (
        build_transition_filters, build_speed_ramp_filters,
        build_flash_cut_filters, build_glitch_cut_filters,
        is_prop_wipe, build_prop_wipe_video_filters, PROP_WIPE_ASSET,
    )

    cmd = ["ffmpeg", "-y", "-i", hook_path, "-i", content_path, "-loop", "1", "-i", pov_png]
    next_idx = 3
    wipe_idx = None
    if is_prop_wipe(transition):
        cmd += ["-i", str(PROP_WIPE_ASSET)]
        wipe_idx = next_idx
        next_idx += 1
    impact_idx = None
    if impact_path:
        cmd += ["-i", impact_path]
        impact_idx = next_idx
        next_idx += 1
    music_idx = None
    if music_path:
        cmd += ["-ss", f"{music_offset:.3f}", "-i", music_path]
        music_idx = next_idx
        next_idx += 1

    filters = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p,fps={VIDEO_FPS}[hookv_scaled]",
        f"[1:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p,fps={VIDEO_FPS}[contv]",
    ]

    effective_hook_dur = hook_dur
    if speed_ramp:
        ramp_filters, effective_hook_dur = build_speed_ramp_filters(
            "hookv_scaled", "hookv", hook_dur, ramp_duration, ramp_factor, fps=VIDEO_FPS)
        if ramp_filters:
            filters += ramp_filters
        else:
            filters.append("[hookv_scaled]null[hookv]")
    else:
        filters.append("[hookv_scaled]null[hookv]")

    filters.append(
        f"[hookv][2:v]overlay=0:0:enable='between(t,0,{effective_hook_dur:.3f})',"
        f"trim=duration={effective_hook_dur:.3f},setpts=PTS-STARTPTS[hookpov]"
    )

    hook_label, content_label = "hookpov", "contv"
    if is_prop_wipe(transition):
        filters += build_prop_wipe_video_filters(hook_label, wipe_idx, content_label, "vout")
    else:
        if cut_style == "flash" and cut_accent_duration > 0:
            filters += build_flash_cut_filters(
                "hookpov", "contv", "hookpov_fl", "contv_fl", effective_hook_dur, cut_accent_duration)
            hook_label, content_label = "hookpov_fl", "contv_fl"
        elif cut_style == "glitch" and cut_accent_duration > 0:
            filters += build_glitch_cut_filters(
                "hookpov", "contv", "hookpov_gl", "contv_gl", effective_hook_dur, cut_accent_duration)
            hook_label, content_label = "hookpov_gl", "contv_gl"

        filters += build_transition_filters(hook_label, content_label, "vout", transition, effective_hook_dur, t_dur)

    _AFMT = "aformat=sample_rates=44100:channel_layouts=stereo"

    audio_cut = max(0.0, effective_hook_dur - music_bleed_in)
    if hook_has_audio:
        filters.append(f"[0:a]atrim=0:{audio_cut:.3f},asetpts=PTS-STARTPTS,{_AFMT}[hooka]")
    else:
        filters.append(
            f"anullsrc=channel_layout=stereo:sample_rate=44100,"
            f"atrim=0:{audio_cut:.3f}[hooka]"
        )

    if music_path:
        if music_bleed_in > 0:
            filters.append(
                f"[{music_idx}:a]afade=t=in:d={min(0.5, music_bleed_in):.3f},{_AFMT}[musraw]"
            )
            filters.append(f"[musraw]adelay={int(audio_cut*1000)}|{int(audio_cut*1000)}[musd]")
            filters.append("[hooka][musd]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[aout_pre]")
        else:
            filters.append(f"[{music_idx}:a]{_AFMT}[musd]")
            filters.append("[hooka][musd]concat=n=2:v=0:a=1[aout_pre]")
    else:
        filters.append("[hooka]apad[aout_pre]")

    if impact_idx is not None:
        filters.append(
            f"[{impact_idx}:a]{_AFMT},adelay={int(audio_cut*1000)}|{int(audio_cut*1000)}[impact_pos]"
        )
        filters.append("[aout_pre][impact_pos]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        filters.append("[aout_pre]anull[aout]")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS), "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    return cmd


# ── Hook-RECUT assembler (format3's interrupted-payoff structure) ─────────────
#
# User idea, 2026-08-06 (see memory project_format3_hook_recut_retention_idea):
# hook (~2s, cut on a stylish moment) -> content #1 (~3s, quote) -> back to
# the hook -- its OWN continuation if there's enough footage left past the
# first cut (with a "sorry, here's the rest" joke card), else a SECOND,
# deliberately UNRELATED hook clip (with a different joke card, the mismatch
# itself is the joke -- see modules.hooks.pick_unrelated_hook) -> content #2
# (~3s, a second distinct quote). ~10s total.
#
# Built as 4 independently-rendered segments (each already normalized to the
# same scale/crop/fps/codec/audio-stream layout) joined with the ffmpeg
# concat demuxer (stream copy) rather than one giant multi-cut filter_complex
# graph -- far more debuggable, and appropriate here since every cut is a
# deliberate HARD cut (no crossfade/speed-ramp/flash polish like
# assemble_with_hook -- the abrupt cutaway/cutback IS the gag, smoothing it
# would undercut the joke).

_RECUT_HOOK_SEG_DUR    = 2.0   # segment 1: hook open, cut on the "stylish moment"
_RECUT_CONTENT_SEG_DUR = 3.0   # segments 2 & 4: luxury content, one quote each
_RECUT_RETURN_SEG_DUR  = 2.0   # segment 3: hook return (continuation or unrelated clip)
_RECUT_MIN_LEFTOVER    = 1.0   # need at least this much hook footage left past the
                                # first cut to "continue" -- below that, fall back to
                                # a second, unrelated hook clip instead (user spec)

_RECUT_CONTINUE_JOKES = [
    "sorry, here's the rest",
    "ok fine, here's the rest",
    "my bad, here's the rest",
]
_RECUT_UNRELATED_JOKES = [
    "wrong clip, my bad",
    "not related but here we go",
    "anyway, watch this instead",
    "ok that one's unrelated but whatever",
]


def _recut_hook_segment_cmd(
    src_path: str, start: float, dur: float, has_audio: bool,
    joke_png: str | None, output_path: str,
) -> list[str]:
    """
    One hook-derived segment for the recut structure: trim [start, start+dur)
    out of src_path, scale/crop/fps-normalized, optional joke-text overlay
    burned on for the whole segment, real audio if the source has any (else
    silence) -- ALWAYS emits both a video and an audio stream so this segment
    can concat-demux-join with the content segments below.
    """
    cmd = ["ffmpeg", "-y", "-i", src_path]
    png_idx = None
    if joke_png:
        cmd += ["-loop", "1", "-i", joke_png]
        png_idx = 1

    filters = [
        f"[0:v]trim=start={start:.3f}:duration={dur:.3f},setpts=PTS-STARTPTS,"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p,fps={VIDEO_FPS}[v0]"
    ]
    v_label = "v0"
    if png_idx is not None:
        filters.append(f"[v0][{png_idx}:v]overlay=0:0[vout]")
        v_label = "vout"

    _AFMT = "aformat=sample_rates=44100:channel_layouts=stereo"
    if has_audio:
        filters.append(
            f"[0:a]atrim=start={start:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,{_AFMT}[aout]"
        )
    else:
        filters.append(f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:{dur:.3f}[aout]")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", f"[{v_label}]", "-map", "[aout]"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS), "-c:a", "aac", "-b:a", "192k",
        "-t", f"{dur:.3f}",
        output_path,
    ]
    return cmd


def _recut_content_cmd(
    bg_clips: list[str], quote_png: str, dur: float,
    clip_dur: float | list[float] | None, darken: bool, output_path: str,
) -> list[str]:
    """
    One luxury-content segment for the recut structure: same background-
    building machinery as _format1_cmd (scale/crop/concat/optional darken/
    optional fast-cut clip_dur), simplified to exactly ONE overlay covering
    the segment's full duration. Always emits a silent audio track -- music
    for the whole hook-recut video is mixed in once, over the complete
    concatenated timeline, not per segment.
    """
    n_clips = len(bg_clips)
    _IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

    cmd = ["ffmpeg", "-y"]
    for clip in bg_clips:
        if clip.lower().endswith(_IMAGE_EXTS):
            cmd += ["-loop", "1", "-i", clip]
        else:
            cmd += ["-i", clip]
    cmd += ["-loop", "1", "-i", quote_png]
    png_idx = n_clips

    filters = []
    for i in range(n_clips):
        d = None
        if isinstance(clip_dur, list):
            d = clip_dur[i % len(clip_dur)] if clip_dur else None
        elif clip_dur:
            d = clip_dur
        trim_step = f",trim=duration={d:.3f},setpts=PTS-STARTPTS" if d else ""
        filters.append(
            f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1{trim_step}[s{i}]"
        )

    base_lbl = "basedim" if darken else "base"
    if n_clips == 1:
        filters.append(f"[s0]trim=duration={dur:.3f},setpts=PTS-STARTPTS[{base_lbl}]")
    else:
        concat_in = "".join(f"[s{i}]" for i in range(n_clips))
        filters.append(f"{concat_in}concat=n={n_clips}:v=1:a=0[cat]")
        filters.append(f"[cat]trim=duration={dur:.3f},setpts=PTS-STARTPTS[{base_lbl}]")
    if darken:
        filters.append(f"[{base_lbl}]eq=brightness=-0.15:saturation=0.80:contrast=1.08[base]")

    filters.append(f"[base][{png_idx}:v]overlay=0:0,format=yuv420p,fps={VIDEO_FPS}[vout]")
    filters.append(f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:{dur:.3f}[aout]")

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS), "-c:a", "aac", "-b:a", "192k",
        "-t", f"{dur:.3f}",
        output_path,
    ]
    return cmd


def assemble_with_hook_recut(script: dict, visuals_result: dict) -> Path:
    """
    format3's "hook recut" structure -- see module comment above. script
    needs a "recut_quote" field (generate_script(..., recut=True)); falls
    back to reusing the hook line for both content segments (with a warning)
    if it's missing, e.g. an older cached script generated before this
    existed. Falls back to the plain (no-hook) assembler if the hook bank is
    empty, same as assemble_with_hook.

    Segment 1 carries the same "POV: ..." curiosity-gap caption
    assemble_with_hook uses (generate_pov_text) -- same job here: keep the
    viewer watching THROUGH the first cutaway to content by signaling
    something's about to happen without saying what (2026-08-06 user ask,
    "comme dans hook normal"). Segment 3 (the hook RETURN) intentionally does
    NOT also get a POV line -- it already carries its own joke card
    ("sorry, here's the rest" / "wrong clip, my bad"), a second caption there
    would just clutter a 2s beat.
    """
    from modules.hooks import pick_hook_and_music, pick_unrelated_hook, generate_pov_text
    from modules.transitions import detect_tempo

    hook, music_path = pick_hook_and_music(script)
    if hook is None:
        logger.warning("assemble_with_hook_recut: hook bank empty, falling back to plain assembly")
        return assemble(script, visuals_result, music_path=None)

    quote1 = (script.get("hook") or "").strip()
    quote2 = (script.get("recut_quote") or "").strip()
    if not quote2:
        logger.warning("assemble_with_hook_recut: script has no recut_quote "
                        "(generate_script needs recut=True) -- reusing the hook line for both content segments")
        quote2 = quote1

    hook_path = hook["_path"]
    hook_dur = float(hook.get("duration") or 0.0) or _duration_ffprobe(hook_path)
    hook_has_audio = _has_audio(hook_path)

    seg1_dur = min(_RECUT_HOOK_SEG_DUR, hook_dur)
    leftover = hook_dur - seg1_dur

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_hook_recut"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(exist_ok=True)

    # -- segment 1: hook open, POV caption (same curiosity-gap job as assemble_with_hook's) --
    pov_text = generate_pov_text(hook, script)
    pov_png  = tmp_dir / "pov_overlay.png"
    _render_pov_overlay(pov_text, str(pov_png))
    seg1_path = tmp_dir / "seg1_hook.mp4"
    _run_ffmpeg(_recut_hook_segment_cmd(hook_path, 0.0, seg1_dur, hook_has_audio, str(pov_png), str(seg1_path)))

    # -- segment 3: hook return -- continuation if enough footage is left,
    # else a deliberately UNRELATED second hook clip (the mismatch is the joke) --
    if leftover >= _RECUT_MIN_LEFTOVER:
        seg3_src, seg3_start, seg3_has_audio = hook_path, seg1_dur, hook_has_audio
        seg3_dur = min(_RECUT_RETURN_SEG_DUR, leftover)
        joke = random.choice(_RECUT_CONTINUE_JOKES)
        seg3_hook_id = hook["id"]
    else:
        hook2 = pick_unrelated_hook(script, exclude_id=hook["id"])
        if hook2 is None:
            hook2 = hook   # bank has exactly 1 hook -- nothing else to pick, replay it
        hook2_dur = float(hook2.get("duration") or 0.0) or _duration_ffprobe(hook2["_path"])
        seg3_src, seg3_start, seg3_has_audio = hook2["_path"], 0.0, _has_audio(hook2["_path"])
        seg3_dur = min(_RECUT_RETURN_SEG_DUR, hook2_dur)
        joke = random.choice(_RECUT_UNRELATED_JOKES)
        seg3_hook_id = hook2["id"]
        logger.info(f"assemble_with_hook_recut: hook '{hook['id']}' too short to continue "
                    f"({leftover:.2f}s left) -- using unrelated hook '{hook2['id']}' instead")

    joke_png = tmp_dir / "joke_overlay.png"
    _render_pov_overlay(joke, str(joke_png))
    seg3_path = tmp_dir / "seg3_hook_return.mp4"
    _run_ffmpeg(_recut_hook_segment_cmd(
        seg3_src, seg3_start, seg3_dur, seg3_has_audio, str(joke_png), str(seg3_path)))

    # -- segments 2 & 4: luxury content, same mixed-pool mechanism as the
    # single-quote format3 content, just sized to _RECUT_CONTENT_SEG_DUR each --
    videos = visuals_result.get("videos", [])
    images = visuals_result.get("images", [])
    media_pool = videos + images
    if not media_pool:
        raise ValueError("format3 hook-recut needs at least 1 image or video clip")

    clip_cut_dur: float = 1.2
    if music_path:
        bpm = detect_tempo(music_path)
        if bpm > 0:
            clip_cut_dur = max(0.4, min(2.0, (60.0 / bpm) * 2))

    quote1_png = tmp_dir / "quote1.png"
    quote2_png = tmp_dir / "quote2.png"
    _render_quote_overlay(quote1, str(quote1_png))
    _render_quote_overlay(quote2, str(quote2_png))

    pool1, pool2 = list(media_pool), list(media_pool)
    random.shuffle(pool1)
    random.shuffle(pool2)
    bg1 = _loop_clips_to_duration(pool1, _RECUT_CONTENT_SEG_DUR, clip_dur=clip_cut_dur)
    bg2 = _loop_clips_to_duration(pool2, _RECUT_CONTENT_SEG_DUR, clip_dur=clip_cut_dur)

    seg2_path = tmp_dir / "seg2_content1.mp4"
    seg4_path = tmp_dir / "seg4_content2.mp4"
    _run_ffmpeg(_recut_content_cmd(bg1, str(quote1_png), _RECUT_CONTENT_SEG_DUR, clip_cut_dur, True, str(seg2_path)))
    _run_ffmpeg(_recut_content_cmd(bg2, str(quote2_png), _RECUT_CONTENT_SEG_DUR, clip_cut_dur, True, str(seg4_path)))

    # -- concat all 4 segments (stream copy -- identical codec params by construction) --
    concat_list = tmp_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in [seg1_path, seg2_path, seg3_path, seg4_path]),
        encoding="utf-8",
    )
    joined_path = tmp_dir / "joined.mp4"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(joined_path),
    ])

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_{script['format']}_hookrecut_{slug}.mp4"

    # -- mix in music starting right where the hook's own audio ends, same
    # afade-in + adelay + amix pattern as _hook_join_cmd's single hook->content cut --
    if music_path:
        _AFMT = "aformat=sample_rates=44100:channel_layouts=stereo"
        music_start_ms = int(seg1_dur * 1000)
        cmd = [
            "ffmpeg", "-y", "-i", str(joined_path), "-i", music_path,
            "-filter_complex",
            f"[0:a]{_AFMT}[joined_a];"
            f"[1:a]afade=t=in:d=0.5,{_AFMT},adelay={music_start_ms}|{music_start_ms}[music_a];"
            "[joined_a][music_a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(out),
        ]
        _run_ffmpeg(cmd)
    else:
        shutil.copyfile(joined_path, out)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(
        f"Hook-recut video ready: {out} ({size_mb:.1f} MB) | hook={hook['id']} | pov={pov_text!r} | "
        f"seg3={'continue' if seg3_hook_id == hook['id'] else 'unrelated:' + seg3_hook_id} | "
        f"leftover={leftover:.2f}s"
    )
    return out


# ── Format1 / Format7 clip-edit assembler ─────────────────────────────────────

def _pick_music_offset(music_path: str) -> float:
    """
    Start the music partway into the track instead of at 0:00 -- heuristic
    (random offset in the middle third of the track), skips the quiet intro
    most royalty-free instrumentals open with and avoids the very tail.
    """
    from modules.transitions import probe_duration
    dur = probe_duration(music_path)
    if dur <= 20:
        return 0.0
    lo, hi = dur * 0.2, dur * 0.5
    return random.uniform(lo, hi)


def assemble_clip_edit(
    script: dict,
    clip_path: Path,
    words: list[dict],
    full_text: str = "",
) -> Path:
    """
    Format1 clip edit -- the ORIGINAL simple version, kept as its own format
    (format1) separate from format7 (assemble_clip_edit_v2).
    Layout: quote band (top) | clip 16:9 (middle) | blur fill (bottom).
    Subtitles synced to speech on clip lower-third, grouped 3-4 words per
    card. ResetWorth watermark. Original audio. No music added.
    """
    from config import MIN_SPEECH_WORDS, SHOW_QUOTE_ON_SILENT_CLIPS

    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    has_speech = len(words) >= MIN_SPEECH_WORDS
    show_quote = has_speech or SHOW_QUOTE_ON_SILENT_CLIPS

    quote_text = ""
    if show_quote:
        from modules.clips.clip_quote import generate_quote
        quote_text = generate_quote(full_text if has_speech else None)

    quote_png = _build_quote_card(quote_text)
    rw_png    = _build_resetworth_png()

    burned_in = _detect_burned_caption_region(clip_path)["has_captions"] if has_speech else False
    add_subs  = has_speech and not burned_in
    ass_path  = _generate_clip_edit_ass(words, clip_path) if add_subs else None
    if has_speech and burned_in:
        logger.info(f"Clip already has burned-in captions -> skipping our subtitles for {clip_path.name}")

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format1_{slug}.mp4"

    from modules.music.music_library import select_music
    music_path = select_music(script)

    cmd = _clip_edit_cmd(str(clip_path), quote_png, rw_png, ass_path, str(out), music_path)
    cwd = str(ass_path.parent) if ass_path else str(quote_png.parent)

    logger.info(
        f"Assemblage clip edit | clip={clip_path.name} | speech={has_speech} "
        f"| subs_added={add_subs} | burned_in_detected={burned_in} "
        f"| mots={len(words)} | quote={quote_text[:50]!r}"
    )
    _run_ffmpeg(cmd, cwd=cwd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Vidéo prête : {out} ({size_mb:.1f} MB)")
    return out


def assemble_clip_edit_v2(
    script: dict,
    clip_path: Path,
    words: list[dict],
    full_text: str = "",
    duration: float | None = None,
) -> Path:
    """
    Format7 clip edit -- the "next level" rebuild from a viral clip-edit
    breakdown, kept as its OWN format (format7) rather than folded into
    format1: trim to the speech's real climax, remove silence gaps, cut in
    B-roll matched to the speech's themes inside a rounded-corner frame,
    word-by-word synced captions, consistent color grade. Falls back to the
    plain framed clip (no trim/silence-removal/B-roll) for near-silent clips.
    Layout: quote band (top) | framed clip 16:9 (middle) | ResetWorth watermark.
    """
    from config import MIN_SPEECH_WORDS, SHOW_QUOTE_ON_SILENT_CLIPS

    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    has_speech = len(words) >= MIN_SPEECH_WORDS
    if duration is None:
        duration = _duration_ffprobe(str(clip_path))

    work_clip = clip_path
    work_words = words
    work_duration = duration

    if has_speech:
        from modules.clips.clip_transcriber import find_climax_segment
        c_start, c_end = find_climax_segment(work_words, work_duration)
        if c_start > 0.0 or c_end < work_duration:
            trimmed = Path(tempfile.gettempdir()) / "vid_pipeline_f1" / f"{clip_path.stem}_climax.mp4"
            trimmed.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(work_clip), "-ss", f"{c_start:.3f}", "-to", f"{c_end:.3f}",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac", str(trimmed)],
                capture_output=True, text=True)
            if r.returncode == 0:
                work_words = [
                    {"word": w["word"], "start": round(w["start"] - c_start, 3), "end": round(w["end"] - c_start, 3)}
                    for w in work_words if c_start <= w["start"] <= c_end
                ]
                work_clip = trimmed
                work_duration = c_end - c_start
            else:
                logger.warning(f"Climax trim failed ({r.stderr[-300:]}); using full clip")

        from modules.clips.clip_silence import remove_silences
        work_clip, work_words, work_duration = remove_silences(work_clip, work_words, work_duration)

    show_quote = has_speech or SHOW_QUOTE_ON_SILENT_CLIPS
    quote_text = ""
    if show_quote:
        from modules.clips.clip_quote import generate_quote
        quote_text = generate_quote(full_text if has_speech else None)

    quote_png = _build_quote_card(quote_text)
    rw_png    = _build_resetworth_png()

    burned_in = _detect_burned_caption_region(clip_path)["has_captions"] if has_speech else False
    if burned_in:
        logger.info("Clip already has burned-in captions -> our subtitles restricted to the B-roll burst only")

    from modules.clips.clip_broll import extract_broll_keywords, fetch_broll_clips, build_framed_clip
    keywords = extract_broll_keywords(full_text) if has_speech else []
    broll_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f1_broll" / clip_path.stem
    broll_clips = fetch_broll_clips(keywords, 12, broll_dir, full_text=full_text) if keywords else []

    framed = Path(tempfile.gettempdir()) / "vid_pipeline_f1" / f"{clip_path.stem}_framed.mp4"
    framed.parent.mkdir(parents=True, exist_ok=True)
    burst_window = build_framed_clip(work_clip, broll_clips, work_duration, framed,
                                     width=1080, height=_F1_CLIP_H, keywords=keywords)

    only_within = None if not burned_in else burst_window
    add_subs = has_speech and (not burned_in or burst_window is not None)
    ass_path = _generate_clip_edit_ass_word_by_word(work_words, clip_path, only_within=only_within) if add_subs else None

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format7_{slug}.mp4"

    from modules.music.music_library import select_music
    music_path = select_music(script)

    cmd = _clip_edit_cmd(str(framed), quote_png, rw_png, ass_path, str(out), music_path)
    cwd = str(ass_path.parent) if ass_path else str(quote_png.parent)

    logger.info(
        f"Assemblage format7 clip edit | clip={clip_path.name} | speech={has_speech} "
        f"| subs_added={add_subs} | burned_in_detected={burned_in} "
        f"| broll={len(broll_clips)}/{len(keywords)} keywords | "
        f"climax_dur={work_duration:.1f}s (of {duration:.1f}s) | quote={quote_text[:50]!r}"
    )
    _run_ffmpeg(cmd, cwd=cwd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Vidéo prête : {out} ({size_mb:.1f} MB)")
    return out


def _build_quote_card(text: str = "") -> Path:
    """
    Title card: 1080x660 pure black. Text LEFT-ALIGNED (x=30), positioned
    near the BOTTOM of the band (just above the clip). Up to 2 lines if needed.
    """
    img  = Image.new("RGB", (1080, _F1_QUOTE_H), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = None
    for fp in ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibrib.ttf"]:
        try:
            font = ImageFont.truetype(fp, 52)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    if text:
        wrapped = textwrap.fill(text, width=28)
        lines   = wrapped.split("\n")
        line_h  = 65
        total_h = len(lines) * line_h

        y0 = _F1_QUOTE_H - total_h - 40

        x = 80
        for i, line in enumerate(lines):
            y = y0 + i * line_h
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((x, y),         line, font=font, fill=(255, 255, 255))

    tmp = Path(tempfile.gettempdir()) / "vid_pipeline_f1"
    tmp.mkdir(exist_ok=True)
    out = tmp / "quote_card.png"
    img.save(str(out), "PNG")
    return out


def _build_resetworth_png() -> Path:
    """Semi-transparent ResetWorth watermark on transparent RGBA background."""
    img  = Image.new("RGBA", (280, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = None
    for fp in ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"]:
        try:
            font = ImageFont.truetype(fp, 28)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    draw.text((4, 14), "ResetWorth", font=font, fill=(255, 255, 255, 110))

    tmp = Path(tempfile.gettempdir()) / "vid_pipeline_f1"
    tmp.mkdir(exist_ok=True)
    out = tmp / "resetworth.png"
    img.save(str(out), "PNG")
    return out


def _detect_burned_caption_region(clip_path: Path) -> dict:
    """
    Vision check: does this source clip already show hardcoded/burned-in text,
    captions or subtitles overlaid on the footage? If yes, our own ASS
    subtitles are skipped entirely.

    Returns {"has_captions": bool}. Fails safe on any error -> False.
    """
    from config import CLAUDE_MODEL
    _none = {"has_captions": False}

    dur = 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
            capture_output=True, text=True)
        dur = float(r.stdout.strip())
    except Exception:
        pass

    stamps = [1.0, max(dur / 2, 1.0), max(dur - 1.0, 1.0)] if dur > 3 else [max(dur / 2, 0.3)]
    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_capcheck"
    tmp_dir.mkdir(exist_ok=True)

    frames = []
    for i, t in enumerate(stamps):
        out = tmp_dir / f"cap_{clip_path.stem}_{i}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(clip_path),
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(out)],
            capture_output=True)
        if r.returncode == 0 and out.exists():
            frames.append(out)

    if not frames:
        return _none

    result_out = dict(_none)
    try:
        from modules.claude_cli import call_claude
        prompt = "\n".join(f"@{f}" for f in frames) + (
            "\nDoes this video already have burned-in / hardcoded text, captions, "
            "or subtitles rendered ON TOP of the footage itself (not app UI, not "
            "a logo, actual caption/subtitle text placed over the video)? "
            'Answer with JSON only: {"has_captions": true or false}'
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        result_out = {"has_captions": bool(parsed.get("has_captions", False))}
    except Exception as e:
        logger.warning(f"Burned-in caption check failed ({e}); assuming none")
    finally:
        for f in frames:
            f.unlink(missing_ok=True)

    return result_out


def _generate_clip_edit_ass(words: list[dict], clip_path: Path) -> Path:
    """ASS subtitles for clip edit -- lower-third of clip area (y=~1080 in 1080x1920)."""
    groups   = _group_words(words, max_words=3)
    ass_dir  = Path(tempfile.gettempdir()) / "vid_pipeline_f1"
    ass_dir.mkdir(exist_ok=True)
    ass_path = ass_dir / f"{clip_path.stem}_clip.ass"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_WIDTH}\n"
        f"PlayResY: {VIDEO_HEIGHT}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Motiv,Arial,{_F1_SUB_FONT},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,2,0,1,6,1,2,60,60,820,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    for g in groups:
        start = _ts_ass(max(0.0, g["start"] - 0.05))
        end   = _ts_ass(g["end"] + 0.08)
        lines.append(f"Dialogue: 0,{start},{end},Motiv,,0,0,0,,{g['text']}")

    ass_path.write_text("\n".join(lines), encoding="utf-8-sig")
    logger.info(f"ASS clip edit : {ass_path.name} ({len(groups)} groupes)")
    return ass_path


def _generate_clip_edit_ass_word_by_word(
    words: list[dict], clip_path: Path, only_within: tuple[float, float] | None = None,
) -> Path:
    """
    ASS subtitles, ONE WORD AT A TIME. Bigger font than the grouped style
    since only one word is on screen at once; a small overlap keeps
    consecutive words from visibly flashing/gapping on tight speech.

    only_within restricts captions to a single (start, end) window on the
    clip's timeline -- used when the source clip's OWN footage already has
    burned-in captions (skip ours there) but the B-roll burst spliced into
    it doesn't.
    """
    ass_dir  = Path(tempfile.gettempdir()) / "vid_pipeline_f1"
    ass_dir.mkdir(exist_ok=True)
    ass_path = ass_dir / f"{clip_path.stem}_clip_word.ass"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_WIDTH}\n"
        f"PlayResY: {VIDEO_HEIGHT}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Motiv,Arial Black,{_F1_SUB_FONT + 14},{_F1_WORD_BODY_COLOR},&H000000FF,"
        f"{_F1_WORD_OUTLINE_COLOR},&H00000000,-1,0,0,0,100,100,2,0,1,6,1,2,60,60,820,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [header]
    _OVERLAP = 0.04
    for w in words:
        if only_within and not (only_within[0] <= w["start"] <= only_within[1]):
            continue
        start = max(0.0, w["start"] - 0.03)
        end   = w["end"] + _OVERLAP
        text  = w["word"].strip().upper()
        if not text:
            continue
        lines.append(f"Dialogue: 0,{_ts_ass(start)},{_ts_ass(end)},Motiv,,0,0,0,,{text}")

    ass_path.write_text("\n".join(lines), encoding="utf-8-sig")
    logger.info(f"ASS clip edit (word-by-word) : {ass_path.name} ({len(words)} mots)")
    return ass_path


def _group_words(
    words: list[dict],
    target_duration: float = 0.9,
    pause_threshold: float = 0.45,
    max_words: int = 4,
) -> list[dict]:
    """
    Group Whisper words into subtitle cards based on natural speech rhythm.
    Breaks on: target display duration reached, natural pause detected, or
    max word count. Fast speech -> 3-4 words per card. Slow speech -> 1-2 words.
    """
    groups: list[dict] = []
    current: list[dict] = []

    for i, w in enumerate(words):
        current.append(w)
        is_last      = (i == len(words) - 1)
        group_dur    = current[-1]["end"] - current[0]["start"]
        gap_to_next  = (words[i + 1]["start"] - w["end"]) if not is_last else 999.0

        should_break = (
            group_dur    >= target_duration
            or gap_to_next >= pause_threshold
            or len(current) >= max_words
            or is_last
        )

        if should_break:
            text = " ".join(ww["word"] for ww in current).strip().replace("  ", " ")
            if text:
                groups.append({
                    "text":  text,
                    "start": current[0]["start"],
                    "end":   current[-1]["end"],
                })
            current = []

    return groups


def _clip_edit_cmd(
    clip_path: str,
    quote_png: Path,
    rw_png: Path,
    ass_path: Path | None,
    output_path: str,
    music_path: str | None = None,
) -> list[str]:
    """
    FFmpeg filter_complex:
      black canvas -> quote PNG (top) -> clip 16:9 (middle) -> blur fill (bottom)
      -> ResetWorth watermark -> ASS subtitles (if speech).
    Clip speech preserved; royalty-free music mixed under it at low volume.
    """
    if ass_path:
        last_filter = f"[with_rw]subtitles={ass_path.name}[final]"
        map_vid     = "[final]"
    else:
        last_filter = None
        map_vid     = "[with_rw]"

    parts = [
        f"[0:v]scale=1080:{_F1_CLIP_H}:force_original_aspect_ratio=increase,"
        f"crop=1080:{_F1_CLIP_H},setsar=1,eq=contrast=1.08:saturation=1.22:brightness=0.015,"
        f"format=yuv420p[clip_v]",
        f"color=c=#000000:size=1080x{VIDEO_HEIGHT}:rate={VIDEO_FPS},format=yuv420p[canvas]",
        f"[canvas][1:v]overlay=0:0,format=yuv420p[with_quote]",
        f"[with_quote][clip_v]overlay=0:{_F1_CLIP_Y}[with_clip]",
        f"[with_clip][2:v]overlay={_F1_RW_X}:{_F1_RW_Y}[with_rw]",
    ]
    if last_filter:
        parts.append(last_filter)

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,
        "-loop", "1", "-i", str(quote_png),
        "-loop", "1", "-i", str(rw_png),
    ]

    if music_path:
        music_offset = _pick_music_offset(music_path)
        cmd += ["-ss", f"{music_offset:.2f}", "-i", music_path]   # input index 3
        if _has_audio(clip_path):
            parts.append(f"[0:a]loudnorm=I={_SPEECH_TARGET_LUFS}:TP=-1.0:LRA=11[speech]")
            parts.append(
                f"[3:a]loudnorm=I={_MUSIC_TARGET_LUFS}:TP=-1.0:LRA=11,afade=t=in:d=1.5[mus]"
            )
            parts.append(
                "[speech][mus]amix=inputs=2:duration=first:dropout_transition=2:normalize=0"
                ",dynaudnorm=f=200:g=15[aout]"
            )
        else:
            parts.append(
                f"[3:a]loudnorm=I={_SPEECH_TARGET_LUFS}:TP=-1.0:LRA=11,afade=t=in:d=1.5[aout]"
            )
        map_aud = ["-map", "[aout]"]
    else:
        map_aud = ["-map", "0:a?"]

    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", map_vid,
        *map_aud,
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path,
    ]
    return cmd
