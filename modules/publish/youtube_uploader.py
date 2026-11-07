"""
YouTube uploader — YouTube Data API v3.

First run: opens browser for OAuth2 consent → saves token to state/youtube_token.json.
Subsequent runs: uses saved token (auto-refreshes when expired).

Requires google-api-python-client + google-auth-oauthlib:
  pip install google-api-python-client google-auth-oauthlib
"""

import logging
import time
from datetime import datetime, timezone as tz
from pathlib import Path

from config import BASE_DIR, YOUTUBE_CLIENT_SECRETS, YOUTUBE_TOKEN_FILE

logger = logging.getLogger(__name__)

_SCOPES          = ["https://www.googleapis.com/auth/youtube.upload"]
_CATEGORY_ID     = "26"    # Howto & Style — best for wealth/money content
_CHUNK_SIZE      = 1024 * 1024 * 8   # 8 MB chunks
_MAX_RETRIES     = 5
_RETRY_STATUSES  = {500, 502, 503, 504}


def upload_to_youtube(video_path: Path, script: dict, publish_at: datetime | None = None) -> str:
    """
    Upload video to YouTube. Returns video URL.
    If publish_at is set, uploads as private scheduled — YouTube publishes automatically at that time.
    On first run, a browser window opens for OAuth2 consent.
    """
    creds = _get_or_refresh_credentials()

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=creds)

    from modules.content_meta import generate_meta, build_youtube_description, FIXED_HASHTAGS
    meta        = generate_meta(script)
    title       = meta["title"]
    description = build_youtube_description(meta)
    tags        = [h.lstrip("#") for h in FIXED_HASHTAGS] + ["shorts"]

    body = {
        "snippet": {
            "title":           title[:100],
            "description":     description,
            "tags":            tags,
            "categoryId":      _CATEGORY_ID,
            "defaultLanguage": "en",
        },
        "status": _build_status(publish_at),
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=_CHUNK_SIZE,
        resumable=True,
    )

    logger.info(f"YouTube upload starting | title={title!r}")
    request  = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = _resumable_upload(request)

    video_id  = response["id"]
    video_url = f"https://youtu.be/{video_id}"
    logger.info(f"YouTube upload done | {video_url}")

    return video_url


def _build_status(publish_at: datetime | None) -> dict:
    status: dict = {"selfDeclaredMadeForKids": False, "madeForKids": False}
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"]     = publish_at.astimezone(tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"YouTube: scheduled for {publish_at.strftime('%d/%m/%Y %Hh%M')} Paris")
    else:
        status["privacyStatus"] = "public"
    return status


def _resumable_upload(request) -> dict:
    """Execute a resumable upload with exponential-backoff retry."""
    from googleapiclient.errors import HttpError

    response = None
    error    = None
    retry    = 0

    while response is None:
        try:
            _, response = request.next_chunk()
            if response:
                return response
        except HttpError as e:
            if e.resp.status in _RETRY_STATUSES:
                error = e
            else:
                raise
        except Exception as e:
            error = e

        if retry >= _MAX_RETRIES:
            raise RuntimeError(f"Upload failed after {_MAX_RETRIES} retries: {error}")

        sleep = (2 ** retry) + 1
        logger.warning(f"Upload error (retry {retry + 1}/{_MAX_RETRIES}) — sleeping {sleep}s: {error}")
        time.sleep(sleep)
        retry += 1

    raise RuntimeError("Upload ended without response")


def _get_or_refresh_credentials():
    """Load saved token or run OAuth2 consent flow (opens browser on first run)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path   = BASE_DIR / YOUTUBE_TOKEN_FILE
    secrets_path = BASE_DIR / YOUTUBE_CLIENT_SECRETS

    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info("YouTube token refreshed")
        else:
            if not secrets_path.exists():
                raise FileNotFoundError(
                    f"YouTube client secrets not found: {secrets_path}\n"
                    "Create a project at https://console.cloud.google.com, enable "
                    "YouTube Data API v3, create OAuth2 credentials (Desktop app), "
                    f"and save the JSON to {secrets_path}"
                )
            logger.info("YouTube OAuth2 consent required — browser will open...")
            flow  = InstalledAppFlow.from_client_secrets_file(str(secrets_path), _SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        logger.info(f"YouTube token saved to {token_path}")

    return creds


