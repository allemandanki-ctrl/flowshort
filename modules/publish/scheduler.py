import base64
import io
import logging
import random
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from config import RESETWORTH_DIR

logger = logging.getLogger(__name__)

_PARIS = ZoneInfo("Europe/Paris")

_OPTIMAL_HOURS: dict[str, list[int]] = {
    "youtube":   [12, 13, 14, 15, 17, 18, 19],
    "tiktok":    [12, 13, 19, 20, 21, 22],
    "instagram": [10, 11, 12, 13],
    # Added 2026-08-06 alongside the manual/email flow's snapchat+facebook fix
    # (see modules/publish/publisher.py) -- these two were never in this table
    # at all, so get_post_time("snapchat"/"facebook") would have KeyError'd the
    # moment either was actually routed through here. Reasonable defaults, not
    # independently data-tuned like youtube/tiktok/instagram's -- adjust if real
    # posting-time data says otherwise.
    "snapchat":  [12, 13, 19, 20, 21, 22],   # same windows as tiktok -- similar short-form/youth audience
    "facebook":  [10, 11, 12, 13],           # same windows as instagram
}

_RECAP_TO      = "you@example.com"
_DRIVE_ROOT    = "ResetWorth"
_MANUAL_FOLDER = "a_poster_manuellement"

_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]
_TOKEN_PATH   = RESETWORTH_DIR / "state" / "google_drive_gmail_token.json"
_SECRETS_PATH = RESETWORTH_DIR / "state" / "youtube_client_secrets.json"


_OFFSET_MINUTES = [2, 7, 13, 17, 22, 27, 31, 37, 42, 47, 51, 57]


def get_post_time(platform: str, days_ahead: int = 1) -> datetime:
    """Next optimal Paris-timezone post datetime. Minutes are never :00 (avoids congestion)."""
    hour      = random.choice(_OPTIMAL_HOURS[platform])
    minute    = random.choice(_OFFSET_MINUTES)
    candidate = datetime.now(_PARIS) + timedelta(days=days_ahead)

    # Instagram: prefer Tue(1), Wed(2), Thu(3)
    if platform == "instagram":
        for extra in range(7):
            d = candidate + timedelta(days=extra)
            if d.weekday() in (1, 2, 3):
                candidate = d
                break

    return candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)


def get_creds():
    from modules.google_auth import get_credentials
    return get_credentials(_SCOPES, _TOKEN_PATH, _SECRETS_PATH)


def upload_manual_video(
    video_path: Path,
    ig_time: datetime | None,
    tt_time: datetime | None,
    yt_time: datetime | None,
    title: str,
    caption: str,
    hashtags: str,
    music_suggestion: dict | None = None,
    snap_time: datetime | None = None,
    fb_time: datetime | None = None,
    snap_caption: str | None = None,
) -> str:
    """Upload video + info .txt once to ResetWorth/a_poster_manuellement/. Returns web link.

    snap_time/fb_time/snap_caption added 2026-08-06 (see modules/publish/publisher.py's
    manual-flow fix) -- kept as new trailing optional kwargs, not inserted among the
    original 3 positional params, so any existing positional call site (there was one,
    in a section of app.py under unrelated concurrent edits at the time) keeps working
    unchanged. snap_caption is the Snapchat-150-char-truncated version (see
    publishers.ayrshare_client._truncate_caption_for_snapchat) -- shown as its OWN
    block since it's genuinely different text from `caption`, not just a timing note.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

    creds     = get_creds()
    drive     = build("drive", "v3", credentials=creds)
    root_id   = _find_or_create_folder(drive, _DRIVE_ROOT, parent_id=None)
    folder_id = _find_or_create_folder(drive, _MANUAL_FOLDER, parent_id=root_id)

    ref_time  = ig_time or tt_time or yt_time or snap_time or fb_time or datetime.now(_PARIS)
    time_str  = ref_time.strftime("%Y-%m-%d")
    safe_stem = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title)[:40].strip()
    base_name = f"{time_str}_{safe_stem}"

    lines = [
        f"Titre           : {title}",
        "",
        "⚠️  Ajouter la musique à la main sur mobile AVANT de poster.",
        "",
    ]
    if yt_time:
        lines.append(f"YouTube Shorts  : poster le {yt_time.strftime('%A %d/%m/%Y à %Hh%M')} (heure de Paris)")
    if ig_time:
        lines.append(f"Instagram       : poster le {ig_time.strftime('%A %d/%m/%Y à %Hh%M')} (heure de Paris)")
    if tt_time:
        lines.append(f"TikTok          : poster le {tt_time.strftime('%A %d/%m/%Y à %Hh%M')} (heure de Paris)")
    if snap_time:
        lines.append(f"Snapchat        : poster le {snap_time.strftime('%A %d/%m/%Y à %Hh%M')} (heure de Paris)")
    if fb_time:
        lines.append(f"Facebook        : poster le {fb_time.strftime('%A %d/%m/%Y à %Hh%M')} (heure de Paris)")
    lines += [
        "",
        _format_music_txt(music_suggestion),
        "",
        "Description / Caption :",
        caption,
        "",
        "Hashtags :",
        hashtags,
    ]
    if snap_caption:
        lines += [
            "",
            f"Caption Snapchat (≤150 caractères, hashtags inclus -- {len(snap_caption)}/150) :",
            snap_caption,
        ]
    info_text = "\n".join(lines)

    info_meta  = {"name": f"{base_name}_info.txt", "parents": [folder_id]}
    info_media = MediaIoBaseUpload(io.BytesIO(info_text.encode("utf-8")), mimetype="text/plain")
    drive.files().create(body=info_meta, media_body=info_media).execute()

    video_meta  = {"name": f"{base_name}.mp4", "parents": [folder_id]}
    video_media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    f = drive.files().create(
        body=video_meta, media_body=video_media, fields="id,webViewLink"
    ).execute()

    link = f.get("webViewLink", f["id"])
    logger.info(f"Drive manual upload → {link}")
    return link


def send_recap_email(recap: list[dict]) -> None:
    from googleapiclient.discovery import build

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)

    now_str       = datetime.now(_PARIS).strftime("%d/%m/%Y %Hh%M")
    msg           = EmailMessage()
    msg["To"]     = _RECAP_TO
    msg["Subject"]= f"VideoAuto — récap publication {now_str}"
    msg.set_content(_build_html(recap, now_str), subtype="html")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    logger.info(f"Recap email sent to {_RECAP_TO}")


def _build_html(recap: list[dict], now_str: str) -> str:
    rows = ""
    for item in recap:
        title = item.get("title", "—")

        if item["status"] == "manual":
            # Platform label + timing lines built dynamically from whichever
            # *_time entries are actually present (2026-08-06 fix -- this used
            # to hardcode "YouTube + Instagram + TikTok" and silently drop
            # snapchat/facebook from the email no matter what the user checked
            # in the Plateformes selector, see modules/publish/publisher.py).
            _PLATFORM_LABELS = [
                ("yt_time", "▶️", "YouTube Shorts"), ("ig_time", "📸", "Instagram"),
                ("tt_time", "🎵", "TikTok"), ("snap_time", "👻", "Snapchat"),
                ("fb_time", "📘", "Facebook"),
            ]
            present = [(icon, name, item[key]) for key, icon, name in _PLATFORM_LABELS if item.get(key)]
            platform = " + ".join(name for _, name, _ in present) or "?"
            timing = "<br>".join(
                f"{icon} {name} : {t.strftime('%A %d/%m/%Y à %Hh%M')}" for icon, name, t in present
            ) or "—"
            music    = _format_music_html(item.get("music_suggestion"))
            caption_html  = item.get("caption", "").replace("\n", "<br>")
            hashtags_html = item.get("hashtags", "").replace(" ", "&nbsp;")
            snap_caption = item.get("snap_caption")
            snap_block = ""
            if snap_caption:
                snap_caption_html = snap_caption.replace("\n", "<br>")
                snap_block = (
                    f"<br><br><b>Caption Snapchat</b> "
                    f"<span style='font-size:11px;color:#888'>(≤150 caractères, hashtags inclus — "
                    f"{len(snap_caption)}/150)</span><br>"
                    f"<span style='font-size:13px'>{snap_caption_html}</span>"
                )
            action = (
                f"📁 <a href='{item['drive_link']}'>Ouvrir la vidéo dans Drive</a><br>"
                f"<span style='color:#c0392b;font-size:12px'>⚠️ Ajouter la musique à la main (mobile) avant de poster</span><br><br>"
                f"<b>Caption :</b><br><span style='font-size:13px'>{caption_html}</span>{snap_block}<br><br>"
                f"<b>Hashtags :</b><br><span style='font-size:12px;color:#555'>{hashtags_html}</span>"
            )

        else:
            platform  = item.get("platform", "?").capitalize()
            timing    = "—"
            music     = "—"
            action    = f"❌ Erreur : {item.get('error', '?')}"

        rows += f"""
        <tr>
          <td style="padding:10px;border:1px solid #ddd;vertical-align:top"><b>{platform}</b></td>
          <td style="padding:10px;border:1px solid #ddd;vertical-align:top">{title}</td>
          <td style="padding:10px;border:1px solid #ddd;vertical-align:top">{timing}</td>
          <td style="padding:10px;border:1px solid #ddd;vertical-align:top">{music}</td>
          <td style="padding:10px;border:1px solid #ddd;vertical-align:top">{action}</td>
        </tr>"""

    return f"""<html><body style="font-family:sans-serif;color:#222">
<h2 style="color:#1a73e8">VideoAuto — récap publication</h2>
<table style="border-collapse:collapse;width:100%;font-size:14px">
  <thead><tr style="background:#f0f0f0">
    <th style="padding:10px;border:1px solid #ddd">Plateforme</th>
    <th style="padding:10px;border:1px solid #ddd">Titre</th>
    <th style="padding:10px;border:1px solid #ddd">Heure de post</th>
    <th style="padding:10px;border:1px solid #ddd">Musique</th>
    <th style="padding:10px;border:1px solid #ddd">Action / Contenu</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="color:#888;font-size:12px;margin-top:16px">Généré le {now_str}</p>
</body></html>"""


_MUSIC_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "phonk":     ("🔊", "Phonk"),
    "funk":      ("🎵", "Brazilian Funk / Montagem"),
    "cinematic": ("🎬", "Cinématique / Émotionnel"),
    "trending":  ("🔥", "Son tendance"),
}


def _format_music_txt(m: dict | None) -> str:
    if not m:
        return "Musique         : À DÉFINIR (même son sur Instagram, TikTok et YouTube)"
    icon, kind = _MUSIC_TYPE_LABELS.get(m.get("type", ""), ("🎵", ""))
    beat = "\n⚡ Idéal si l'edit est calé sur le beat" if m.get("beat_match") else ""
    return (
        f"Musique         : {m['name']} — {m['artist']}  [{icon} {kind}]{beat}\n"
        f"Où trouver      : {m['where']}"
    )


def _format_music_html(m: dict | None) -> str:
    if not m:
        return "À DÉFINIR"
    icon, kind = _MUSIC_TYPE_LABELS.get(m.get("type", ""), ("🎵", ""))
    beat_note  = (
        "<br><span style='color:#e67e00;font-weight:bold'>⚡ Idéal si l'edit est calé sur le beat</span>"
        if m.get("beat_match") else ""
    )
    return (
        f"<b>{icon} {m['name']}</b> — {m['artist']}<br>"
        f"<span style='color:#666;font-size:12px'>{kind}</span>{beat_note}<br>"
        f"<span style='color:#555;font-size:12px'>{m['where']}</span>"
    )


def _find_or_create_folder(drive, name: str, parent_id: str | None) -> str:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = drive.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files   = results.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    f = drive.files().create(body=meta, fields="id").execute()
    logger.info(f"Drive: created folder '{name}'")
    return f["id"]
