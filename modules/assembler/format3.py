"""
Format 3 assembler -- Luxury slideshow, mixed image/video pool.

format3/3a/3b used to be three separate renderers (static-image slideshow,
video-bg + overlay, and an alias of the first). Merged into one: the pool
mixes whatever real footage is available (Drive/Pexels videos AND
Drive/Pexels/Pollinations images, shuffled together) -- doesn't matter if it
ends up all-video, all-image, or mixed. assemble_format3/assemble_format3a
are now the SAME function kept under both names, since mf2 (MindFile brand)
and modules.publish.manual_queue still import assemble_format3a specifically.
"""

import logging
import random
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _render_quote_overlay, _loop_clips_to_duration,
    _format1_cmd, _concat_raw_clips_cmd,
)

logger = logging.getLogger(__name__)

_FORMAT3_SEG_DUR = 4
_SINGLE_QUOTE_DUR = 8.5   # default total length of the single-quote-for-the-whole-video format

def assemble_format3a(script, visuals_result, voice_result, timestamps_result, music_path,
                       fast_cut: bool = False, clip_cut_dur: float | list[float] = 1.2,
                       single_quote: bool = False, single_quote_dur: float = _SINGLE_QUOTE_DUR,
                       darken: bool = False):
    videos = visuals_result.get("videos", [])
    images = visuals_result.get("images", [])
    media_pool = videos + images
    if not media_pool:
        raise ValueError("format3 needs at least 1 image or video clip")
    random.shuffle(media_pool)

    if single_quote:
        lines = [script["hook"]]
        total = sum(clip_cut_dur) if (fast_cut and isinstance(clip_cut_dur, list)) else single_quote_dur
        seg_dur = total
    else:
        lines = [script["hook"]] + list(script.get("body", []))
        if script.get("include_book_cta") and script.get("cta"):
            lines.append(script["cta"])
        seg_dur = _FORMAT3_SEG_DUR
        total   = len(lines) * seg_dur

    n = len(lines)

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f3a"
    tmp_dir.mkdir(exist_ok=True)
    overlay_pngs = []
    for i, line in enumerate(lines):
        out = str(tmp_dir / f"overlay_{i:02d}.png")
        _render_quote_overlay(line, out)
        overlay_pngs.append(out)
        logger.info(f"Overlay {i}: {line[:50]}")

    clip_dur = clip_cut_dur if fast_cut else None
    bg_clips = _loop_clips_to_duration(media_pool, total, clip_dur=clip_dur)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = script["subject"][:30].replace(" ", "_")
    out  = _VIDEOS_DIR / f"{ts}_format3_{slug}.mp4"

    cmd = _format1_cmd(bg_clips, overlay_pngs, seg_dur, total, music_path, str(out), clip_dur=clip_dur, darken=darken)
    logger.info(f"Assembling format3 | segments={n} | total={total}s | bg_clips={len(bg_clips)} "
                f"({len(videos)} videos + {len(images)} images in pool) | "
                f"fast_cut={fast_cut}" + (" | single_quote=True" if single_quote else ""))
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


# format3/format3b used to be a separate static-image-only renderer -- now
# the same mixed-pool function under both names (dispatchers dict + mf2 +
# manual_queue all still reference one or the other).
assemble_format3 = assemble_format3a

def assemble_format3a_manual(script, visuals_result, music_path=None):
    """
    Beat-matched, hook-free format3 rendering -- used both by the manual
    publish queue (see modules/publish/manual_queue.py) and by the regular batch/
    dashboard "Matcher sur le beat" toggle for format3 slots that don't get
    a hook (see pipeline.run's beat_match param).

    Same multi-line quote-overlay structure as a normal (default, no-hook,
    no-beat-match) format3 video -- the only deliberate difference:
    background clips (whatever mix of Drive/Pexels videos and images ended
    up in the pool) are cut on the music's beat grid, using the exact same
    mechanism assemble_with_hook already uses for its content segment
    (detect_beat_grid + assemble_format3a's own fast_cut/clip_cut_dur
    machinery) -- just sized to the video's FULL duration instead of a hook
    video's truncated ~16s tail, since there's no hook eating into the
    runtime here.
    """
    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    target_total = _SINGLE_QUOTE_DUR

    clip_cut_dur: float | list[float] = 1.2
    bpm = 0.0
    if music_path:
        from modules.transitions import detect_beat_grid, detect_tempo
        bpm = detect_tempo(music_path)
        beat_durations = detect_beat_grid(music_path, start_offset=0.0, max_duration=max(target_total, 25.0))
        if beat_durations:
            truncated, running = [], 0.0
            for d in beat_durations:
                if running >= target_total:
                    break
                truncated.append(d)
                running += d
            clip_cut_dur = truncated or beat_durations[:1]
        elif bpm > 0:
            clip_cut_dur = max(0.4, min(2.0, (60.0 / bpm) * 2))

    logger.info(f"format3a manual: beat-matched, no hook, single quote | target={target_total}s")
    out = assemble_format3a(script, visuals_result, None, None, music_path,
                            fast_cut=True, clip_cut_dur=clip_cut_dur,
                            single_quote=True, single_quote_dur=target_total, darken=True)

    # Rule n°4 (implementation §4): record bpm/genre/duration for the manual
    # queue's trending-sound matching (modules/manual_queue.pick_trending_sound).
    from modules.transitions import probe_duration
    credit = script.get("_music_credit") or {}
    script["_manual_music_meta"] = {
        "bpm": bpm or credit.get("bpm"),
        "genre": credit.get("genre"),
        "duration": probe_duration(str(out)),
    }
    return out


# "Mode template" -- 9 equal hard cuts, no hook, no beat matching. Built so the
# 9 cut boundaries are known in ADVANCE (i * _TEMPLATE_SEG_DUR) instead of
# needing scene-detection after the fact -- modules/publish/template_parts.py's
# split_into_parts() slices the finished render back into those exact 9
# segments for a per-video Drive folder, which the user then edits together
# for real in a CapCut template. See plan doc for the full round-trip.
_TEMPLATE_CLIP_COUNT = 9
_TEMPLATE_SEG_DUR = _SINGLE_QUOTE_DUR / _TEMPLATE_CLIP_COUNT   # ~0.944s each, sums to exactly 8.5s

def assemble_format3a_template(script, visuals_result, music_path=None):
    """
    Hook-free, beat-match-free format3 rendering for "Mode template": same
    single-quote-overlay structure as assemble_format3a_manual, but cut on a
    plain equal grid (_TEMPLATE_CLIP_COUNT fixed-length segments) instead of
    the music's beat grid -- deliberately, so the caller can split the
    finished render back into its 9 source segments without re-detecting cut
    points. Exactly 9, never more (each extra segment is real API/render
    cost for a deliverable that only ever gets exported at 9 anyway).
    """
    if music_path is None:
        from modules.music.music_library import select_music
        music_path = select_music(script)

    clip_cut_dur = [_TEMPLATE_SEG_DUR] * _TEMPLATE_CLIP_COUNT

    logger.info(f"format3a template: {_TEMPLATE_CLIP_COUNT} equal cuts, no hook, no beat-match | "
                f"total={_SINGLE_QUOTE_DUR}s")
    out = assemble_format3a(script, visuals_result, None, None, music_path,
                             fast_cut=True, clip_cut_dur=clip_cut_dur,
                             single_quote=True, single_quote_dur=_SINGLE_QUOTE_DUR, darken=True)
    _build_template_extras(visuals_result.get("videos", []), out)
    return out


def _build_template_extras(videos: list[str], out: Path) -> None:
    """
    Mode template's Drive drop also gets 2 raw (no quote overlay) B-roll
    deliverables on top of the 9 quote segments, straight from the SAME
    Drive pool this render already used (format3 is Drive-exclusive, no
    Pexels fallback -- see modules/visuals.py):
      - "{stem}_extra_clip.<ext>"  -- one lone source clip, untouched
        (straight file copy, no re-encode -- "un clip normal que tu
        télécharges").
      - "{stem}_extra_2clips.mp4" -- two DISTINCT source clips concatenated
        back to back, no text/music ("2 clips que tu colles ensemble sans
        quote"). Needs >=2 clips in the pool; skipped with a warning if the
        pool is too small (silently reusing the same clip glued to itself
        would look like a glitch, not an extra).
    Both saved next to `out` -- modules/publish/template_parts.py picks them
    up by that naming convention when uploading the 9 parts. User request
    2026-08-08.
    """
    if not videos:
        logger.warning("format3a template: no video clips in pool, skipping the 2 raw extra clips")
        return

    pool = list(videos)
    random.shuffle(pool)

    normal_src = pool[0]
    normal_out = out.with_name(f"{out.stem}_extra_clip{Path(normal_src).suffix}")
    shutil.copyfile(normal_src, normal_out)
    logger.info(f"format3a template extra: normal clip -> {normal_out.name}")

    if len(pool) < 2:
        logger.warning("format3a template: only 1 video clip in pool, skipping the 2-clips-glued extra")
        return
    # Prefer 2 clips distinct from the "normal" one above when the pool is
    # big enough (3+); with exactly 2, reusing pool[0] is unavoidable.
    glue_pair = pool[1:3] if len(pool) >= 3 else pool[:2]
    glued_out = out.with_name(f"{out.stem}_extra_2clips.mp4")
    _run_ffmpeg(_concat_raw_clips_cmd(glue_pair, str(glued_out)))
    logger.info(f"format3a template extra: 2 clips glued -> {glued_out.name}")
