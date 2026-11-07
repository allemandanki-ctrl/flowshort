"""
Manual publish queue — pulls a fixed per-week quota of batch slots OUT of the
normal auto-schedule (never adds to the total), so the user can post them by
hand with a native trending sound attached. TikTok's Content Posting API and
YouTube's Data API can't attach a platform trending sound themselves -- only
the app's own upload flow can.

Rule recap (see conversation / project spec for the full rationale):
  1. Manual slots are PULLED from auto, the daily/total video count never changes.
  2. Each week is an independent block: its own quota, its own single recap email.
  3. Manual videos render first each week so they're ready before the batch finishes.
  4. A manual video is always format3a/format3b, always hook-free.
"""

import logging
import random

from config import MANUAL_PER_WEEK, MANUAL_ELIGIBLE_FORMATS, TREND_MARKETS

logger = logging.getLogger(__name__)

_DAYS_PER_WEEK = 7


def weeks_for(days: int) -> list[int]:
    """
    Split a day range into successive 7-day week blocks starting at day 1.
    The last block, if incomplete, is shorter (its own independent unit --
    rule n°2 -- not merged into the previous week).
    14 -> [7, 7]. 10 -> [7, 3]. 7 -> [7]. 3 -> [3].
    """
    if days <= 0:
        return []
    weeks: list[int] = []
    remaining = days
    while remaining > 0:
        weeks.append(min(_DAYS_PER_WEEK, remaining))
        remaining -= _DAYS_PER_WEEK
    return weeks


def week_day_ranges(days: int) -> list[tuple[int, int]]:
    """1-indexed inclusive (day_start, day_end) per week block. 14 -> [(1,7),(8,14)]."""
    ranges: list[tuple[int, int]] = []
    d = 1
    for wl in weeks_for(days):
        ranges.append((d, d + wl - 1))
        d += wl
    return ranges


def manual_quota(week_len: int, per_week: int = MANUAL_PER_WEEK) -> int:
    """
    Prorated quota for a week block shorter than 7 days: floor(per_week *
    week_len / 7), never negative. A full 7-day week always gets the full
    per_week quota exactly.
    """
    if week_len >= _DAYS_PER_WEEK:
        return max(0, per_week)
    return max(0, (per_week * week_len) // _DAYS_PER_WEEK)


def assign_manual_routes(
    schedule: list[tuple[str, bool]],
    day_vpd: list[int],
    days: int,
    per_week: int = MANUAL_PER_WEEK,
    eligible_formats: set[str] = MANUAL_ELIGIBLE_FORMATS,
    rng: random.Random | None = None,
) -> tuple[list[str], list[str]]:
    """
    Build the full schedule normally first (`schedule` is already built --
    rule n°1), then decide which of its slots get flipped from "auto" to
    "manual", independently per week (rule n°2).

    day_vpd: videos for each day (len == days), NOT necessarily uniform --
    per-day custom plans (app.py's day_plans) can put a different count on
    each day, including 0. Slot-index ranges per week are derived from this
    instead of a single day_lo/day_hi * vpd multiplication.

    Returns (routes, warnings):
      routes   — one "auto"/"manual" string per slot in `schedule`.
      warnings — human-readable notes for weeks that came up short on
                 eligible slots (never crashes, just takes what's available).
    """
    rng = rng or random
    total = len(schedule)
    routes = ["auto"] * total
    warnings: list[str] = []

    day_starts: list[int] = []
    acc = 0
    for v in day_vpd:
        day_starts.append(acc)
        acc += v

    for week_idx, (day_lo, day_hi) in enumerate(week_day_ranges(days), start=1):
        week_len = day_hi - day_lo + 1
        quota = manual_quota(week_len, per_week)
        if quota <= 0:
            continue

        lo_i = day_starts[day_lo - 1]
        hi_i = min(day_starts[day_hi - 1] + day_vpd[day_hi - 1], total)   # exclusive
        eligible = [i for i in range(lo_i, hi_i) if schedule[i][0] in eligible_formats]

        if len(eligible) < quota:
            warnings.append(
                f"week {week_idx}: only {len(eligible)} eligible slot(s) for a quota of "
                f"{quota} -- using what's available."
            )
        picked = rng.sample(eligible, min(quota, len(eligible)))
        for i in picked:
            routes[i] = "manual"

    return routes, warnings


# ── Trending sound matching (rule n°6) ──────────────────────────────────────

def pick_trending_sound(
    bpm: float | None,
    sounds: list[dict],
    markets: list[str] = TREND_MARKETS,
    exclude_names: set[str] | None = None,
) -> dict | None:
    """
    Pick the trending sound whose bpm is closest to the video's own track bpm.
    `sounds` items are expected to carry {"name", "artist", "market", "bpm"?, "where"?}
    (see modules/music/music_updater.py). Sounds with no recognized/allowed market
    (market not in `markets`, e.g. "FR") are dropped entirely -- never suggested.
    Sounds with no bpm on record sort last (still usable, just not bpm-matched).
    """
    exclude_names = exclude_names or set()
    candidates = [
        s for s in sounds
        if s.get("name") not in exclude_names
        and (s.get("market") or "GLOBAL").upper() in {m.upper() for m in markets}
    ]
    if not candidates:
        return None

    if bpm and bpm > 0:
        def _distance(s: dict) -> float:
            sbpm = s.get("bpm")
            return abs(sbpm - bpm) if sbpm else float("inf")
        candidates.sort(key=_distance)
    return candidates[0]
