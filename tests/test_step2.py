"""
Test Step 2: voice generation + transcription with timestamps.
Requires Step 1 output (runs a quick script generation first).
Run: python tests/test_step2.py
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
from modules.voice import generate_voice, build_spoken_text
from modules.transcriber import transcribe


def test_voice_and_timestamps():
    print("\n=== STEP 2 TEST: Voice + Timestamps ===\n")

    # Use a format WITH voice (format4) and one WITHOUT (format3)
    test_cases = [
        ("format4", "comment investir 100 euros quand on est debutant"),
        ("format3", "la richesse ne dort jamais"),  # no voice expected
    ]

    all_passed = True

    for fmt, subject in test_cases:
        print(f"--- {fmt} | {subject!r} ---")

        # Step 1: generate script
        script = generate_script(fmt, subject)
        spoken = build_spoken_text(script)
        print(f"  voice-over expected : {spoken is not None}")
        if spoken:
            print(f"  spoken text preview : {spoken[:120]}...")

        # Step 2a: TTS
        voice_result = generate_voice(script)

        if fmt == "format3":
            assert voice_result is None, "format3 should return None (no voice)"
            print("  PASS - format3 correctly skipped TTS")
            print()
            continue

        if not voice_result:
            print("  FAIL - generate_voice returned None unexpectedly")
            all_passed = False
            print()
            continue

        from pathlib import Path
        audio_path = voice_result["audio_path"]
        audio_size = Path(audio_path).stat().st_size
        print(f"  audio path : {audio_path}")
        print(f"  audio size : {audio_size / 1024:.1f} KB")

        if audio_size < 1000:
            print("  FAIL - audio file suspiciously small")
            all_passed = False
            print()
            continue

        # Step 2b: Whisper transcription
        ts_result = transcribe(audio_path, script)

        if not ts_result:
            print("  FAIL - transcription returned None")
            all_passed = False
            print()
            continue

        words = ts_result["word_timestamps"]
        lines = ts_result["line_timestamps"]
        duration = ts_result["duration"]

        print(f"  duration   : {duration}s")
        print(f"  words      : {len(words)}")
        print(f"  lines      : {len(lines)}")

        if not words:
            print("  FAIL - no word timestamps")
            all_passed = False
        elif not lines:
            print("  FAIL - no line timestamps")
            all_passed = False
        else:
            print("  Sample word timestamps:")
            for w in words[:5]:
                print(f"    [{w['start']:.2f}s - {w['end']:.2f}s] {w['word']!r}")
            print("  Sample line timestamps:")
            for ln in lines[:3]:
                print(f"    [{ln['start']:.2f}s - {ln['end']:.2f}s] ({ln['type']}) {ln['text'][:60]!r}")
            print("  PASS")

        print()

    print("=== RESULT:", "ALL PASS" if all_passed else "SOME FAILED", "===\n")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    test_voice_and_timestamps()
