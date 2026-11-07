"""
Trending sounds — auto-refreshed monthly.

Primary source: Reddit public JSON API (no auth required)
Fallback:       Anthropic web_search_20250305 tool
Cache:          state/trending_sounds_cache.json  (TTL 30 days)

On refresh failure: keeps stale cache, retries after 7 days.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from config import RESETWORTH_DIR, ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

_CACHE_PATH     = RESETWORTH_DIR / "state" / "trending_sounds_cache.json"
_CACHE_TTL_DAYS = 30
_RETRY_TTL_DAYS = 7   # if last refresh failed, retry sooner

_REDDIT_URLS = [
    "https://www.reddit.com/r/tiktok/search.json?q=trending+sounds&sort=relevance&t=month&limit=10",
    "https://www.reddit.com/r/Tiktokhelp/search.json?q=trending+audio&sort=new&t=month&limit=10",
    "https://www.reddit.com/r/Instagram/search.json?q=trending+reels+audio&sort=relevance&t=month&limit=10",
]
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VideoAutoBot/1.0)"}


def get_trending_sounds() -> list[dict]:
    """
    Return list of currently trending sounds.
    Refreshes cache if stale (>30 days), or >7 days after a failed refresh.
    Never raises — falls back to stale cache or [] on failure.
    """
    sounds, updated_at, prev_failed = _load_cache()

    ttl     = _RETRY_TTL_DAYS if prev_failed else _CACHE_TTL_DAYS
    is_stale = not sounds or updated_at is None or (datetime.now() - updated_at) > timedelta(days=ttl)

    if not is_stale:
        return sounds

    logger.info("Trending sounds cache stale — refreshing...")
    month_year = datetime.now().strftime("%B %Y")

    # Primary: Reddit JSON → Claude extraction
    raw        = _fetch_reddit()
    new_sounds = _extract_via_claude(raw, month_year) if raw else []

    # Fallback: Anthropic web_search tool
    if not new_sounds:
        logger.info("Reddit returned nothing — trying Anthropic web_search fallback...")
        new_sounds = _fetch_via_claude_websearch(month_year)

    if new_sounds:
        _save_cache(new_sounds, refresh_failed=False)
        logger.info(f"Trending sounds updated: {[s['name'] for s in new_sounds]}")
        return new_sounds

    logger.warning("Trending sounds refresh failed — keeping stale cache, will retry in 7 days")
    _save_cache(sounds or [], refresh_failed=True)
    return sounds or []


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache() -> tuple[list[dict], datetime | None, bool]:
    if not _CACHE_PATH.exists():
        return [], None, False
    try:
        data       = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(data["updated_at"])
        return data.get("sounds", []), updated_at, data.get("refresh_failed", False)
    except Exception:
        return [], None, False


def _save_cache(sounds: list[dict], refresh_failed: bool = False) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(
        json.dumps(
            {"updated_at": datetime.now().isoformat(), "sounds": sounds, "refresh_failed": refresh_failed},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


# ── Primary: Reddit public JSON → Claude extraction ────────────────────────────

def _fetch_reddit() -> str:
    try:
        import requests
    except ImportError:
        logger.warning("requests not installed — skipping Reddit fetch")
        return ""

    texts = []
    for url in _REDDIT_URLS:
        try:
            resp  = requests.get(url, headers=_HEADERS, timeout=10)
            if not resp.ok:
                continue
            posts = resp.json().get("data", {}).get("children", [])
            for post in posts[:5]:
                pd = post.get("data", {})
                title = pd.get("title", "")
                body  = pd.get("selftext", "")[:600]
                texts.append(f"TITLE: {title}\nBODY: {body}")
        except Exception as e:
            logger.warning(f"Reddit fetch error ({url}): {e}")

    return "\n\n---\n\n".join(texts)


def _extract_via_claude(raw_text: str, month_year: str) -> list[dict]:
    from modules.claude_cli import call_claude

    prompt = f"""Reddit posts about trending TikTok/Reels sounds in {month_year}:

{raw_text[:3000]}

Extract up to 8 real, confirmed trending sounds from this text.
Return ONLY a JSON array (no other text):
[{{"name": "song name", "artist": "artist or 'original sound'", "where": "short search hint e.g. Cherche X dans les sons TikTok", "bpm": <your best estimate as a number, or null if you can't tell>, "market": "US" | "UK" | "GLOBAL"}}]

Rules:
- Only include sounds clearly described as trending
- Never invent sounds not mentioned in the text
- market: only "US", "UK", or "GLOBAL" -- NEVER a French-market-only sound, skip it entirely if so
- Return [] if nothing confirmed"""

    try:
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        raw   = raw.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        parsed = json.loads(fence.group(1).strip() if fence else raw)
        return [{"type": "trending", **s} for s in parsed if s.get("name") and s.get("artist")]
    except Exception as e:
        logger.warning(f"Claude extraction failed: {e}")
        return []


# ── Fallback: Anthropic web_search_20250305 tool ───────────────────────────────

def _fetch_via_claude_websearch(month_year: str) -> list[dict]:
    try:
        import anthropic
    except ImportError:
        return []

    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    messages = [{
        "role": "user",
        "content": (
            f"Search for the most viral TikTok and Instagram Reels sounds in {month_year}, "
            "US/UK/global trends only -- never a France-specific sound. "
            "Check sources like Buffer, HeyOrca, Socialbee, or tokchart.com. "
            "Find 4-8 real, currently trending sounds with artist names. "
            "Return ONLY a JSON array: "
            '[{"name": "...", "artist": "...", "where": "short search hint for TikTok/Reels", '
            '"bpm": <your best estimate as a number, or null>, "market": "US" | "UK" | "GLOBAL"}]'
        ),
    }]

    try:
        for _ in range(6):
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        raw   = block.text.strip()
                        fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
                        try:
                            parsed = json.loads(fence.group(1).strip() if fence else raw)
                            return [{"type": "trending", **s} for s in parsed if s.get("name") and s.get("artist")]
                        except Exception:
                            pass
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = [
                    {"type": "tool_result", "tool_use_id": b.id, "content": []}
                    for b in response.content
                    if hasattr(b, "type") and b.type == "tool_use"
                ]
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                continue

            break

    except Exception as e:
        logger.warning(f"Claude web_search fallback failed: {e}")

    return []
