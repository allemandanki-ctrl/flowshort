"""
Test Google Drive + Gmail connection.

Run once to authorize:
  python tests/test_drive_gmail.py

Browser opens → authorize → token saved to users/resetworth/state/google_drive_gmail_token.json
"""

import sys
import tempfile
import base64
import logging
from email.message import EmailMessage
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

BASE_DIR       = Path(__file__).parent.parent
RESETWORTH_DIR = BASE_DIR / "users" / "resetworth"
SECRETS_PATH   = RESETWORTH_DIR / "state" / "youtube_client_secrets.json"
TOKEN_PATH     = RESETWORTH_DIR / "state" / "google_drive_gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    sys.path.insert(0, str(BASE_DIR))
    from modules.google_auth import get_credentials

    print("Connecting to Google (Drive + Gmail)...")
    creds = get_credentials(SCOPES, TOKEN_PATH, SECRETS_PATH)
    print(f"✓ Token saved → {TOKEN_PATH}\n")

    _test_drive(creds)
    _test_gmail(creds)


def _test_drive(creds) -> None:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    print("── Drive upload test ──")
    drive = build("drive", "v3", credentials=creds)

    tmp = Path(tempfile.mktemp(suffix=".txt"))
    tmp.write_text("Test upload from VideoAuto robot", encoding="utf-8")

    try:
        meta  = {"name": "robot_test_upload.txt"}
        media = MediaFileUpload(str(tmp), mimetype="text/plain")
        f     = drive.files().create(body=meta, media_body=media, fields="id,name,webViewLink").execute()
        print(f"✓ Upload OK | {f.get('webViewLink', f['id'])}\n")
    finally:
        tmp.unlink(missing_ok=True)


def _test_gmail(creds) -> None:
    from googleapiclient.discovery import build

    print("── Gmail send test ──")
    gmail    = build("gmail", "v1", credentials=creds)
    profile  = gmail.users().getProfile(userId="me").execute()
    my_email = profile["emailAddress"]

    msg = EmailMessage()
    msg["To"]      = my_email
    msg["From"]    = my_email
    msg["Subject"] = "VideoAuto — test connexion Google"
    msg.set_content(
        "Si tu reçois ce mail, Drive + Gmail sont bien connectés au robot VideoAuto."
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✓ Mail envoyé à {my_email}\n")
    print("Test complet ✓")


if __name__ == "__main__":
    main()
