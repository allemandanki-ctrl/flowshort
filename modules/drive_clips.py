"""
Drive clips sourcing - pulls real clips from public Google Drive folders
(links listed in a PDF) and builds visually coherent groups for the
FlowShort render pipeline (format3's mixed image/video pool and friends).

Flow:
  1. ingest_pdf()        : PDF -> state/drive_folders.json  (folder id + tag)
  2. select + staging    : list folders (metadata only, no bulk download),
                           exclude clips used < 50 days ago (used_clips.json),
                           download a small candidate set to C:/temp/flowshort_staging
                           (hard cap 1 GB present simultaneously)
  3. analyze             : ffprobe duration per clip (no Claude call)
  4. group               : grouped by Drive folder tag (free coherence proxy --
                           clips from the same themed folder read as one montage);
                           group >= 20 s, ordered
  5. handoff             : chosen clips copied to the session dir; staging wiped
  6. commit_usage()      : after a successful render, used_clips.json updated

Folders are read via the authenticated Google Drive API v3 (OAuth, read-only
scope) -- NOT gdown's anonymous public-link scraping. Google actively blocks/
throttles that anonymous path ("many accesses" / permission errors) regardless
of how many files a folder actually has; the authenticated API has its own,
much more generous quota and works as long as the signed-in account can see
the file (shared with it, or "anyone with the link").
Dropbox / Telegram links in the PDF are logged and skipped.
"""

import io
import json
import logging
import queue
import random
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import BASE_DIR, STATE_DIR, RESETWORTH_DIR

logger = logging.getLogger(__name__)


class DriveAuthError(RuntimeError):
    """
    Raised when Google Drive rejects the request for an auth reason (expired/
    revoked refresh token, 401/403) rather than a per-folder problem. Aborts
    staging immediately instead of silently exhausting all 22 folders one by
    one -- when auth is broken every single folder fails identically, so
    there's nothing to learn by trying the rest.
    """


_DRIVE_SCOPES  = ["https://www.googleapis.com/auth/drive.readonly"]
_TOKEN_PATH    = STATE_DIR / "drive_readonly_token.json"
# Same OAuth client (Google Cloud project) already used for YouTube/manual-post --
# only the requested scope + token file differ, so this needs its own one-time
# browser consent but no new API/project setup.
_SECRETS_PATH  = RESETWORTH_DIR / "state" / "youtube_client_secrets.json"

_drive_service = None


def _get_drive_service():
    global _drive_service
    if _drive_service is None:
        from modules.google_auth import get_credentials
        from googleapiclient.discovery import build
        creds = get_credentials(_DRIVE_SCOPES, _TOKEN_PATH, _SECRETS_PATH)
        _drive_service = build("drive", "v3", credentials=creds)
    return _drive_service

def _safe_log_name(name: str) -> str:
    """ASCII-only version of a filename for log lines -- Drive filenames can
    contain emoji/unicode that crash Windows' default cp1252 console encoder.
    Only affects the log message; the real filename/path is untouched."""
    return name.encode("ascii", "replace").decode("ascii")


_FOLDERS_FILE  = STATE_DIR / "drive_folders.json"
_USED_FILE     = STATE_DIR / "used_clips.json"
_STAGING_DIR   = Path("C:/temp/flowshort_staging")
_STAGING_CAP   = 1_000_000_000          # 1 GB hard cap
_REUSE_DAYS    = 50
_VIDEO_EXTS    = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
_IMAGE_EXTS    = (".jpg", ".jpeg", ".png", ".webp")
_MAX_CANDIDATES = 36                    # clips downloaded per run -- was 12, causing
                                         # visible repetition (16-cut fast montage with
                                         # only ~5 distinct clips after grouping). The
                                         # real library has thousands of clips per
                                         # folder; this cap was never about scarcity.
                                         # Bumped again once cuts moved to every single
                                         # beat (was every 2nd) -- ~30 cuts now need
                                         # ~30 distinct clips to avoid repeats.
_FOLDERS_PER_RUN = 5                    # themes mixed per run
_MIN_GROUP_SECONDS = 20.0
_MAX_FILE_BYTES = 200_000_000           # skip single files > 200 MB
_LIST_TIMEOUT_S = 25                    # hard cap on listing one folder
_DOWNLOAD_TIMEOUT_S = 45                # hard cap on downloading one file
# Some public folders are quota-throttled (many users hitting the same free
# link) and the underlying gdown/requests calls can stall with a TCP
# connection open but no data ever arriving -- no built-in timeout catches
# that. Every such call is run in a daemon thread with a hard wall-clock
# timeout; on timeout we abandon the thread (it dies with the process) and
# move on instead of blocking the whole pipeline.


def _with_timeout(fn, timeout: float, *args, **kwargs):
    """
    Run fn(*args, **kwargs) with a hard wall-clock timeout. Raises TimeoutError.
    Uses a plain daemon Thread (NOT ThreadPoolExecutor -- its worker threads are
    joined via an atexit hook, so an abandoned stalled call would still freeze
    process exit). A daemon thread is killed outright when the process ends, so
    a stalled network call can never block this call or a later clean exit.
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _runner():
        try:
            q.put(("ok", fn(*args, **kwargs)))
        except Exception as e:
            q.put(("err", e))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    try:
        kind, value = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"timed out after {timeout}s")
    if kind == "err":
        raise value
    return value


# ---------------------------------------------------------------------------
# 1. PDF ingest
# ---------------------------------------------------------------------------

def ingest_pdf(pdf_path: str | Path) -> list[dict]:
    """Extract Drive folder links + tags from the PDF -> state/drive_folders.json."""
    from pypdf import PdfReader

    text = ""
    for page in PdfReader(str(pdf_path)).pages:
        text += (page.extract_text() or "") + "\n"

    # Links can wrap across lines in the PDF; strip whitespace inside URLs.
    compact = re.sub(r"\s+", " ", text)

    folders: dict[str, dict] = {}
    for m in re.finditer(
        r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]{15,})", compact
    ):
        fid = m.group(1)
        # tag = first "(Word)" within 40 chars after the link, if any
        tail = compact[m.end(): m.end() + 40]
        tag_m = re.match(r"[^(]{0,15}\(([A-Za-z ]{2,20})\)", tail)
        tag = tag_m.group(1).strip().lower() if tag_m else "clips"
        if fid not in folders:
            folders[fid] = {"id": fid, "tag": tag}

    skipped = re.findall(r"https://(?:www\.dropbox\.com|t\.me)/\S+", compact)
    for s in skipped:
        logger.info(f"drive_clips: skipping unsupported source {s[:60]}")

    out = list(folders.values())
    _FOLDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FOLDERS_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    logger.info(f"drive_clips: {len(out)} unique Drive folders saved "
                f"({len(skipped)} non-Drive links skipped)")
    return out


def load_folders() -> list[dict]:
    if not _FOLDERS_FILE.exists():
        return []
    return json.loads(_FOLDERS_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 2. Usage memory (50-day rule)
# ---------------------------------------------------------------------------

def _load_used() -> dict:
    if _USED_FILE.exists():
        try:
            return json.loads(_USED_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _is_reusable(key: str, used: dict) -> bool:
    ts = used.get(key)
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days >= _REUSE_DAYS
    except Exception:
        return True


def commit_usage(usage_ids: list[str]) -> None:
    """
    Mark clips as used (call only after a render succeeded).
    usage_ids comes from build_visuals()'s return value -- NOT a module-global,
    so two concurrent renders in the same process (e.g. a manual generate
    firing while a batch job is mid-run, both spawned as threads by app.py)
    can never overwrite each other's pending usage list before it is committed.
    """
    if not usage_ids:
        return
    used = _load_used()
    now = datetime.now(timezone.utc).isoformat()
    for key in usage_ids:
        used[key] = now
    _USED_FILE.write_text(json.dumps(used, indent=2), encoding="utf-8")
    logger.info(f"drive_clips: {len(usage_ids)} clips marked used in used_clips.json")


# ---------------------------------------------------------------------------
# 3. Folder listing + candidate selection + staging download
# ---------------------------------------------------------------------------

_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_FOLDER_MIME   = "application/vnd.google-apps.folder"


def _list_folder_raw(folder_id: str) -> list[dict]:
    """
    List a folder's files via the Drive API. Shortcuts (e.g. from "Add shortcut
    to My Drive") are resolved to their real target file id -- the shortcut's
    own id can't be downloaded directly, only the file it points to.
    """
    drive = _get_drive_service()
    out = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,shortcutDetails)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            mime = f.get("mimeType", "")
            if mime == _FOLDER_MIME:
                continue
            if mime == _SHORTCUT_MIME:
                target = f.get("shortcutDetails") or {}
                fid = target.get("targetId")
                if not fid:
                    continue
            else:
                fid = f["id"]
            out.append({"id": fid, "name": f["name"]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _list_folder(folder_id: str) -> list[dict]:
    """Metadata-only listing of a public Drive folder (id, name). No downloads.
    Quota-throttled shared folders can stall with no error and no timeout from
    gdown itself, so this call is bounded by a hard wall-clock timeout."""
    return _with_timeout(_list_folder_raw, _LIST_TIMEOUT_S, folder_id)


def _staging_size() -> int:
    if not _STAGING_DIR.exists():
        return 0
    return sum(f.stat().st_size for f in _STAGING_DIR.rglob("*") if f.is_file())


_MIN_VIDEO_BYTES = 50_000    # below this, a "clip" download is almost always a
                             # corrupted/partial file, not a real video
_MIN_IMAGE_BYTES = 5_000     # a legitimate compressed photo can be well under
                             # 50KB -- that floor was silently starving format3's
                             # already-scarce image pool (see project memory)


def _download_file_raw(file_id: str, dest: Path, min_bytes: int) -> Path | None:
    from googleapiclient.http import MediaIoBaseDownload

    drive = _get_drive_service()
    request = drive.files().get_media(fileId=file_id)
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if dest.exists() and dest.stat().st_size > _MAX_FILE_BYTES:
                dest.unlink(missing_ok=True)
                return None

    size = dest.stat().st_size if dest.exists() else 0
    if size < min_bytes:
        logger.warning(f"drive_clips: downloaded file {file_id} too small "
                        f"({size}B < {min_bytes}B floor), discarding")
        dest.unlink(missing_ok=True)
        return None
    return dest


def _download_file(file_id: str, dest: Path, want_images: bool = False) -> Path | None:
    """Download one Drive file to dest. Returns path or None. Enforces size cap
    and a hard wall-clock timeout (shared quota-throttled folders can stall
    with an open connection and no data forever)."""
    if _staging_size() >= _STAGING_CAP:
        logger.warning("drive_clips: staging cap 1 GB reached, stopping downloads")
        return None
    min_bytes = _MIN_IMAGE_BYTES if want_images else _MIN_VIDEO_BYTES
    try:
        result = _with_timeout(_download_file_raw, _DOWNLOAD_TIMEOUT_S, file_id, dest, min_bytes)
        if result is None:
            _safe_unlink(dest)
        return result
    except TimeoutError:
        logger.warning(f"drive_clips: download stalled >{_DOWNLOAD_TIMEOUT_S}s, skipping {file_id}")
        _safe_unlink(dest)
        return None
    except Exception as e:
        logger.warning(f"drive_clips: download failed {file_id}: {e}")
        _safe_unlink(dest)
        return None


def _safe_unlink(path: Path) -> None:
    """Best-effort delete. An abandoned timed-out download thread may still be
    writing to this file on Windows, where deleting an open file can raise."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def stage_candidates(want_images: bool = False) -> list[dict]:
    """
    Pick candidates across SEVERAL folders/themes, excluding recently used clips,
    and download them to staging (<= 1 GB). Returns [{id, name, tag, path}].
    """
    folders = load_folders()
    if not folders:
        raise RuntimeError("drive_clips: no folders ingested. Run: "
                           "python -m modules.drive_clips --ingest-pdf <pdf>")

    used = _load_used()
    exts = _IMAGE_EXTS if want_images else _VIDEO_EXTS

    random.shuffle(folders)
    _STAGING_DIR.mkdir(parents=True, exist_ok=True)

    considered = excluded = 0
    staged: list[dict] = []
    tags_seen: set[str] = set()

    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    # Bug found in testing: the loop breaks once len(tags_seen) >= _FOLDERS_PER_RUN
    # (5 themes) REGARDLESS of _MAX_CANDIDATES -- with the old take=5/folder that
    # capped total staged at 5*5=25, silently below _MAX_CANDIDATES=36 once cuts
    # moved to every beat. folders[:N] attempt budget raised too, since today's
    # Drive flakiness means several attempts fail outright and need backup folders.
    for folder in folders[:max(_FOLDERS_PER_RUN * 3, 15)]:
        if len(staged) >= _MAX_CANDIDATES or len(tags_seen) >= _FOLDERS_PER_RUN:
            break
        logger.info(f"drive_clips: listing folder {folder['id'][:12]}... (tag={folder['tag']})")
        try:
            entries = _list_folder(folder["id"])
        except TimeoutError:
            logger.warning(f"drive_clips: folder {folder['id'][:12]} listing stalled "
                           f">{_LIST_TIMEOUT_S}s, skipping")
            continue
        except RefreshError as e:
            raise DriveAuthError(
                "ERREUR AUTH GOOGLE DRIVE : le token a expiré ou a été révoqué, "
                f"relancer l'authentification ({e})"
            ) from e
        except HttpError as e:
            if e.resp.status in (401, 403):
                raise DriveAuthError(
                    "ERREUR AUTH GOOGLE DRIVE : le token a expiré ou a été révoqué, "
                    f"relancer l'authentification (HTTP {e.resp.status})"
                ) from e
            logger.warning(f"drive_clips: folder {folder['id'][:12]} inaccessible: {e}")
            continue
        except Exception as e:
            logger.warning(f"drive_clips: folder {folder['id'][:12]} inaccessible: {e}")
            continue

        files = [e for e in entries if e["name"].lower().endswith(exts)]
        considered += len(files)
        fresh = [e for e in files if _is_reusable(e["id"], used)]
        excluded += len(files) - len(fresh)
        logger.info(f"drive_clips: folder {folder['id'][:12]} -> {len(files)} matching, "
                    f"{len(fresh)} reusable")
        if not fresh:
            continue

        random.shuffle(fresh)
        take = min(8, _MAX_CANDIDATES - len(staged))
        for e in fresh[:take]:
            safe = re.sub(r"[^\w.-]", "_", e["name"])[:80]
            dest = _STAGING_DIR / f"{e['id'][:12]}_{safe}"
            logger.info(f"drive_clips: downloading {_safe_log_name(e['name'][:50])}...")
            got = _download_file(e["id"], dest, want_images=want_images)
            if got:
                staged.append({"id": e["id"], "name": e["name"],
                               "tag": folder["tag"], "path": str(got)})
                logger.info(f"drive_clips: staged {_safe_log_name(e['name'][:50])} "
                            f"({got.stat().st_size // 1024} KB)")
        tags_seen.add(folder["tag"])

    logger.info(f"drive_clips: candidates considered={considered} "
                f"excluded_recent={excluded} staged={len(staged)} "
                f"themes={sorted(tags_seen)}")
    return staged


# ---------------------------------------------------------------------------
# 4. Duration probe (no Claude call -- was per-clip Claude vision, dropped:
#    36 candidates x 1 vision call each was the actual token cost of every
#    format3 render, unrelated to whether beat-match was toggled)
# ---------------------------------------------------------------------------

def _duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def analyze_clips(staged: list[dict]) -> list[dict]:
    """Add duration to each staged clip (ffprobe only, no Claude call)."""
    analyzed = []
    for c in staged:
        dur = _duration(c["path"])
        if dur < 1.0:
            logger.info(f"drive_clips: {c['name']} unreadable, skipped")
            continue
        c2 = dict(c)
        c2["duration"] = round(dur, 2)
        analyzed.append(c2)
        logger.info(f"drive_clips: analyzed {_safe_log_name(c['name'][:40])} "
                    f"({dur:.1f}s) [tag={c['tag']}]")
    return analyzed


# ---------------------------------------------------------------------------
# 5. Visual grouping (by Drive folder tag -- free coherence proxy, no Claude call)
# ---------------------------------------------------------------------------

def group_clips(analyzed: list[dict], target_seconds: float = _MIN_GROUP_SECONDS) -> list[dict]:
    """
    Group clips by their Drive folder tag (clips from the same themed folder
    read as one coherent montage) instead of asking Claude to judge visual
    coherence per clip. Returns the ordered clips of the best group (>= target_seconds).
    """
    if not analyzed:
        return []

    total = sum(c["duration"] for c in analyzed)
    if total < target_seconds:
        logger.warning(f"drive_clips: only {total:.0f}s of footage staged, "
                       "using everything in one group")
        return analyzed

    by_tag: dict[str, list[dict]] = {}
    for c in analyzed:
        by_tag.setdefault(c["tag"], []).append(c)
    candidates = sorted(by_tag.values(), key=lambda g: sum(c["duration"] for c in g), reverse=True)

    for group in candidates:
        group_total = sum(c["duration"] for c in group)
        if group_total >= target_seconds:
            logger.info(f"drive_clips: group of {len(group)} clips "
                        f"({group_total:.0f}s) tag={group[0]['tag']}")
            return group

    # No single tag reaches target: merge tags (largest first) until it does
    merged, acc = [], 0.0
    for group in candidates:
        merged.extend(group)
        acc += sum(c["duration"] for c in group)
        if acc >= target_seconds:
            break
    logger.info(f"drive_clips: merged {len(by_tag)} tags -> {len(merged)} clips ({acc:.0f}s)")
    return merged or analyzed


# ---------------------------------------------------------------------------
# 6. Orchestration + cleanup
# ---------------------------------------------------------------------------

_staging_lock = threading.Lock()


def build_visuals(session_dir: str | Path, target_seconds: float = _MIN_GROUP_SECONDS,
                  want_images: bool = False) -> tuple[list[str], list[str]]:
    """
    Full run: stage -> analyze -> group -> copy chosen clips into session_dir ->
    wipe staging. Returns (ordered clip paths, usage_ids) -- empty lists on failure.
    Pass usage_ids to commit_usage() after the final render succeeds.

    Guarded by a process-wide lock: app.py can run a manual generate and a
    batch job concurrently in separate threads, and both would otherwise share
    the single C:/temp/flowshort_staging directory -- one job's cleanup could
    delete files the other is mid-copy on. Serializing here fixes that without
    touching the shared staging path the spec calls for.
    """
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    with _staging_lock:
        try:
            staged = stage_candidates(want_images=want_images)
            if not staged:
                return [], []
            if want_images:
                chosen = staged            # images: no grouping needed
            else:
                analyzed = analyze_clips(staged)
                chosen = group_clips(analyzed, target_seconds)
            if not chosen:
                return [], []

            out_paths, ids = [], []
            for c in chosen:
                dest = session_dir / Path(c["path"]).name
                shutil.copy2(c["path"], dest)
                out_paths.append(str(dest))
                ids.append(c["id"])
            return out_paths, ids
        finally:
            cleanup_staging()


def cleanup_staging() -> None:
    if _STAGING_DIR.exists():
        shutil.rmtree(_STAGING_DIR, ignore_errors=True)
        logger.info("drive_clips: staging wiped")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingest-pdf", help="Path to the PDF with Drive links")
    ap.add_argument("--test-run", action="store_true", help="Stage+analyze+group, no render")
    args = ap.parse_args()

    if args.ingest_pdf:
        folders = ingest_pdf(args.ingest_pdf)
        for f in folders:
            print(f"  {f['id'][:20]}...  tag={f['tag']}")
    if args.test_run:
        paths, ids = build_visuals(Path("C:/temp/flowshort_test_session"), 20.0)
        print(f"\nGroup ready: {len(paths)} clips")
        for p in paths:
            print("  ", p)
