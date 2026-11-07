"""
Character image sourcing for format14 (and future clash-persona formats) --
pulls one real, publicly indexed image of the named character for the
"case file" photo shown for the whole video.

Real photos/stills carry a copyright risk on the source material itself --
the user has already weighed that and accepts it as their own business
call (same as the real clips used in format1/format3a). That is a
different question from voice cloning: an image is the copyright holder's
risk to negotiate, not a real person's identity/consent question, so it
does not fall under the same hard block as voice_bank's real-actor-timbre
rule. See BESOIN_TON_AVIS.md.
"""

import logging
import re
from io import BytesIO
from pathlib import Path

import requests
from ddgs import DDGS
from PIL import Image

logger = logging.getLogger(__name__)

_MIN_SIZE = 400
_TIMEOUT = 8
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_STOPWORDS = {"the", "and", "mr", "mrs", "dr", "of", "de", "la"}
_MAX_FALLBACK_VERIFY_TRIES = 3


def _verify_photo_is_character(image_path: Path, name: str) -> bool:
    """One quick vision check via the user's own Claude subscription --
    confirms a downloaded FALLBACK candidate (no title match) actually
    depicts the named character before accepting it. A title match alone
    can't catch this failure mode: an untitled-search fallback once
    returned a random footballer's photo for "Dwight Schrute" that ranked
    near the top purely by image-search luck, not relevance."""
    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude, ClaudeCliError
    prompt = f"@{image_path}\nIs the main person shown in this image {name}? Answer with exactly one word: YES or NO."
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
    except ClaudeCliError as e:
        logger.debug(f"Photo verification call failed ({e}); treating as unverified")
        return False
    return raw.strip().upper().startswith("YES")


def _distinctive_tokens(name: str) -> list[str]:
    """Same relevance heuristic as character_clip.py's -- ALL of the
    character's identifying name tokens (not just the first word: a real
    photo title often uses the surname alone, e.g. "Gatsby" rather than
    "Jay Gatsby", and matching on the first word only discarded those).
    Used to reject image results that are unrelated to the actual named
    character (an image search on a loosely matching query has returned a
    completely different person's photo)."""
    return [t.lower() for t in re.findall(r"[A-Za-z']+", name) if len(t) >= 3 and t.lower() not in _STOPWORDS]


def _to_int(value, default: int = 0) -> int:
    """ddgs backends are inconsistent about width/height typing (int on some,
    numeric string on others) -- normalize before comparing."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_character_image(query: str, out_path: Path, max_attempts: int = 6, name: str | None = None) -> Path | None:
    """
    Search and download one real image for `query` (e.g. "Walter White
    Breaking Bad"). Returns out_path on success, None if nothing usable was
    found -- caller falls back to the plain gradient background, no crash.
    """
    try:
        results = list(DDGS().images(query, max_results=max_attempts))
    except Exception as e:
        logger.warning(f"Character image search failed for {query!r}: {e}")
        return None

    tokens = _distinctive_tokens(name) if name else []
    needs_verification = False
    if tokens:
        matched = [r for r in results if any(tok in (r.get("title") or "").lower() for tok in tokens)]
        if matched:
            results = matched
        else:
            # Nothing actually mentions the character in the title -- still
            # try the raw top results rather than showing nothing (an empty
            # half of a POV split-screen reads as visibly broken), but a
            # title match alone isn't available here as a quality signal, so
            # each downloaded candidate gets vision-verified below before
            # being accepted -- a title-less fallback previously returned a
            # random footballer's photo for "Dwight Schrute" that ranked
            # near the top by pure image-search luck, not relevance.
            sane = [r for r in results if 0.45 <= (_to_int(r.get("width"), 1) / max(1, _to_int(r.get("height"), 1))) <= 2.2]
            results = sane or results
            needs_verification = True
            logger.warning(f"No image titles matched {name!r} (tokens {tokens!r}) for query {query!r} -- using unfiltered, vision-verified results")

    verify_tries = 0
    for r in results:
        url = r.get("image")
        w, h = _to_int(r.get("width")), _to_int(r.get("height"))
        if not url or w < _MIN_SIZE or h < _MIN_SIZE:
            continue
        try:
            resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
            resp.raise_for_status()
            Image.open(BytesIO(resp.content)).verify()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            img.save(out_path, format="JPEG", quality=90)
        except Exception as e:
            logger.debug(f"Skipping image candidate {url!r}: {e}")
            continue

        if needs_verification and name:
            if verify_tries >= _MAX_FALLBACK_VERIFY_TRIES:
                break
            verify_tries += 1
            if not _verify_photo_is_character(out_path, name):
                logger.warning(f"Vision check rejected fallback candidate for {name!r}: {url}")
                continue

        logger.info(f"Character image fetched for {query!r}: {url}")
        return out_path

    if needs_verification:
        out_path.unlink(missing_ok=True)
    logger.warning(f"No usable character image found for {query!r}")
    return None


def fetch_character_images(query: str, tmp_dir: Path, count: int, max_attempts: int = 12, name: str | None = None) -> list[Path]:
    """Same search as fetch_character_image, but downloads up to `count`
    DISTINCT images for a multi-media cycling background instead of a
    single case-file photo."""
    try:
        results = list(DDGS().images(query, max_results=max_attempts))
    except Exception as e:
        logger.warning(f"Character image search failed for {query!r}: {e}")
        return []

    tokens = _distinctive_tokens(name) if name else []
    if tokens:
        relevant = [r for r in results if any(tok in (r.get("title") or "").lower() for tok in tokens)]
        if relevant:
            results = relevant
        else:
            logger.warning(f"No image titles matched {name!r} (tokens {tokens!r}) for query {query!r} -- discarding all candidates")
            return []

    images: list[Path] = []
    for i, r in enumerate(results):
        if len(images) >= count:
            break
        url = r.get("image")
        w, h = _to_int(r.get("width")), _to_int(r.get("height"))
        if not url or w < _MIN_SIZE or h < _MIN_SIZE:
            continue
        out_path = tmp_dir / f"image_{i}.jpg"
        try:
            resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
            resp.raise_for_status()
            Image.open(BytesIO(resp.content)).verify()
            img = Image.open(BytesIO(resp.content)).convert("RGB")
            img.save(out_path, format="JPEG", quality=90)
            images.append(out_path)
        except Exception as e:
            logger.debug(f"Skipping image candidate {url!r}: {e}")
            continue

    if images:
        logger.info(f"Fetched {len(images)} character image(s) for {query!r}")
    return images
