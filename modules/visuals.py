"""
Step 3 - Visual Fetcher
Input  : script dict (uses visual_keywords + format)
Output : dict with lists of downloaded video/image paths

Sources (per format):
  format1 : 2 Pexels B-roll clips (main clip provided manually by user)
  format2 : 3-4 Pexels video clips (looping background)
  format3 : mixed pool -- Drive real footage ONLY (videos + images). No
            Pexels/Pixabay/Pollinations fallback: if the Drive can't supply
            enough of either media type, fetch_visuals raises
            DriveUnavailableError instead of substituting stock footage.
  format4 : 1-2 Pexels photos (minimal background)
  format5 : 3-4 Pexels video clips (narrative B-roll)

Pexels  : portrait/vertical preferred, fallback to landscape (FFmpeg crops in Step 4)
Pixabay : fallback when Pexels returns < 2 results
Pollinations : free, no key, returns 1080x1920 image directly
"""

import json
import logging
import random
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

from config import (
    BASE_DIR,
    OUTPUT_DIR,
    PEXELS_API_KEY,
    PIXABAY_API_KEY,
    POLLINATIONS_BASE_URL,
)

logger = logging.getLogger(__name__)


class DriveUnavailableError(RuntimeError):
    """
    Raised for format3 when real Google Drive footage/images can't be
    fetched. Explicit user instruction: report the real problem and stop,
    rather than silently substituting Pexels/Pixabay/Pollinations stock the
    user has rejected. Caller (pipeline.run / app.py job handling) surfaces
    this as a failed job with this message, not a generated-but-wrong video.
    """


_VISUALS_DIR = OUTPUT_DIR / "visuals"

# How many assets to fetch per format
_VIDEO_COUNTS = {
    "format1":  2,
    "format2":  4,
    "format3":  8,   # mixed into the beat-cut montage alongside images
    "format4":  0,
    "format5":  4,
    "format6":  6,   # dark B-roll, one per ranked item (cycled if item count > 6)
    # MindFile brand (see users/mindfile/) -- deliberately its OWN keys, never
    # remapped to format3's count_fmt below, so mf2 never pulls in ResetWorth's
    # Drive-exclusive sourcing (see _is_format3 below) -- mf2 still uses Pexels.
    "mf1":      5,   # POV Reenactment -- looped real-life B-roll under continuous narration
    "mf2":      4,   # Decode -- video/image quote-card pool
    "mf3":      6,   # Which One Are You -- one clip per ranked item, cycled (same mechanic as format6)
}
_IMAGE_COUNTS = {
    "format1":  0,
    "format2":  0,
    "format3":  8,   # mixed into the beat-cut montage alongside videos
    "format4":  2,
    "format5":  0,
    "mf2":      4,   # Decode -- image fallback/mix alongside video
}
_GENERATE_COUNTS: dict[str, int] = {}

_PEXELS_HEADERS  = {"Authorization": PEXELS_API_KEY}
_REQUEST_TIMEOUT = 30

_HISTORY_PATH = BASE_DIR / "state" / "visual_history.json"
_HISTORY_KEEP = 200  # remember last N Pexels IDs per asset type


def _load_history() -> dict:
    if not _HISTORY_PATH.exists():
        return {"video_ids": [], "photo_ids": []}
    try:
        return json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"video_ids": [], "photo_ids": []}


def _save_history(h: dict) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_PATH.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_visuals(script: dict) -> dict:
    """
    Fetches and downloads all visuals for a script.

    Returns:
    {
      "videos": [str path, ...],
      "images": [str path, ...],
      "format": str,
    }
    """
    fmt = script["format"]
    keywords = script.get("visual_keywords", [])[:4]
    if not keywords:
        raise ValueError("Script has no visual_keywords")

    # format3a/format3b are accepted aliases (old persisted scripts/retries)
    # but count lookups always use the merged "format3" entry.
    _is_format3 = fmt in ("format3", "format3a", "format3b")
    count_fmt = "format3" if _is_format3 else fmt

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = _VISUALS_DIR / f"{ts}_{fmt}"
    session_dir.mkdir(parents=True, exist_ok=True)

    n_videos = _VIDEO_COUNTS.get(count_fmt, 0)
    n_images = _IMAGE_COUNTS.get(count_fmt, 0)
    n_generated = _GENERATE_COUNTS.get(count_fmt, 0)

    videos = []
    images = []
    drive_usage_ids: list[str] = []   # committed post-render in pipeline.py

    if _is_format3:
        # format3 is Drive-EXCLUSIVE for its whole pool (video + image) --
        # explicit user instruction: "je préfère que tu me dises qu'il y a un
        # problème avec le drive... que tu me mentes et me donnes ces clips
        # de merde." No Pexels/Pixabay/Pollinations fallback whatsoever: if
        # the Drive can't supply enough of either media type, this run fails
        # loudly (DriveUnavailableError) instead of silently degrading to
        # stock footage the user has explicitly rejected.
        from modules.drive_clips import DriveAuthError, build_visuals as _drive_build, load_folders as _drive_folders

        if not _drive_folders():
            raise DriveUnavailableError(
                "Format 3: pas assez de clips disponibles dans le Drive -- aucun "
                "dossier Drive configuré (python -m modules.drive_clips --ingest-pdf <pdf>).")

        # target_seconds=90 (not the ~20s final video length): the beat-cut
        # montage cuts on EVERY beat (~0.5s/cut at typical phonk tempo), so a
        # 16s content window needs ~30 distinct clips. group_clips only keeps
        # clips until their SUMMED native duration reaches target_seconds, so
        # this needs to be generous enough that most of the ~36 staged
        # candidates (see drive_clips._MAX_CANDIDATES) actually make it in.
        try:
            videos, ids = _drive_build(session_dir, target_seconds=90.0)
        except DriveAuthError as e:
            raise DriveUnavailableError(str(e)) from e
        except Exception as e:
            raise DriveUnavailableError(f"Format 3: échec de récupération des clips vidéo Drive: {e}") from e
        drive_usage_ids.extend(ids)
        logger.info(f"Drive clips group: {len(videos)} real clips")

        # The assembler mixes videos+images into one shuffled pool and only
        # needs that combined pool non-empty (modules/assembler/format3.py) --
        # it does NOT need both media types present. So a shortfall of one
        # type alone is not fatal here; only an empty combined pool is.
        try:
            images, img_ids = _drive_build(session_dir, want_images=True)
        except DriveAuthError as e:
            raise DriveUnavailableError(str(e)) from e
        except Exception as e:
            logger.warning(f"Format 3: échec de récupération des images Drive (on continue avec les vidéos seules): {e}")
            images, img_ids = [], []
        images = images[:n_images]
        drive_usage_ids.extend(img_ids[:len(images)])
        logger.info(f"Drive images: {len(images)} real images")

        if not videos and not images:
            raise DriveUnavailableError(
                "Format 3: pas assez de clips vidéo ni de photos disponibles dans "
                "le Drive (problème réseau/permissions, ou dossiers épuisés) -- pas "
                "de repli vers Pexels/Pollinations pour ce format.")

    else:
        if n_videos > 0:
            videos = _fetch_pexels_videos(keywords, n_videos, session_dir)
            if len(videos) < 2 and PIXABAY_API_KEY:
                extra = _fetch_pixabay_videos(keywords, n_videos - len(videos), session_dir)
                videos.extend(extra)
            logger.info(f"Videos fetched: {len(videos)}")

        if n_images > 0:
            images = _fetch_pexels_photos(keywords, n_images, session_dir)
            if len(images) < 1 and PIXABAY_API_KEY:
                extra = _fetch_pixabay_photos(keywords, n_images - len(images), session_dir)
                images.extend(extra)
            logger.info(f"Photos fetched: {len(images)}")

        if n_generated > 0:
            style_prompt = _build_pollinations_prompt(keywords, count_fmt)
            for i in range(n_generated):
                path = _generate_pollinations_image(style_prompt, session_dir, i)
                if path:
                    images.append(str(path))
            logger.info(f"Generated images: {n_generated}")

    return {
        "videos": videos,
        "images": images,
        "format": fmt,
        "session_dir": str(session_dir),
        "drive_usage_ids": drive_usage_ids,
    }


# ── Pexels ────────────────────────────────────────────────────────────────────

def _fetch_pexels_videos(keywords: list[str], count: int, dest: Path) -> list[str]:
    history    = _load_history()
    used_ids   = set(history["video_ids"])
    newly_used: list[int] = []
    paths: list[str] = []

    for keyword in keywords:
        if len(paths) >= count:
            break
        results = _pexels_video_search(keyword, per_page=10)
        random.shuffle(results)
        for video in results:
            if len(paths) >= count:
                break
            vid_id = video.get("id")
            if vid_id and vid_id in used_ids:
                logger.info(f"Skipping Pexels video {vid_id} (already used)")
                continue
            url = _best_pexels_video_url(video)
            if not url:
                continue
            path = _download(url, dest, prefix="vid", ext=".mp4")
            if path:
                paths.append(str(path))
                if vid_id:
                    used_ids.add(vid_id)
                    newly_used.append(vid_id)

    if newly_used:
        history["video_ids"] = (history["video_ids"] + newly_used)[-_HISTORY_KEEP:]
        _save_history(history)

    return paths


def _pexels_video_search(query: str, per_page: int = 10) -> list[dict]:
    page = random.randint(1, 5)
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=_PEXELS_HEADERS,
            params={"query": query, "per_page": per_page, "orientation": "portrait", "page": page},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        videos = resp.json().get("videos", [])
        if not videos:
            resp2 = requests.get(
                "https://api.pexels.com/videos/search",
                headers=_PEXELS_HEADERS,
                params={"query": query, "per_page": per_page, "page": page},
                timeout=_REQUEST_TIMEOUT,
            )
            resp2.raise_for_status()
            videos = resp2.json().get("videos", [])
        logger.info(f"Pexels video search '{query}' page={page} -> {len(videos)} results")
        return videos
    except Exception as e:
        logger.warning(f"Pexels video search failed for '{query}': {e}")
        return []


def _best_pexels_video_url(video: dict) -> str | None:
    """Pick the smallest HD file (to limit download size)."""
    files = video.get("video_files", [])
    # prefer hd quality, portrait if available
    for quality in ("hd", "sd"):
        for f in files:
            if f.get("quality") == quality:
                return f["link"]
    if files:
        return files[0]["link"]
    return None


def _fetch_pexels_photos(keywords: list[str], count: int, dest: Path) -> list[str]:
    history    = _load_history()
    used_ids   = set(history["photo_ids"])
    newly_used: list[int] = []
    paths: list[str] = []

    for keyword in keywords:
        if len(paths) >= count:
            break
        results = _pexels_photo_search(keyword, per_page=20)
        random.shuffle(results)
        for photo in results:
            if len(paths) >= count:
                break
            photo_id = photo.get("id")
            if photo_id and photo_id in used_ids:
                logger.info(f"Skipping Pexels photo {photo_id} (already used)")
                continue
            url = photo.get("src", {}).get("large2x") or photo.get("src", {}).get("large")
            if not url:
                continue
            path = _download(url, dest, prefix="img", ext=".jpg")
            if path:
                paths.append(str(path))
                if photo_id:
                    used_ids.add(photo_id)
                    newly_used.append(photo_id)

    if newly_used:
        history["photo_ids"] = (history["photo_ids"] + newly_used)[-_HISTORY_KEEP:]
        _save_history(history)

    return paths


def _pexels_photo_search(query: str, per_page: int = 20) -> list[dict]:
    page = random.randint(1, 8)
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers=_PEXELS_HEADERS,
            params={"query": query, "per_page": per_page, "orientation": "portrait", "page": page},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        logger.info(f"Pexels photo search '{query}' page={page} -> {len(photos)} results")
        return photos
    except Exception as e:
        logger.warning(f"Pexels photo search failed for '{query}': {e}")
        return []


# ── Pixabay (fallback) ────────────────────────────────────────────────────────

def _fetch_pixabay_videos(keywords: list[str], count: int, dest: Path) -> list[str]:
    paths = []
    for keyword in keywords:
        if len(paths) >= count:
            break
        try:
            resp = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": PIXABAY_API_KEY, "q": keyword, "per_page": 5},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            logger.info(f"Pixabay video '{keyword}' -> {len(hits)} results")
            for hit in hits:
                if len(paths) >= count:
                    break
                url = (
                    hit.get("videos", {}).get("medium", {}).get("url")
                    or hit.get("videos", {}).get("small", {}).get("url")
                )
                if url:
                    path = _download(url, dest, prefix="pbv", ext=".mp4")
                    if path:
                        paths.append(str(path))
        except Exception as e:
            logger.warning(f"Pixabay video failed for '{keyword}': {e}")
    return paths


def _fetch_pixabay_photos(keywords: list[str], count: int, dest: Path) -> list[str]:
    paths = []
    for keyword in keywords:
        if len(paths) >= count:
            break
        try:
            resp = requests.get(
                "https://pixabay.com/api/",
                params={
                    "key": PIXABAY_API_KEY,
                    "q": keyword,
                    "per_page": 5,
                    "image_type": "photo",
                    "orientation": "vertical",
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            logger.info(f"Pixabay photo '{keyword}' -> {len(hits)} results")
            for hit in hits:
                if len(paths) >= count:
                    break
                url = hit.get("largeImageURL")
                if url:
                    path = _download(url, dest, prefix="pbi", ext=".jpg")
                    if path:
                        paths.append(str(path))
        except Exception as e:
            logger.warning(f"Pixabay photo failed for '{keyword}': {e}")
    return paths


# ── Pollinations (free generation) ───────────────────────────────────────────

def _build_pollinations_prompt(keywords: list[str], fmt: str) -> str:
    base = ", ".join(keywords[:3])
    style_suffix = {
        "format3":  "luxury lifestyle, cinematic lighting, dark moody aesthetic, 4k sharp",
        "format4":  "minimal dark background, clean professional, subtle texture",
    }.get(fmt, "cinematic, high quality")
    return f"{base}, {style_suffix}"


def _generate_pollinations_image(prompt: str, dest: Path, index: int) -> Path | None:
    encoded = urllib.parse.quote(prompt)
    seed    = random.randint(1, 99999)
    url = f"{POLLINATIONS_BASE_URL}/{encoded}?width=1080&height=1920&nologo=true&seed={seed}"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        path = dest / f"gen_{index:02d}.jpg"
        path.write_bytes(resp.content)
        logger.info(f"Pollinations image generated -> {path} ({len(resp.content)//1024}KB)")
        return path
    except Exception as e:
        logger.warning(f"Pollinations generation failed: {e}")
        return None


# ── Download helper ───────────────────────────────────────────────────────────

_download_counter = 0

def _download(url: str, dest: Path, prefix: str, ext: str) -> Path | None:
    global _download_counter
    _download_counter += 1
    path = dest / f"{prefix}_{_download_counter:03d}{ext}"
    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_kb = path.stat().st_size // 1024
        logger.info(f"Downloaded {path.name} ({size_kb}KB)")
        return path
    except Exception as e:
        logger.warning(f"Download failed {url[:60]}...: {e}")
        return None
