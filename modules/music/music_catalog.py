"""
Music catalog ingest — pull the Incompetech royalty-free catalog (1442 tracks,
CC-BY 4.0) into the SQLite DB with mood metadata. No audio downloaded here;
MP3s are fetched just-in-time on selection.

Run:  python -m modules.music.music_catalog
"""

import logging
import urllib.parse

import requests

from modules.music import music_db

logger = logging.getLogger(__name__)

_PIECES_JSON = "https://incompetech.com/music/royalty-free/pieces.json"
_MP3_BASE    = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
_LICENSE     = "CC-BY-4.0 (Kevin MacLeod / incompetech.com)"
_SOURCE      = "incompetech"

# Keep tracks whose 'feel' matches dark/cinematic/somber wealth-motivation content.
# No "bright"/"bouncy"/pop-leaning tags -- user wants dark cinematic, even sad,
# never upbeat/pop.
_RELEVANT = {
    "dark", "driving", "aggressive", "epic", "intense", "tense", "serious",
    "mysterious", "ominous", "suspense", "suspenseful", "uplifting", "determined",
    "powerful", "grooving", "sneaky", "dramatic", "heroic", "action", "adventure",
    "energetic", "hopeful", "inspirational", "triumphant", "bold",
    "unnerving", "eerie", "pulsing", "brooding", "somber", "mystical", "calming",
}

# Hard exclusion: a track is dropped if ANY of these appear in its feel tags,
# even alongside relevant dark tags (e.g. "Driving, Action, Intense, Bright"
# still reads as pop-leaning once heard -- exclude outranks include).
_EXCLUDE = {"bright", "bouncy", "humorous", "cheerful", "happy", "playful", "fun", "quirky", "upbeat"}


def _hms_to_seconds(s: str) -> float:
    try:
        parts = [int(x) for x in s.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return 0.0


def _is_relevant(feel: str) -> bool:
    f = (feel or "").lower()
    return any(k in f for k in _RELEVANT)


def _is_excluded(feel: str) -> bool:
    f = (feel or "").lower()
    return any(k in f for k in _EXCLUDE)


def ingest(relevant_only: bool = True) -> int:
    music_db.init_db()
    logger.info("Fetching Incompetech catalog...")
    r = requests.get(_PIECES_JSON, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    pieces = r.json()
    logger.info(f"Catalog: {len(pieces)} pieces")

    added = 0
    for p in pieces:
        feel = p.get("feel") or ""
        if relevant_only and not _is_relevant(feel):
            continue
        if _is_excluded(feel):
            continue
        filename = p.get("filename") or ""
        if not filename.lower().endswith(".mp3"):
            continue
        try:
            bpm = int(p.get("bpm") or 0)
        except (ValueError, TypeError):
            bpm = 0
        music_db.upsert_track({
            "track_id":    f"incompetech:{p.get('uuid')}",
            "source":      _SOURCE,
            "license":     _LICENSE,
            "title":       p.get("title"),
            "filename":    filename,
            "url":         _MP3_BASE + urllib.parse.quote(filename),
            "path":        None,
            "bpm":         bpm,
            "duration":    _hms_to_seconds(p.get("length") or ""),
            "genre":       str(p.get("genre") or ""),
            "feel":        feel,
            "instruments": p.get("instruments") or "",
            "description": p.get("description") or "",
            "embedding":   None,
        })
        added += 1

    logger.info(f"Ingested {added} relevant tracks into music.db")
    return added


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = ingest()
    print(f"\nTracks in DB: {music_db.count_tracks()} (relevant added this run: {n})")
