"""
Test Step 1: script generation.
Run: python tests/test_step1.py
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
from modules.cta_counter import status

REQUIRED_KEYS = {
    "format", "subject", "hook", "body", "cta",
    "include_book_cta", "visual_keywords", "music_style",
    "duration_target", "description", "hashtags",
}

TEST_CASES = [
    ("format3", "comment investir son premier salaire"),
    ("format4", "la regle des 90 jours pour changer son rapport a l'argent"),
    ("format2", "les erreurs financieres que tout le monde fait a 20 ans"),
]


def validate(script: dict) -> list[str]:
    errors = []
    missing = REQUIRED_KEYS - set(script.keys())
    if missing:
        errors.append(f"Missing keys: {missing}")
    if not isinstance(script.get("body"), list) or len(script["body"]) < 2:
        errors.append("body must be a list with >= 2 lines")
    if not isinstance(script.get("visual_keywords"), list) or len(script["visual_keywords"]) < 3:
        errors.append("visual_keywords must have >= 3 entries")
    if not isinstance(script.get("hashtags"), list) or len(script["hashtags"]) < 5:
        errors.append("hashtags must have >= 5 entries")
    if "--" in json.dumps(script):
        errors.append("Output contains em dashes (--), forbidden by style rules")
    return errors


def main():
    print("\n=== STEP 1 TEST: Script Generator ===\n")
    print(f"CTA counter before tests: {status()}\n")

    all_passed = True

    for i, (fmt, subject) in enumerate(TEST_CASES, 1):
        print(f"--- Test {i}: {fmt} | {subject!r} ---")
        try:
            script = generate_script(fmt, subject)
            errors = validate(script)

            if errors:
                print(f"  FAIL - validation errors:")
                for e in errors:
                    print(f"    * {e}")
                all_passed = False
            else:
                print(f"  PASS")
                print(f"  hook         : {script['hook']}")
                print(f"  body lines   : {len(script['body'])}")
                print(f"  book CTA     : {script['include_book_cta']}")
                print(f"  duration     : {script['duration_target']}s")
                print(f"  music        : {script['music_style']}")
                print(f"  keywords     : {script['visual_keywords']}")
                print(f"  cta          : {script.get('cta')}")

        except Exception as e:
            print(f"  ERROR - {e}")
            all_passed = False

        print()

    print(f"CTA counter after tests: {status()}\n")
    print("=== RESULT:", "ALL PASS" if all_passed else "SOME FAILED", "===\n")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
