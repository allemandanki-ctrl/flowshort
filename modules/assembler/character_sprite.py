"""
AI-generated 2D fighting-game sprite art for format15's "arena" shots --
distinct from the real photo used for the HP-bar portrait: a stylized
fighter rendering (not a real photo) used between real-footage cutaways,
so the archetype look does the bouncing/hit-reacting, not an actual photo
of the real person/actor.

Reuses the same Pollinations mechanism modules/visuals.py already relies
on for format1/3's generated-image fallback -- a free, keyless HTTP image
endpoint, callable at render time with no human/MCP step, so this works
inside the fully-automated pipeline (random character -> instant render),
not just when a human is available to generate art interactively.
"""

import logging
import random
import urllib.parse
from pathlib import Path

import requests

from config import POLLINATIONS_BASE_URL

logger = logging.getLogger(__name__)

_MAX_SPRITE_TRIES = 3


def _verify_sprite_is_character(image_path: Path, name: str, context: str) -> bool:
    """One quick vision check via the user's own Claude subscription --
    confirms a generated pixel-art sprite is actually recognizable as
    `name` before accepting it. Pollinations has no guarantee of getting a
    specific character right and nothing was catching it before this: real
    incident, 2026-08-26, a "Tony Stark" sprite came back as a generic
    Street-Fighter-style fighter with zero Iron Man visual identity, and a
    "Winnie the Pooh" sprite came back as a broken, mismatched chimera
    (Pooh's head on unrelated legs/clothing) -- both got used as-is because
    nothing ever checked the output."""
    from config import CLAUDE_MODEL
    from modules.claude_cli import call_claude, ClaudeCliError
    prompt = (
        f"@{image_path}\nThis is meant to be a stylized retro pixel-art fighting-game "
        f"sprite of {name} from {context}. Even allowing for the pixel-art stylization, "
        f"does it clearly read as that specific character (right silhouette/outfit/colors/"
        f"species for them), AND is the body coherent (not a broken, mismatched, or "
        f"disfigured render)? Answer with exactly one word: YES or NO."
    )
    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
    except ClaudeCliError as e:
        logger.debug(f"Sprite verification call failed ({e}); treating as unverified")
        return False
    return raw.strip().upper().startswith("YES")


def fetch_arena_backdrop(out_path: Path) -> Path | None:
    """A generic retro fighting-game STAGE background (empty, no characters
    -- both fighters are composited onto it separately). Not tied to either
    character's specific IP, just the classic arcade-stage look."""
    prompt = (
        "retro 16-bit pixel art fighting game stage background, "
        "Street Fighter Mortal Kombat Sega Genesis arcade stage, "
        "empty background no characters, vibrant colors, side view, "
        "classic 90s arcade video game battle arena"
    )
    encoded = urllib.parse.quote(prompt)
    seed = random.randint(1, 99999)
    url = f"{POLLINATIONS_BASE_URL}/{encoded}?width=1080&height=1920&nologo=true&seed={seed}"
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
        logger.info(f"Arena backdrop generated -> {out_path} ({len(resp.content)//1024}KB)")
        return out_path
    except Exception as e:
        logger.warning(f"Arena backdrop generation failed: {e}")
        return None


def fetch_character_sprite(name: str, context: str, out_path: Path) -> Path | None:
    """Generate a 2D fighting-game sprite of `name` (from `context`) and
    save a background-removed RGBA PNG to out_path. Retries with a fresh
    seed (up to _MAX_SPRITE_TRIES times) until a vision check confirms the
    result actually reads as that character -- Pollinations otherwise has
    no guarantee of getting a specific character right (see
    _verify_sprite_is_character). Returns None once every attempt fails --
    caller falls back to a photo-based cutout instead of leaving that
    fighter invisible on the arena.

    Background removal is a real model (rembg/u2net), not chroma-keying --
    a flat-color keyed background repeatedly failed here whenever the
    generated outfit happened to be close to the key color (green shirt on
    green screen, then pink jacket on magenta screen), eating holes in the
    character itself. rembg segments the actual subject regardless of what
    colors the AI picked, so this stops being a per-render coin flip."""
    prompt = (
        f"{name} from {context}, retro 16-bit pixel art fighting game sprite, "
        f"Street Fighter II Mortal Kombat Sega Genesis arcade style, "
        f"full body dynamic fighting stance, VIBRANT SATURATED colors, "
        f"bold high contrast color palette, bright saturated outfit colors "
        f"that pop and stand out, strong black outline around the character, "
        f"visible pixelation, classic 90s arcade sprite sheet, plain background"
    )
    encoded = urllib.parse.quote(prompt)

    for attempt in range(1, _MAX_SPRITE_TRIES + 1):
        seed = random.randint(1, 99999)
        url = f"{POLLINATIONS_BASE_URL}/{encoded}?width=768&height=1024&nologo=true&seed={seed}"
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Sprite generation failed for {name!r} (attempt {attempt}/{_MAX_SPRITE_TRIES}): {e}")
            continue

        try:
            from io import BytesIO
            from PIL import Image
            from rembg import remove
            cutout = remove(Image.open(BytesIO(resp.content)).convert("RGB"))
            cutout.save(str(out_path), format="PNG")
        except Exception as e:
            logger.warning(f"Background removal failed for {name!r} (attempt {attempt}/{_MAX_SPRITE_TRIES}): {e}")
            continue

        if _verify_sprite_is_character(out_path, name, context):
            logger.info(f"Character sprite generated for {name!r} -> {out_path} (verified, attempt {attempt})")
            return out_path
        logger.warning(f"Vision check rejected sprite for {name!r} (attempt {attempt}/{_MAX_SPRITE_TRIES}) -- retrying")

    out_path.unlink(missing_ok=True)
    logger.warning(f"No verified sprite found for {name!r} after {_MAX_SPRITE_TRIES} attempts")
    return None
