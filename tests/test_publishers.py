"""
Manual, opt-in test posts — one per provider. NOT auto-discovered by pytest
(no test_ function names) and NOT run unless invoked directly, since each
one fires a REAL scheduled post using real API keys.

Usage:
  python tests/test_publishers.py snapchat
  python tests/test_publishers.py tiktok
  python tests/test_publishers.py instagram
  python tests/test_publishers.py all

Each schedules a real post ~10 minutes from now on a tiny 3-line notice video
(reuses whatever .mp4 you pass, or the newest one in output/videos/ if none
given) so you can confirm the post actually appears before trusting the
pipeline with real content.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_publishers")

sys.path.insert(0, str(Path(__file__).parent.parent))


def _pick_video(video_arg: str | None) -> Path:
    if video_arg:
        p = Path(video_arg)
        if not p.exists():
            sys.exit(f"Video not found: {p}")
        return p
    from config import OUTPUT_DIR
    videos = sorted((OUTPUT_DIR / "videos").glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not videos:
        sys.exit("No video given and none found in output/videos/ — pass a path explicitly.")
    return videos[0]


def _run_one(platform: str, video_path: Path) -> None:
    from modules.publish.publish_config import load_publish_config, schedule_video

    cfg = load_publish_config()
    when = datetime.now(timezone.utc) + timedelta(minutes=10)
    caption = f"[FlowShort test post] {platform} — {datetime.now().isoformat(timespec='seconds')}"

    print(f"\n>> Scheduling REAL test post -> {platform} at {when.isoformat()} ...")
    res = schedule_video(video_path, [platform], when, caption=caption, cfg=cfg)
    print(f"   status={res.status}  detail={res.detail}")
    for r in res.results:
        print(f"   [{r.platform}] status={r.status} post_id={r.post_id} detail={r.detail}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["snapchat", "tiktok", "youtube", "instagram", "facebook", "all"])
    parser.add_argument("--video", help="Path to a test .mp4 (default: newest in output/videos/)")
    args = parser.parse_args()

    video_path = _pick_video(args.video)
    print(f"Using video: {video_path}")

    confirm = input(
        f"\nThis will create a REAL scheduled post on {args.target} in ~10 minutes "
        "using your real API keys. Type 'yes' to continue: "
    )
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    targets = ["snapchat", "tiktok", "instagram"] if args.target == "all" else [args.target]
    # one per provider is enough to prove the wiring (snapchat=ayrshare, tiktok=zernio_a, instagram=zernio_b)
    for platform in targets:
        _run_one(platform, video_path)


if __name__ == "__main__":
    main()
