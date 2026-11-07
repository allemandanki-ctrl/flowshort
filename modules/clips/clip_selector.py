"""
Pick clip(s) from assets/clips/ (root only, not subfolders).
After use, clips are moved to assets/clips/utilisés/ — never picked again.

If the first picked clip is shorter than _SHORT_TRIGGER_SEC, additional WHOLE
clips (never trimmed/cut) are appended back-to-back until the total reaches
_TARGET_MIN_SEC, so format1 videos never end up too short. Padding stops as
soon as the target is crossed — never piles on more than needed.
"""

import logging
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import BASE_DIR

logger = logging.getLogger(__name__)

_CLIPS_DIR  = BASE_DIR / "assets" / "clips"
_USED_DIR   = _CLIPS_DIR / "utilisés"
_EXTENSIONS = {".mp4", ".mov", ".mkv"}

_SHORT_TRIGGER_SEC = 8.0    # below this, pad with more clips
_TARGET_MIN_SEC    = 10.0   # stop padding once total reaches this


def _pick_one() -> Path | None:
    """Pick a random unused clip and move it to utilisés/. None if none left."""
    if not _CLIPS_DIR.exists():
        raise FileNotFoundError(
            f"Dossier clips introuvable : {_CLIPS_DIR}\n"
            "Crée le dossier et ajoute des fichiers .mp4 / .mov."
        )

    clips = [
        p for p in _CLIPS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _EXTENSIONS
    ]
    if not clips:
        return None

    choice = random.choice(clips)
    _USED_DIR.mkdir(exist_ok=True)
    dest = _USED_DIR / choice.name
    shutil.move(str(choice), str(dest))
    logger.info(f"Clip selectionne et archive : {choice.name} -> utilises/")
    return dest


def _pick_one_max_dur(max_dur: float) -> tuple[Path, float] | None:
    """
    Pick a random unused clip whose OWN duration is <= max_dur, move it to
    utilisés/. Used only during the padding phase, so a long clip (e.g. 20s)
    can never get pulled in to "complete" a short first clip. None if no
    eligible clip is available (folder empty or every remaining clip is
    longer than max_dur).
    """
    from modules.transitions import probe_duration

    if not _CLIPS_DIR.exists():
        raise FileNotFoundError(
            f"Dossier clips introuvable : {_CLIPS_DIR}\n"
            "Crée le dossier et ajoute des fichiers .mp4 / .mov."
        )

    candidates = [
        p for p in _CLIPS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _EXTENSIONS
    ]
    eligible = [(p, probe_duration(str(p))) for p in candidates]
    eligible = [(p, d) for p, d in eligible if d <= max_dur]
    if not eligible:
        return None

    choice, dur = random.choice(eligible)
    _USED_DIR.mkdir(exist_ok=True)
    dest = _USED_DIR / choice.name
    shutil.move(str(choice), str(dest))
    logger.info(f"Clip selectionne et archive : {choice.name} ({dur:.1f}s) -> utilises/")
    return dest, dur


def select_clip() -> Path:
    """
    Pick a clip. If it's shorter than _SHORT_TRIGGER_SEC, keep appending whole
    additional clips (never cut/trimmed) until the total duration reaches
    _TARGET_MIN_SEC, then concatenate them back-to-back into one file.
    """
    from modules.transitions import probe_duration

    first = _pick_one()
    if first is None:
        raise FileNotFoundError(
            f"Aucun clip disponible dans {_CLIPS_DIR}.\n"
            "Ajoute de nouveaux clips .mp4 / .mov dans ce dossier."
        )

    first_dur = probe_duration(str(first))
    if first_dur >= _SHORT_TRIGGER_SEC:
        return first

    chosen: list[tuple[Path, float]] = [(first, first_dur)]
    total = first_dur
    while total < _TARGET_MIN_SEC:
        # Only clips <= _SHORT_TRIGGER_SEC are eligible here — otherwise a
        # long clip (e.g. 20s) could get pulled in "to complete" a short
        # first clip and blow the total way past the target.
        picked = _pick_one_max_dur(_SHORT_TRIGGER_SEC)
        if picked is None:
            logger.warning(
                f"Plus de clips <= {_SHORT_TRIGGER_SEC:.0f}s disponibles pour compléter "
                f"({total:.1f}s, cible {_TARGET_MIN_SEC:.0f}s) — la vidéo restera courte."
            )
            break
        nxt, nxt_dur = picked
        chosen.append((nxt, nxt_dur))
        total += nxt_dur

    if len(chosen) == 1:
        return chosen[0][0]

    logger.info(
        f"Clip trop court ({first.name}, {first_dur:.1f}s) — complété avec "
        f"{len(chosen) - 1} clip(s) supplémentaire(s), aucun découpage -> {total:.1f}s total"
    )
    return _concat_clips(chosen)


def _concat_clips(chosen: list[tuple[Path, float]]) -> Path:
    """
    Join whole clips back-to-back (no trimming) via ffmpeg's concat filter.
    Each input is first normalized to a common resolution/fps/pixel format —
    the concat filter requires matching streams, and source clips vary.
    """
    from modules.assembler import _has_audio
    from config import VIDEO_FPS

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f1_concat"
    tmp_dir.mkdir(exist_ok=True)
    out = tmp_dir / f"concat_{'_'.join(p.stem[:12] for p, _ in chosen)}.mp4"

    cmd = ["ffmpeg", "-y"]
    for p, _ in chosen:
        cmd += ["-i", str(p)]

    filters = []
    concat_labels = []
    for i, (p, dur) in enumerate(chosen):
        # Normalize to a common 1920x1080 16:9 canvas (minimal crop only) so
        # differing source resolutions/fps can't break the concat filter.
        filters.append(
            f"[{i}:v:0]scale=1920:1080:force_original_aspect_ratio=increase,"
            f"crop=1920:1080,setsar=1,fps={VIDEO_FPS},format=yuv420p[v{i}]"
        )
        if _has_audio(str(p)):
            filters.append(f"[{i}:a:0]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")
        else:
            # Silent source clip -> synthesize silence for its own duration
            # so the concat filter always has a valid audio stream to join.
            filters.append(
                f"anullsrc=channel_layout=stereo:sample_rate=44100,atrim=0:{dur:.3f}[a{i}]"
            )
        concat_labels.append(f"[v{i}][a{i}]")

    filters.append(f"{''.join(concat_labels)}concat=n={len(chosen)}:v=1:a=1[outv][outa]")

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat (clips trop courts) a échoué : {r.stderr[-800:]}")

    logger.info(f"Clips concaténés -> {out.name}")
    return out
