"""
Ayrshare client — Snapchat publishing (Spotlight by default).

Video delivery (verified against https://www.ayrshare.com/docs, 2026-07):
  Ayrshare's /post endpoint needs a PUBLIC https URL in `mediaUrls`, it does
  not accept a raw file upload. There's no local server to host the file on
  (the VPS is being decommissioned), so we use Ayrshare's own large-media
  presigned-upload flow instead:
    1. GET  /api/media/uploadUrl?fileName=...&contentType=...  -> {uploadUrl, accessUrl}
    2. PUT  the raw video bytes to `uploadUrl` (no auth header needed there)
    3. POST /api/post with `accessUrl` in `mediaUrls`
  accessUrl stays valid 30 days — plenty for a scheduled post to fire.

Auth: Authorization: Bearer <API_KEY> on every Ayrshare API call (not on the PUT).
"""

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from publishers.base import Publisher, PublishResult

logger = logging.getLogger(__name__)

_API_BASE       = "https://api.ayrshare.com/api"
_MAX_RETRIES    = 3
_BACKOFF_BASE   = 2.0    # seconds: 2, 4, 8
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_TIMEOUT        = 120

_CONTENT_TYPES = {
    ".mp4": "mp4", ".mov": "mov", ".webm": "webm",
}

# Snapchat caps post text at 150 chars, hashtags included. Ayrshare doesn't
# truncate on our behalf -- an over-length caption gets rejected or silently
# loses its hashtags off the end. This client is the ONLY Snapchat code path
# (see profiles.yaml: snapchat -> ayrshare_main, every other platform routes
# through publishers/zernio_client.py instead) -- so gating this here, rather
# than in the shared caption builder, cannot affect TikTok/YouTube/Instagram.
_SNAPCHAT_MAX_CAPTION = 150
_HASHTAG_RE = re.compile(r"#\w+")


_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")


def _trim_to_sentence_boundary(text: str, max_len: int) -> str | None:
    """
    Cut `text` at the last complete-sentence end (., !, or ?) that fits
    within max_len -- e.g. keeps the CTA line whole and drops the rest of
    the body instead of severing mid-thought ("...keeps you #hashtag",
    found on a real published Snapchat post, 2026-08-09: never write that
    again). Returns None if no sentence end fits at all within budget, so
    the caller can fall back to _trim_to_word_boundary.
    """
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(text) if m.end() <= max_len]
    if not ends:
        return None
    return text[:ends[-1]].rstrip()


def _trim_to_word_boundary(text: str, max_len: int) -> str:
    """
    Cut `text` to at most max_len chars without breaking mid-word -- backs up
    to the previous whitespace (space or newline) when the natural cut point
    lands inside a word, instead of leaving a broken fragment like "wea"
    where the source said "wealthy" (found on a real published Snapchat
    post, 2026-08-09). Falls back to the hard cut only if there's no
    whitespace at all within the budget (one very long unbroken "word").
    """
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    if text[max_len:max_len + 1] and not text[max_len].isspace():
        last_ws = max(truncated.rfind(" "), truncated.rfind("\n"))
        if last_ws > 0:
            truncated = truncated[:last_ws]
    return truncated.rstrip()


_SNAPCHAT_HASHTAG_FLOOR = 3   # never drop below this many, even to make room for body copy


def _cut_body(body_full: str, budget: int) -> str:
    """
    Maximizes how much of the body actually shows within `budget`: a
    word-boundary cut (never severs a word mid-way) always gets AT LEAST as
    much content on screen as a sentence-boundary cut, often much more --
    e.g. a body whose first sentence (the CTA) is short but whose second
    sentence (the real description) runs long would sentence-cut down to
    just the CTA, wasting most of the budget. Only prefer the cleaner
    sentence-boundary stop when it isn't leaving much of the budget unused
    (2026-08-10, explicit user ask: "coupe à la phrase et peut-être au mot,
    je m'en fous" -- a partial extra sentence beats an empty one).
    """
    if len(body_full) <= budget:
        return body_full
    word_cut = _trim_to_word_boundary(body_full, max(0, budget))
    sentence_cut = _trim_to_sentence_boundary(body_full, max(0, budget))
    if sentence_cut is not None and len(sentence_cut) >= budget * 0.8:
        return sentence_cut
    return word_cut


def _truncate_caption_for_snapchat(caption: str, max_len: int = _SNAPCHAT_MAX_CAPTION) -> str:
    """
    Trim `caption` to max_len chars. Hashtags are the LAST resort to cut, not
    the first: the CTA line + hashtags alone routinely eat ~100 of the 150
    chars, leaving no room at all for the video's actual description --
    exactly the "no real sentence, just CTA+hashtags" gap a real published
    post showed (2026-08-10). So this tries the full hashtag set first, and
    only shrinks it (from the FRONT of the list -- _pick_hashtags appends the
    brand's 2 stable/broad tags last, see its docstring, so keeping the tail
    keeps those longest) down to _SNAPCHAT_HASHTAG_FLOOR if that's what it
    takes to show real body content beyond the bare CTA sentence -- never
    fewer than the floor, per explicit user ask ("mets le maximum mais faut
    au moins 3"). Stops at the largest hashtag count that still shows real
    content; if even the floor can't, falls back to the floor's own best
    effort (whatever fits, same as the old CTA-only behavior, just never
    below 3 tags).
    """
    if len(caption) <= max_len:
        return caption

    hashtags = _HASHTAG_RE.findall(caption)
    body_full = _HASHTAG_RE.sub("", caption)
    body_full = re.sub(r"[ \t]+", " ", body_full).strip()

    # How much of body_full is JUST the CTA (its first sentence) -- "real
    # content" below means the cut body reaches meaningfully past this, not
    # that it contains a full second sentence (which a long description
    # would rarely fit regardless of hashtag count).
    first_sentence_end = 0
    m = _SENTENCE_END_RE.search(body_full)
    if m:
        first_sentence_end = m.end()

    floor = min(_SNAPCHAT_HASHTAG_FLOOR, len(hashtags))
    fallback_result = None
    for kept_count in range(len(hashtags), floor - 1, -1):
        kept_tags = hashtags[len(hashtags) - kept_count:]   # keep the TAIL -- see docstring
        hashtags_str = " ".join(kept_tags)
        budget = max_len - len(hashtags_str) - (1 if hashtags_str else 0)
        if budget < 0:
            continue   # even this many tags alone don't fit -- try fewer

        body = _cut_body(body_full, budget)
        result = f"{body} {hashtags_str}".strip() if hashtags_str else body
        fallback_result = result   # keeps updating -- ends on the floor's own best effort if nothing "real" is found

        if len(body) > first_sentence_end + 10:
            return result

    if fallback_result is not None:
        return fallback_result

    # Nothing fit even with zero hashtags (caption's own first sentence alone
    # exceeds max_len) -- should be effectively unreachable, but a Snapchat
    # post must never leave this function over the limit.
    logger.warning(f"snapchat caption: could not fit any hashtags within {max_len} chars, hard-cutting.")
    result = _cut_body(body_full, max_len)

    if len(result) > max_len:
        logger.warning(
            f"snapchat caption: truncation still produced {len(result)} chars "
            f"(> {max_len}); hard-cutting as a last resort."
        )
        result = _trim_to_word_boundary(result, max_len)

    return result


class AyrshareClient(Publisher):
    def __init__(self, api_key: str, api_base: str = _API_BASE):
        if not api_key:
            raise ValueError("Ayrshare API key is empty — set AYRSHARE_API_KEY in .env")
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
                logger.warning(f"Ayrshare {method} {path} network error (try {attempt}/{_MAX_RETRIES}): {e}")
            else:
                if resp.status_code in _RETRY_STATUSES:
                    last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                    logger.warning(f"Ayrshare {method} {path} -> {resp.status_code} (try {attempt}/{_MAX_RETRIES})")
                elif not resp.ok:
                    raise RuntimeError(f"Ayrshare {method} {path} -> {resp.status_code}: {resp.text[:300]}")
                else:
                    return resp

            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE ** attempt)

        raise RuntimeError(f"Ayrshare {method} {path} failed after {_MAX_RETRIES} tries: {last_err}")

    # -- media upload (presigned, no server needed) -------------------------

    def _upload_video(self, video_path: Path) -> str:
        content_type = _CONTENT_TYPES.get(video_path.suffix.lower(), "mp4")
        resp = self._request(
            "GET", "media/uploadUrl",
            params={"fileName": video_path.name, "contentType": content_type},
        )
        data = resp.json()
        upload_url = data["uploadUrl"]
        access_url = data["accessUrl"]

        with open(video_path, "rb") as fh:
            put_resp = requests.put(
                upload_url,
                data=fh,
                headers={"Content-Type": data.get("contentType", f"video/{content_type}")},
                timeout=_TIMEOUT * 3,   # large files: allow more time than API calls
            )
        if not put_resp.ok:
            raise RuntimeError(f"Ayrshare media PUT failed: {put_resp.status_code}: {put_resp.text[:300]}")

        logger.info(f"Ayrshare upload OK -> {access_url}")
        return access_url

    # -- post management (maintenance scripts, e.g. fix_snapchat_captions.py) --

    def get_post(self, post_id: str) -> dict:
        """Fetch a single post's current data by id (GET /post/{id})."""
        resp = self._request("GET", f"post/{post_id}")
        return resp.json()

    def update_post(self, post_id: str, caption: str) -> dict:
        """
        Edit an already-scheduled (not-yet-published) post's text in place.
        PATCH /post with the target id in the JSON body -- NOT /post/{id},
        that 404s (confirmed against the live account 2026-07-28: "This
        endpoint does not exist").
        """
        resp = self._request("PATCH", "post", json={"id": post_id, "post": caption})
        return resp.json()

    def delete_post(self, post_id: str) -> dict:
        """
        Cancel a scheduled (not-yet-published) post -- DELETE /post with the
        target id in the JSON body (same body-not-URL convention as
        update_post above; per Ayrshare docs a still-`pending` post is
        removed and never sent to the network).
        """
        resp = self._request("DELETE", "post", json={"id": post_id})
        return resp.json()

    # -- public API ----------------------------------------------------------

    def publish(
        self,
        video_path: Path,
        caption: str,
        platform: str,
        scheduled_datetime: datetime,
        *,
        title: str = "",
        dry_run: bool = False,
        format: str = "spotlight",   # "spotlight" | "story" | "saved_story"
        **_ignored,
    ) -> PublishResult:
        video_path = Path(video_path)
        sched_iso = _to_utc_iso(scheduled_datetime)
        caption = _truncate_caption_for_snapchat(caption)

        if not video_path.exists():
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_iso,
                status="error", detail=f"file not found: {video_path}",
            )

        if dry_run:
            logger.info(f"[DRY-RUN] Ayrshare would post {video_path.name} -> snapchat ({format}) at {sched_iso}")
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_iso,
                status="dry-run", detail=f"snapchat/{format}",
            )

        try:
            access_url = self._upload_video(video_path)

            snap_options: dict = {}
            if format == "spotlight":
                snap_options = {"spotlight": True}
            elif format == "saved_story":
                snap_options = {"savedStory": True}
            # format == "story" -> no option needed, it's the Ayrshare default

            payload = {
                "post": caption,
                "platforms": ["snapchat"],
                "mediaUrls": [access_url],
                "isVideo": True,
                "scheduleDate": sched_iso,
            }
            if snap_options:
                payload["snapChatOptions"] = snap_options

            resp = self._request("POST", "post", json=payload)
            data = resp.json()
            post_id = data.get("id")

            logger.info(f"Ayrshare CREATED post {post_id or ''} -> snapchat/{format} at {sched_iso}")
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_iso,
                status="created", detail=f"snapchat/{format}", post_id=post_id,
            )

        except Exception as e:
            logger.error(f"Ayrshare publish failed for {video_path.name}: {e}", exc_info=True)
            return PublishResult(
                video=video_path.name, platform=platform, scheduled_time=sched_iso,
                status="error", detail=str(e),
            )


def _to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
