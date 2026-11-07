"""
Clip selection for the YouTube long-format compilation.

Reads assets/clips/utilisés/ -- clips already archived by Format1/7's
clip_selector.py (assets/clips/ root is a single-use-per-clip pool for those
formats; long-format is a many-clips-per-video COMPILATION, so reusing
already-"used" clips is fine here, a different consumption pattern). This
module never moves or deletes files during selection -- the SOURCE pool in
utilisés/ never shrinks from long-format's own usage.

Reuse of a clip ACROSS separate long-format videos (not just within one
video's own pick list) IS allowed and controlled -- tracked in
state/long_format_clip_history.json via modules.long_format.long_format_state, capped
and scattered per config.LONG_FORMAT_REUSE_CAP_RATIO (max reuse share) and
the _MIN_POSITION_GAP_FRAC / _MIN_REUSED_GAP_CLIPS rules below (a reused
clip can't land too close to where it appeared before, or too close to
another reused clip in the same video). This is the real anti-repetition
mechanism -- user explicitly asked to keep it working: "je veux remettre le
système plus sophistiqué en place."

After a video renders successfully, long_format_generator.py calls
archive_used_clips() below, which COPIES (not moves) each used clip into
assets/clips/utilisés/utilisés pour youtube/ as a historical record of
"this clip has appeared in a YouTube long-format video" -- purely a marker
subfolder, NOT a pool-exclusion mechanism. The source clip stays in
utilisés/ and remains a normal candidate for the next long-format
generation, governed entirely by the reuse-cap system above.
"""

import logging
import random
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from config import (
    ASSETS_DIR, LONG_FORMAT_MIN_SEC, LONG_FORMAT_MAX_SEC,
    LONG_FORMAT_REUSE_CAP_RATIO,
)
from modules.transitions import probe_duration
from modules.long_format.long_format_state import past_placements

logger = logging.getLogger(__name__)

_CLIPS_DIR  = ASSETS_DIR / "clips" / "utilisés"
_USED_FOR_YOUTUBE_DIR = _CLIPS_DIR / "utilisés pour youtube"
_EXTENSIONS = {".mp4", ".mov", ".mkv"}

_MIN_POSITION_GAP_FRAC = 0.20   # reused clip must land >=20% of video away from any past placement
_MIN_REUSED_GAP_CLIPS  = 3      # two reused clips must be >=3 picks apart in THIS video
_MAX_ATTEMPTS_MULT     = 20     # attempts cap = pool size * this, before giving up


@dataclass(frozen=True)
class ClipPick:
    path: Path
    duration: float
    is_reused: bool


def _candidate_pool() -> list[Path]:
    if not _CLIPS_DIR.exists():
        raise FileNotFoundError(
            f"Dossier clips introuvable : {_CLIPS_DIR}\n"
            "Il se remplit automatiquement au fur et à mesure que Format1/7 utilisent des clips."
        )
    # Non-recursive: naturally excludes the "utilisés pour youtube" subfolder
    # (a directory, filtered out by is_file()) without extra logic.
    clips = [p for p in _CLIPS_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in _EXTENSIONS]
    if not clips:
        raise FileNotFoundError(
            f"Aucun clip disponible dans {_CLIPS_DIR}.\n"
            "Utilise Format1/7 sur quelques vidéos d'abord pour peupler ce dossier, "
            "ou ajoute des clips .mp4 / .mov directement dedans."
        )
    return clips


def _order_pool(pool: list[Path], assembly_mode: str) -> list[Path]:
    if assembly_mode != "context":
        shuffled = list(pool)
        random.shuffle(shuffled)
        return shuffled

    # "context" mode — best-effort heuristic only (no full transcription pass
    # here, that happens once per chosen clip during render; ordering by
    # filename-stem similarity is a cheap proxy for topical grouping, not
    # true semantic coherence). Greedy nearest-neighbor chain.
    remaining = list(pool)
    random.shuffle(remaining)
    if not remaining:
        return remaining
    chain = [remaining.pop()]
    while remaining:
        last_stem = chain[-1].stem.lower()
        best_i, best_score = 0, -1.0
        for i, p in enumerate(remaining):
            score = SequenceMatcher(None, last_stem, p.stem.lower()).ratio()
            if score > best_score:
                best_i, best_score = i, score
        chain.append(remaining.pop(best_i))
    return chain


def select_clips(assembly_mode: str = "random") -> list[ClipPick]:
    """
    Greedily pick clips until total duration crosses LONG_FORMAT_MIN_SEC,
    never letting a pick push the running total past LONG_FORMAT_MAX_SEC.
    Enforces the reuse cap/scatter rules against long_format_clip_history.json.
    """
    pool = _candidate_pool()

    chosen: list[ClipPick] = []
    chosen_names: set[str] = set()
    running_total  = 0.0
    reused_total   = 0.0
    last_reused_at = -_MIN_REUSED_GAP_CLIPS
    attempts       = 0
    max_attempts   = len(pool) * _MAX_ATTEMPTS_MULT

    order = _order_pool(pool, assembly_mode)
    idx = 0

    while running_total < LONG_FORMAT_MIN_SEC and attempts < max_attempts:
        if idx >= len(order):
            order = _order_pool(pool, assembly_mode)
            idx = 0
        candidate = order[idx]
        idx += 1
        attempts += 1

        if candidate.name in chosen_names:
            continue   # never the same physical clip twice within ONE video

        dur = probe_duration(str(candidate))
        if dur <= 0:
            continue
        if running_total + dur > LONG_FORMAT_MAX_SEC:
            continue   # would overshoot the ceiling — try a different (shorter) clip

        placements = past_placements(candidate.name)
        is_reused  = bool(placements)

        if is_reused:
            reuse_cap = LONG_FORMAT_REUSE_CAP_RATIO * LONG_FORMAT_MIN_SEC
            if reused_total + dur > reuse_cap:
                continue   # would blow the 40% reuse cap
            if len(chosen) - last_reused_at < _MIN_REUSED_GAP_CLIPS:
                continue   # reused clips must be scattered, not clumped
            est_frac = min(1.0, running_total / LONG_FORMAT_MIN_SEC)
            if any(abs(est_frac - p["frac"]) < _MIN_POSITION_GAP_FRAC for p in placements):
                continue   # same relative position as a past use — skip for now

        chosen.append(ClipPick(path=candidate, duration=dur, is_reused=is_reused))
        chosen_names.add(candidate.name)
        running_total += dur
        if is_reused:
            reused_total   += dur
            last_reused_at = len(chosen) - 1

    if running_total < LONG_FORMAT_MIN_SEC:
        logger.warning(
            f"long_format: only reached {running_total:.0f}s after {attempts} attempts "
            f"(target {LONG_FORMAT_MIN_SEC}s) — pool too small or too constrained by "
            "reuse/scatter rules. Using what was found."
        )

    logger.info(
        f"long_format: selected {len(chosen)} clips, {running_total:.0f}s total "
        f"({reused_total:.0f}s reused, {len(pool)} candidates in pool)"
    )
    return chosen


def archive_used_clips(picks: list[ClipPick]) -> None:
    """
    Called by long_format_generator.py AFTER a video has fully rendered --
    COPIES each picked clip from assets/clips/utilisés/ into
    assets/clips/utilisés/utilisés pour youtube/ as a historical marker
    ("this clip has appeared in a YouTube long-format video"). Does NOT
    remove the source clip -- it stays a normal candidate for future
    long-format picks, governed by the reuse-cap/scatter system in
    select_clips() above, not by this subfolder. Deliberately only runs
    post-render, not at selection time.

    Best-effort per file: a clip already moved/renamed/deleted between
    selection and archiving is logged and skipped, never raises (the video
    already exists on disk at this point, nothing should block returning it).
    """
    import shutil
    _USED_FOR_YOUTUBE_DIR.mkdir(parents=True, exist_ok=True)
    archived = 0
    for pick in picks:
        try:
            if pick.path.exists():
                shutil.copy2(str(pick.path), str(_USED_FOR_YOUTUBE_DIR / pick.path.name))
                archived += 1
        except Exception as e:
            logger.warning(f"long_format: could not archive {pick.path.name} ({e})")
    logger.info(f"long_format: archived (copy) {archived}/{len(picks)} clip(s) -> "
                f"{_USED_FOR_YOUTUBE_DIR.relative_to(ASSETS_DIR)} (source stays in the pool)")
