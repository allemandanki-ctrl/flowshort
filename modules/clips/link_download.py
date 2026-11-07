"""
Downloads a single source clip straight from a URL (Snapchat Spotlight,
TikTok, IG, YouTube -- anywhere yt-dlp has an extractor) so format18's white
card reaction can take a pasted link directly instead of requiring the user
to download the clip by hand first and upload the file.

Same yt-dlp dependency modules/assembler/character_clip.py and
tools/media/youtube_pipeline.py already rely on -- no new tool, just a new
caller.
"""

import logging
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_S = 90

# Browsers to try pulling cookies from, in order, when a download fails
# because the platform wants a logged-in session (Instagram increasingly
# gates reels/posts behind this -- confirmed 2026-08-10 real failure: "st is
# accessible in your browser without being logged-in... use
# --cookies-from-browser"). Tried in this order, first one that lets the
# download succeed wins; none of these being installed/logged-in just falls
# through to the next.
_COOKIE_BROWSERS = ("chrome", "edge", "brave", "firefox")

# Substrings yt-dlp's own error text uses for this specific failure mode --
# matched case-insensitively so a plain "bad link"/timeout/other real error
# never triggers a pointless cookie retry.
_LOGIN_WALL_MARKERS = ("logged-in", "log in", "login required", "rate-limit reached")


def _run_yt_dlp(url: str, out_template: str, cookies_from_browser: str | None) -> tuple[int, str]:
    cmd = [
        "yt-dlp", "-f", "mp4/bestvideo+bestaudio/best", "--merge-output-format", "mp4",
        "--no-playlist", "-o", out_template, url,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    # Same Windows-specific tree-kill as character_clip.py's _run_yt_dlp --
    # yt-dlp can spawn ffmpeg as a grandchild to merge streams, and a plain
    # subprocess timeout only kills the direct child, leaving the merge
    # orphaned and the pipe open forever.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        _, stderr = proc.communicate(timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError(f"Download timed out after {_TIMEOUT_S}s: {url}")
    return proc.returncode, stderr


def download_clip(url: str, dest_dir: Path) -> Path:
    """
    Downloads `url` into dest_dir as an mp4, returns the resulting path.
    Raises RuntimeError on any failure (bad link, no extractor, timeout,
    empty/corrupt output) -- callers decide how to surface that.

    First try is cookie-free (unchanged behavior for the common case --
    TikTok/YouTube/Snapchat/public Instagram all work anonymously). Only if
    that fails AND the error looks like a login-wall (see
    _LOGIN_WALL_MARKERS) does it retry with real browser cookies, trying
    each of _COOKIE_BROWSERS in turn until one works or all are exhausted.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f"dl_{uuid.uuid4().hex[:10]}"
    out_template = str(dest_dir / f"{stem}.%(ext)s")

    returncode, stderr = _run_yt_dlp(url, out_template, cookies_from_browser=None)

    if returncode != 0 and any(marker in stderr.lower() for marker in _LOGIN_WALL_MARKERS):
        logger.info(f"link_download: {url} needs a logged-in session, retrying with browser cookies...")
        for browser in _COOKIE_BROWSERS:
            returncode, stderr = _run_yt_dlp(url, out_template, cookies_from_browser=browser)
            if returncode == 0:
                logger.info(f"link_download: succeeded using cookies from {browser}")
                break
            logger.warning(f"link_download: cookies from {browser} didn't work ({stderr.strip()[-200:]})")

    if returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {stderr.strip()[-500:]}")

    matches = sorted(dest_dir.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        raise RuntimeError("yt-dlp reported success but no output file was found")

    path = matches[0]
    if path.stat().st_size < 10_000:
        path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file looks empty or corrupt")

    logger.info(f"link_download: {url} -> {path.name} ({path.stat().st_size // 1024} KB)")
    return path
