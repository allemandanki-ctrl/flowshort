"""
Test Step 3: visual fetching from Pexels, Pixabay, Pollinations.
Run: python tests/test_step3.py
"""

import json
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

# (format, subject) pairs — one with video B-roll, one with images+generated
TEST_CASES = [
    ("format2", "les erreurs financieres que tout le monde fait a 20 ans"),  # -> videos
    ("format3", "richesse et luxe minimaliste"),                              # -> photos + generated
]

MIN_FILE_SIZE_KB = 10


def validate_result(result: dict, fmt: str) -> list[str]:
    errors = []
    videos = result.get("videos", [])
    images = result.get("images", [])

    if fmt in ("format2", "format5") and len(videos) < 2:
        errors.append(f"Expected >= 2 videos, got {len(videos)}")
    if fmt == "format3" and len(images) < 1:
        errors.append(f"Expected >= 1 image for format3, got {len(images)}")

    for p in videos + images:
        path = Path(p)
        if not path.exists():
            errors.append(f"File missing: {p}")
        elif path.stat().st_size < MIN_FILE_SIZE_KB * 1024:
            errors.append(f"File suspiciously small ({path.stat().st_size}B): {path.name}")

    return errors


def main():
    print("\n=== STEP 3 TEST: Visuals ===\n")
    all_passed = True

    for fmt, subject in TEST_CASES:
        print(f"--- {fmt} | {subject!r} ---")

        script = generate_script(fmt, subject)
        print(f"  keywords: {script['visual_keywords']}")

        result = fetch_visuals(script)

        errors = validate_result(result, fmt)
        videos = result["videos"]
        images = result["images"]

        print(f"  videos  : {len(videos)}")
        for v in videos:
            p = Path(v)
            print(f"    {p.name}  {p.stat().st_size // 1024}KB")

        print(f"  images  : {len(images)}")
        for i in images:
            p = Path(i)
            print(f"    {p.name}  {p.stat().st_size // 1024}KB")

        if errors:
            print(f"  FAIL:")
            for e in errors:
                print(f"    * {e}")
            all_passed = False
        else:
            print("  PASS")

        print()

    print("=== RESULT:", "ALL PASS" if all_passed else "SOME FAILED", "===\n")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
