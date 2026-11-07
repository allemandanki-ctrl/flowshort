"""
Instagram uploader — Playwright persistent session (Reels).

Session setup (one-time, manual):
  python tools/publishing/setup_sessions.py --platform instagram

Then fully automated: loads saved cookies, uploads as Reel, adds audio, posts.
"""

import asyncio
import logging
from pathlib import Path

from config import BASE_DIR, INSTAGRAM_PROFILE_DIR

logger = logging.getLogger(__name__)

_IG_URL = "https://www.instagram.com"


def upload_to_instagram(video_path: Path, script: dict) -> str:
    """Upload video to Instagram as a Reel. Returns post URL or empty string."""
    return asyncio.run(_upload_async(video_path, script))


async def _upload_async(video_path: Path, script: dict) -> str:
    from playwright.async_api import async_playwright
    try:
        from playwright_stealth import stealth_async
        _has_stealth = True
    except ImportError:
        _has_stealth = False

    profile_dir = BASE_DIR / INSTAGRAM_PROFILE_DIR
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

    (BASE_DIR / "logs").mkdir(exist_ok=True)
    logger.info("Instagram: navigating to home page")
    await page.goto(_IG_URL, wait_until="networkidle", timeout=30_000)
    await page.wait_for_timeout(2000)

    # Click the "Create" / "+" new post button
    logger.info("Instagram: opening new post dialog")
    new_post_selectors = [
        "svg[aria-label='New post']",
        "[aria-label='New post']",
        "a[href='/create/style/']",
        "svg[aria-label='Create']",
        "[aria-label='Create']",
    ]
    clicked = False
    for sel in new_post_selectors:
        try:
            await page.locator(sel).first.click(timeout=4000)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        logger.error("Instagram: could not find new post button — is the session valid?")
        return ""

    await page.wait_for_timeout(1500)

    # Click "Post" scoped to the dropdown that just appeared (Post / Live video / Ad)
    logger.info("Instagram: clicking 'Post' in Create dropdown")
    menu_container_selectors = [
        "[role='dialog']",
        "[role='menu']",
        "nav",
        "body",  # last-resort scope
    ]
    post_item_selectors = [
        ":text-is('Post')",
        ":text-is('Publication')",
        "span:text-is('Post')",
        "span:text-is('Publication')",
    ]
    post_clicked = False
    for container_sel in menu_container_selectors:
        if post_clicked:
            break
        container = page.locator(container_sel).last  # freshest match = just-opened menu
        for item_sel in post_item_selectors:
            try:
                await container.locator(item_sel).first.click(timeout=3000)
                post_clicked = True
                logger.info(f"Instagram: 'Post' clicked via {container_sel!r} > {item_sel!r}")
                break
            except Exception:
                continue

    if not post_clicked:
        screenshot_path = str(BASE_DIR / "logs" / "ig_dropdown_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.error(f"Instagram: could not click 'Post' in dropdown — screenshot saved to {screenshot_path}")
        return ""

    await page.wait_for_timeout(2000)

    # Verify upload dialog appeared; screenshot if not
    upload_visible = False
    for check_sel in ["input[type='file']", "button:has-text('Select from computer')", "button:has-text('ordinateur')"]:
        if await page.locator(check_sel).count() > 0:
            upload_visible = True
            break
    if not upload_visible:
        screenshot_path = str(BASE_DIR / "logs" / "ig_post_click_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.error(f"Instagram: upload dialog did not appear after 'Post' click — screenshot saved to {screenshot_path}")
        return ""

    # Upload video — click "Select from computer" inside expect_file_chooser
    logger.info(f"Instagram: uploading {video_path.name}")
    # Use expect_file_chooser: click must happen inside the context manager
    select_btn_selectors = [
        "button:has-text('Select from computer')",
        "button:has-text('Sélectionner sur l\\'ordinateur')",
        "button:has-text('Sélectionner depuis l\\'ordinateur')",
        "div[role='button']:has-text('computer')",
        "div[role='button']:has-text('ordinateur')",
    ]
    file_uploaded = False
    for sel in select_btn_selectors:
        try:
            async with page.expect_file_chooser(timeout=6000) as fc_info:
                await page.locator(sel).first.click(timeout=4000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(video_path))
            file_uploaded = True
            logger.info(f"Instagram: file set via chooser ({sel!r})")
            break
        except Exception:
            continue

    if not file_uploaded:
        # Fallback: directly set hidden input (older IG UI or selector mismatch)
        logger.warning("Instagram: file chooser path failed, trying hidden input directly")
        file_input = page.locator("input[type='file']").first
        await file_input.set_input_files(str(video_path), timeout=10_000)

    await page.wait_for_timeout(4000)

    # Dismiss "Video posts are now shared as reels" popup if present (optional)
    for ok_sel in ["button:has-text('OK')", "button:has-text('Ok')"]:
        try:
            await page.locator(ok_sel).first.click(timeout=3000)
            logger.info("Instagram: dismissed 'reels' info popup")
            await page.wait_for_timeout(1000)
            break
        except Exception:
            continue

    # Step 1: Crop → Filters screen
    dialog = page.locator("[role='dialog']")
    try:
        next_el = dialog.locator(":text-is('Next')").first
        await next_el.wait_for(state="visible", timeout=5000)
        await next_el.click()
        logger.info("Instagram: Next clicked (step 1 — Crop → Filters)")
        await page.wait_for_timeout(2000)
    except Exception:
        screenshot_path = str(BASE_DIR / "logs" / "ig_next_step1_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.warning(f"Instagram: Next not found at step 1 — screenshot: {screenshot_path}")

    # Step 2: Filters → Caption screen
    try:
        next_el = dialog.locator(":text-is('Next')").first
        await next_el.wait_for(state="visible", timeout=5000)
        await next_el.click()
        logger.info("Instagram: Next clicked (step 2 — Filters → Caption)")
        await page.wait_for_timeout(2000)
    except Exception:
        screenshot_path = str(BASE_DIR / "logs" / "ig_next_step2_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.warning(f"Instagram: Next not found at step 2 — screenshot: {screenshot_path}")

    # Fill caption
    caption = _build_caption(script)
    logger.info(f"Instagram: setting caption ({len(caption)} chars)")
    caption_selectors = [
        "div[aria-label='Write a caption...']",
        "div[aria-label='Rédiger une légende...']",
        "textarea[placeholder*='caption' i]",
        "textarea[placeholder*='légende' i]",
        "div[contenteditable='true'][aria-label]",
    ]
    caption_set = False
    for sel in caption_selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=5000)
            await el.click()
            await el.fill(caption)
            # Verify fill worked — contenteditable may ignore fill()
            content = await el.inner_text()
            if not content.strip():
                logger.info("Instagram: fill() empty, falling back to keyboard.type()")
                await el.click()
                await page.keyboard.type(caption, delay=10)
            caption_set = True
            logger.info(f"Instagram: caption set via {sel!r}")
            break
        except Exception:
            continue
    if not caption_set:
        logger.warning("Instagram: caption field not found — posting without caption")

    # Click "Share" scoped to dialog — may be div/span, not <button>
    share_clicked = False
    for sel in [":text-is('Share')", ":text-is('Partager')",
                "button:has-text('Share')", "button:has-text('Partager')"]:
        try:
            btn = dialog.locator(sel).first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
            share_clicked = True
            logger.info(f"Instagram: share clicked via {sel!r}")
            break
        except Exception:
            continue
    if not share_clicked:
        screenshot_path = str(BASE_DIR / "logs" / "ig_share_btn_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.error(f"Instagram: share button not found — screenshot: {screenshot_path}")

    # Wait for confirmation
    await page.wait_for_timeout(5000)
    confirmed = False
    for confirm_text in ["Your reel has been shared", "Votre reel a été partagé", "Reel shared"]:
        try:
            await page.wait_for_selector(f"text={confirm_text}", timeout=15_000)
            logger.info("Instagram: reel shared successfully")
            confirmed = True
            break
        except Exception:
            continue
    if not confirmed:
        screenshot_path = str(BASE_DIR / "logs" / "ig_share_debug.png")
        await page.screenshot(path=screenshot_path)
        logger.warning(f"Instagram: could not confirm share — screenshot saved to {screenshot_path}")

    post_url = page.url
    logger.info(f"Instagram: done → {post_url}")
    return post_url


def _build_caption(script: dict) -> str:
    """Build IG caption via Claude-generated description + fixed hashtags (max 2200 chars)."""
    from modules.content_meta import generate_meta, build_social_caption
    meta = generate_meta(script)
    return build_social_caption(meta)[:2200]
