"""
Format long1 assembler -- "L'Autopsie Financiere" (12-15 minute financial
documentary). The narration is the USER'S OWN REAL VOICE, recorded once per
chaptered section (hook/engrenage/psychologie/plan_action) and dropped into
assets/voices/long1/<subject>/ -- NEVER text-to-speech, per explicit user
requirement (long-form TTS narration reads as robotic). Each section's
recording is split into per-beat clips via Whisper alignment (same technique
as format14/15/16's manual-audio matching, see modules.assembler.voice_split),
so there is no TTS fallback here: a missing/unusable recording is a hard
error, not a silent substitution.

Visual language: one real fetched photo per beat (Ken Burns), landscape
1920x1080, same stock-agency-blocklist + vision watermark check as
format9/format17 (see modules.assembler.dilemma). A labeled chapter card
(blurred real-photo backdrop, not a flat gradient -- user feedback,
2026-07-27: a flat card "ne fait pas pro") opens engrenage/psychologie/
plan_action -- NOT hook, which flows straight from nothing into content.

Each beat/chapter-card clip is rendered with its OWN paired audio (silence
for chapter cards) muxed together in the SAME ffmpeg call, then every clip
is joined with the concat DEMUXER (-c copy, all clips share identical codec
params by construction). This -- not separately concatenating a video track
and an audio track and hoping their durations still line up after 25-30+
independent encodes -- is what actually guarantees the two never drift out
of sync (the earlier separate-track design produced exactly that: a stretch
with audio playing over a black/frozen frame, reported 2026-07-27).
"""

import logging
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import ASSETS_DIR, LONG_FORMAT_HEIGHT, LONG_FORMAT_WIDTH, VIDEO_FPS
from modules.assembler import _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text, _FONT_REGULAR, _FONT_SEMIBOLD, _TEXT_COLOR
from modules.assembler.dilemma import _has_visible_watermark, _is_relevant_image, _is_stock_agency_url
from modules.assembler.red_phone import _cover_resize
from modules.assembler.voice_split import concat_recordings, find_numbered_recordings, split_combined_audio
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

_SECTION_ORDER = ["hook", "engrenage", "psychologie", "plan_action"]
_VOICE_INPUT_ROOT = ASSETS_DIR / "voices" / "long1"

_CHAPTER_CARD_DUR = 3.0
_IMG_MIN_SIZE = 300
_CARD_BG_DARK  = (14, 14, 16)
_CARD_BG_LIGHT = (44, 45, 50)
_MUSIC_VOLUME = 0.10   # kept low and constant under 12-15 min of continuous narration
_AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]   # identical on every
                                                                             # clip -- required for
                                                                             # concat-demuxer -c copy


def voice_input_dir(subject: str) -> Path:
    return _VOICE_INPUT_ROOT / _slugify(subject)


def _resolve_section_recording(folder: Path, sec_id: str, tmp_dir: Path) -> Path | None:
    """A section's recording may be spread across several separate uploads
    (see voice_split.find_numbered_recordings -- user feedback, 2026-07-27:
    "je prepare pas forcement tous les audios d'une traite") -- joins them
    into one continuous take before splitting. Returns None if nothing was
    uploaded for this section at all."""
    pieces = find_numbered_recordings(folder, sec_id)
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0]
    combined = tmp_dir / f"{sec_id}_combined_input.wav"
    concat_recordings(pieces, combined)
    logger.info(f"long1: joined {len(pieces)} recording piece(s) for section {sec_id!r}")
    return combined


def build_beats(script: dict) -> list[dict]:
    """Flat, ordered list of {section, index, text, visual_query} across all 4
    sections -- the single source of truth for both the FlowShort panel (which
    text the user needs to read per section) and the assembler (audio
    splitting + per-beat rendering)."""
    beats: list[dict] = []
    sections = script.get("sections") or {}
    for sec_id in _SECTION_ORDER:
        sec = sections.get(sec_id) or {}
        for i, b in enumerate(sec.get("beats") or []):
            text = (b.get("text") or "").strip()
            if not text:
                continue
            beats.append({
                "section": sec_id, "index": i, "text": text,
                "visual_query": (b.get("visual_query") or text).strip(),
            })
    return beats


_MAX_IMAGES_PER_BEAT = 3   # cap on distinct images cycled within one beat -- bounds both the
                           # vision-check cost and how many DDGS candidates a query needs to yield
_MAX_IMAGE_HOLD = 6.0      # seconds a single image is held before cutting to the next, within a beat


def _fetch_beat_images(query: str, count: int, tmp_dir: Path, tag: str) -> list[Path]:
    """Up to `count` distinct real, on-topic images for one query -- used to cycle the
    background within a single beat instead of holding one static image for the beat's
    whole spoken duration (user feedback, 2026-07-27: static images for 20-40s straight
    "rend la video boring"). Same watermark/stock-agency/relevance filtering as
    _fetch_beat_image used to apply to a single image (see modules.assembler.dilemma)."""
    from io import BytesIO

    import requests
    from ddgs import DDGS

    if not query.strip():
        return []
    try:
        results = list(DDGS().images(query, max_results=20))
    except Exception as e:
        logger.warning(f"long1: image search failed for {query!r}: {e}")
        return []

    found: list[Path] = []
    for r in results:
        if len(found) >= count:
            break
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
        out_path = tmp_dir / f"{tag}_{len(found)}.jpg"
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            Image.open(BytesIO(resp.content)).verify()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            img.save(out_path, format="JPEG", quality=90)
        except Exception as e:
            logger.debug(f"long1: skipping image candidate {url!r}: {e}")
            continue
        if _has_visible_watermark(out_path):
            out_path.unlink(missing_ok=True)
            continue
        if not _is_relevant_image(out_path, query):
            logger.debug(f"long1: rejecting off-topic image candidate {url!r} for query {query!r}")
            out_path.unlink(missing_ok=True)
            continue
        found.append(out_path)
    return found


def _flat_gradient(dark: tuple, light: tuple) -> Image.Image:
    img = Image.new("RGB", (LONG_FORMAT_WIDTH, LONG_FORMAT_HEIGHT), dark)
    draw = ImageDraw.Draw(img)
    for y in range(LONG_FORMAT_HEIGHT):
        f = y / (LONG_FORMAT_HEIGHT - 1)
        row = tuple(int(light[c] + (dark[c] - light[c]) * f) for c in range(3))
        draw.line([(0, y), (LONG_FORMAT_WIDTH, y)], fill=row)
    return img


def _blurred_backdrop(photo: Image.Image) -> Image.Image:
    """Full-bleed cover-cropped + blurred + darkened copy of `photo` -- a
    real, on-topic backdrop for chapter cards instead of an empty flat
    gradient (user feedback, 2026-07-27: "ca fait pas pro")."""
    bg = _cover_resize(photo, LONG_FORMAT_WIDTH, LONG_FORMAT_HEIGHT)
    bg = bg.filter(ImageFilter.GaussianBlur(50))
    arr = np.asarray(bg).astype(np.float32) * 0.40
    return Image.fromarray(arr.astype(np.uint8), mode="RGB")


def _render_beat_clip(image_paths: list[Path], audio_path: Path, duration: float, out_path: Path) -> None:
    """Ken Burns zoom cycling through 1-3 real photos (or a flat gradient fallback if none
    were found) MUXED TOGETHER with this beat's own real narration audio in one ffmpeg call
    -- guarantees audio/video stay frame-accurate for this segment regardless of any
    rounding in how long the video encode actually came out. Multiple images (see
    _MAX_IMAGE_HOLD) instead of one held for the whole beat -- user feedback, 2026-07-27:
    one static image for 20-40s straight read as boring. No on-screen chapter label (separate
    earlier feedback: broke the documentary feel)."""
    srcs: list[Path] = []
    if image_paths:
        for i, p in enumerate(image_paths):
            src = out_path.with_suffix(f".src{i}.png")
            try:
                Image.open(p).convert("RGB").save(src)
                srcs.append(src)
            except Exception:
                continue
    if not srcs:
        src = out_path.with_suffix(".src0.png")
        _flat_gradient(_CARD_BG_LIGHT, _CARD_BG_DARK).save(src)
        srcs.append(src)

    n = len(srcs)
    seg_dur = duration / n
    n_frames_seg = max(1, int(round(seg_dur * VIDEO_FPS)))

    cmd = ["ffmpeg", "-y"]
    for src in srcs:
        cmd += ["-loop", "1", "-i", str(src)]
    cmd += ["-i", str(audio_path)]
    audio_idx = n

    filters = []
    for i in range(n):
        filters.append(
            f"[{i}:v]scale={LONG_FORMAT_WIDTH}:{LONG_FORMAT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={LONG_FORMAT_WIDTH}:{LONG_FORMAT_HEIGHT},setsar=1,"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={n_frames_seg}:s={LONG_FORMAT_WIDTH}x{LONG_FORMAT_HEIGHT}:fps={VIDEO_FPS},"
            f"format=yuv420p,trim=duration={seg_dur:.3f},setpts=PTS-STARTPTS[seg{i}]"
        )
    concat_in = "".join(f"[seg{i}]" for i in range(n))
    filters.append(f"{concat_in}concat=n={n}:v=1:a=0[vout]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        *_AUDIO_ARGS,
        str(out_path),
    ]
    _run_ffmpeg(cmd)
    for src in srcs:
        src.unlink(missing_ok=True)


def _render_chapter_card_clip(label: str, image_path: Path | None, duration: float, out_path: Path) -> None:
    """Chapter transition card -- blurred real-photo backdrop (falls back to
    a flat gradient only if no photo could be fetched) + the chapter label,
    muxed with matching silence. Opens engrenage/psychologie/plan_action --
    NOT hook, which starts straight into content."""
    png = out_path.with_suffix(".png")
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
        font = ImageFont.truetype(_FONT_SEMIBOLD, size=64)
    except OSError:
        font = ImageFont.truetype(_FONT_REGULAR, size=64)
    wrapped = textwrap.fill(label, width=34, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=font, align="center", spacing=14)
    x = (LONG_FORMAT_WIDTH - (bbox[2] - bbox[0])) // 2
    y = (LONG_FORMAT_HEIGHT - (bbox[3] - bbox[1])) // 2
    _soft_text(img, (x, y), wrapped, font, _TEXT_COLOR, align="center", spacing=14, stroke_width=3)
    img.convert("RGB").save(str(png), quality=92)

    n_frames = max(1, int(round(duration * VIDEO_FPS)))
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(png),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", f"scale={LONG_FORMAT_WIDTH}:{LONG_FORMAT_HEIGHT},setsar=1,fps={VIDEO_FPS},format=yuv420p",
        "-map", "0:v", "-map", "1:a",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        *_AUDIO_ARGS,
        str(out_path),
    ]
    _run_ffmpeg(cmd)
    png.unlink(missing_ok=True)


def _render_zoom_image_clip(image_path: Path, audio_path: Path, duration: float, out_path: Path) -> None:
    """Single static (already-composited) image, held for `duration` with a slow
    Ken Burns zoom instead of a flat unmoving frame -- shared by long2 (debate/
    narrator cards) and long3 (chart beats/chapter cards) so a real backdrop photo
    isn't just sitting there completely still for the length of a whole turn/beat
    (user feedback, 2026-07-27: long2/long3 "il n'y a pas d'image ou video en fond
    ... ca manque de fond et tout"). Muxed with this clip's own audio in one ffmpeg
    call, same A/V-sync reasoning as every other clip in long1/long2/long3."""
    n_frames = max(1, int(round(duration * VIDEO_FPS)))
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path), "-i", str(audio_path),
        "-vf", (
            f"scale={LONG_FORMAT_WIDTH}:{LONG_FORMAT_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={LONG_FORMAT_WIDTH}:{LONG_FORMAT_HEIGHT},setsar=1,"
            f"zoompan=z='min(zoom+0.0006,1.10)':d={n_frames}:s={LONG_FORMAT_WIDTH}x{LONG_FORMAT_HEIGHT}:fps={VIDEO_FPS},"
            f"format=yuv420p"
        ),
        "-map", "0:v", "-map", "1:a",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        *_AUDIO_ARGS,
        str(out_path),
    ]
    _run_ffmpeg(cmd)


def _concat_av_clips(paths: list[Path], out_path: Path) -> None:
    """Concat DEMUXER, not filter -- every clip was encoded with IDENTICAL
    codec params (see _AUDIO_ARGS) specifically so -c copy here is lossless
    and exact, carrying each clip's own internal A/V sync straight through
    instead of re-deriving timing from two separately concatenated tracks."""
    list_file = paths[0].parent / "av_concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.name}'" for p in paths), encoding="utf-8")
    _run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "av_concat_list.txt", "-c", "copy", out_path.name],
        cwd=str(paths[0].parent),
    )


def _mux_with_music(av_path: Path, music_path: str | None, total_dur: float, output_path: str) -> None:
    cmd = ["ffmpeg", "-y", "-i", str(av_path)]
    if music_path:
        cmd += ["-stream_loop", "-1", "-i", music_path]
        fade_out = max(0, total_dur - 3)
        filters = (
            f"[1:a]volume={_MUSIC_VOLUME},afade=t=in:st=0:d=2,afade=t=out:st={fade_out:.2f}:d=3[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        cmd += ["-filter_complex", filters, "-map", "0:v", "-map", "[aout]"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a"]
    cmd += [
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total_dur:.3f}",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)


def assemble_long1(script, visuals_result, voice_result, timestamps_result, music_path):
    """Documentary assembler -- ignores visuals_result/voice_result/timestamps_result
    (uses the user's own uploaded narration audio, split per-section, see
    _resolve_section_recording). Raises if any section's recording is missing --
    this format never falls back to synthetic TTS."""
    subject = script["subject"]
    sections = script.get("sections") or {}

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_long1"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    folder = voice_input_dir(subject)
    section_beat_audio: dict[str, list[Path]] = {}
    missing: list[str] = []

    for sec_id in _SECTION_ORDER:
        beats = (sections.get(sec_id) or {}).get("beats") or []
        if not beats:
            continue
        recording = _resolve_section_recording(folder, sec_id, tmp_dir)
        if recording is None:
            missing.append(sec_id)
            continue
        out_paths = [tmp_dir / f"{sec_id}_beat_{i}.mp3" for i in range(len(beats))]
        ok = split_combined_audio(recording, [b["text"] for b in beats], out_paths)
        if not ok:
            missing.append(f"{sec_id} (alignment failed, see logs)")
            continue
        section_beat_audio[sec_id] = out_paths

    if missing:
        raise RuntimeError(
            f"long1: missing or unusable narration audio for section(s): {', '.join(missing)} -- "
            f"drop a recording at {folder} named '<section>_recording.<ext>' for each "
            "(hook_recording, engrenage_recording, psychologie_recording, plan_action_recording). "
            "This format never falls back to synthetic TTS."
        )

    clips: list[Path] = []

    for sec_id in _SECTION_ORDER:
        sec = sections.get(sec_id) or {}
        beats = sec.get("beats") or []
        if not beats:
            continue

        if sec_id != "hook":
            # Chapter card opens every section except the hook, which flows straight
            # from nothing into content. Reuses the section's first beat's own image
            # query as the chapter card's backdrop.
            chapter_fetched = _fetch_beat_images(beats[0].get("visual_query", ""), 1, tmp_dir, f"{sec_id}_chapter")
            chapter_clip = tmp_dir / f"{sec_id}_chapter.mp4"
            _render_chapter_card_clip(sec.get("label", sec_id), chapter_fetched[0] if chapter_fetched else None,
                                       _CHAPTER_CARD_DUR, chapter_clip)
            clips.append(chapter_clip)

        for i, beat in enumerate(beats):
            audio_path = section_beat_audio[sec_id][i]
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
    out  = _VIDEOS_DIR / f"{ts}_long1_{slug}.mp4"

    _mux_with_music(av_concat, music_path, total_dur, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"long1: video ready | {subject!r} | {len(clips)} clips | "
                f"{total_dur / 60:.1f} min | {size_mb:.1f} MB -> {out}")
    return out
