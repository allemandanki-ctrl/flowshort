"""
Music Fetcher — royalty-free tracks for wealth/motivation content.

SOURCE: Mixkit (mixkit.co) — free, no API key, proprietary "free for commercial
use, no attribution required" license, wealth/cinematic vibes.

Jamendo was dropped as a fallback here: its catalog mixes in CC-BY-SA tracks,
and ShareAlike's "adaptations must be republished under the same license"
clause is an unresolved grey area for music synced to video. Mixkit alone
already covers this mood pack without that ambiguity.

Usage:
    from modules.music.music_fetcher import download_mood_pack, list_available
    download_mood_pack()                   # fills assets/music/
    paths = list_available("motivation")  # for assembler
"""

import logging
import re
import time
from pathlib import Path

import requests

from config import ASSETS_DIR

logger = logging.getLogger(__name__)

_MUSIC_DIR = ASSETS_DIR / "music"
_TIMEOUT = 30
_TRACKS_PER_MOOD = 2

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Mixkit search terms per mood — mixkit.co/free-stock-music/?q=...
_MIXKIT_QUERIES: dict[str, list[str]] = {
    "luxury_dark":   ["dark cinematic", "luxury ambient", "dark trap"],
    "motivation":    ["epic motivational", "inspiring orchestral", "powerful rise"],
    "lofi":          ["lo-fi hip hop", "chill lofi", "study beats"],
    "inspirational": ["uplifting piano", "emotional inspiring", "hope cinematic"],
}


def download_mood_pack(moods: list[str] | None = None) -> dict[str, list[str]]:
    """Download royalty-free music for each mood. Returns mood -> [paths]."""
    _MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    moods = moods or list(_MIXKIT_QUERIES.keys())
    results = {}

    for mood in moods:
        mood_dir = _MUSIC_DIR / mood
        mood_dir.mkdir(exist_ok=True)

        existing = [p for p in mood_dir.glob("*.mp3") if "tmp" not in p.name]
        if len(existing) >= _TRACKS_PER_MOOD:
            logger.info(f"{mood}: {len(existing)} tracks already cached")
            results[mood] = [str(p) for p in existing]
            continue

        paths = list(str(p) for p in existing)

        mixkit = _fetch_mixkit(mood, mood_dir, needed=_TRACKS_PER_MOOD - len(paths))
        paths.extend(mixkit)

        results[mood] = paths
        logger.info(f"{mood}: {len(paths)} tracks ready")

    total = sum(len(v) for v in results.values())
    logger.info(f"Done — {total} total tracks in {_MUSIC_DIR}")
    return results


# ── Mixkit ────────────────────────────────────────────────────────────────────

_MIXKIT_AUDIO_RE = re.compile(
    r'https://assets\.mixkit\.co/music/(?:preview|download)/\d+/[^\s"\']+\.mp3'
)
_MIXKIT_ID_RE = re.compile(r'"audio_url":"(https://assets\.mixkit\.co/music/[^"]+)"')


def _fetch_mixkit(mood: str, dest_dir: Path, needed: int = 2) -> list[str]:
    queries = _MIXKIT_QUERIES.get(mood, [mood])
    found_urls: list[str] = []

    for query in queries:
        if len(found_urls) >= needed:
            break
        urls = _mixkit_search(query)
        for u in urls:
            if u not in found_urls:
                found_urls.append(u)
        time.sleep(1)

    paths = []
    for i, url in enumerate(found_urls[:needed]):
        name = re.sub(r"[^\w\-.]", "_", url.split("/")[-1])[:60]
        dest = dest_dir / f"{mood}_{i:02d}_{name}"
        if dest.exists():
            paths.append(str(dest))
            continue
        path = _download_file(url, dest)
        if path:
            paths.append(str(path))
        time.sleep(0.5)

    return paths


def _mixkit_search(query: str) -> list[str]:
    """Scrape Mixkit music search and extract audio CDN URLs."""
    try:
        resp = requests.get(
            "https://mixkit.co/free-stock-music/",
            params={"q": query},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.text

        # Try JSON-embedded URLs first (more reliable)
        urls = _MIXKIT_ID_RE.findall(text)
        if not urls:
            # Fallback: generic CDN pattern scan
            urls = _MIXKIT_AUDIO_RE.findall(text)

        urls = list(dict.fromkeys(urls))  # deduplicate, preserve order
        logger.info(f"Mixkit '{query}' -> {len(urls)} audio URLs")
        return urls

    except Exception as e:
        logger.warning(f"Mixkit search failed for '{query}': {e}")
        return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _download_file(url: str, dest: Path) -> Path | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=60, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_kb = dest.stat().st_size // 1024
        logger.info(f"  Downloaded {dest.name} ({size_kb}KB)")
        return dest
    except Exception as e:
        logger.warning(f"  Download failed {url[:80]}: {e}")
        return None


def list_available(mood: str | None = None) -> list[str]:
    """Return paths of all downloaded music files."""
    if mood:
        d = _MUSIC_DIR / mood
        return [str(p) for p in d.glob("*.mp3")] if d.exists() else []
    return [str(p) for p in _MUSIC_DIR.rglob("*.mp3")]
