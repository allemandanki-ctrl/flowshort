"""
Picks a track from music/dark_ambient or music/fort_beat, with anti-repeat
cooldown and least-used priority. Reads/writes the manifest.json produced by
harvest_freesound.py.
"""

import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

MUSIC_DIR = Path(__file__).parent.parent.parent / "music"
COOLDOWN_DAYS = 30


def _manifest_path(folder):
    return MUSIC_DIR / folder / "manifest.json"


def _load_manifest(folder):
    path = _manifest_path(folder)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(folder, manifest):
    _manifest_path(folder).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _is_cooled_down(rec, now):
    if not rec.get("last_used"):
        return True
    last = datetime.fromisoformat(rec["last_used"])
    return (now - last).days >= COOLDOWN_DAYS


def _bpm_ok(rec, min_bpm, max_bpm):
    if min_bpm is None and max_bpm is None:
        return True
    bpm = rec.get("bpm")
    if bpm is None:
        return False
    if min_bpm is not None and bpm < min_bpm:
        return False
    if max_bpm is not None and bpm > max_bpm:
        return False
    return True


def pick_track(folder, min_bpm=None, max_bpm=None):
    """Returns a dict with file path + metadata, or None if no track fits."""
    manifest = _load_manifest(folder)
    if not manifest:
        return None

    now = datetime.now(timezone.utc)
    candidates = [(fid, rec) for fid, rec in manifest.items() if _bpm_ok(rec, min_bpm, max_bpm)]
    if not candidates:
        return None

    cooled = [c for c in candidates if _is_cooled_down(c[1], now)]
    pool = cooled or candidates  # everyone's on cooldown: pick anyway rather than fail

    pool.sort(key=lambda c: (c[1].get("times_used", 0), c[1].get("last_used") or ""))
    min_uses = pool[0][1].get("times_used", 0)
    tied = [c for c in pool if c[1].get("times_used", 0) == min_uses]
    fid, rec = random.choice(tied)

    rec["times_used"] = rec.get("times_used", 0) + 1
    rec["last_used"] = now.isoformat()
    manifest[fid] = rec
    _save_manifest(folder, manifest)

    return {
        "file": str(MUSIC_DIR / folder / rec["file"]),
        "freesound_id": fid,
        "name": rec.get("name"),
        "bpm": rec.get("bpm"),
        "license": rec.get("license"),
        "username": rec.get("username"),
        "source_url": rec.get("source_url"),
        "attribution_required": rec.get("attribution_required", False),
    }


def generate_credits():
    """CC-BY attribution lines across both folders, for video descriptions."""
    lines = set()
    for folder in ("dark_ambient", "fort_beat"):
        for rec in _load_manifest(folder).values():
            if rec.get("attribution_required"):
                title = rec.get("name") or rec["file"]
                lines.add(f'"{title}" by {rec["username"]} — {rec["source_url"]}')
    return sorted(lines)


if __name__ == "__main__":
    if "--credits" in sys.argv:
        for line in generate_credits():
            print(line)
    else:
        folder = sys.argv[1] if len(sys.argv) > 1 else "dark_ambient"
        print(pick_track(folder))
