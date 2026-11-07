"""
Persistent state for the YouTube long-format generator — fully separate from
the mixte/batch counter and from Format1's utilisés/ clip archive.

Two files:
  state/long_format_counter.json      — own start date, own day/video counters.
  state/long_format_clip_history.json — which clips landed at which relative
                                         position in past long-format videos,
                                         so a future video can enforce the
                                         reuse cap + anti-clump/anti-repeat-
                                         position rules.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import STATE_DIR

logger = logging.getLogger(__name__)

_PARIS = ZoneInfo("Europe/Paris")

_COUNTER_FILE = STATE_DIR / "long_format_counter.json"
_HISTORY_FILE = STATE_DIR / "long_format_clip_history.json"
_HISTORY_MAX_VIDEOS = 50   # capped log, no unbounded growth


# ── Day / video counter ──────────────────────────────────────────────────────

def _load_counter() -> dict:
    if _COUNTER_FILE.exists():
        try:
            return json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"start_date": None, "last_date": None, "videos_today": 0,
            "total_generated": 0, "day_index": 0, "last_run": None}


def _save_counter(data: dict) -> None:
    _COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COUNTER_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def record_generation() -> dict:
    """
    Call once per successfully generated long-format video. Advances the
    persistent counters (rolling videos_today over at midnight Paris time,
    setting start_date on the very first call) and returns the updated state.
    """
    data  = _load_counter()
    today = datetime.now(_PARIS).date()

    if data["start_date"] is None:
        data["start_date"] = today.isoformat()

    if data["last_date"] != today.isoformat():
        data["last_date"]    = today.isoformat()
        data["videos_today"] = 0

    data["videos_today"]    += 1
    data["total_generated"] += 1
    start = datetime.fromisoformat(data["start_date"]).date()
    data["day_index"] = (today - start).days + 1
    data["last_run"]  = datetime.now(_PARIS).isoformat()

    _save_counter(data)
    logger.info(f"long_format counter -> day {data['day_index']}, "
                f"video {data['videos_today']} today, {data['total_generated']} total")
    return data


def get_counter() -> dict:
    return _load_counter()


# ── Clip reuse history ────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    if _HISTORY_FILE.exists():
        try:
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_history(records: list[dict]) -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")


def append_record(video_name: str, clip_placements: list[dict]) -> None:
    """
    clip_placements: [{"name": clip filename, "index": position in the final
    video, "frac": index / (n_clips - 1), clamped to 0.0 for a single-clip video}]
    """
    records = load_history()
    records.append({
        "video": video_name,
        "generated_at": datetime.now(_PARIS).isoformat(),
        "clips": clip_placements,
    })
    _save_history(records[-_HISTORY_MAX_VIDEOS:])


def past_placements(clip_name: str) -> list[dict]:
    """All past {video, frac} placements of this clip name across history, most recent last."""
    out = []
    for rec in load_history():
        for c in rec["clips"]:
            if c["name"] == clip_name:
                out.append({"video": rec["video"], "frac": c["frac"]})
    return out
