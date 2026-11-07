"""
fix_snapchat_captions.py

One-off maintenance script (not part of the main pipeline): scans
state/publish_schedule_log.json for every Snapchat post already scheduled
via Ayrshare, checks whether its caption text is over Snapchat's 150-char
limit (hashtags included), and re-truncates it in place via Ayrshare's
update-post API -- reusing the exact same _truncate_caption_for_snapchat()
rule the pipeline now applies to new posts (publishers/ayrshare_client.py),
so hashtags that would otherwise get silently dropped/cut when Snapchat
actually processes the post stay intact.

Usage:
  python tools/publishing/fix_snapchat_captions.py            # dry-run: report only, no API writes
  python tools/publishing/fix_snapchat_captions.py --apply    # actually PATCH the corrected captions

STATUS (2026-07-28): --apply does NOT currently work. get_post() (GET /post/{id})
reads fine, but update_post() (PATCH /post with {id, post[, platforms]} in the
body -- confirmed by the user as the documented endpoint) always returns
{"status": "success"} while leaving the post's text completely unchanged, even
on a still-"pending" (not yet published) post, and even with a distinctive
marker string used to rule out a caching artifact. Tried both with and without
a "platforms" field in the body -- same no-op either time. Decision (per user,
2026-07-28): leave already-scheduled/already-published Snapchat posts as-is,
don't keep guessing against the live account. If this is revisited later, the
likely next step is asking Ayrshare support for the real update mechanism (it
may require delete-then-recreate rather than an in-place edit) rather than
more trial-and-error on update_post()'s request shape.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import BASE_DIR
from publishers.ayrshare_client import _truncate_caption_for_snapchat, _SNAPCHAT_MAX_CAPTION

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("fix_snapchat_captions")

_LOG_PATH = BASE_DIR / "state" / "publish_schedule_log.json"


def _snapchat_post_ids() -> list[tuple[str, str]]:
    """(dedup_key, post_id) for every snapchat entry in the local schedule log.

    networks/post_ids are parallel lists (see modules/publish/publish_config.py's
    _record_scheduled) -- must index-match rather than assume post_ids[0].
    """
    if not _LOG_PATH.exists():
        return []
    log = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for key, entry in log.items():
        networks = entry.get("networks", [])
        post_ids = entry.get("post_ids", [])
        for i, net in enumerate(networks):
            if net == "snapchat" and i < len(post_ids):
                out.append((key, post_ids[i]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Actually PUT corrected captions to Ayrshare (default: dry-run, report only).")
    args = parser.parse_args()

    from modules.publish.publish_config import load_publish_config, get_client
    cfg = load_publish_config()
    client = get_client("ayrshare_main", cfg)

    targets = _snapchat_post_ids()
    if not targets:
        logger.info("No Snapchat posts found in state/publish_schedule_log.json.")
        return

    logger.info(f"Found {len(targets)} Snapchat post(s) in the local log. Checking lengths "
                f"(limit={_SNAPCHAT_MAX_CAPTION})...")

    n_over = n_fixed = n_skipped = n_errors = 0
    for key, post_id in targets:
        try:
            data = client.get_post(post_id)
        except Exception as e:
            logger.warning(f"[{post_id}] could not fetch post ({e}) -- skipping ({key})")
            n_skipped += 1
            continue

        caption = data.get("post")
        if caption is None:
            logger.warning(
                f"[{post_id}] unexpected response shape, no 'post' field -- "
                f"raw response: {json.dumps(data)[:400]} -- skipping"
            )
            n_skipped += 1
            continue

        if len(caption) <= _SNAPCHAT_MAX_CAPTION:
            continue

        n_over += 1
        fixed = _truncate_caption_for_snapchat(caption)
        logger.info(f"[{post_id}] {len(caption)} -> {len(fixed)} chars ({key})")
        logger.info(f"  before: {caption!r}")
        logger.info(f"  after:  {fixed!r}")

        if not args.apply:
            logger.info(f"[{post_id}] dry-run -- not applied (pass --apply to write this change).")
            continue

        try:
            client.update_post(post_id, fixed)
            logger.info(f"[{post_id}] updated.")
            n_fixed += 1
        except Exception as e:
            logger.error(f"[{post_id}] update FAILED: {e}")
            n_errors += 1

    logger.info(
        f"Done. checked={len(targets)} over_limit={n_over} fixed={n_fixed} "
        f"errors={n_errors} skipped={n_skipped}"
        + ("" if args.apply else " (dry-run -- re-run with --apply to write changes)")
    )


if __name__ == "__main__":
    main()
