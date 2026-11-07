"""
format3 "Mode template" support: split a template-mode render back into its 9
known-in-advance cut segments, upload them to a per-video Drive subfolder, and
track the round-trip ("waiting for the user's CapCut edit") as a small flat
JSON state file -- same idiom as modules/publish/publish_config.py's dedup log
and modules/cta_counter.py's counter file, no database.

Round trip: modules.assembler.format3.assemble_format3a_template renders one
8.5s video with exactly 9 equal hard cuts (and, as a side effect, 2 raw
no-quote B-roll extras -- see its _build_template_extras) -> split_into_parts()
re-cuts the main render at those same 9 known timestamps, find_template_extras()
locates the 2 side-effect files -> upload_template_parts() drops all of it
into Drive/<brand>/template_mode/<video_stem>/ -> add_pending() records the
ORIGINAL SCRIPT (not just a path -- see docstring below) so the later finish
step (app.py's /api/publish_upload, template_pending_id branch) can build the
real caption without depending on the original video/script files still
being around under their old name.
"""

import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_DRIVE_ROOT = "template_mode"
_PENDING_FILE_NAME = "template_pending.json"


def split_into_parts(video_path: Path, n: int = 9, seg_dur: float | None = None) -> list[Path]:
    """
    Re-cut the finished template-mode render into `n` equal segments of
    `seg_dur` seconds each (defaults to format3._TEMPLATE_SEG_DUR, i.e. the
    exact grid assemble_format3a_template built the video on). Re-encodes
    each piece (ffmpeg -ss/-t on an arbitrary cut point isn't guaranteed to
    land on a keyframe, so `-c copy` can produce a broken/black-frame-leading
    segment) -- these are short (<1s) so re-encoding all 9 is fast.

    Output: <video's own folder>/_template_parts/<video_stem>/part_1.mp4 ...
    part_{n}.mp4, in order.
    """
    if seg_dur is None:
        from modules.assembler.format3 import _TEMPLATE_SEG_DUR
        seg_dur = _TEMPLATE_SEG_DUR

    out_dir = video_path.parent / "_template_parts" / video_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    parts = []
    for i in range(n):
        start = i * seg_dur
        out = out_dir / f"part_{i + 1}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-t", f"{seg_dur:.3f}",
            "-i", str(video_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(out),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            tail = "\n".join(result.stderr.splitlines()[-15:])
            raise RuntimeError(f"template split part {i + 1} failed:\n{tail}")
        parts.append(out)

    logger.info(f"template split: {video_path.name} -> {len(parts)} parts in {out_dir}")
    return parts


_EXTRA_MIMETYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


def find_template_extras(video_path: Path) -> list[Path]:
    """
    Locate the 2 raw (no quote overlay) B-roll extras
    modules.assembler.format3._build_template_extras saves next to the main
    render -- "{stem}_extra_clip.<ext>" (1 untouched source clip) and
    "{stem}_extra_2clips.mp4" (2 distinct source clips concatenated). Either
    or both may be missing (pool had <1 or <2 video clips at render time,
    see that function's own fallback logging) -- callers just get whatever
    actually exists. User request 2026-08-08.
    """
    return sorted(video_path.parent.glob(f"{video_path.stem}_extra_*"))


def upload_template_parts(ctx, video_stem: str, parts: list[Path]) -> str:
    """
    Upload the 9 split parts (plus, when present, the 2 raw extra clips from
    find_template_extras -- callers just concatenate them into `parts`) into
    <brand Drive root>/template_mode/<video_stem>/. Reuses the same OAuth
    creds + folder-lookup helper as the manual-publish Drive upload
    (modules/publish/scheduler.py) -- same drive.file scope, same token.
    Returns the subfolder's webViewLink.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from modules.publish.scheduler import get_creds, _find_or_create_folder, _DRIVE_ROOT

    creds  = get_creds()
    drive  = build("drive", "v3", credentials=creds)
    root_id     = _find_or_create_folder(drive, _DRIVE_ROOT, parent_id=None)
    template_id = _find_or_create_folder(drive, _TEMPLATE_DRIVE_ROOT, parent_id=root_id)
    video_id    = _find_or_create_folder(drive, video_stem, parent_id=template_id)

    for part in parts:
        mimetype = _EXTRA_MIMETYPES.get(part.suffix.lower(), "video/mp4")
        meta  = {"name": part.name, "parents": [video_id]}
        media = MediaFileUpload(str(part), mimetype=mimetype, resumable=True)
        drive.files().create(body=meta, media_body=media).execute()

    folder = drive.files().get(fileId=video_id, fields="webViewLink").execute()
    link = folder.get("webViewLink", f"https://drive.google.com/drive/folders/{video_id}")
    logger.info(f"template parts uploaded -> {link}")
    return link


# ── Pending-state tracking (flat JSON, same idiom as publish_config.py's log) ──

def _pending_path(ctx) -> Path:
    return ctx.root / "state" / _PENDING_FILE_NAME


def _load(ctx) -> dict:
    path = _pending_path(ctx)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(f"template_pending.json unreadable, starting fresh: {path}")
    return {}


def _save(ctx, state: dict) -> None:
    path = _pending_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def add_pending(ctx, video_stem: str, script: dict, drive_link: str) -> None:
    """
    Records the FULL script dict (not a pointer to the original video/script
    files) so the later finish step never depends on those still existing
    under their original timestamp -- self-contained record.

    Deliberately silent -- no email, no publish. User was explicit
    (2026-08-08): at the "9 parts created" stage nothing gets sent out,
    manual or auto-schedule alike. The round trip only produces any
    email/publish output later, at the finish step (app.py's
    /api/publish_upload, template_pending_id branch), once the user has
    actually dropped their CapCut export back in.

    Precomputes {title, description} via modules.content_meta.generate_meta
    and caches it into script["_meta"] before saving -- generate_meta()
    itself guarantees a single Claude call per video across every use (skips
    re-generating once script["_meta"] is set), so the finish step's actual
    publish call (publisher.publish/_schedule_post, both call generate_meta
    again) reuses this exact cached result for free instead of a second
    Claude call. Surfaced directly on the pending record too so the
    Publication panel can show a title/description preview without a lookup
    of its own. Best-effort: a Claude hiccup here must not break the render
    that's already succeeded by the time this runs -- falls back to empty
    strings, the finish step will simply generate them fresh then.
    """
    title, description = "", ""
    try:
        from modules.content_meta import generate_meta
        meta = generate_meta(script)
        script["_meta"] = meta
        title, description = meta.get("title", ""), meta.get("description", "")
    except Exception:
        logger.exception(f"template add_pending: generate_meta failed for {video_stem} (non-fatal, "
                          "finish step will retry)")

    state = _load(ctx)
    state[video_stem] = {
        "subject": script.get("subject", ""),
        "title": title,
        "description": description,
        "script": script,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "drive_link": drive_link,
        "status": "awaiting_capcut",
    }
    _save(ctx, state)


def list_pending(ctx) -> list[dict]:
    state = _load(ctx)
    return [
        {"id": stem, **entry}
        for stem, entry in state.items()
        if entry.get("status") == "awaiting_capcut"
    ]


def get_pending(ctx, video_stem: str) -> dict | None:
    return _load(ctx).get(video_stem)


def mark_done(ctx, video_stem: str) -> None:
    state = _load(ctx)
    if video_stem in state:
        state[video_stem]["status"] = "done"
        _save(ctx, state)
