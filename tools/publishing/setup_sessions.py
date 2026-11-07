"""
One-time session setup for TikTok and Instagram.

Opens a real browser window → you log in manually → session saved to state/browser_profiles/

Usage:
  python tools/publishing/setup_sessions.py --platform tiktok
  python tools/publishing/setup_sessions.py --platform instagram
  python tools/publishing/setup_sessions.py --platform all
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("setup_sessions")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import BASE_DIR, TIKTOK_PROFILE_DIR, INSTAGRAM_PROFILE_DIR, YOUTUBE_STUDIO_PROFILE_DIR


_PLATFORMS = {
    "tiktok":         ("https://www.tiktok.com/login", TIKTOK_PROFILE_DIR),
    "instagram":      ("https://www.instagram.com/accounts/login/", INSTAGRAM_PROFILE_DIR),
    "youtube_studio": ("https://studio.youtube.com", YOUTUBE_STUDIO_PROFILE_DIR),
}


def _find_chrome() -> str | None:
    """Find Brave or Chrome on Windows. Brave takes priority."""
    from pathlib import Path as _Path
    home = _Path.home()
    candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        str(home / r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(home / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if _Path(c).exists():
            return c
    return None


async def setup(platform: str) -> None:
    url, profile_rel = _PLATFORMS[platform]
    profile_dir = BASE_DIR / profile_rel
    profile_dir.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    # YouTube Studio requires real Chrome to avoid Google's automation block
    use_chrome = platform == "youtube_studio"
    chrome_path = _find_chrome() if use_chrome else None
    if use_chrome and not chrome_path:
        logger.error("Brave/Chrome not found — install Brave or Google Chrome and retry")
        return

    logger.info(f"Opening {platform} login page...")
    logger.info("Log in manually in the browser window, then press ENTER here.")

    async with async_playwright() as p:
        launch_kwargs: dict = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
        ctx = await p.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)

        # Wait for user to log in
        print(f"\n>>> Log in to {platform} in the browser window.")
        print(">>> Press ENTER here when you're fully logged in and see your feed/home.\n")
        input()

        # Verify session by checking URL
        current = page.url
        logger.info(f"Current URL: {current}")
        logger.info(f"Session saved to {profile_dir}")

        await ctx.close()

    logger.info(f"{platform} session setup complete.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["tiktok", "instagram", "youtube_studio", "all"], required=True)
    args = parser.parse_args()

    platforms = list(_PLATFORMS.keys()) if args.platform == "all" else [args.platform]
    for p in platforms:
        asyncio.run(setup(p))


if __name__ == "__main__":
    main()
