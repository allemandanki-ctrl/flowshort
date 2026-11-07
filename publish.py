"""
Publish CLI — upload an existing video to configured platforms.

Usage:
  python publish.py output/videos/my_video.mp4
  python publish.py output/videos/my_video.mp4 --platforms youtube
  python publish.py output/videos/my_video.mp4 --platforms tiktok instagram
  python publish.py output/videos/my_video.mp4 --script output/scripts/my_script.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("publish")

sys.path.insert(0, str(Path(__file__).parent))


def _find_script_for_video(video_path: Path) -> dict | None:
    """
    Try to find the matching script JSON by timestamp prefix in the filename.
    Video:  20260627_163838_format1_...mp4
    Script: 20260627_163838_format1_...json
    """
    from config import OUTPUT_DIR
    scripts_dir = OUTPUT_DIR / "scripts"
    ts_prefix = video_path.stem[:15]  # "20260627_163838"
    candidates = list(scripts_dir.glob(f"{ts_prefix}*.json"))
    if candidates:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", help="Path to the .mp4 video file")
    parser.add_argument(
        "--platforms", nargs="+",
        choices=["youtube", "tiktok", "instagram"],
        help="Override platforms (default: from .env PUBLISH_PLATFORMS)",
    )
    parser.add_argument("--script", help="Path to the matching script JSON (auto-detected if omitted)")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        logger.error(f"Video not found: {video_path}")
        sys.exit(1)

    # Load or auto-detect script
    if args.script:
        script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    else:
        script = _find_script_for_video(video_path)
        if script is None:
            logger.warning("Could not auto-detect matching script — using minimal stub")
            script = {
                "format": "format1",
                "subject": video_path.stem,
                "hook": video_path.stem.replace("_", " "),
                "body": [],
                "cta": None,
                "include_book_cta": False,
                "hashtags": ["#wealth", "#money", "#mindset", "#success"],
            }

    from modules.publish.publisher import publish
    results = publish(video_path, script, platforms=args.platforms)

    print("\n── Publish results ─────────────────────────────")
    for platform, url in results.items():
        status = "✓" if not url.startswith("ERROR") else "✗"
        print(f"  {status} {platform:12s}  {url}")
    print()


if __name__ == "__main__":
    main()
