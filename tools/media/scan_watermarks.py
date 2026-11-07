"""
Scans a format18 (white card reaction) source clip's four corners PLUS
full-width top/bottom edge strips for baked-in watermarks and writes one
contact-sheet PNG per region -- see modules/clips/watermark_scan.py for why
this is a look-and-read step rather than a numeric auto-detector.

Usage:
  python tools/media/scan_watermarks.py <video_path> [--box x,y,w,h] [--interval 1.0]
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("video_path")
    parser.add_argument("--box", help="x,y,w,h -- skip auto-detection of the content box")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    box = tuple(int(v) for v in args.box.split(",")) if args.box else None

    from modules.clips.watermark_scan import build_corner_contact_sheets

    result = build_corner_contact_sheets(args.video_path, box=box, interval=args.interval, out_dir=args.out_dir)
    print(f"\ncontent box: {result['box']}")
    print("region windows (x, y, w, h) -- same coordinate space as white_card_reaction's redactions:")
    for name, window in result["regions"].items():
        print(f"  [{name}] box={list(window)}  sheet={result['sheets'].get(name)}")
