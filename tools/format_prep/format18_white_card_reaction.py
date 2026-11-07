"""
Format 18 -- "White Card Reaction". Standalone entry point -- see
modules/assembler/white_card_reaction.py.

Two modes, picked by whether the config has a "lines" key:
  - config HAS "lines": manual, exactly as before -- build_white_card_reaction
    renders the given lines/redactions as-is, no Claude involved.
  - config has NO "lines" (just "input_path" and optionally "idea"): fully
    automatic -- auto_build_white_card_reaction has Claude look at the clip
    itself to write the joke and find/redact any baked-in watermark.

Usage:
  python tools/format_prep/format18_white_card_reaction.py configs/white_card_reaction_example.json
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("format18")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python tools/format_prep/format18_white_card_reaction.py <config.json>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        logger.error(f"Config introuvable : {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))

    from modules.assembler.white_card_reaction import auto_build_white_card_reaction, build_white_card_reaction

    try:
        if config.get("lines"):
            video = build_white_card_reaction(config)
        else:
            video = auto_build_white_card_reaction(
                config["input_path"], idea=config.get("idea"), output_name=config.get("output_name"),
                keep_audio=config.get("keep_audio", True),
            )
        subprocess.Popen(["start", "", str(video)], shell=True)
    except Exception as e:
        logger.error(f"format18 failed: {e}", exc_info=True)
        sys.exit(1)
