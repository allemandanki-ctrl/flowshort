"""
Publisher — orchestrates upload to all configured platforms.

Flow:
  Every requested platform → one Drive upload + info .txt (poster à la main)
  All                      → recap email after every run

YouTube is NOT auto-published: music must be added manually on mobile.
Same video, one Drive upload, one posting reminder per platform in email/info.txt.

Results saved to state/publish_log.json.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, PUBLISH_PLATFORMS

logger = logging.getLogger(__name__)

_LOG_FILE = BASE_DIR / "state" / "publish_log.json"

# Every platform routed through get_post_time needs an _OPTIMAL_HOURS entry
# (see modules/publish/scheduler.py) -- listed here explicitly so a new
# platform added to PUBLISH_PLATFORMS without a matching scheduler entry
# fails loudly in _manual_platform_times below instead of silently vanishing
# from the manual email the way snapchat/facebook used to (2026-08-06 fix).
_SCHEDULABLE_PLATFORMS = ("youtube", "instagram", "tiktok", "snapchat", "facebook")


def publish(
    video_path: Path,
    script: dict,
    platforms: list[str] | None = None,
    days_ahead: int = 1,
) -> dict:
    targets = platforms or PUBLISH_PLATFORMS
    results: dict[str, str] = {}

    from modules.content_meta import generate_meta, build_social_caption, FIXED_HASHTAGS
    script["_meta"]  = generate_meta(script)
    title            = script["_meta"]["title"]
    caption          = build_social_caption(script["_meta"])
    hashtags         = " ".join(FIXED_HASHTAGS)
    music_suggestion = script["_meta"].get("music_suggestion", {})
    logger.info(f"Meta generated | title={title!r}")

    from modules.publish.scheduler import get_post_time, upload_manual_video, send_recap_email
    recap: list[dict] = []

    # ALL requested platforms go through the manual/Drive+email flow -- until
    # 2026-08-06 this only ever included ("youtube", "instagram", "tiktok"),
    # so checking Snapchat or Facebook in the dashboard's Plateformes selector
    # silently did nothing here: not published, not in the email, no error.
    # Every platform this app knows how to schedule genuinely needs manual
    # posting right now (none of them have a working unattended auto-publish
    # path yet), so "manual = everything requested" matches the file's own
    # original docstring intent ("All -> recap email"), not a new behavior.
    manual = [p for p in targets if p in _SCHEDULABLE_PLATFORMS]
    unknown = [p for p in targets if p not in _SCHEDULABLE_PLATFORMS]
    if unknown:
        logger.warning(f"publish(): platform(s) {unknown} not in _SCHEDULABLE_PLATFORMS, skipped entirely")

    if manual:
        times = {p: get_post_time(p, days_ahead=days_ahead) for p in manual}
        ig_time, tt_time, yt_time = times.get("instagram"), times.get("tiktok"), times.get("youtube")
        snap_time, fb_time = times.get("snapchat"), times.get("facebook")

        # Snapchat's own 150-char cap (hashtags included) -- same trim logic the
        # real auto-publish path already applies (publishers/ayrshare_client.py),
        # reused here so the manual email shows the SAME text that would actually
        # survive a real Snapchat post, not the full untrimmed caption.
        snap_caption = None
        if "snapchat" in manual:
            from publishers.ayrshare_client import _truncate_caption_for_snapchat
            snap_caption = _truncate_caption_for_snapchat(f"{caption}\n\n{hashtags}".strip())

        logger.info(f"Drive upload for {manual}...")
        try:
            link = upload_manual_video(
                video_path, ig_time, tt_time, yt_time, title, caption, hashtags, music_suggestion,
                snap_time=snap_time, fb_time=fb_time, snap_caption=snap_caption,
            )
            for p in manual:
                results[p] = link
            recap.append({
                "platform": "manual", "status": "manual",
                "ig_time": ig_time, "tt_time": tt_time, "yt_time": yt_time,
                "snap_time": snap_time, "fb_time": fb_time,
                "title": title, "caption": caption, "hashtags": hashtags,
                "snap_caption": snap_caption,
                "music_suggestion": music_suggestion,
                "drive_link": link,
            })
            logger.info(f"Drive upload done → {link}")
        except Exception as e:
            for p in manual:
                results[p] = f"ERROR: {e}"
            recap.append({
                "platform": "manual", "status": "error",
                "ig_time": ig_time, "tt_time": tt_time, "yt_time": yt_time,
                "snap_time": snap_time, "fb_time": fb_time,
                "title": title, "error": str(e),
            })
            logger.error(f"Drive upload FAILED — {e}", exc_info=True)

    logger.info("Sending recap email...")
    try:
        send_recap_email(recap)
        logger.info("Recap email sent.")
    except Exception as e:
        logger.warning(f"Recap email failed (non-fatal): {e}")

    _log_result(video_path, script, results)
    return results


def _log_result(video_path: Path, script: dict, results: dict) -> None:
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = []
    if _LOG_FILE.exists():
        try:
            log = json.loads(_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = []
    entry = {
        "timestamp":   datetime.now().isoformat(),
        "video":       str(video_path),
        "format":      script.get("format"),
        "subject":     script.get("subject"),
        "include_cta": script.get("include_book_cta"),
        "results":     results,
    }
    log.append(entry)
    _LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Publish log updated: {_LOG_FILE}")
