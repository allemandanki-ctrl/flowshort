"""
Music matcher — pick the best-fit track for a video via semantic similarity.

Flow (as specified):
  1. Claude turns the video (script + format) into a mood description
     e.g. "confident dark instrumental, finance motivation, ~120 BPM, no vocals".
  2. That text is embedded and cosine-matched against the track embeddings.
  3. Anti-repetition: exclude tracks used in the last 30 days OR last 50 videos.
  4. Weighted-random pick among the surviving top 5 (score-weighted) — avoids
     always taking the single best match (which would loop).
  5. The chosen track's MP3 is downloaded just-in-time and cached.
"""

import logging
import random
import re
import urllib.parse
from pathlib import Path

import numpy as np
import requests

from config import ASSETS_DIR, CLAUDE_MODEL
from modules.music import music_db, music_embed

logger = logging.getLogger(__name__)

_CACHE_DIR = ASSETS_DIR / "music" / "catalog"
_TOP_CANDIDATES = 40
_TOP_POOL       = 5
_EXCLUDE_DAYS   = 30
_EXCLUDE_LAST_N = 50


def select_track(script: dict, video_name: str | None = None) -> str | None:
    """Return absolute path to the chosen track's mp3, or None if DB empty."""
    ids, mat = music_db.load_embeddings()
    if len(ids) == 0:
        logger.warning("music matcher: DB empty — run music_catalog + music_embed. Falling back.")
        return None

    # generate_script() already asks Claude for a "music_style" field on every
    # script (see script_generator.py's per-format instructions) -- reuse it
    # instead of firing a second, separate `claude -p` call here for what's
    # effectively the same question. Only formats with no music at all set it
    # to "none"/empty, and those (plus any pre-existing script missing the
    # field) still fall back to the dedicated Claude call below.
    music_style = (script.get("music_style") or "").strip()
    if music_style and music_style.lower() not in ("none", "n/a"):
        query = f"{music_style}, no vocals"
        logger.info(f"music matcher: reusing script's music_style -> {query!r}")
    else:
        query = generate_mood_query(script)
        logger.info(f"music matcher: mood query = {query!r}")
    qv = music_embed.embed_query(query)

    scores = mat @ qv                      # cosine (both normalized)
    order = np.argsort(-scores)[:_TOP_CANDIDATES]

    excluded = music_db.recently_used(days=_EXCLUDE_DAYS, last_n=_EXCLUDE_LAST_N)
    ranked = [(ids[i], float(scores[i])) for i in order]
    fresh = [(tid, sc) for tid, sc in ranked if tid not in excluded]
    pool = (fresh or ranked)[:_TOP_POOL]

    chosen_id = _weighted_choice(pool)
    track = music_db.get_track(chosen_id)
    path = _ensure_downloaded(track)
    if path is None:
        # download failed — try next candidates
        for tid, _ in pool:
            if tid == chosen_id:
                continue
            path = _ensure_downloaded(music_db.get_track(tid))
            if path:
                chosen_id = tid
                break
    if path is None:
        return None

    music_db.record_usage(chosen_id, video_name or script.get("subject", "?"))
    logger.info(f"music matcher: {track['title']!r} (bpm={track['bpm']}, feel={track['feel']!r})")

    # Every track in this DB is Incompetech/Kevin MacLeod, CC-BY-4.0 -- attribution
    # is a legal requirement of that license, not optional. Stash it on the script
    # so content_meta.build_social_caption can append a credit line automatically.
    chosen_track = music_db.get_track(chosen_id)
    license_str = (chosen_track.get("license") or "").strip()
    if license_str and not license_str.upper().startswith("CC0"):
        script["_music_credit"] = {
            "title": (chosen_track.get("title") or "").strip(),
            "artist": "Kevin MacLeod",
            "license": license_str,
            "url": chosen_track.get("url") or "",
            "bpm": chosen_track.get("bpm"),
            "genre": chosen_track.get("genre"),
        }
    return path


def _weighted_choice(pool: list[tuple[str, float]]) -> str:
    """Softmax-weighted random over (track_id, score)."""
    if not pool:
        raise ValueError("empty pool")
    ids = [t for t, _ in pool]
    sc = np.array([s for _, s in pool], dtype=np.float64)
    w = np.exp((sc - sc.max()) / 0.05)     # temperature 0.05 → favors top but not deterministic
    w /= w.sum()
    return random.choices(ids, weights=w.tolist(), k=1)[0]


def _ensure_downloaded(track: dict) -> str | None:
    if not track:
        return None
    if track.get("path"):
        p = Path(track["path"])
        if p.exists() and p.stat().st_size > 10_000:
            return str(p)

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", track["filename"])
    dest = _CACHE_DIR / f"{track['track_id'].replace(':','_')}_{safe}"
    try:
        r = requests.get(track["url"], timeout=90, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        music_db.set_track_path(track["track_id"], str(dest))
        logger.info(f"music: downloaded {dest.name} ({dest.stat().st_size//1024}KB)")
        return str(dest)
    except Exception as e:
        logger.warning(f"music: download failed {track.get('title')}: {e}")
        return None


# ── Claude mood query ──────────────────────────────────────────────────────

# Fallback used only when Claude is unavailable. Dark/cinematic/somber ONLY --
# never bright, pop, upbeat, or happy.
_FORMAT_HINT = {
    "format1":  "dark aggressive cinematic instrumental, hustle motivation, driving, no vocals",
    "format3":  "dark confident cinematic instrumental, luxury finance, tense and powerful, no vocals",
    "format2":  "dark cinematic instrumental, tense motivational build, no vocals",
    "format4":  "dark calm lofi instrumental, somber advice mood, no vocals",
    "format5":  "dark emotional cinematic storytelling instrumental, somber build, no vocals",
}


def generate_mood_query(script: dict) -> str:
    """Ask Claude for a short mood description; fall back to a format heuristic."""
    fmt = script.get("format", "")
    fallback = _FORMAT_HINT.get(fmt, "confident motivational instrumental, no vocals")
    try:
        from modules.claude_cli import call_claude
        hook = script.get("hook", "")
        body = " ".join(script.get("body", [])[:2])
        prompt = (
            "You pick background music for a vertical short-form finance/motivation video.\n"
            f"Video format: {fmt}\nHook: {hook}\nBody: {body}\n\n"
            "Output ONE short music mood description for matching an instrumental "
            "royalty-free track. Include vibe, genre, approx BPM, and 'no vocals'. "
            "HARD CONSTRAINT: dark, cinematic, tense or somber/sad ONLY. "
            "NEVER bright, pop, upbeat, cheerful, or happy -- those are banned directions. "
            "Example: 'dark somber cinematic instrumental, finance motivation, ~90 BPM, no vocals'. "
            "Output only the description, nothing else."
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        text = raw.strip().strip('"')
        return text[:200] or fallback
    except Exception as e:
        logger.warning(f"music matcher: Claude mood query failed ({e}); using heuristic")
        return fallback
