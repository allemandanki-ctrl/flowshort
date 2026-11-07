"""
Real hook ingest from transitionalhooks.com -- replaces the synthetic
placeholder generator with actual downloaded hook clips.

Source: WordPress REST API (https://transitionalhooks.com/wp-json/wp/v2/media),
443+ clips as of this writing, each with a direct MP4 source_url, real duration
(media_details.length, seconds), and 1080x1920 (already vertical, matches this
pipeline's target resolution exactly -- no crop/scale surprises).

Licensing note (terms-and-conditions page, read before building this):
  "All Content provided on the Website is for personal, non-commercial use only
  unless expressly stated otherwise... Content may be protected by copyright...
  belonging to third parties." This is a trend-aggregation site, not a stock
  library with cleared commercial licenses. Flagged here for the record; the
  user directed this exact source, so ingest proceeds, but this is NOT the same
  license posture as the CC-BY music catalog.

Writes real sidecar JSON manifests into assets/hooks/ in the SAME schema
hooks.py already expects (id, filename, duration, cut_time, ambiance,
hook_type, source) -- so hooks.list_hooks() picks these up with zero changes
downstream.

Run:
  python -m modules.hook_source_transitionalhooks --limit 80
  python -m modules.hook_source_transitionalhooks --limit 80 --offset 80   # next batch
"""

import json
import logging
import re
import time

import requests

from config import CLAUDE_MODEL, HOOKS_DIR
from modules.hooks import _AMBIANCE_VOCAB

logger = logging.getLogger(__name__)

_API_BASE = "https://transitionalhooks.com/wp-json/wp/v2/media"
_UA = {"User-Agent": "Mozilla/5.0"}
_PER_PAGE = 100
_TIMEOUT = 60


def fetch_catalog(max_items: int | None = None) -> list[dict]:
    """Paginate the full WP REST media catalog (video only). Cheap -- JSON only."""
    items: list[dict] = []
    page = 1
    while True:
        r = requests.get(_API_BASE, params={"per_page": _PER_PAGE, "page": page,
                                            "media_type": "video"},
                         headers=_UA, timeout=_TIMEOUT)
        if r.status_code == 400:   # past last page
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        logger.info(f"hook_source: fetched page {page} ({len(batch)} items, {len(items)} total)")
        if max_items and len(items) >= max_items:
            items = items[:max_items]
            break
        page += 1
        if page > 20:   # hard safety cap (would be 2000 items)
            break
    return items


def _slug_id(title: str) -> str:
    return "th_" + re.sub(r"[^\w-]", "_", title.strip().lower())[:40]


def _classify_batch(titles: list[str]) -> dict[str, str]:
    """One Claude call classifies many hook titles at once into _AMBIANCE_VOCAB."""
    if not titles:
        return {}
    try:
        from modules.claude_cli import call_claude
        listing = "\n".join(f"{i}: {t}" for i, t in enumerate(titles))
        prompt = (
            "Classify each short-form video hook title below into EXACTLY ONE tag "
            f"from this list: {', '.join(_AMBIANCE_VOCAB)}.\n"
            "Titles describe a short action/reaction clip (e.g. a stunt, a fall, "
            "an emotional moment) used as a scroll-stopping intro.\n\n"
            f"{listing}\n\n"
            'Answer with JSON only: {"0": "tag", "1": "tag", ...} using the index numbers above.'
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        raw = raw.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        idx_map = json.loads(m.group(0)) if m else {}
        return {titles[int(i)]: tag for i, tag in idx_map.items()
                if tag in _AMBIANCE_VOCAB and int(i) < len(titles)}
    except Exception as e:
        logger.warning(f"hook_source: batch ambiance classification failed ({e}); using fallback tag")
        return {}


def ingest(limit: int = 80, offset: int = 0) -> int:
    """
    Download `limit` real hook clips (starting after `offset` already-ingested
    ones) into assets/hooks/ with sidecar manifests. Idempotent: skips a title
    whose manifest already exists.
    """
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    catalog = fetch_catalog(max_items=offset + limit)
    batch = catalog[offset: offset + limit]
    if not batch:
        logger.warning("hook_source: nothing to ingest (offset past catalog end?)")
        return 0

    titles = [it["title"]["rendered"] for it in batch]
    ambiance_map = _classify_batch(titles)

    added = 0
    for it in batch:
        title = it["title"]["rendered"]
        hook_id = _slug_id(title)
        manifest_path = HOOKS_DIR / f"{hook_id}.json"
        video_path = HOOKS_DIR / f"{hook_id}.mp4"

        if manifest_path.exists() and video_path.exists() and video_path.stat().st_size > 10_000:
            continue   # already ingested

        url = it["source_url"]
        try:
            r = requests.get(url, headers=_UA, timeout=_TIMEOUT, stream=True)
            r.raise_for_status()
            with open(video_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        except Exception as e:
            logger.warning(f"hook_source: download failed for {title!r}: {e}")
            continue

        # The API's media_details.length is rounded to whole seconds (e.g. a real
        # 3.55s clip is reported as "4"). Frame-accurate hook->transition sync
        # needs the REAL duration, so always ffprobe the downloaded file --
        # the API value is never used, not even as a fallback.
        from modules.transitions import probe_duration
        duration = probe_duration(str(video_path))

        manifest = {
            "id": hook_id,
            "filename": video_path.name,
            "duration": round(duration, 2),
            "cut_time": round(duration, 2),   # no finer cut-point data from the source
            "ambiance": ambiance_map.get(title, "mysterious"),
            "hook_type": "reaction_clip",
            "source": "transitionalhooks.com",
            "title": title,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        added += 1
        logger.info(f"hook_source: ingested {title!r} -> {hook_id} "
                    f"({duration:.1f}s, {manifest['ambiance']})")
        time.sleep(0.2)   # light courtesy delay between downloads

    logger.info(f"hook_source: {added} new hooks ingested (offset={offset}, limit={limit})")
    return added


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--offset", type=int, default=0)
    args = ap.parse_args()
    n = ingest(limit=args.limit, offset=args.offset)
    print(f"\n{n} real hooks ingested into {HOOKS_DIR}")
