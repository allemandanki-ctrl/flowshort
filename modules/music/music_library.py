"""
Music library — royalty-free tracks tagged by mood, plus smart selection.

Why embedded (not platform library):
  Social platforms do NOT swap your audio for their library track. They either
  keep embedded audio or MUTE it on copyright hits. Royalty-free music embedded
  at generation plays on ALL platforms (TikTok/IG/YouTube/Facebook), never muted.

Moods (folders under assets/music/):
  motivation  — epic / powerful / uplifting  (format3 hype, general)
  luxury_dark — dark cinematic / tension      (format3 luxury aesthetic)
  trap_beat   — aggressive / energetic        (format1 clip edits, gym energy)
  lofi        — calm / background

Selection picks a mood by video format + script tone, then a track (recent-repeat
avoidance). Drop your own .mp3 into any mood folder anytime — it gets picked up.
"""

import logging
import random
import shutil
import urllib.parse
from collections import deque
from pathlib import Path

import requests

from config import ASSETS_DIR

logger = logging.getLogger(__name__)

_MUSIC_DIR = ASSETS_DIR / "music"
_TIMEOUT = 60
_UA = {"User-Agent": "Mozilla/5.0"}

# Kevin MacLeod / incompetech — CC-BY 4.0, direct download, reliable.
_INCOMPETECH = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"

# mood -> list of incompetech track titles
_CATALOG: dict[str, list[str]] = {
    "motivation": [
        "Deliberate Thought", "Volatile Reaction", "Impact Prelude",
        "Inspired", "Enchanted Journey", "Exhilarate",
    ],
    "luxury_dark": [
        "Crypto", "Darkest Child", "Ossuary 6 - Air", "Ossuary 5 - Rest",
        "Long Note Two", "The Descent", "Anguish",
    ],
    "trap_beat": [
        "Industrial Music Box", "Killers", "Hitman", "Machinations", "Static Motion",
    ],
    "lofi": [
        "Fluffing a Duck",
    ],
    # MindFile brand (mf5, see users/mindfile/) -- a small, own curated pool,
    # same reliable CC-BY-4.0 incompetech source as every other mood above, just
    # picked for a quiet, unresolved-tension feel rather than luxury_dark's more
    # overtly dramatic/villainous cinematic tracks.
    "tension": [
        "Stay the Course", "Symmetry", "I Can Feel it Coming", "Man Down", "Half Mystery",
        "Long Note One",
    ],
}

# format -> ordered mood preference
_FORMAT_MOODS: dict[str, list[str]] = {
    "format1":  ["trap_beat", "motivation"],   # aggressive clip edits
    "format3":  ["luxury_dark", "motivation"], # luxury slideshow
    "format2":  ["motivation", "lofi"],
    "format4":  ["lofi", "motivation"],
    "format5":  ["motivation", "luxury_dark"],
    # MindFile brand -- mf1/mf3 already read fine off the generic "motivation"/
    # "luxury_dark" fallback per explicit user approval ("pour le reste on est
    # bon"); mf5/mf6 both want a quiet, unresolved-tension bed.
    # (mf4 was cut entirely 2026-07-28, see config.py.)
    "mf5":      ["tension"],
    "mf6":      ["tension"],
}

# script music_suggestion.type -> mood override (when present)
_TYPE_MOODS: dict[str, str] = {
    "phonk": "trap_beat",
    "funk": "trap_beat",
    "cinematic": "luxury_dark",
    "trending": "motivation",
}

_RECENT: deque[str] = deque(maxlen=15)


# ── Building the library ──────────────────────────────────────────────────

def download_library(seed_desktop: bool = True) -> dict[str, int]:
    """
    Populate assets/music/<mood>/ with royalty-free tracks. Idempotent.
    Returns {mood: file_count}.
    """
    counts: dict[str, int] = {}
    for mood, titles in _CATALOG.items():
        folder = _MUSIC_DIR / mood
        folder.mkdir(parents=True, exist_ok=True)
        for title in titles:
            dest = folder / f"{_slug(title)}.mp3"
            if dest.exists() and dest.stat().st_size > 10_000:
                continue
            url = _INCOMPETECH + urllib.parse.quote(title) + ".mp3"
            try:
                r = requests.get(url, timeout=_TIMEOUT, headers=_UA, stream=True)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                logger.info(f"music: downloaded {mood}/{dest.name} ({dest.stat().st_size//1024}KB)")
            except Exception as e:
                logger.warning(f"music: failed {title}: {e}")

    if seed_desktop:
        _seed_from_desktop()

    for mood in _CATALOG:
        counts[mood] = len(list((_MUSIC_DIR / mood).glob("*.mp3")))
    return counts


def _seed_from_desktop() -> None:
    """Copy user's Desktop MUSIQUE tracks into trap_beat (energetic seeds)."""
    src = Path.home() / "OneDrive" / "Bureau" / "MUSIQUE"
    if not src.exists():
        return
    dest = _MUSIC_DIR / "trap_beat"
    dest.mkdir(parents=True, exist_ok=True)
    for mp3 in src.glob("*.mp3"):
        target = dest / mp3.name
        if not target.exists():
            try:
                shutil.copy2(mp3, target)
                logger.info(f"music: seeded trap_beat/{mp3.name} from Desktop MUSIQUE")
            except Exception as e:
                logger.warning(f"music: seed failed {mp3.name}: {e}")


# ── Selection ──────────────────────────────────────────────────────────────

def select_music(script: dict) -> str | None:
    """
    Pick the best-fit track path for this video.
    Primary: semantic matcher (SQLite catalog + embeddings + anti-repetition).
    Fallback: mood-folder heuristic below (assets/music/<mood>/).
    """
    try:
        from modules.music.music_matcher import select_track
        path = select_track(script, video_name=script.get("subject"))
        if path:
            return path
    except Exception as e:
        logger.warning(f"music: semantic matcher unavailable ({e}); using folder fallback")

    return _select_from_folders(script)


def _select_from_folders(script: dict) -> str | None:
    """Legacy mood-folder selection (fallback when the catalog DB is empty)."""
    fmt = script.get("format", "")
    moods = list(_FORMAT_MOODS.get(fmt, ["motivation"]))

    # Refine with the script's music suggestion type, if any.
    meta = script.get("_meta") or {}
    sugg_type = (meta.get("music_suggestion") or {}).get("type", "")
    if sugg_type in _TYPE_MOODS:
        preferred = _TYPE_MOODS[sugg_type]
        moods = [preferred] + [m for m in moods if m != preferred]

    for mood in moods:
        tracks = list((_MUSIC_DIR / mood).glob("*.mp3"))
        fresh = [t for t in tracks if t.name not in _RECENT] or tracks
        if fresh:
            chosen = random.choice(fresh)
            _RECENT.append(chosen.name)
            logger.info(f"music: {fmt} -> mood '{mood}' -> {chosen.name}")
            _attach_credit(script, chosen)
            return str(chosen)

    # Fallback: any track anywhere
    allt = list(_MUSIC_DIR.glob("*/*.mp3")) + list(_MUSIC_DIR.glob("*.mp3"))
    if allt:
        chosen = random.choice(allt)
        logger.info(f"music: fallback -> {chosen.name}")
        _attach_credit(script, chosen)
        return str(chosen)

    logger.warning("music: library empty — video will be silent. Run download_library().")
    return None


def _slug(title: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in title).strip("_").lower()


_SLUG_TO_TITLE: dict[str, str] | None = None


def _known_incompetech_title(filename_stem: str) -> str | None:
    """
    Only the _CATALOG titles above have a known, verified source -- any other
    file sitting in a mood folder (e.g. manually dropped in, or seeded from
    _seed_from_desktop) has NO recorded license and must never be auto-credited
    as if it were. Match is by slug so "crypto.mp3" -> "Crypto" resolves.
    """
    global _SLUG_TO_TITLE
    if _SLUG_TO_TITLE is None:
        _SLUG_TO_TITLE = {_slug(t): t for titles in _CATALOG.values() for t in titles}
    return _SLUG_TO_TITLE.get(filename_stem)


def _attach_credit(script: dict, chosen: Path) -> None:
    title = _known_incompetech_title(chosen.stem)
    if title:
        script["_music_credit"] = {
            "title": title,
            "artist": "Kevin MacLeod",
            "license": "CC-BY-4.0 (Kevin MacLeod / incompetech.com)",
            "url": _INCOMPETECH + urllib.parse.quote(title) + ".mp3",
        }
    else:
        script.pop("_music_credit", None)
        logger.warning(
            f"music: {chosen.name} has no verified license on record -- "
            "no attribution added, and it should be checked manually before reuse."
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Downloading royalty-free music library...")
    counts = download_library()
    total = sum(counts.values())
    print(f"\nLibrary ready: {total} tracks")
    for mood, n in counts.items():
        print(f"  {mood}: {n}")
