from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def get_credentials(scopes: list[str], token_path: Path, secrets_path: Path):
    """Load or refresh OAuth2 credentials. Opens browser on first run."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info(f"Token refreshed: {token_path.name}")
        else:
            if not secrets_path.exists():
                raise FileNotFoundError(f"Client secrets not found: {secrets_path}")
            logger.info(f"OAuth2 consent required — browser will open ({token_path.name})")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), scopes)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        logger.info(f"Token saved: {token_path}")

    return creds
