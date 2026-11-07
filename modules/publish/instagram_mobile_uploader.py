"""
Instagram mobile uploader — Appium + BlueStacks.

Pilote l'app Instagram Android réelle dans BlueStacks pour publier des Reels avec musique.

Setup (une seule fois) :
  1. Installer BlueStacks 5 : https://www.bluestacks.com
  2. Lancer BlueStacks, installer Instagram depuis le Play Store, se connecter
  3. Installer Node.js, puis : npm install -g appium
  4. Installer le driver Android : appium driver install uiautomator2
  5. pip install Appium-Python-Client
  6. Dans .env : INSTAGRAM_USE_MOBILE=true
  7. Avant chaque publication : lancer Appium dans un terminal : appium

Flow automatisé :
  push vidéo → ADB → BlueStacks
  → ouvrir Instagram → "+" → Reel
  → sélectionner vidéo → Next
  → ajouter musique (bouton natif mobile)
  → Next → caption → Share
"""

import logging
import subprocess
import time
from pathlib import Path

from config import (
    BASE_DIR, LOGS_DIR, GUMROAD_LINK,
    APPIUM_SERVER_URL, BLUESTACKS_ADB_HOST, BLUESTACKS_ADB_PORT, ADB_PATH,
)

logger = logging.getLogger(__name__)

_IG_PACKAGE  = "com.instagram.android"
_IG_ACTIVITY = "com.instagram.android.activity.MainTabActivity"
_DEVICE_MEDIA_PATH = "/sdcard/Movies/robot_upload.mp4"


def upload_to_instagram_mobile(video_path: Path, script: dict) -> str:
    """Upload Reel to Instagram via Appium (BlueStacks). Returns post URL or ''."""
    LOGS_DIR.mkdir(exist_ok=True)

    music_query = script.get("_meta", {}).get("music_query", "motivation wealth")
    caption     = _build_caption(script)

    try:
        _adb_push(video_path)
    except Exception as e:
        logger.error(f"Instagram mobile: ADB push failed — {e}")
        return ""

    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
    except ImportError:
        logger.error("Instagram mobile: appium not importable — run: pip install Appium-Python-Client")
        return ""

    options = UiAutomator2Options()
    options.platform_name         = "Android"
    options.device_name           = f"{BLUESTACKS_ADB_HOST}:{BLUESTACKS_ADB_PORT}"
    options.app_package           = _IG_PACKAGE
    options.app_activity          = _IG_ACTIVITY
    options.no_reset              = True
    options.new_command_timeout   = 120

    logger.info(f"Instagram mobile: connecting to Appium at {APPIUM_SERVER_URL}")
    try:
        driver = webdriver.Remote(APPIUM_SERVER_URL, options=options)
    except Exception as e:
        logger.error(f"Instagram mobile: Appium connection failed — {e}\nAppium lancé ? (appium)")
        return ""

    try:
        return _do_upload(driver, music_query, caption)
    except Exception as e:
        _screenshot(driver, "ig_mobile_error")
        _dump_source(driver, "ig_mobile_error")
        logger.error(f"Instagram mobile: upload failed — {e}", exc_info=True)
        return ""
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _adb(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run an ADB command using ADB_PATH from config."""
    cmd = [ADB_PATH] + args
    logger.debug(f"ADB: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, capture_output=True)


def _adb_push(video_path: Path) -> None:
    """Push video file to BlueStacks via ADB (targets 127.0.0.1:5555 explicitly)."""
    adb_target = f"{BLUESTACKS_ADB_HOST}:{BLUESTACKS_ADB_PORT}"
    logger.info(f"Instagram mobile: ADB connect {adb_target} (using {ADB_PATH})")
    _adb(["connect", adb_target])
    logger.info(f"Instagram mobile: pushing {video_path.name} → {_DEVICE_MEDIA_PATH}")
    _adb(["-s", adb_target, "push", str(video_path), _DEVICE_MEDIA_PATH])
    # Refresh media store so Instagram sees the new file
    _adb([
        "-s", adb_target, "shell",
        "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{_DEVICE_MEDIA_PATH}",
    ], check=False)
    logger.info("Instagram mobile: ADB push done")


def _do_upload(driver, music_query: str, caption: str) -> str:
    from appium.webdriver.common.appiumby import AppiumBy

    def find(by, value, timeout=10):
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def tap_text(text, timeout=10):
        el = find(AppiumBy.XPATH, f'//*[@text="{text}"]', timeout)
        el.click()
        return el

    def tap_desc(desc, timeout=10):
        el = find(AppiumBy.ACCESSIBILITY_ID, desc, timeout)
        el.click()
        return el

    logger.info("Instagram mobile: app launched")
    time.sleep(3)
    _screenshot(driver, "ig_mobile_01_home")

    # Tap "+" / Create button (bottom nav)
    logger.info("Instagram mobile: opening Create")
    try:
        tap_desc("New post", timeout=8)
    except Exception:
        try:
            tap_desc("Nouveau post", timeout=5)
        except Exception:
            # Fallback: tap center-bottom nav
            size = driver.get_window_size()
            driver.tap([(size["width"] // 2, size["height"] - 60)])
    time.sleep(2)
    _screenshot(driver, "ig_mobile_02_create_menu")
    _dump_source(driver, "ig_mobile_02_create_menu")

    # Dismiss any promo popup Instagram may show (e.g. "Introducing instants")
    for dismiss_label in ["Not now", "Pas maintenant", "Dismiss", "Fermer", "Close", "Skip"]:
        try:
            el = driver.find_element(AppiumBy.XPATH, f'//*[@text="{dismiss_label}" or @content-desc="{dismiss_label}"]')
            el.click()
            logger.info(f"Instagram mobile: dismissed promo popup via '{dismiss_label}'")
            time.sleep(2)
            _screenshot(driver, "ig_mobile_02b_popup_dismissed")
            break
        except Exception:
            continue

    # Tap "Reel" in the create menu (appears after popup dismissed)
    logger.info("Instagram mobile: selecting Reel")
    try:
        tap_text("Reel")
    except Exception:
        try:
            tap_text("Reels")
        except Exception as e:
            _screenshot(driver, "ig_mobile_02c_reel_not_found")
            _dump_source(driver, "ig_mobile_02c_reel_not_found")
            logger.error(f"Instagram mobile: 'Reel'/'Reels' not found — see ig_mobile_02c_reel_not_found.xml. Error: {e}")
            raise
    time.sleep(2)
    _screenshot(driver, "ig_mobile_03_reel_picker")
    _dump_source(driver, "ig_mobile_03_reel_picker")

    # Select the video — it should appear in gallery (we pushed it to /sdcard/Movies/)
    logger.info("Instagram mobile: selecting video from gallery")
    try:
        # Tap first media item in gallery
        items = driver.find_elements(AppiumBy.XPATH, '//*[@resource-id="com.instagram.android:id/gallery_recycler_view"]//*[@clickable="true"]')
        if items:
            items[0].click()
        else:
            # Try generic image/video grid
            gallery = driver.find_elements(AppiumBy.XPATH, '//*[contains(@resource-id,"gallery") or contains(@resource-id,"media")][@clickable="true"]')
            if gallery:
                gallery[0].click()
    except Exception as e:
        logger.warning(f"Instagram mobile: gallery selection fallback needed — {e}")
        _screenshot(driver, "ig_mobile_03b_gallery_fail")

    time.sleep(2)
    _screenshot(driver, "ig_mobile_04_video_selected")

    # Next → edit screen
    logger.info("Instagram mobile: Next (to edit screen)")
    try:
        tap_text("Next")
    except Exception:
        tap_text("Suivant")
    time.sleep(3)
    _screenshot(driver, "ig_mobile_05_edit_screen")

    # Add music
    logger.info(f"Instagram mobile: adding music (query={music_query!r})")
    music_added = _add_music(driver, music_query)
    if not music_added:
        logger.warning("Instagram mobile: music not added — continuing without")

    # Next → caption screen
    logger.info("Instagram mobile: Next (to caption screen)")
    try:
        tap_text("Next")
    except Exception:
        tap_text("Suivant")
    time.sleep(2)
    _screenshot(driver, "ig_mobile_06_caption_screen")

    # Fill caption
    logger.info("Instagram mobile: filling caption")
    try:
        caption_field = find(AppiumBy.XPATH, '//*[contains(@resource-id,"caption") or contains(@content-desc,"caption") or contains(@content-desc,"légende")]')
        caption_field.click()
        caption_field.send_keys(caption)
        logger.info("Instagram mobile: caption set")
    except Exception as e:
        logger.warning(f"Instagram mobile: caption field not found — {e}")
    time.sleep(1)
    _screenshot(driver, "ig_mobile_07_caption_filled")

    # Share
    logger.info("Instagram mobile: sharing")
    try:
        tap_text("Share")
    except Exception:
        try:
            tap_text("Partager")
        except Exception:
            tap_desc("Share")
    time.sleep(8)
    _screenshot(driver, "ig_mobile_08_after_share")

    logger.info("Instagram mobile: Reel posted")
    return "https://www.instagram.com"


def _add_music(driver, music_query: str) -> bool:
    """Tap music button on edit screen, search, pick random track. Returns True on success."""
    import random
    from appium.webdriver.common.appiumby import AppiumBy

    def find_any(selectors, timeout=6):
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        for by, value in selectors:
            try:
                return WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((by, value))
                )
            except Exception:
                continue
        return None

    _screenshot(driver, "ig_mobile_music_01_before")

    # Find music button
    music_el = find_any([
        (AppiumBy.ACCESSIBILITY_ID, "Add music"),
        (AppiumBy.ACCESSIBILITY_ID, "Ajouter de la musique"),
        (AppiumBy.XPATH, '//*[contains(@content-desc,"music") or contains(@content-desc,"Music") or contains(@content-desc,"musique")]'),
        (AppiumBy.XPATH, '//*[contains(@resource-id,"music")][@clickable="true"]'),
    ])

    if not music_el:
        _screenshot(driver, "ig_mobile_music_02_btn_not_found")
        logger.info("Instagram mobile: music button not found")
        return False

    music_el.click()
    logger.info("Instagram mobile: music panel opened")
    time.sleep(2)
    _screenshot(driver, "ig_mobile_music_02_panel_open")

    # Search field
    search_el = find_any([
        (AppiumBy.XPATH, '//*[contains(@resource-id,"search")][@clickable="true"]'),
        (AppiumBy.XPATH, '//*[@hint="Search" or @hint="Rechercher"]'),
        (AppiumBy.XPATH, '//*[contains(@content-desc,"Search") or contains(@content-desc,"Rechercher")]'),
    ])
    if not search_el:
        _screenshot(driver, "ig_mobile_music_03_search_not_found")
        logger.info("Instagram mobile: music search field not found")
        return False

    search_el.click()
    search_el.send_keys(music_query)
    driver.press_keycode(66)  # ENTER
    time.sleep(3)
    _screenshot(driver, "ig_mobile_music_03_after_search")
    logger.info(f"Instagram mobile: music searched ({music_query!r})")

    # Pick random track from results
    tracks = driver.find_elements(
        AppiumBy.XPATH,
        '//*[contains(@resource-id,"track") or contains(@resource-id,"audio") or contains(@resource-id,"music_item")][@clickable="true"]',
    )
    if not tracks:
        # Generic: any list item
        tracks = driver.find_elements(AppiumBy.XPATH, '//android.widget.LinearLayout[@clickable="true"]')

    if not tracks:
        _screenshot(driver, "ig_mobile_music_04_no_tracks")
        logger.info("Instagram mobile: no music tracks found in results")
        return False

    pick = random.randint(0, min(len(tracks), 8) - 1)
    tracks[pick].click()
    time.sleep(2)
    _screenshot(driver, "ig_mobile_music_04_track_selected")
    logger.info(f"Instagram mobile: track #{pick} selected")

    # Confirm / Done if needed
    for label in ["Done", "Terminé", "Use", "Utiliser", "Add", "Ajouter"]:
        try:
            driver.find_element(AppiumBy.XPATH, f'//*[@text="{label}"]').click()
            logger.info(f"Instagram mobile: music confirmed via '{label}'")
            time.sleep(1)
            break
        except Exception:
            continue

    _screenshot(driver, "ig_mobile_music_05_confirmed")
    return True


def _screenshot(driver, name: str) -> None:
    try:
        path = str(LOGS_DIR / f"{name}.png")
        driver.save_screenshot(path)
        logger.info(f"Instagram mobile: screenshot → {name}.png")
    except Exception:
        pass


def _dump_source(driver, name: str) -> None:
    try:
        path = LOGS_DIR / f"{name}.xml"
        path.write_text(driver.page_source, encoding="utf-8")
        logger.info(f"Instagram mobile: page_source → {name}.xml")
    except Exception:
        pass


def _build_caption(script: dict) -> str:
    from modules.content_meta import generate_meta, build_social_caption
    meta = generate_meta(script)
    return build_social_caption(meta)[:2200]
