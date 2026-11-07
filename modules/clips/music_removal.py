"""
Conditional background-music removal for format1/format7/format18's source
clips ONLY -- see those formats' call sites (pipeline.py's format1/format7
branches, app.py's format18 build closure). Wraps tools/media/remove_music.py
(Demucs htdemucs vocal isolation, already used by tools/media/youtube_pipeline.py)
behind the has_background_music() gate from music_presence.py, so Demucs'
~5-15 min runtime (per remove_music.py's own comment) only fires when it's
actually likely to matter.

KNOWN LIMITATION (2026-07-30): tools/media/remove_music.py's Demucs 2-stem
split (--two-stems=vocals) keeps ONLY the vocals stem -- it discards
no_vocals entirely, which bundles the music bed AND any ambient sound/foley
together indistinguishably. So despite that script's own docstring ("garde
voix + effets"), calling it actually strips ALL non-vocal audio, ambient
included, not just music. Not fixed here (out of scope for this pass) --
documented so it's a known, visible tradeoff rather than a silent surprise.
"""

import logging
import shutil
import sys
from pathlib import Path

from config import BASE_DIR

logger = logging.getLogger(__name__)

_TOOLS_MEDIA_DIR = BASE_DIR / "tools" / "media"


def maybe_strip_music(clip_path: Path, *, force: bool | None = None) -> Path:
    """
    Returns the (possibly music-stripped) clip to actually use downstream.
    `force`: True/False overrides the detector (the UI's manual checkbox);
    None runs has_background_music() and uses its verdict.

    Fails soft on ANY removal-side error, INCLUDING SystemExit -- remove_music()
    calls sys.exit(1) on its own internal failure paths (missing input, no
    vocals stem produced) instead of raising, and sys.exit raises SystemExit,
    a BaseException NOT caught by a bare `except Exception`. A naive wrapper
    would let a Demucs hiccup kill this whole background job -- every
    generation job in this app runs through ONE persistent worker thread (see
    app.py's _gen_worker), so an uncaught SystemExit there risks taking down
    every future queued job, not just this one. Always returns the ORIGINAL
    clip on any failure rather than propagate.
    """
    from modules.clips.music_presence import has_background_music

    clip_path = Path(clip_path)
    if force is None:
        should_strip, score = has_background_music(str(clip_path))
    else:
        should_strip, score = force, None

    if not should_strip:
        logger.info(f"maybe_strip_music: no music detected in {clip_path.name} (score={score}); left as-is")
        return clip_path

    # Copy into a scratch dir before calling remove_music() -- it always
    # writes <stem>_no_music<ext> NEXT TO its input, and for a pool-picked
    # clip that input already lives in assets/clips/utilisés/ (clip_selector
    # already archived it there before this runs) -- writing derivatives
    # into that archive folder would clutter it forever. Harmless for an
    # already-scratch upload/download path, but copying unconditionally
    # keeps this one code path simple.
    scratch_dir = BASE_DIR / "scratch" / "music_removal"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    work_copy = scratch_dir / clip_path.name
    shutil.copy2(str(clip_path), str(work_copy))

    try:
        if str(_TOOLS_MEDIA_DIR) not in sys.path:
            sys.path.insert(0, str(_TOOLS_MEDIA_DIR))
        from remove_music import remove_music   # tools/media/remove_music.py -- no package __init__, path-inserted like tools/media/youtube_pipeline.py already does for its own imports
        out_path = remove_music(str(work_copy))
        logger.info(f"maybe_strip_music: stripped music from {clip_path.name} (score={score}) -> {Path(out_path).name}")
        return Path(out_path)
    except (Exception, SystemExit) as e:
        logger.warning(f"maybe_strip_music: removal failed for {clip_path.name} ({e}); using original clip")
        return clip_path
