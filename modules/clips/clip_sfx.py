"""
Ambient scene SFX for format1 B-roll cutaways -- Freesound.org, token-based
auth (search + preview download both work without full OAuth2, only needed
for original-quality downloads which we don't need for a low-volume
background bed under speech).

Reference technique: "adding background audio... using a bunch of audios
which match the scenery... when there's waves we'll add an audio of a
wave... when there's butterflies we can add nature sounds... it just takes
it to another level."

Requires FREESOUND_CLIENT_SECRET in .env (apply at freesound.org/apiv2/apply
-- the "Client secret" from that form is the token used here, confirmed
against the live API; the "Client id" is NOT the token and returns
"Invalid token" if used as one).
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

_SEARCH_URL   = "https://freesound.org/apiv2/search/text/"
_MAX_DURATION = 40.0     # skip long-form soundscapes, keep downloads small
_MIN_DURATION = 2.0      # skip one-shot stings, we need a loopable bed
_TIMEOUT      = 15
_UA           = {"User-Agent": "Mozilla/5.0"}


def fetch_ambient_sfx(keyword: str, dest_path) -> "Path | None":
    """
    Search Freesound for `keyword`, download the best-fit preview MP3 to
    dest_path. Picks the shortest clip within [_MIN_DURATION, _MAX_DURATION]
    that still meets the floor -- short loopable textures mix in more
    cleanly under speech than a long field recording. Returns None on any
    failure (missing key, no results, network) -- SFX is a polish layer,
    never worth blocking the video over.
    """
    from pathlib import Path
    from config import FREESOUND_CLIENT_SECRET

    if not FREESOUND_CLIENT_SECRET:
        return None

    try:
        resp = requests.get(
            _SEARCH_URL,
            headers=_UA,
            params={
                "query": keyword,
                "token": FREESOUND_CLIENT_SECRET,
                "fields": "id,name,previews,duration,license",
                "filter": f"duration:[{_MIN_DURATION} TO {_MAX_DURATION}]",
                "sort": "score",
                "page_size": 10,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            logger.info(f"clip_sfx: no results for '{keyword}'")
            return None

        best = min(results, key=lambda r: r.get("duration", 999))
        url = best.get("previews", {}).get("preview-hq-mp3") or best.get("previews", {}).get("preview-lq-mp3")
        if not url:
            return None

        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(url, headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        dest_path.write_bytes(r.content)
        logger.info(f"clip_sfx: '{keyword}' -> {best['name']} ({best['duration']:.1f}s)")
        return dest_path
    except Exception as e:
        logger.warning(f"clip_sfx: fetch failed for '{keyword}' ({e})")
        return None
