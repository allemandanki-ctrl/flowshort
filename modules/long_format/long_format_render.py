"""
Per-clip render + stitching for the YouTube long-format compilation.

Each picked clip goes through the SAME rules as format7 ("Clip Edit 2.0"):
climax-trim, silence removal, mood-matched B-roll burst (rounded-corner
frame, curated Pinterest pool), word-by-word captions shown on the B-roll
burst even when the clip's own footage already has burned-in captions
elsewhere, consistent color grade -- minus format7's top quote band (no room
for it in a landscape frame meant to fill the whole 16:9) and at
1920x1080 landscape instead of format7's portrait canvas. User: "tu fais
dans la même idée dans les short format 1 2.0... tout pareil avec les
mêmes règles, sauf pas de titres en haut et la compil est en format
paysage."

The rendered segments are then joined with a binary tree of pairwise merges
(not one long linear fold) so a 40-100 clip compilation only goes through
~log2(N) re-encode generations instead of N.
"""

import logging
import random
import subprocess
import tempfile
from pathlib import Path

from config import (
    LONG_FORMAT_WIDTH, LONG_FORMAT_HEIGHT, VIDEO_FPS,
    LONG_FORMAT_TRANSITIONS, MIN_SPEECH_WORDS,
)
from modules.assembler import (
    _build_resetworth_png, _detect_burned_caption_region, _has_audio,
    _ts_ass, _run_ffmpeg,
    _F1_WORD_BODY_COLOR, _F1_WORD_OUTLINE_COLOR,
)
from modules.transitions import probe_duration, _XFADE_NAME
from modules.long_format.long_format_clip_picker import ClipPick

logger = logging.getLogger(__name__)

_TMP_DIR = Path(tempfile.gettempdir()) / "vid_pipeline_long_format"

_SUB_FONT = 50
_WM_MARGIN = 24
_WM_W, _WM_H = 280, 60


def _tmp_dir() -> Path:
    _TMP_DIR.mkdir(exist_ok=True)
    return _TMP_DIR


def _generate_landscape_ass_word_by_word(
    words: list[dict], clip_stem: str, only_within: tuple[float, float] | None = None,
) -> Path:
    """
    Landscape counterpart to assembler._generate_clip_edit_ass_word_by_word
    -- same one-word-at-a-time style and plain white/black colors, sized for
    a 1920x1080 frame instead of portrait. only_within restricts captions to
    a single (start, end) window (the B-roll burst) when the source clip's
    own footage has burned-in captions elsewhere.
    """
    ass_path = _tmp_dir() / f"{clip_stem}_long_word.ass"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {LONG_FORMAT_WIDTH}\n"
        f"PlayResY: {LONG_FORMAT_HEIGHT}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Motiv,Arial Black,{_SUB_FONT + 14},{_F1_WORD_BODY_COLOR},&H000000FF,"
        f"{_F1_WORD_OUTLINE_COLOR},&H00000000,-1,0,0,0,100,100,2,0,1,6,1,2,80,80,100,1\n"
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
    return ass_path


def render_segment(pick: ClipPick, index: int, rw_png: Path) -> Path:
    """
    Full format7-style per-clip treatment at landscape size: climax-trim,
    silence removal, mood-matched B-roll burst (rounded corners), word-by-
    word captions (skipped on the main footage but shown on the burst when
    the clip already has its own burned-in captions), color grade, watermark.
    Always produces a video with an audio stream (silent clips get
    synthesized silence) so every segment is xfade/acrossfade/concat-
    compatible later.
    """
    from modules.clips.clip_transcriber import transcribe_clip, find_climax_segment
    from modules.clips.clip_silence import remove_silences
    from modules.clips.clip_broll import (
        extract_broll_keywords, classify_speech_mood, fetch_broll_clips, build_framed_clip,
    )

    clip_path  = pick.path
    duration   = pick.duration
    has_audio_stream = _has_audio(str(clip_path))
    words: list[dict] = []
    full_text = ""
    if has_audio_stream:
        transcript = transcribe_clip(clip_path)
        words = transcript["words"]
        full_text = transcript.get("full_text", "")

    has_speech = len(words) >= MIN_SPEECH_WORDS

    work_clip, work_words, work_duration = clip_path, words, duration
    if has_speech:
        c_start, c_end = find_climax_segment(work_words, work_duration)
        if c_start > 0.0 or c_end < work_duration:
            trimmed = _tmp_dir() / f"{index:03d}_{clip_path.stem[:20]}_climax.mp4"
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(work_clip), "-ss", f"{c_start:.3f}", "-to", f"{c_end:.3f}",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", str(trimmed)],
                capture_output=True, text=True)
            if r.returncode == 0:
                work_words = [
                    {"word": w["word"], "start": round(w["start"] - c_start, 3), "end": round(w["end"] - c_start, 3)}
                    for w in work_words if c_start <= w["start"] <= c_end
                ]
                work_clip, work_duration = trimmed, c_end - c_start
            else:
                logger.warning(f"long_format: climax trim failed for segment {index} ({r.stderr[-300:]})")

        work_clip, work_words, work_duration = remove_silences(work_clip, work_words, work_duration)

    burned_in = _detect_burned_caption_region(clip_path)["has_captions"] if has_speech else False

    keywords = extract_broll_keywords(full_text) if has_speech else []
    mood = classify_speech_mood(full_text) if has_speech else None
    broll_dir = _tmp_dir() / f"broll_{index:03d}"
    broll_clips = fetch_broll_clips(keywords, 12, broll_dir, full_text=full_text) if keywords else []

    framed = _tmp_dir() / f"{index:03d}_{clip_path.stem[:20]}_framed.mp4"
    burst_window = build_framed_clip(
        work_clip, broll_clips, work_duration, framed,
        width=LONG_FORMAT_WIDTH, height=LONG_FORMAT_HEIGHT, keywords=keywords,
    )

    only_within = None if not burned_in else burst_window
    add_subs = has_speech and (not burned_in or burst_window is not None)
    ass_path = (
        _generate_landscape_ass_word_by_word(work_words, f"{index:03d}_{clip_path.stem[:20]}", only_within=only_within)
        if add_subs else None
    )

    wm_x = LONG_FORMAT_WIDTH  - _WM_W - _WM_MARGIN
    wm_y = LONG_FORMAT_HEIGHT - _WM_H - _WM_MARGIN

    # Consistent color grade -- same as format7 -- applied to the already
    # framed/B-roll-composited clip, covers main footage AND B-roll in one pass.
    filters = [
        f"[0:v]eq=contrast=1.08:saturation=1.22:brightness=0.015,format=yuv420p[graded]",
        f"[graded][1:v]overlay={wm_x}:{wm_y}[wm]",
    ]
    map_vid = "[wm]"
    if ass_path:
        filters.append(f"[wm]subtitles={ass_path.name}[subd]")
        map_vid = "[subd]"

    cmd = ["ffmpeg", "-y", "-i", str(framed), "-loop", "1", "-i", str(rw_png)]
    # build_framed_clip maps audio optionally (-map 0:a?) -- a genuinely
    # silent source clip produces a framed output with NO audio stream at
    # all, so [0:a] would fail; synthesize silence in that case instead.
    if _has_audio(str(framed)):
        filters.append("[0:a]aformat=sample_rates=44100:channel_layouts=stereo[aout]")
    else:
        filters.append(
            f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:{work_duration:.3f}[aout]"
        )

    out = _tmp_dir() / f"seg_{index:03d}_{clip_path.stem[:20]}.mp4"
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", map_vid, "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out),
    ]
    cwd = str(ass_path.parent) if ass_path else None
    _run_ffmpeg(cmd, cwd=cwd)

    logger.info(f"long_format: segment {index} rendered | speech={has_speech} "
                f"| subs={'yes' if add_subs else 'no'} | burned_in={burned_in} | mood={mood} "
                f"| broll={len(broll_clips)}/{len(keywords)} keywords | {out.name}")
    return out


def _merge_pair(left: Path, right: Path, transition: str) -> Path:
    dur = LONG_FORMAT_TRANSITIONS.get(transition, 0.0)
    out = _tmp_dir() / f"merge_{random.randint(0, 10**9)}.mp4"

    if dur <= 0:
        cmd = [
            "ffmpeg", "-y", "-i", str(left), "-i", str(right),
            "-filter_complex",
            "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[a1];"
            "[0:v][a0][1:v][a1]concat=n=2:v=1:a=1[outv][outa]",
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(out),
        ]
    else:
        left_dur = probe_duration(str(left))
        offset   = max(0.0, left_dur - dur)
        xn       = _XFADE_NAME[transition]
        cmd = [
            "ffmpeg", "-y", "-i", str(left), "-i", str(right),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition={xn}:duration={dur}:offset={offset:.3f}[outv];"
            f"[0:a][1:a]acrossfade=d={dur}[outa]",
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(out),
        ]
    _run_ffmpeg(cmd)
    return out


def stitch_tree(segments: list[Path], boundary_transitions: list[str]) -> Path:
    """
    boundary_transitions[i] is the transition joining segments[i] and
    segments[i+1] — a binary pairwise reduce, ~log2(N) merge generations.
    """
    if len(segments) == 1:
        return segments[0]
    if len(segments) - 1 != len(boundary_transitions):
        raise ValueError("boundary_transitions must have exactly len(segments)-1 entries")

    items = list(segments)
    bnds  = list(boundary_transitions)

    while len(items) > 1:
        new_items: list[Path] = []
        new_bnds:  list[str]  = []
        i = 0
        while i < len(items) - 1:
            merged = _merge_pair(items[i], items[i + 1], bnds[i])
            new_items.append(merged)
            if i + 2 < len(items):
                new_bnds.append(bnds[i + 1])
            i += 2
        if i == len(items) - 1:
            new_items.append(items[i])
        items, bnds = new_items, new_bnds

    return items[0]


def build_segments_and_transitions(picks: list[ClipPick]) -> tuple[list[Path], list[str]]:
    rw_png = _build_resetworth_png()
    segments = [render_segment(p, i, rw_png) for i, p in enumerate(picks)]
    
    # Improved rhythm: vary transitions to avoid identical repeats
    boundary_pool = list(LONG_FORMAT_TRANSITIONS.keys())
    boundaries = []
    last_transition = None
    
    for _ in range(len(segments) - 1):
        possible_transitions = [t for t in boundary_pool if t != last_transition]
        choice = random.choice(possible_transitions if possible_transitions else boundary_pool)
        boundaries.append(choice)
        last_transition = choice
        
    return segments, boundaries
