"""
Music rotation for hook videos -- two configurable modes (config.HOOK_MUSIC_MODE):

  "pool"   : phonk+trap pool (assets/music_phonk/, 29 tracks: 7 tagged
             "Phonk" + 22 tagged "Trap" on freetouse.com -- same dynamic-BPM
             bass-heavy energy, widened because 7 alone repeats too fast at
             1 hook-video/day), anti-repeat rotation. Default. User feedback:
             hook-video music needs a "dynamic BPM phonk" character, not the
             rest of the pipeline's dark-cinematic Incompetech pool -- the
             fast-cut post-hook montage is built around a real detected beat
             grid (modules.transitions.detect_beat_grid), which only makes
             sense with a track that actually has a driving beat.
  "weekly" : one track is chosen once per ISO week and reused for every hook
             video generated that week ("musique de la semaine"), persisted in
             state/weekly_music.json. Rolls over automatically at week boundary.

LICENSING CAVEAT (unresolved, flagged again here on purpose): assets/music_phonk/
tracks are from freetouse.com's free tier, whose terms prohibit "Commercial
Content" (a brand/product-promoting account, which FlowShort is). Do not
treat "wired into rotation" as "cleared for publishing" -- that decision is
still the user's to make (pay for a commercial license, source real CC0
phonk, or accept the free-tier terms knowingly) before any hook video using
this pool goes out publicly.
"""

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from config import ASSETS_DIR, STATE_DIR, HOOK_MUSIC_MODE

logger = logging.getLogger(__name__)

_WEEKLY_FILE = STATE_DIR / "weekly_music.json"
_PHONK_DIR = ASSETS_DIR / "music_phonk"
_PHONK_USED_FILE = STATE_DIR / "phonk_used.json"
_PHONK_EXCLUDE_DAYS = 14
_PHONK_EXCLUDE_LAST_N = 4   # small pool (single digits of tracks) -- days alone
                             # wouldn't stop back-to-back repeats in one batch


def _iso_week_key(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def get_hook_music(script: dict) -> str | None:
    """Return the music path to use for a hook video, per HOOK_MUSIC_MODE."""
    if HOOK_MUSIC_MODE == "weekly":
        return _get_weekly_track(script)
    return _get_pool_track(script)


def _get_pool_track(script: dict) -> str | None:
    return _select_phonk_track()


def _get_weekly_track(script: dict) -> str | None:
    week = _iso_week_key()
    data = _load()
    entry = data.get(week)

    if entry and Path(entry["path"]).exists():
        logger.info(f"music_rotation: reusing weekly track ({week}) -> {Path(entry['path']).name}")
        return entry["path"]

    # New week (or missing file): pick a fresh track and persist it for the week.
    path = _select_phonk_track()
    if path:
        data[week] = {"path": path, "chosen_at": datetime.now(timezone.utc).isoformat()}
        _save(data)
        logger.info(f"music_rotation: new weekly track for {week} -> {Path(path).name}")
    return path


def _select_phonk_track() -> str | None:
    """
    Weighted-random pick from assets/music_phonk/, excluding tracks used in
    the last _PHONK_EXCLUDE_DAYS days OR the last _PHONK_EXCLUDE_LAST_N
    videos (whichever excludes more) -- same anti-repeat shape as the main
    Incompetech pool (music_db.recently_used), sized down for a much smaller
    pool. Falls back to the single least-recently-used track if EVERY track
    is within the exclusion window (small pool, high volume day), rather
    than returning None and silently dropping music from the video.
    """
    if not _PHONK_DIR.exists():
        return None
    tracks = sorted(_PHONK_DIR.glob("*.mp3"))
    if not tracks:
        logger.warning(f"music_rotation: no phonk tracks in {_PHONK_DIR}")
        return None

    used = _load_phonk_used()
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - _PHONK_EXCLUDE_DAYS * 86400
    recent_ids = {u["path"] for u in used if u["ts"] >= cutoff}
    recent_ids |= {u["path"] for u in used[-_PHONK_EXCLUDE_LAST_N:]}

    available = [t for t in tracks if str(t) not in recent_ids]
    if not available:
        # Whole pool excluded (small pool, high daily volume) -- reuse the
        # least-recently-used track rather than silently return no music.
        lru_order = [u["path"] for u in used]
        available = sorted(tracks, key=lambda t: lru_order.index(str(t)) if str(t) in lru_order else -1)[:1]
        logger.warning("music_rotation: entire phonk pool was in the exclusion window -- "
                       f"reusing least-recently-used track {available[0].name}")

    choice = random.choice(available)
    used.append({"path": str(choice), "ts": now.timestamp()})
    _save_phonk_used(used[-200:])   # cap the log, no unbounded growth
    logger.info(f"music_rotation: phonk pick -> {choice.name} "
                f"({len(tracks)} in pool, {len(available)} eligible)")
    return str(choice)


def _load_phonk_used() -> list[dict]:
    if _PHONK_USED_FILE.exists():
        try:
            return json.loads(_PHONK_USED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_phonk_used(used: list[dict]) -> None:
    _PHONK_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PHONK_USED_FILE.write_text(json.dumps(used, indent=2), encoding="utf-8")


def _load() -> dict:
    if _WEEKLY_FILE.exists():
        try:
            return json.loads(_WEEKLY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    _WEEKLY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WEEKLY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
