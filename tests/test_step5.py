"""
Step 5 test — voice formats (format2, format4).
Runs full pipeline: script → voice → transcription → visuals → subtitles → assembly.
"""

import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_step5")

sys.path.insert(0, str(Path(__file__).parent.parent))


def run(fmt: str) -> None:
    logger.info(f"{'='*60}")
    logger.info(f"TEST {fmt.upper()} — voice + subtitles")
    logger.info(f"{'='*60}")

    # ── 1. Script ────────────────────────────────────────────────
    logger.info("Step 1 — generating script")
    from modules.script_generator import generate_script
    script = generate_script(fmt, subject="investir avec 100€ par mois")
    logger.info(f"  hook     : {script.get('hook', '')[:80]}")
    logger.info(f"  body[0]  : {str(script.get('body', [''])[:1])[:80]}")
    logger.info(f"  book_cta : {script.get('include_book_cta')}")

    # ── 2. Voice ─────────────────────────────────────────────────
    logger.info("Step 2 — generating voice")
    from modules.voice import generate_voice
    voice = generate_voice(script)
    logger.info(f"  audio    : {voice['audio_path']}")
    logger.info(f"  voice    : {voice['voice']}")

    # ── 3. Transcription ─────────────────────────────────────────
    logger.info("Step 3 — transcribing (word timestamps)")
    from modules.transcriber import transcribe
    timestamps = transcribe(voice["audio_path"], script)
    logger.info(f"  duration : {timestamps['duration']:.2f}s")
    logger.info(f"  words    : {len(timestamps['word_timestamps'])}")
    if timestamps["word_timestamps"]:
        first = timestamps["word_timestamps"][0]
        logger.info(f"  first    : '{first['word']}' @ {first['start']:.2f}s")

    # ── 4. Subtitles ─────────────────────────────────────────────
    logger.info("Step 4 — generating ASS subtitles")
    from modules.subtitle_generator import generate_ass
    ass = generate_ass(timestamps)
    logger.info(f"  ass file : {ass}")
    content = ass.read_text(encoding="utf-8-sig")
    n_events = content.count("\nDialogue:")
    logger.info(f"  events   : {n_events} subtitle lines")

    # ── 5. Visuals ───────────────────────────────────────────────
    logger.info("Step 5 — fetching visuals")
    from modules.visuals import fetch_visuals
    visuals = fetch_visuals(script)
    videos = visuals.get("videos", [])
    images = visuals.get("images", [])
    logger.info(f"  videos   : {len(videos)}")
    logger.info(f"  images   : {len(images)}")

    # ── 6. Assembly ───────────────────────────────────────────────
    logger.info("Step 6 — assembling video")
    from modules.assembler import assemble
    out = assemble(script, visuals, voice, timestamps, music_path=None)
    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"  output   : {out}")
    logger.info(f"  size     : {size_mb:.1f} MB")

    logger.info(f"SUCCESS {fmt.upper()}")
    logger.info("")


if __name__ == "__main__":
    fmts = sys.argv[1:] if sys.argv[1:] else ["format2", "format4"]
    for f in fmts:
        try:
            run(f)
        except Exception as e:
            logger.error(f"FAILED {f}: {e}", exc_info=True)
            sys.exit(1)
    logger.info("All tests passed.")
