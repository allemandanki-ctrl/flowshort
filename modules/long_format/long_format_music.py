"""
Background music bed for the YouTube long-format compilation.

Same library, same mix level as Format1 (modules/assembler.py — loudnorm to
fixed LUFS targets + a dynaudnorm final pass, see _clip_edit_cmd). Repeats
are allowed but capped: no single track may cover more than
LONG_FORMAT_MUSIC_TRACK_CAP_RATIO of the final bed, and repeats of the same
track are spaced apart rather than glued back-to-back.
"""

import logging
import random
from pathlib import Path

from config import ASSETS_DIR, LONG_FORMAT_MUSIC_TRACK_CAP_RATIO
from modules.transitions import probe_duration
from modules.assembler import _run_ffmpeg, _SPEECH_TARGET_LUFS, _MUSIC_TARGET_LUFS

logger = logging.getLogger(__name__)

_MUSIC_DIR = ASSETS_DIR / "music"


def _pool() -> list[Path]:
    tracks = list(_MUSIC_DIR.glob("*/*.mp3")) + list(_MUSIC_DIR.glob("*.mp3"))
    if not tracks:
        raise FileNotFoundError(
            f"Aucune musique dans {_MUSIC_DIR} — la bibliothèque doit contenir "
            "les mêmes pistes que le Format1."
        )
    return tracks


_MIN_SLICE_SEC = 2.0   # below this, a track's remaining cap budget isn't worth including


def build_playlist(target_duration: float) -> list[tuple[Path, float]]:
    """
    Ordered list of (track, seconds_to_use) whose durations sum to
    target_duration. Tracks longer than their 20%-of-total cap allowance are
    TRIMMED to fit rather than rejected outright -- a real royalty-free track
    is often 2-4 minutes, comfortably over a cap of 132-180s (20% of an
    11-15min video), so all-or-nothing rejection would exclude most of the
    library and could empty the playlist entirely.
    """
    pool = _pool()
    durations = {t: probe_duration(str(t)) for t in pool}
    pool = [t for t in pool if durations[t] > 0]
    if not pool:
        raise FileNotFoundError("Aucune piste musicale exploitable (durée introuvable).")

    cap      = LONG_FORMAT_MUSIC_TRACK_CAP_RATIO * target_duration
    min_gap  = max(5, len(pool) // 2)   # spread repeats across roughly half the pool

    playlist: list[tuple[Path, float]] = []
    used_total: dict[Path, float] = {t: 0.0 for t in pool}
    last_slot: dict[Path, int] = {}
    total = 0.0
    attempts = 0
    max_attempts = len(pool) * 50

    order = list(pool)
    random.shuffle(order)
    idx = 0
    while total < target_duration and attempts < max_attempts:
        if idx >= len(order):
            order = list(pool)
            random.shuffle(order)
            idx = 0
        track = order[idx]
        idx += 1
        attempts += 1

        remaining_cap = cap - used_total[track]
        if remaining_cap < _MIN_SLICE_SEC:
            continue   # this track already used up its share of the total
        prev = last_slot.get(track)
        if prev is not None and (len(playlist) - prev) < min_gap:
            continue

        use_dur = min(durations[track], remaining_cap, target_duration - total)
        if use_dur < _MIN_SLICE_SEC:
            continue

        playlist.append((track, use_dur))
        used_total[track] += use_dur
        last_slot[track] = len(playlist) - 1
        total += use_dur

    if total < target_duration:
        logger.warning(
            f"long_format music: only built {total:.0f}s of {target_duration:.0f}s target "
            f"after {attempts} attempts — pool too small even after per-track trimming."
        )

    logger.info(f"long_format music: playlist of {len(playlist)} slices, {total:.0f}s "
                f"(pool={len(pool)}, cap={cap:.0f}s/track, min_gap={min_gap})")
    return playlist


def render_bed(playlist: list[tuple[Path, float]], target_duration: float, out_path: Path) -> Path:
    """Concatenate the (track, duration) slices into one continuous bed."""
    if not playlist:
        raise ValueError("Empty music playlist")

    cmd = ["ffmpeg", "-y"]
    for t, _ in playlist:
        cmd += ["-i", str(t)]

    filters: list[str] = []
    labels:  list[str] = []
    running = 0.0
    for i, (_, planned_dur) in enumerate(playlist):
        remaining = target_duration - running
        if remaining <= 0:
            break
        d = min(planned_dur, remaining)
        filters.append(
            f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{d:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        labels.append(f"[a{i}]")
        running += d

    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[abed]")
    fade_start = max(0.0, running - 2.0)
    filters.append(f"[abed]afade=t=out:st={fade_start:.3f}:d=2[aout]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[aout]",
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(out_path),
    ]
    _run_ffmpeg(cmd)
    return out_path


def mix_with_clip_audio(stitched_video: Path, music_bed: Path, out_path: Path) -> Path:
    """
    Music under the clips' own audio — same LUFS targets + dynaudnorm final
    pass as Format1's _clip_edit_cmd (modules/assembler.py), so the two
    formats sound consistent rather than one using the old flat volume=0.80.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(stitched_video), "-i", str(music_bed),
        "-filter_complex",
        f"[0:a]loudnorm=I={_SPEECH_TARGET_LUFS}:TP=-1.0:LRA=11[speech];"
        f"[1:a]loudnorm=I={_MUSIC_TARGET_LUFS}:TP=-1.0:LRA=11,afade=t=in:d=1.5[mus];"
        "[speech][mus]amix=inputs=2:duration=first:dropout_transition=2:normalize=0"
        ",dynaudnorm=f=200:g=15[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    _run_ffmpeg(cmd)
    return out_path
