"""
Mass-schedule videos direct-to-SaaS (Ayrshare/Zernio) from a CSV or JSON file.

Usage:
  # List a Zernio provider's connected social channels (to fill account_id in profiles.yaml)
  python tools/publishing/schedule_batch.py --list-accounts zernio_a
  python tools/publishing/schedule_batch.py --list-accounts zernio_b

  # Dry-run a batch (no upload, no post created) — prints what WOULD happen
  python tools/publishing/schedule_batch.py --file schedule.csv --dry-run

  # Real run
  python tools/publishing/schedule_batch.py --file schedule.csv
  python tools/publishing/schedule_batch.py --file schedule.json --delay 3

Input file columns / keys:
  video_path      absolute or relative path to the .mp4
  scheduled_time  ISO 8601 (e.g. 2026-07-05T19:13:00) — local naive treated as UTC
  networks        pipe- or comma-separated (e.g. "instagram|tiktok|youtube")
  caption         post text/description
"""

import argparse
import csv
import io
import json
import logging
import sys
import time
from pathlib import Path

# Windows consoles default to cp1252 and choke on non-ASCII — force UTF-8 output.
_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(_stdout)],
)
logger = logging.getLogger("schedule_batch")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _parse_networks(raw) -> list[str]:
    if isinstance(raw, list):
        return [n.strip() for n in raw if str(n).strip()]
    return [n.strip() for n in str(raw).replace(",", "|").split("|") if n.strip()]


def _load_items(file_path: Path) -> list[dict]:
    if file_path.suffix.lower() == ".json":
        data = json.loads(file_path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("items", [])
    elif file_path.suffix.lower() == ".csv":
        with open(file_path, newline="", encoding="utf-8") as fh:
            items = list(csv.DictReader(fh))
    else:
        raise ValueError("Input file must be .csv or .json")

    for it in items:
        it["networks"] = _parse_networks(it.get("networks", ""))
    return items


def _cmd_list_accounts(provider_name: str, profiles_file: str | None) -> None:
    from modules.publish.publish_config import load_publish_config
    from publishers.zernio_client import ZernioClient

    cfg = load_publish_config(profiles_file)
    provider = cfg.providers.get(provider_name)
    if provider is None:
        logger.error(f"Unknown provider '{provider_name}'. Known: {list(cfg.providers)}")
        sys.exit(1)
    if provider.type != "zernio":
        print(
            f"\n'{provider_name}' is an Ayrshare provider — Ayrshare has no separate "
            "accountId concept, its API key already maps to your linked socials.\n",
            file=_stdout,
        )
        return

    import os
    api_key = os.getenv(provider.api_key_env, "")
    if not api_key:
        logger.error(f"Env var {provider.api_key_env} is empty — set it in .env first")
        sys.exit(1)

    client = ZernioClient(api_key)
    print(f"\n-- Connected accounts ({provider_name}) --------------------", file=_stdout)
    for acc in client.list_accounts():
        print(f"  {acc.get('platform', '?'):12s}  accountId={acc.get('_id', '?')}  name={acc.get('name', '?')}", file=_stdout)
    print(file=_stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="CSV or JSON schedule file")
    parser.add_argument("--dry-run", action="store_true", help="Simulate, create nothing")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds between create calls")
    parser.add_argument("--list-accounts", metavar="PROVIDER",
                        help="List connected accounts for a Zernio provider (e.g. zernio_a) and exit")
    parser.add_argument("--profiles-file", help="Path to profiles.yaml (default: ./profiles.yaml)")
    args = parser.parse_args()

    if args.list_accounts:
        _cmd_list_accounts(args.list_accounts, args.profiles_file)
        return

    if not args.file:
        logger.error("Provide --file schedule.csv|json (or use --list-accounts <provider>)")
        sys.exit(1)

    from datetime import datetime
    from modules.publish.publish_config import load_publish_config, schedule_video

    items = _load_items(Path(args.file))
    logger.info(f"Loaded {len(items)} item(s) from {args.file} | dry_run={args.dry_run}")

    cfg = load_publish_config(args.profiles_file)
    results = []
    for idx, item in enumerate(items, 1):
        sched = item["scheduled_time"]
        if isinstance(sched, str):
            sched = datetime.fromisoformat(sched)

        logger.info(f"[{idx}/{len(items)}] {item.get('video_path')} -> {item['networks']}")
        res = schedule_video(
            item["video_path"], item["networks"], sched,
            caption=item.get("caption", ""), dry_run=args.dry_run, cfg=cfg,
            profiles_file=args.profiles_file or "profiles.yaml",
        )
        results.append(res)

        if res.status in ("created", "partial") and idx < len(items):
            time.sleep(args.delay)

    print("\n-- Results ---------------------------------------------", file=_stdout)
    for r in results:
        mark = {"created": "OK", "skipped": "--", "dry-run": "..", "error": "XX", "partial": "!!"}.get(r.status, "??")
        print(f"  [{mark}] {r.status:8s} {r.video}  {r.networks}  {r.detail}", file=_stdout)
    print(file=_stdout)

    if any(r.status == "error" for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
