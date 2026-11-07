"""
Remove silence gaps from a speech clip (video+audio together), based on the
Whisper word timestamps already computed for it -- no separate silence
detection pass needed, the gaps between consecutive words ARE the silences.

Reference technique: "we want to remove any kind of silences so that the
video is flowing super smooth... this will help retention and increase
watch time." Cutting on WORD gaps (not a generic silencedetect threshold)
keeps mid-word breath pauses intact and only removes genuinely dead air.
"""

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_MIN_GAP        = 0.5    # gaps shorter than this are natural speech rhythm, keep them
_KEEP_PADDING   = 0.08   # seconds of padding kept on each side of a cut, avoids clipping the word
_MAX_CUTS       = 40     # sanity ceiling -- a transcript with more gaps than this is probably noisy


def remove_silences(
    clip_path: Path, words: list[dict], duration: float,
) -> tuple[Path, list[dict], float]:
    """
    Cuts out silence gaps >= _MIN_GAP between consecutive words. Returns
    (new_clip_path, new_words, new_duration) -- new_words has timestamps
    re-based to the shortened timeline so captions stay in sync.

    Falls back to the original clip unchanged if there's nothing worth
    cutting (few/no gaps) or if the ffmpeg pass fails.
    """
    if len(words) < 2:
        return clip_path, words, duration

    gaps = []
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end   = words[i + 1]["start"]
        if gap_end - gap_start >= _MIN_GAP:
            gaps.append((gap_start, gap_end))

    if not gaps or len(gaps) > _MAX_CUTS:
        if len(gaps) > _MAX_CUTS:
            logger.warning(f"clip_silence: {len(gaps)} gaps found (> {_MAX_CUTS}), "
                           "likely noisy transcript -- skipping silence removal")
        return clip_path, words, duration

    # Build "keep" segments: everything BETWEEN gaps (with padding eaten back
    # into the gap, never into the word itself).
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for gap_start, gap_end in gaps:
        keep.append((cursor, min(gap_start + _KEEP_PADDING, duration)))
        cursor = max(gap_end - _KEEP_PADDING, gap_start)
    keep.append((cursor, duration))
    keep = [(s, e) for s, e in keep if e - s > 0.05]   # drop degenerate slivers

    if len(keep) < 2:
        return clip_path, words, duration

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f1_silence"
    tmp_dir.mkdir(exist_ok=True)
    out = tmp_dir / f"{clip_path.stem}_nosilence.mp4"

    filters = []
    v_labels, a_labels = [], []
    for i, (s, e) in enumerate(keep):
        filters.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
        filters.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")
    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    filters.append(f"{concat_inputs}concat=n={len(keep)}:v=1:a=1[outv][outa]")

    cmd = [
        "ffmpeg", "-y", "-i", str(clip_path),
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning(f"clip_silence: ffmpeg cut failed ({r.stderr[-400:]}); using original clip")
        return clip_path, words, duration

    removed = sum((s1 - e0) for (_, e0), (s1, _) in zip(keep[:-1], keep[1:]))
    new_duration = duration - removed

    # Re-base word timestamps: subtract, for each word, the total silence
    # removed strictly before it.
    new_words = []
    for w in words:
        shift = sum(max(0.0, min(gap_end, w["start"]) - gap_start)
                    for gap_start, gap_end in gaps if gap_start < w["start"])
        new_words.append({
            "word":  w["word"],
            "start": round(max(0.0, w["start"] - shift), 3),
            "end":   round(max(0.0, w["end"] - shift), 3),
        })

    logger.info(f"clip_silence: removed {len(gaps)} gaps ({removed:.1f}s) -> "
                f"{duration:.1f}s to {new_duration:.1f}s")
    return out, new_words, new_duration
