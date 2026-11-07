"""
YouTube long-form compilation generator.

Orchestrates: clip selection from assets/clips/utilisés/ (11-15min window,
reuse-capped WITHIN one video) -> per-clip watermark+subtitle render ->
binary-tree transition stitching -> continuous background music bed -> final
mux -> archive the used clips into assets/clips/utilisés/utilisés pour
youtube/ so they're never picked for a future long-format video. Produces
one .mp4 in output/videos/ plus a script JSON so the dashboard (app.py)
picks it up like any other format.

Does not touch Format1/3/3a/3b code paths or the mixte batch counter --
DOES read from and archive into assets/clips/utilisés/ (clips already
archived there by Format1/7, reused here since a long-format compilation is
a different consumption pattern -- see long_format_clip_picker.py).
"""

import json
import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from config import OUTPUT_DIR
from modules.long_format.long_format_clip_picker import select_clips, archive_used_clips
from modules.long_format.long_format_render import build_segments_and_transitions, stitch_tree, _tmp_dir
from modules.long_format.long_format_music import build_playlist, render_bed, mix_with_clip_audio
from modules.long_format.long_format_state import record_generation, append_record
from modules.transitions import probe_duration
from modules.content_meta import generate_meta_long
from modules.thumbnail import build_thumbnail

logger = logging.getLogger(__name__)

_VIDEOS_DIR = OUTPUT_DIR / "videos"
_SCRIPTS_DIR = OUTPUT_DIR / "scripts"
_THUMBS_DIR = OUTPUT_DIR / "thumbnails"


def _slugify(text: str, max_len: int = 30) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.strip().lower())[:max_len].strip("_") or "video"


def generate(subject: str, assembly_mode: str = "random") -> Path:
    logger.info("=" * 60)
    logger.info(f"LONG-FORMAT START | subject={subject!r} | assembly_mode={assembly_mode}")
    logger.info("=" * 60)

    shutil.rmtree(_tmp_dir(), ignore_errors=True)
    _tmp_dir().mkdir(parents=True, exist_ok=True)

    logger.info("[1/4] Sélection des clips...")
    picks = select_clips(assembly_mode=assembly_mode)
    if not picks:
        raise RuntimeError("Aucun clip sélectionné — vérifie assets/clips/.")

    logger.info(f"[2/4] Rendu de {len(picks)} segments + assemblage transitions...")
    segments, boundaries = build_segments_and_transitions(picks)
    stitched = stitch_tree(segments, boundaries)
    total_duration = probe_duration(str(stitched))
    logger.info(f"      durée assemblée : {total_duration:.1f}s")

    logger.info("[3/4] Musique de fond...")
    playlist   = build_playlist(total_duration)
    bed_path   = _tmp_dir() / "music_bed.mp3"
    render_bed(playlist, total_duration, bed_path)

    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(subject)
    out  = _VIDEOS_DIR / f"{ts}_youtube_long_{slug}.mp4"

    logger.info("[4/4] Mixage audio final...")
    mix_with_clip_audio(stitched, bed_path, out)

    logger.info("[meta] Titre / description...")
    meta = generate_meta_long({
        "format": "youtube_long", "subject": subject,
        "clips_used": [p.path.name for p in picks], "duration_sec": total_duration,
    })

    logger.info("[thumb] Miniature...")
    _THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = _THUMBS_DIR / f"{ts}_youtube_long_{slug}.jpg"
    thumb = build_thumbnail(
        [p.path for p in picks], meta["title"], thumb_path, _tmp_dir() / "thumb",
    )

    _save_script(ts, subject, picks, total_duration, assembly_mode, meta, thumb)

    clip_placements = [
        {"name": p.path.name, "index": i,
         "frac": (i / (len(picks) - 1)) if len(picks) > 1 else 0.0}
        for i, p in enumerate(picks)
    ]
    append_record(out.name, clip_placements)
    counter = record_generation()

    size_mb = out.stat().st_size / (1024 * 1024)

    # Only archive once the file genuinely exists on disk -- a failed render
    # must never remove a clip from the utilisés/ pool.
    archive_used_clips(picks)

    logger.info("=" * 60)
    logger.info(f"DONE | {out.name} | {size_mb:.1f} MB | {total_duration/60:.1f} min | "
                f"day {counter['day_index']}, video {counter['videos_today']} today")
    logger.info("=" * 60)
    return out


def _save_script(ts: str, subject: str, picks, total_duration: float, assembly_mode: str,
                 meta: dict, thumb: Path | None) -> None:
    from modules.content_meta import _pick_hashtags
    _SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    script = {
        "format": "youtube_long",
        "subject": subject,
        "hook": "",
        "assembly_mode": assembly_mode,
        "duration_sec": round(total_duration, 1),
        "clips_used": [p.path.name for p in picks],
        "clips_reused": [p.path.name for p in picks if p.is_reused],
        "title": meta.get("title", ""),
        "description": meta.get("description", ""),
        "hashtags": _pick_hashtags(),
        "thumbnail": str(thumb.relative_to(OUTPUT_DIR)) if thumb else "",
    }
    path = _SCRIPTS_DIR / f"{ts}_youtube_long_{_slugify(subject)}.json"
    path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Script saved to {path}")
