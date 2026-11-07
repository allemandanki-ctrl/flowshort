"""
Daily runner — generates one video and publishes to all platforms.
Designed to run via Windows Task Scheduler once per day.

Schedule setup (run once as admin):
  python run_daily.py --schedule   # registers Task Scheduler entry at PUBLISH_HOUR

Manual run:
  python run_daily.py
  python run_daily.py --format format1
  python run_daily.py --subject "how compound interest works"
"""

import argparse
import logging
import random
import sys
from pathlib import Path
from datetime import datetime

import io as _io
_utf8_stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(_utf8_stdout),
        logging.FileHandler(
            Path(__file__).parent / "logs" / f"daily_{datetime.now().strftime('%Y%m%d')}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("run_daily")

sys.path.insert(0, str(Path(__file__).parent))

# Ensure logs dir exists before FileHandler writes
(Path(__file__).parent / "logs").mkdir(exist_ok=True)

_SUBJECTS = [
    "how compound interest makes you rich while you sleep",
    "the 3 financial habits of self-made millionaires",
    "why the poor trade time for money and the rich don't",
    "how to go from zero to 1000 dollars saved in 90 days",
    "the rule of 72 — how long to double your money",
    "why most people stay broke despite a good salary",
    "how to buy assets instead of liabilities",
    "the investment that changed everything for me",
    "stop upgrading your lifestyle every time you earn more",
    "the difference between income and wealth",
    "how to build passive income from scratch with nothing",
    "the one mindset shift that separates the wealthy from everyone else",
    "why saving money alone will never make you rich",
    "the debt trap most people never escape",
    "how to think like an investor not a consumer",
    "why your salary is your worst financial plan",
    "the three accounts every wealthy person keeps",
    "how small daily habits create massive financial results",
    "the first 1000 dollars — why it changes everything",
    "why the rich get richer while the middle class shrinks",
]

# format1 (clip edit) ~33%, format3 (luxury aesthetic) ~67%
_FORMATS = ["format1", "format3", "format3"]


def run(fmt: str | None = None, subject: str | None = None) -> None:
    fmt     = fmt     or random.choice(_FORMATS)
    subject = subject or random.choice(_SUBJECTS)

    logger.info("=" * 70)
    logger.info(f"DAILY RUN | {datetime.now().strftime('%Y-%m-%d %H:%M')} | {fmt} | {subject}")
    logger.info("=" * 70)

    # Step 1: generate video
    from pipeline import run as generate_video
    try:
        video_path = generate_video(fmt, subject)
    except Exception as e:
        logger.error(f"Video generation failed: {e}", exc_info=True)
        sys.exit(1)

    # Step 2: publish
    from modules.publish.publisher import publish
    from modules.script_generator import generate_script
    # Script was already generated inside pipeline.run(); reload from disk
    import json
    from config import OUTPUT_DIR
    scripts_dir = OUTPUT_DIR / "scripts"
    ts_prefix   = video_path.stem[:15]
    candidates  = list(scripts_dir.glob(f"{ts_prefix}*.json"))
    script      = json.loads(candidates[0].read_text(encoding="utf-8")) if candidates else {}

    try:
        results = publish(video_path, script)
        logger.info("Publish results:")
        for platform, url in results.items():
            logger.info(f"  {platform}: {url}")
    except Exception as e:
        logger.error(f"Publishing failed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("Daily run complete.")


def register_task_scheduler(hour: int) -> None:
    """Register a Windows Task Scheduler entry to run daily at the given hour."""
    import subprocess
    script_path = str(Path(__file__).resolve())
    python_path = sys.executable
    task_name   = "VideoAutomationDaily"

    # Delete existing task if any
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True,
    )

    cmd = [
        "schtasks", "/create",
        "/tn",    task_name,
        "/tr",    f'"{python_path}" "{script_path}"',
        "/sc",    "daily",
        "/st",    f"{hour:02d}:00",
        "/rl",    "highest",
        "/f",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        logger.info(f"Task Scheduler: '{task_name}' registered — runs daily at {hour:02d}:00")
    else:
        logger.error(f"Task Scheduler failed: {result.stderr}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--format",   help="Force a specific format (format1, format3, ...)")
    parser.add_argument("--subject",  help="Force a specific subject")
    parser.add_argument("--schedule", action="store_true", help="Register Windows Task Scheduler entry and exit")
    args = parser.parse_args()

    if args.schedule:
        from config import PUBLISH_HOUR
        register_task_scheduler(PUBLISH_HOUR)
        sys.exit(0)

    run(fmt=args.format, subject=args.subject)
