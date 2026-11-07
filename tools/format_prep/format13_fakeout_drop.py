"""
Format 13 -- "Fake-out Drop". Standalone entry point (config-only, no
generate_script/Claude involved -- see modules/assembler/fakeout_drop.py).

Usage:
  python tools/format_prep/format13_fakeout_drop.py configs/fakeout_drop_example.json
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
logger = logging.getLogger("format13")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

if __name__ == "__main__":
    if len(sys.argv) < 2:
        logger.error("Usage: python tools/format_prep/format13_fakeout_drop.py <config.json>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        logger.error(f"Config introuvable : {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))

    from modules.assembler.fakeout_drop import build_fakeout_drop

    try:
        video = build_fakeout_drop(config)
        subprocess.Popen(["start", "", str(video)], shell=True)
    except Exception as e:
        logger.error(f"format13 failed: {e}", exc_info=True)
        sys.exit(1)
