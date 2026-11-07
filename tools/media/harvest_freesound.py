"""
Fills music/dark_ambient/ and music/fort_beat/ from the Freesound APIv2.

Auth: FREESOUND_API_KEY env var, sent as "Authorization: Token <KEY>".
Only "Creative Commons 0" and "Attribution" licenses are kept (commercial-safe
for monetized videos). Downloads previews (preview-hq-mp3), never the OAuth2
/download/ endpoint, so the whole thing runs unattended.

Usage:
    export FREESOUND_API_KEY=xxxx
    python tools/media/harvest_freesound.py
    python tools/media/harvest_freesound.py --only dark_ambient --per-folder 50
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import librosa

    HAVE_LIBROSA = True
except ImportError:
    HAVE_LIBROSA = False

SEARCH_URL = "https://freesound.org/apiv2/search/text/"
FIELDS = "id,name,tags,duration,previews,license,username,url,analysis"
PAGE_SIZE = 150
MAX_DURATION = 200

MUSIC_DIR = Path(__file__).parent.parent.parent / "music"

LICENSE_CLAUSE = 'license:"Creative Commons 0" OR license:"Attribution"'

TARGETS = {"dark_ambient": 200, "fort_beat": 100}

QUERIES = {
    "dark_ambient": [
        "dark ambient",
        "dark drone",
        "horror ambient",
        "tension underscore",
        "ominous",
        "dark cinematic",
        "suspense",
        "eerie atmosphere",
        "dark soundscape",
        "sinister",
    ],
    "fort_beat": [
        "phonk",
        "dark phonk",
        "drift phonk",
        "trap beat aggressive",
        "hard trap",
        "808 dark",
        "aggressive electronic",
        "dark trap instrumental",
    ],
}

BLACKLIST_WORDS = [
    "happy", "fun", "cheerful", "uplifting", "positive", "hopeful", "cute",
    "love", "romantic", "wedding", "kids", "baby", "lullaby", "sleep",
    "relax", "calm", "peaceful", "meditation", "spa", "chill", "lofi",
    "jazz", "funk", "disco", "tropical", "summer", "beach", "comedy",
    "quirky", "playful",
]

DARK_AMBIENT_WHITELIST = [
    "dark", "tension", "suspense", "thriller", "horror", "ominous",
    "sinister", "eerie", "haunting", "gloomy", "drone", "atmospheric",
    "cinematic", "dramatic", "doom", "night", "cold", "menacing",
]


def _headers():
    key = os.environ.get("FREESOUND_API_KEY")
    if not key:
        sys.exit("FREESOUND_API_KEY not set in environment.")
    return {"Authorization": f"Token {key}"}


def _api_get(url, headers, params=None, max_retries=8):
    delay = 1
    for _ in range(max_retries):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", delay))
            print(f"  rate limited (429), waiting {wait}s...")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 500:
            print(f"  server error {resp.status_code}, retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def _download(url, headers, dest, max_retries=8):
    delay = 1
    for _ in range(max_retries):
        resp = requests.get(url, headers=headers, timeout=60, stream=True)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", delay))
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 500:
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code != 200:
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        tmp.rename(dest)
        return True
    return False


def license_ok(license_str):
    l = (license_str or "").lower()
    if "nc" in l and ("by-nc" in l or "noncommercial" in l):
        return False
    return "zero" in l or "/by/" in l


def attribution_required(license_str):
    return "/by/" in (license_str or "").lower()


def compute_bpm(path):
    if not HAVE_LIBROSA:
        return None
    try:
        y, sr = librosa.load(str(path), sr=None, mono=True, duration=60)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        return round(float(tempo), 1)
    except Exception:
        return None


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s[:60] or "track"


def load_manifest(folder_path):
    path = folder_path / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_manifest(folder_path, manifest):
    path = folder_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def passes_vibe_filter(folder, name, tags):
    blob = (name + " " + " ".join(tags)).lower()
    if any(bad in blob for bad in BLACKLIST_WORDS):
        return False
    if folder == "dark_ambient" and not any(w in blob for w in DARK_AMBIENT_WHITELIST):
        return False
    return True


def harvest_folder(folder, target, min_duration, headers):
    folder_path = MUSIC_DIR / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(folder_path)

    seen_ids = {fid for fid, rec in manifest.items() if (folder_path / rec["file"]).exists()}
    count = len(seen_ids)
    print(f"\n=== {folder}: {count}/{target} already on disk ===")

    filter_str = f'({LICENSE_CLAUSE}) duration:[{min_duration} TO {MAX_DURATION}]'

    for query in QUERIES[folder]:
        if count >= target:
            break
        print(f"-- query: '{query}'")
        url = SEARCH_URL
        params = {
            "query": query,
            "filter": filter_str,
            "fields": FIELDS,
            "page_size": PAGE_SIZE,
            "sort": "downloads_desc",
        }
        while url and count < target:
            resp = _api_get(url, headers, params=params)
            params = None  # "next" already carries the full query string
            data = resp.json()

            for result in data.get("results", []):
                if count >= target:
                    break
                fid = str(result["id"])
                if fid in seen_ids:
                    continue

                name = result.get("name", "")
                tags = result.get("tags", []) or []
                if not passes_vibe_filter(folder, name, tags):
                    continue

                license_str = result.get("license", "")
                if not license_ok(license_str):
                    continue

                duration = result.get("duration")
                if duration is None or not (min_duration <= duration <= MAX_DURATION):
                    continue

                preview_url = (result.get("previews") or {}).get("preview-hq-mp3")
                if not preview_url:
                    continue

                dest = folder_path / f"{fid}_{slugify(name)}.mp3"
                if not _download(preview_url, headers, dest):
                    print(f"  ! download failed: {name} ({fid})")
                    continue

                bpm = compute_bpm(dest)
                manifest[fid] = {
                    "file": dest.name,
                    "freesound_id": fid,
                    "name": name,
                    "source_url": result.get("url"),
                    "license": license_str,
                    "username": result.get("username"),
                    "attribution_required": attribution_required(license_str),
                    "duration_sec": duration,
                    "bpm": bpm,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                    "times_used": 0,
                    "last_used": None,
                }
                seen_ids.add(fid)
                count += 1
                save_manifest(folder_path, manifest)
                print(f"  [{count}/{target}] {name}  bpm={bpm}")

            url = data.get("next")

    print(f"=== {folder}: done, {count}/{target} tracks ===")
    return count


def main():
    parser = argparse.ArgumentParser(description="Fill FlowShort music banks from Freesound.")
    parser.add_argument("--per-folder", type=int, default=None, help="Override target count for the selected folder(s).")
    parser.add_argument("--only", choices=["dark_ambient", "fort_beat"], default=None, help="Only harvest this folder.")
    parser.add_argument("--min-duration", type=int, default=20, help="Minimum track duration in seconds (default 20).")
    args = parser.parse_args()

    headers = _headers()
    folders = [args.only] if args.only else list(TARGETS.keys())

    summary = {}
    for folder in folders:
        target = args.per_folder or TARGETS[folder]
        summary[folder] = harvest_folder(folder, target, args.min_duration, headers)

    print("\n=== SUMMARY ===")
    for folder, count in summary.items():
        print(f"{folder}: {count} tracks")


if __name__ == "__main__":
    main()
