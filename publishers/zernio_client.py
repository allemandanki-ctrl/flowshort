"""
Zernio client — TikTok / YouTube / Instagram / Facebook publishing.

Video delivery (verified against https://docs.zernio.com, 2026-07):
  1. POST /v1/media/presign {filename, contentType} -> {uploadUrl, publicUrl, key, expiresIn}
  2. PUT  the raw video bytes to `uploadUrl` (no auth header needed there)
  3. POST /v1/posts with `mediaItems: [{"url": publicUrl, "type": "video"}]`
  Uploads sit in temporary storage that expires after 7 days; once a post
  referencing the publicUrl actually publishes, Zernio copies it to
  permanent storage. Plenty of headroom for a same-day scheduled post.

Auth: Authorization: Bearer <API_KEY> on every Zernio API call (not on the PUT).

Account model: one ZernioClient = one API key = one Zernio workspace. Each
social channel connected in that workspace (TikTok, YouTube, ...) has its
own `accountId`, fetched via `list_accounts()` / GET /v1/accounts — this is
DIFFERENT from the API key itself, same two-level model Postiz used
(org key + per-channel integration id).
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from publishers.base import Publisher, PublishResult

logger = logging.getLogger(__name__)

_API_BASE       = "https://zernio.com/api/v1"
# Sent explicitly on every scheduled post so Zernio's account-level dashboard
# timezone setting (independent of this) can never cause a mismatch -- the
# request is self-contained regardless of what the account is configured to.
# 2026-08-23 (user request): switched Europe/Paris -> America/New_York --
# audience is US/UK-heavy (21.1% US, 16.2% UK, 2.7% Germany, all English-
# language), so posts should be timed off the US Eastern clock, not the
# operator's own Paris time. app.py's _PUBLISH_TZ was updated to match (it's
# what builds the datetime this module converts) -- the two must stay in
# sync, but even if they drifted, _to_naive_schedule_tz_iso's astimezone call would
# still convert to the correct real-world instant; only the label would be
# confusing.
_SCHEDULE_TZ    = "America/New_York"
_MAX_RETRIES    = 3
_BACKOFF_BASE   = 2.0    # seconds: 2, 4, 8
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT        = 120

_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
}
_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
}


class ZernioClient(Publisher):
    """One instance per Zernio account/API key (e.g. one for zernio_a, one for zernio_b)."""

    def __init__(self, api_key: str, api_base: str = _API_BASE):
        if not api_key:
            raise ValueError("Zernio API key is empty — set it in .env")
        self.api_base = api_base.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {api_key}"})

    # -- low-level request with retry --------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.api_base}/{path.lstrip('/')}"
        last_err: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=_TIMEOUT, **kwargs)
            except requests.RequestException as e:
                last_err = e
                logger.warning(f"Zernio {method} {path} network error (try {attempt}/{_MAX_RETRIES}): {e}")
            else:
                if resp.status_code in _RETRY_STATUSES:
                    last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                    logger.warning(f"Zernio {method} {path} -> {resp.status_code} (try {attempt}/{_MAX_RETRIES})")
                elif not resp.ok:
                    raise RuntimeError(f"Zernio {method} {path} -> {resp.status_code}: {resp.text[:300]}")
                else:
                    return resp

            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE ** attempt)

        raise RuntimeError(f"Zernio {method} {path} failed after {_MAX_RETRIES} tries: {last_err}")

    # -- media upload (presigned, no server needed) -------------------------

    def _upload_video(self, video_path: Path) -> str:
        content_type = _CONTENT_TYPES.get(video_path.suffix.lower(), "video/mp4")
        resp = self._request(
            "POST", "media/presign",
            json={"filename": video_path.name, "contentType": content_type},
        )
        data = resp.json()
        upload_url = data["uploadUrl"]
        public_url = data["publicUrl"]

        with open(video_path, "rb") as fh:
            put_resp = requests.put(
                upload_url, data=fh,
                headers={"Content-Type": content_type},
                timeout=_TIMEOUT * 3,
            )
        if not put_resp.ok:
            raise RuntimeError(f"Zernio media PUT failed: {put_resp.status_code}: {put_resp.text[:300]}")

        logger.info(f"Zernio upload OK -> {public_url}")
        return public_url

    def _upload_thumbnail(self, thumb_path: Path) -> str | None:
        content_type = _IMAGE_CONTENT_TYPES.get(thumb_path.suffix.lower())
        if content_type is None:
            logger.warning(f"Zernio: unsupported thumbnail extension {thumb_path.suffix!r}, skipping thumbnail")
            return None
        try:
            resp = self._request(
                "POST", "media/presign",
                json={"filename": thumb_path.name, "contentType": content_type},
            )
            data = resp.json()
            upload_url = data["uploadUrl"]
            public_url = data["publicUrl"]

            with open(thumb_path, "rb") as fh:
                put_resp = requests.put(
                    upload_url, data=fh,
                    headers={"Content-Type": content_type},
                    timeout=_TIMEOUT,
                )
            if not put_resp.ok:
                raise RuntimeError(f"Zernio thumbnail PUT failed: {put_resp.status_code}: {put_resp.text[:300]}")

            logger.info(f"Zernio thumbnail upload OK -> {public_url}")
            return public_url
        except Exception as e:
            logger.warning(f"Zernio thumbnail upload failed ({e}), posting without a thumbnail")
            return None

    # -- account discovery ---------------------------------------------------

    def list_accounts(self) -> list[dict]:
        """GET /v1/accounts -> connected social channels for this workspace."""
        return self._request("GET", "accounts").json().get("accounts", [])

    # -- post-publish verification -------------------------------------------
    # `publish()` below only confirms Zernio ACCEPTED the schedule request
    # (POST /v1/posts -> 2xx). The actual TikTok/YouTube/etc delivery happens
    # later, server-side on Zernio's end, and can still fail there (observed:
    # TikTok's Content Posting API intermittently rejects direct posts with
    # "at capacity" / "Daily active user quota reached" -- a TikTok-side rate
    # limit, not something wrong with our request). That failure never reaches
    # our logs unless something explicitly polls for it afterwards -- hence
    # this method + modules/publish/publish_config.verify_pending_posts, which
    # is what actually calls it. 2026-09-03 incident: two White Card videos
    # showed "created" in app.log but never appeared on TikTok.

    def get_post_status(self, post_id: str) -> dict:
        """GET /v1/posts/{id} -> the full post doc, including each platform's
        real delivery status ("pending" | "published" | "failed") and, on
        failure, errorCategory/errorMessage/errorSource."""
        resp = self._request("GET", f"posts/{post_id}")
        data = resp.json()
        return data.get("post") or data

    def resubmit_platform(
        self,
        post_id: str,
        platform: str,
        *,
        draft: bool = False,
        delay_minutes: int = 5,
    ) -> PublishResult:
        """
        Re-create a post for ONE platform of an existing (usually failed) post,
        reusing its already-uploaded media (no local file needed -- Zernio's
        temp media storage holds it for 7 days) and content. Scheduled a few
        minutes out rather than immediately, since Zernio's API is schedule-
        based (no true "post now" verb) and instant capacity issues rarely
        clear instantly either.

        draft=True targets TikTok's Creator Inbox instead of direct posting
        (`tiktokSettings.draft: true`, per Zernio's own suggested mitigation
        for the "at capacity" error) -- the video lands in the creator's
        TikTok app for a manual one-tap publish instead of being silently
        dropped a second time.
        """
        post = self.get_post_status(post_id)
        entry = next((p for p in post.get("platforms", []) if p.get("platform") == platform), None)
        if entry is None:
            raise ValueError(f"post {post_id} has no '{platform}' platform entry to resubmit")

        media_items = post.get("mediaItems") or []
        if not media_items:
            raise ValueError(f"post {post_id} has no mediaItems to reuse")

        account_id = (entry.get("accountId") or {}).get("_id") or entry.get("accountId")
        content = post.get("content", "")
        title = (entry.get("platformSpecificData") or {}).get("title", "")

        sched = datetime.now(ZoneInfo(_SCHEDULE_TZ)) + timedelta(minutes=delay_minutes)
        sched_str = sched.strftime("%Y-%m-%dT%H:%M:%S")

        platform_entry = {"platform": platform, "accountId": account_id}
        if draft and platform == "tiktok":
            platform_entry["tiktokSettings"] = {"draft": True}
        if title and platform == "youtube":
            platform_entry["platformSpecificData"] = {"title": title[:100]}

        payload = {
            "content": content,
            "scheduledFor": sched_str,
            "timezone": _SCHEDULE_TZ,
            "platforms": [platform_entry],
            "mediaItems": [{"url": media_items[0]["url"], "type": "video"}],
        }

        try:
            resp = self._request("POST", "posts", json=payload)
            data = resp.json()
            new_post_id = (data.get("post") or data).get("_id")
            logger.warning(
                f"Zernio RESUBMITTED {platform} post (was {post_id}, failed) -> new post "
                f"{new_post_id} at {sched_str}{' [draft/Creator Inbox]' if draft else ''}"
            )
            return PublishResult(
                video=media_items[0]["url"].rsplit("/", 1)[-1], platform=platform, scheduled_time=sched_str,
                status="created", detail=f"resubmit of {post_id}" + (" (draft)" if draft else ""), post_id=new_post_id,
            )
        except Exception as e:
            logger.error(f"Zernio resubmit failed for {platform} (original post {post_id}): {e}", exc_info=True)
            return PublishResult(
                video=media_items[0]["url"].rsplit("/", 1)[-1], platform=platform, scheduled_time=sched_str,
                status="error", detail=str(e),
            )

    # -- public API ------------------------------------------------------------

    def publish(
        self,
        video_path: Path,
        caption: str,
        platform: str,
        scheduled_datetime: datetime,
        *,
        title: str = "",
        dry_run: bool = False,
        account_id: str = "",
        thumbnail_path: Path | str | None = None,
        api_platform: str = "",
        **_ignored,
    ) -> PublishResult:
        video_path = Path(video_path)
        sched_str = _to_naive_schedule_tz_iso(scheduled_datetime)
        # api_platform is the real provider-facing platform id (profiles.yaml's
        # `platform:` override, e.g. "youtube" for the "youtube_long" network) --
        # what actually goes in the Zernio API payload below. `platform` itself
        # stays the profiles.yaml network key for PublishResult/logging.
        api_platform = api_platform or platform

        if not account_id:
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_str,
                status="error", detail=f"no accountId configured for platform '{platform}' in profiles.yaml",
            )

        if not video_path.exists():
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_str,
                status="error", detail=f"file not found: {video_path}",
            )

        if dry_run:
            logger.info(f"[DRY-RUN] Zernio would post {video_path.name} -> {platform} ({account_id}) at {sched_str}")
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_str,
                status="dry-run", detail=f"{platform}/{account_id}",
            )

        try:
            public_url = self._upload_video(video_path)

            media_item = {"url": public_url, "type": "video"}
            thumb_path = Path(thumbnail_path) if thumbnail_path else None
            if thumb_path and thumb_path.exists():
                thumb_url = self._upload_thumbnail(thumb_path)
                if thumb_url:
                    media_item["thumbnail"] = thumb_url

            platform_entry = {"platform": api_platform, "accountId": account_id}
            # YouTube reads its video title from platformSpecificData.title, NOT from
            # `content` (docs.zernio.com/platforms/youtube) -- without this, Zernio
            # falls back to the first line of `content` (the caption's CTA line), which
            # is exactly the "Comment WORD..." title youtube_title() was written to avoid.
            # `title` was being computed correctly upstream but silently dropped here.
            if title and api_platform == "youtube":
                platform_entry["platformSpecificData"] = {"title": title[:100]}

            payload = {
                "content": caption,
                "scheduledFor": sched_str,
                "timezone": _SCHEDULE_TZ,
                "platforms": [platform_entry],
                "mediaItems": [media_item],
            }

            resp = self._request("POST", "posts", json=payload)
            data = resp.json()
            post_id = (data.get("post") or data).get("_id")

            logger.info(f"Zernio CREATED post {post_id or ''} -> {api_platform} at {sched_str}")
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_str,
                status="created", detail=f"{platform}/{account_id}", post_id=post_id,
            )

        except Exception as e:
            logger.error(f"Zernio publish failed for {video_path.name} ({platform}): {e}", exc_info=True)
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_str,
                status="error", detail=str(e),
            )


def _to_naive_schedule_tz_iso(dt: datetime) -> str:
    """
    Zernio's scheduledFor is a naive local timestamp paired with an explicit
    timezone field -- we pass timezone=_SCHEDULE_TZ ("America/New_York"), so
    the naive timestamp must represent US-Eastern wall-clock time, not UTC.
    app.py already builds every scheduled datetime as US-Eastern-aware
    (ZoneInfo), so this is a same-zone conversion (no-op on the instant) that
    just strips tzinfo -- but the astimezone call also correctly handles the
    rare case of a caller passing a datetime aware in some other zone (it
    still converts to the right real-world instant, just via a non-trivial
    conversion instead of a no-op).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(_SCHEDULE_TZ)).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
