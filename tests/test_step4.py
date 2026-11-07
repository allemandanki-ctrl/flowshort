"""
Test Step 4: FFmpeg assembly — format3 (images + text + music).
Run: python tests/test_step4.py

Note: add a .mp3 to assets/music/ for music test. Without one, video is silent (still passes).
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/pipeline.log", encoding="utf-8"),
    ],
)

from modules.script_generator import generate_script
from modules.visuals import fetch_visuals
from modules.assembler import assemble

MIN_VIDEO_SIZE_KB = 200


def main():
    print("\n=== STEP 4 TEST: FFmpeg Assembly (format3) ===\n")

    subject = "le luxe comme etat d esprit pas comme un prix"
    fmt = "format3"

    print(f"Subject : {subject!r}")
    print(f"Format  : {fmt}\n")

    # Step 1
    print("[ Step 1 ] Generating script...")
    script = generate_script(fmt, subject)
    print(f"  hook     : {script['hook'][:80]}")
    print(f"  body     : {len(script['body'])} lines")
    print(f"  keywords : {script['visual_keywords'][:3]}")
    print()

    # Step 3
    print("[ Step 3 ] Fetching visuals...")
    visuals = fetch_visuals(script)
    print(f"  images   : {len(visuals['images'])}")
    print()

    # Step 4
    print("[ Step 4 ] Assembling video with FFmpeg...")
    try:
        output = assemble(script, visuals)
    except Exception as e:
        print(f"  FAIL - {e}")
        sys.exit(1)

    size_kb = output.stat().st_size // 1024
    print(f"  output   : {output}")
    print(f"  size     : {size_kb}KB")

    if size_kb < MIN_VIDEO_SIZE_KB:
        print(f"  FAIL - video suspiciously small ({size_kb}KB < {MIN_VIDEO_SIZE_KB}KB)")
        sys.exit(1)

    print("  PASS")
    print(f"\n  Open the video to verify it looks correct:")
    print(f"  {output}\n")

    print("=== RESULT: PASS ===\n")


if __name__ == "__main__":
    main()
