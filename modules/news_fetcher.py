"""
Weekly finance/markets news digest (format17) -- free RSS sources only, no
paid API, no Claude web_search (that route needs the direct Anthropic API --
see music_updater.py's rare fallback -- which this project avoids as a
primary dependency, not just an occasional exception). Raw headlines/
summaries get handed to Claude (via claude_cli.py, the user's subscription)
for STRUCTURING into the video script -- Claude never invents facts here,
only reformats what these feeds actually reported this week.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

_FEEDS = [
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch", "https://www.marketwatch.com/rss/topstories"),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
]
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VideoAutoBot/1.0)"}
_LOOKBACK_DAYS = 7
_MAX_ITEMS_PER_FEED = 15


def fetch_weekly_digest() -> str:
    """
    Plain-text digest of real headlines from the last 7 days across all
    feeds -- fed to Claude as ground truth for format17's script. Returns ""
    on total failure (caller must refuse to generate rather than let Claude
    invent financial news out of thin air).
    """
    import requests

    cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)
    lines: list[str] = []

    for name, url in _FEEDS:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:_MAX_ITEMS_PER_FEED]:
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue
                desc = (item.findtext("description") or "").strip()
                desc = re.sub(r"<[^>]+>", " ", desc)
                desc = re.sub(r"\s+", " ", desc).strip()

                pub_raw = item.findtext("pubDate")
                if pub_raw:
                    try:
                        pub = parsedate_to_datetime(pub_raw)
                        if pub.tzinfo is None:
                            pub = pub.replace(tzinfo=timezone.utc)
                        if pub < cutoff:
                            continue
                    except Exception:
                        pass  # unparseable date -- keep the item rather than silently drop it

                lines.append(f"[{name}] {title}\n{desc[:300]}")
        except Exception as e:
            logger.warning(f"news_fetcher: {name} feed failed ({e})")
            continue

    logger.info(f"news_fetcher: collected {len(lines)} headline(s) from the last {_LOOKBACK_DAYS} days")
    return "\n\n".join(lines)
