"""
Publish config — loads providers/platforms from profiles.yaml and dispatches
a video to the right Ayrshare/Zernio client per platform.

Replaces modules/postiz_scheduler.py. No self-hosted server involved anymore:
each publisher client uploads media straight to Ayrshare/Zernio's own hosting
and schedules the post server-side on their end (fire and forget).

profiles.yaml shape:
    providers:
      ayrshare_main: {type: ayrshare, api_key_env: AYRSHARE_API_KEY}
      zernio_a:      {type: zernio,   api_key_env: ZERNIO_A_API_KEY}
    platforms:
      snapchat:  {provider: ayrshare_main, format: spotlight}
      tiktok:    {provider: zernio_a, account_id: "acc_..."}

Secrets are NEVER stored in code or profiles.yaml — only env-var *names* live there.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import BASE_DIR, PUBLISH_PROFILES_FILE, RESETWORTH_DIR
from publishers.ayrshare_client import AyrshareClient
from publishers.base import Publisher, PublishResult
from publishers.zernio_client import ZernioClient

logger = logging.getLogger(__name__)

_SCHEDULE_LOG = BASE_DIR / "state" / "publish_schedule_log.json"


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str            # "ayrshare" | "zernio"
    api_key_env: str


@dataclass(frozen=True)
class PlatformConfig:
    platform: str
    provider: str
    account_id: str = ""     # required for zernio platforms, unused for ayrshare
    format: str = ""         # e.g. ayrshare snapchat: spotlight | story | saved_story


@dataclass(frozen=True)
class PublishConfig:
    providers: dict[str, ProviderConfig]
    platforms: dict[str, PlatformConfig]


@dataclass
class AggregateResult:
    """One row per video for app.py's batch summary — mirrors the old
    Postiz ScheduleResult shape so the calling code needs minimal changes."""
    video: str
    scheduled_time: str
    networks: list[str]
    status: str                # "created" | "skipped" | "dry-run" | "error" | "partial"
    detail: str = ""
    results: list[PublishResult] = field(default_factory=list)


# ── Loading ───────────────────────────────────────────────────────────────────

def load_publish_config(profiles_file: str | Path | None = None) -> PublishConfig:
    path = Path(profiles_file) if profiles_file else RESETWORTH_DIR / PUBLISH_PROFILES_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"Profiles file not found: {path}\n"
            "Copy profiles.example.yaml -> profiles.yaml and fill in your API keys."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    providers = {
        name: ProviderConfig(name=name, type=cfg["type"], api_key_env=cfg["api_key_env"])
        for name, cfg in (raw.get("providers") or {}).items()
    }
    platforms = {
        name: PlatformConfig(
            platform=str(cfg.get("platform", name)),
            provider=cfg["provider"],
            account_id=str(cfg.get("account_id", "")),
            format=str(cfg.get("format", "")),
        )
        for name, cfg in (raw.get("platforms") or {}).items()
    }
    if not providers or not platforms:
        raise ValueError(f"No providers/platforms defined in {path}")
    return PublishConfig(providers=providers, platforms=platforms)


# ── Client cache (one instance per provider, reused across videos) ───────────

_CLIENTS: dict[str, Publisher] = {}


def get_client(provider_name: str, cfg: PublishConfig) -> Publisher:
    if provider_name in _CLIENTS:
        return _CLIENTS[provider_name]

    provider = cfg.providers.get(provider_name)
    if provider is None:
        raise ValueError(f"Unknown provider '{provider_name}' referenced in profiles.yaml")

    api_key = os.getenv(provider.api_key_env, "")
    if not api_key:
        raise ValueError(
            f"Provider '{provider_name}': env var {provider.api_key_env} is empty. "
            "Add the API key to your .env."
        )

    if provider.type == "ayrshare":
        client: Publisher = AyrshareClient(api_key)
    elif provider.type == "zernio":
        client = ZernioClient(api_key)
    else:
        raise ValueError(f"Unknown provider type '{provider.type}' for '{provider_name}'")

    _CLIENTS[provider_name] = client
    return client


# ── Dedup log (avoid double-posting the same video/platform/time on rerun) ──

def _dedup_key(video_path: Path, platforms: list[str], scheduled_time: datetime) -> str:
    nets = ",".join(sorted(p.lower() for p in platforms))
    return f"{video_path.name}|{scheduled_time.strftime('%Y-%m-%dT%H:%M')}|{nets}"


def _load_log() -> dict:
    if _SCHEDULE_LOG.exists():
        try:
            return json.loads(_SCHEDULE_LOG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_log(log: dict) -> None:
    _SCHEDULE_LOG.parent.mkdir(parents=True, exist_ok=True)
    _SCHEDULE_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def _already_scheduled(key: str) -> bool:
    return key in _load_log()


def _record_scheduled(
    key: str, result: AggregateResult, *,
    platform_providers: dict[str, str] | None = None, profiles_file: str = "",
) -> None:
    log = _load_log()
    platform_providers = platform_providers or {}
    log[key] = {
        "at":            datetime.now(timezone.utc).isoformat(),
        "video":         result.video,
        "networks":      result.networks,
        "post_ids":      [r.post_id for r in result.results if r.post_id],   # legacy, kept for older readers
        "scheduled_for": result.scheduled_time,   # ISO UTC -- when to expect the real platform delivery
        "profiles_file": profiles_file,           # which brand's profiles.yaml -> which provider/keys to re-check with
        "platform_posts": [
            {"platform": r.platform, "provider": platform_providers.get(r.platform, ""), "post_id": r.post_id}
            for r in result.results if r.post_id
        ],
        "verified": False,
    }
    _save_log(log)


# ── Public API ────────────────────────────────────────────────────────────────

def schedule_video(
    video_path: str | Path,
    platforms: list[str],
    scheduled_time: datetime,
    caption: str = "",
    *,
    title: str = "",
    dry_run: bool = False,
    thumbnail_path: str | Path | None = None,
    cfg: PublishConfig | None = None,
    route: str | None = None,
    profiles_file: str | Path | None = None,
) -> AggregateResult:
    """
    Schedule ONE video across one or more platforms. Each platform is
    dispatched to whichever provider (Ayrshare/Zernio) profiles.yaml maps it
    to — a single video going to tiktok+youtube+instagram+facebook+snapchat
    fans out to 3 different provider accounts (zernio_a, zernio_b, ayrshare_main).

    route: pass the video's script["route"] through here (see modules/publish/manual_queue.py).
    "manual" videos are posted by hand with a trending sound attached -- this
    is the lowest-level function that actually talks to Zernio/Ayrshare, so
    the refusal lives HERE, not just in the batch orchestration that normally
    never calls this for a manual slot in the first place. A script reloaded
    from disk later by an unrelated publish action must still be caught.
    """
    video_path = Path(video_path)
    cfg = cfg or load_publish_config()

    if not platforms:
        raise ValueError("No platforms given to schedule_video")

    if route == "manual":
        logger.warning(
            f"schedule_video: REFUSED — {video_path.name} is routed 'manual' "
            "(posted by hand with a trending sound), it must never auto-publish."
        )
        return AggregateResult(
            video=video_path.name, scheduled_time=_iso(scheduled_time),
            networks=platforms, status="skipped", detail="manual route -- auto-publish refused",
        )

    key = _dedup_key(video_path, platforms, scheduled_time)
    if not dry_run and _already_scheduled(key):
        logger.info(f"SKIP (already scheduled): {key}")
        return AggregateResult(
            video=video_path.name, scheduled_time=_iso(scheduled_time),
            networks=platforms, status="skipped", detail="duplicate",
        )

    if not video_path.exists():
        return AggregateResult(
            video=video_path.name, scheduled_time=_iso(scheduled_time),
            networks=platforms, status="error", detail=f"file not found: {video_path}",
        )

    results: list[PublishResult] = []
    platform_providers: dict[str, str] = {}
    for platform in platforms:
        pconf = cfg.platforms.get(platform)
        if pconf is None:
            results.append(PublishResult(
                video=video_path.name, platform=platform, scheduled_time=_iso(scheduled_time),
                status="error", detail=f"platform '{platform}' not configured in profiles.yaml",
            ))
            continue
        platform_providers[platform] = pconf.provider
        try:
            client = get_client(pconf.provider, cfg)
        except ValueError as e:
            results.append(PublishResult(
                video=video_path.name, platform=platform, scheduled_time=_iso(scheduled_time),
                status="error", detail=str(e),
            ))
            continue

        kwargs = {}
        if pconf.format:
            kwargs["format"] = pconf.format
        if pconf.account_id:
            kwargs["account_id"] = pconf.account_id
        # pconf.platform is the REAL provider-facing platform id (e.g. "youtube"),
        # which can differ from the profiles.yaml network key used everywhere else
        # for bookkeeping/dedup/UI labels (e.g. "youtube_long" for the long-form
        # network sharing the youtube account). See profiles.yaml's `platform:` field.
        if pconf.platform and pconf.platform != platform:
            kwargs["api_platform"] = pconf.platform

        res = client.publish(
            video_path, caption, platform, scheduled_time,
            title=title, dry_run=dry_run, thumbnail_path=thumbnail_path, **kwargs,
        )
        results.append(res)

    statuses = {r.status for r in results}
    if statuses == {"created"}:
        status = "created"
    elif statuses == {"dry-run"}:
        status = "dry-run"
    elif statuses == {"error"}:
        status = "error"
    else:
        status = "partial"

    detail = "; ".join(f"{r.platform}={r.status}" + (f" ({r.detail})" if r.status == "error" else "") for r in results)
    agg = AggregateResult(
        video=video_path.name, scheduled_time=_iso(scheduled_time),
        networks=[r.platform for r in results], status=status, detail=detail, results=results,
    )

    if not dry_run and status in ("created", "partial"):
        _record_scheduled(
            key, agg, platform_providers=platform_providers,
            profiles_file=str(profiles_file) if profiles_file else "",
        )

    logger.info(f"SCHEDULED {video_path.name} -> {agg.networks} [{status}] at {agg.scheduled_time}")
    return agg


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── Post-publish verification ────────────────────────────────────────────────
# schedule_video()/client.publish() only confirm the provider ACCEPTED the
# schedule request. The real delivery happens later, provider-side, and can
# still fail there with zero signal back to us (2026-09-03 incident: two
# White Card videos logged "created" but never appeared on TikTok -- Zernio's
# TikTok delivery failed afterwards with "at capacity" / "Daily active user
# quota reached", a TikTok-side rate limit). This walks the schedule log,
# checks each not-yet-verified entry whose scheduled time has passed against
# the provider's real per-platform status, and on a TikTok capacity/account
# failure auto-resubmits to TikTok's Creator Inbox (draft) as a fallback so
# the video isn't silently lost a second time -- it just needs a manual tap
# to publish in the TikTok app instead.

_RETRIABLE_ERROR_CATEGORIES = {"account_issue", "rate_limit", "capacity"}


def verify_pending_posts(min_age_minutes: int = 20, *, auto_retry_tiktok: bool = True) -> list[dict]:
    """
    Check real delivery status for every schedule-log entry whose scheduled
    time is more than `min_age_minutes` in the past and hasn't been verified
    yet. Returns a list of {video, platform, status, detail} dicts for
    anything that did NOT come back "published" -- callers (app.py's
    background thread, a CLI) log these loudly. Marks entries verified in
    the log either way so a rerun doesn't re-check them.
    """
    from datetime import datetime as _dt

    log = _load_log()
    now = _dt.now(timezone.utc)
    problems: list[dict] = []
    changed = False

    for key, entry in log.items():
        if entry.get("verified"):
            continue
        sched_for = entry.get("scheduled_for")
        platform_posts = entry.get("platform_posts")
        if not sched_for or not platform_posts:
            continue  # legacy entry recorded before this field existed -- can't verify, leave alone
        try:
            sched_dt = _dt.strptime(sched_for, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now - sched_dt < timedelta(minutes=min_age_minutes):
            continue  # too soon -- the provider may not have attempted delivery yet

        try:
            cfg = load_publish_config(entry.get("profiles_file") or None)
        except Exception as e:
            logger.warning(f"verify_pending_posts: can't load profiles for {key}: {e}")
            continue

        entry_statuses = {}
        all_resolved = True
        for pp in platform_posts:
            platform, provider_name, post_id = pp["platform"], pp["provider"], pp["post_id"]
            provider = cfg.providers.get(provider_name)
            if provider is None or provider.type != "zernio":
                continue  # only Zernio exposes a status-check endpoint today
            try:
                client = get_client(provider_name, cfg)
                post = client.get_post_status(post_id)
                pentry = next((p for p in post.get("platforms", []) if p.get("platform") == platform), None)
                if pentry is None:
                    continue
                pstatus = pentry.get("status", "unknown")
                entry_statuses[platform] = pstatus
                if pstatus == "pending":
                    all_resolved = False
                elif pstatus == "failed":
                    err_msg = pentry.get("errorMessage", "")
                    err_cat = pentry.get("errorCategory", "")
                    detail = {
                        "video": entry.get("video", ""), "platform": platform,
                        "status": "failed", "detail": f"{err_cat}: {err_msg}",
                    }
                    if (
                        auto_retry_tiktok and platform == "tiktok"
                        and err_cat in _RETRIABLE_ERROR_CATEGORIES
                    ):
                        retry = client.resubmit_platform(post_id, platform, draft=True)
                        detail["retry"] = f"resubmitted as TikTok draft/Creator Inbox -> post {retry.post_id}" \
                            if retry.status == "created" else f"resubmit also failed: {retry.detail}"
                    problems.append(detail)
            except Exception as e:
                logger.warning(f"verify_pending_posts: status check failed for {platform}/{post_id}: {e}")
                all_resolved = False

        if entry_statuses:
            entry["platform_status"] = entry_statuses
        if all_resolved:
            entry["verified"] = True
        changed = True

    if changed:
        _save_log(log)
    return problems
