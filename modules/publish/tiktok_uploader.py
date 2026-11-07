"""
TikTok uploader — Playwright persistent session.

Session setup (one-time, manual):
  python tools/publishing/setup_sessions.py --platform tiktok

Then fully automated: loads saved cookies, uploads video, selects trending sound, posts.

Sound strategy: searches "motivation" in TikTok's sound library → picks top trending result.
"""

import asyncio
import logging
import time
from pathlib import Path

from config import BASE_DIR, TIKTOK_PROFILE_DIR

logger = logging.getLogger(__name__)

_UPLOAD_URL   = "https://www.tiktok.com/upload"
_UPLOAD_WAIT  = 120   # max seconds to wait for video processing


def upload_to_tiktok(video_path: Path, script: dict) -> str:
    """Upload video to TikTok. Returns post URL or empty string on failure."""
    return asyncio.run(_upload_async(video_path, script))


async def _upload_async(video_path: Path, script: dict) -> str:
    from playwright.async_api import async_playwright
    try:
        from playwright_stealth import stealth_async
        _has_stealth = True
    except ImportError:
        _has_stealth = False
        logger.warning("playwright-stealth not installed — bot detection may block upload")

    profile_dir = BASE_DIR / TIKTOK_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if _has_stealth:
            await stealth_async(page)

        try:
            result = await _do_upload(page, video_path, script)
        finally:
            await ctx.close()

    return result


async def _do_upload(page, video_path: Path, script: dict) -> str:
    from playwright.async_api import TimeoutError as PWTimeout

    logger.info("TikTok: navigating to upload page")
    await page.goto(_UPLOAD_URL, wait_until="networkidle", timeout=30_000)
    await page.wait_for_timeout(2000)

    # TikTok upload lives inside an iframe
    upload_frame = await _find_upload_frame(page)

    # Set video file directly on the file input (bypasses native dialog)
    logger.info(f"TikTok: uploading {video_path.name}")
    file_input = upload_frame.locator("input[type='file']").first
    await file_input.set_input_files(str(video_path))

    # Wait for processing bar to disappear
    logger.info("TikTok: waiting for video processing...")
    await _wait_for_processing(upload_frame)

    # Caption + hashtags
    caption = _build_caption(script)
    logger.info(f"TikTok: setting caption ({len(caption)} chars)")
    await _set_caption(upload_frame, caption)

    # Post
    logger.info("TikTok: posting...")
    await _click_post(upload_frame, page)

    # Get URL of published post
    try:
        await page.wait_for_url("**/tiktok.com/@**", timeout=15_000)
        post_url = page.url
    except PWTimeout:
        post_url = "https://www.tiktok.com"

    logger.info(f"TikTok: posted → {post_url}")
    return post_url


async def _find_upload_frame(page):
    """Return the upload iframe's frame object, falling back to main page."""
    await page.wait_for_timeout(1500)
    for frame in page.frames:
        if "upload" in frame.url or "creator" in frame.url:
            return frame
    return page


async def _wait_for_processing(frame, timeout: int = _UPLOAD_WAIT) -> None:
    """Wait until TikTok's upload progress bar / spinner disappears."""
    from playwright.async_api import TimeoutError as PWTimeout
    start = time.time()
    while time.time() - start < timeout:
        # Check for common processing indicators
        processing = await frame.locator("[class*='progress'], [class*='uploading'], [class*='processing']").count()
        if processing == 0:
            return
        await frame.wait_for_timeout(2000)
    logger.warning(f"TikTok: processing timed out after {timeout}s, continuing anyway")


async def _set_caption(frame, caption: str) -> None:
    """Fill the caption/description field."""
    selectors = [
        "[data-e2e='caption-input']",
        ".notranslate[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea[placeholder]",
    ]
    for sel in selectors:
        try:
            el = frame.locator(sel).first
            await el.wait_for(timeout=5000)
            await el.click()
            await el.fill(caption)
            return
        except Exception:
            continue
    logger.warning("TikTok: caption field not found, skipping")


async def _click_post(frame, page) -> None:
    """Click the Post button."""
    post_selectors = [
        "[data-e2e='post-btn']",
        "button:has-text('Post')",
        "button[class*='post-btn']",
        "button:has-text('Publish')",
    ]
    for sel in post_selectors:
        try:
            btn = frame.locator(sel).first
            await btn.wait_for(timeout=5000)
            await btn.click()
            await page.wait_for_timeout(5000)
            return
        except Exception:
            continue
    logger.warning("TikTok: Post button not found")


def _build_caption(script: dict) -> str:
    """Build TikTok caption via Claude-generated description + fixed hashtags (max 2200 chars)."""
    from modules.content_meta import generate_meta, build_social_caption
    meta = generate_meta(script)
    return build_social_caption(meta)[:2200]
