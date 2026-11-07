"""
Dashboard web — interface to manage video generation and publishing.
Run: python app.py
Open: http://localhost:5000
"""

import json
import logging
import queue
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 2026-08-23 (user request): switched from Europe/Paris to US Eastern -- the
# audience is 21.1% US / 16.2% UK / 2.7% Germany (all English-language,
# US-anchored), so every date/time typed into FlowShort (and every
# auto-spread batch window) is now interpreted/anchored in US Eastern, not
# the operator's own Paris timezone. publishers/zernio_client.py's
# _SCHEDULE_TZ was updated to match -- see its comment for why that one
# also needed to change even though it re-derives the instant either way.
_PUBLISH_TZ = ZoneInfo("America/New_York")

# Publishing window: 07:00 -> 01:00 next day = 18h span.
# (07:00 to 01:00 is 18h, not 19h; with vpd=3 that's clean 6h slots: 07-13, 13-19, 19-01.)
_WINDOW_START_MIN = 7 * 60      # 420  = 07:00
_WINDOW_TOTAL_MIN = 18 * 60     # 1080 -> ends at 1500 = 01:00 next day
_MIN_GAP_MIN      = 120         # >= 2h between two consecutive videos

from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    BASE_DIR, OUTPUT_DIR, LOGS_DIR,
    YOUTUBE_CLIENT_SECRETS, TIKTOK_PROFILE_DIR, INSTAGRAM_PROFILE_DIR,
    FORMATS,
)
from modules import users

# Explicit UTF-8 file handler -- without this, logging has no configured
# handler at all under pythonw (windowless, no console to fall back to), and
# any non-ASCII character in a log message (arrows, accents, emoji from
# Claude-generated titles) raises UnicodeEncodeError against the Windows
# console codepage when a handler DOES exist. Logging traps that error
# internally so it never crashes the caller, but it silently discards the
# log line and spams "--- Logging error ---" noise -- a real file, always
# UTF-8 regardless of console/codepage, fixes both problems at once.
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOGS_DIR / "app.log", encoding="utf-8")],
)
logger = logging.getLogger("app")

app = Flask(__name__)

VIDEOS_DIR  = OUTPUT_DIR / "videos"
SCRIPTS_DIR = OUTPUT_DIR / "scripts"
PUBLISH_LOG = BASE_DIR / "state" / "publish_log.json"   # shared cross-brand ledger, not ResetWorth's own STATE_DIR

# Each non-default brand keeps its generated videos/scripts physically
# separate under its own users/<name>/output/ tree (modules.users.run_generator
# relocates them there right after generation, 2026-07-28) -- the dashboard
# still needs to see across ALL of them for the grid/serve/publish routes.
def _all_video_roots() -> list[Path]:
    roots = [VIDEOS_DIR]
    for name in users.list_users():
        if name != users.DEFAULT_USER:
            roots.append(users.get_user(name).root / "output" / "videos")
    return roots


def _all_script_roots() -> list[Path]:
    roots = [SCRIPTS_DIR]
    for name in users.list_users():
        if name != users.DEFAULT_USER:
            roots.append(users.get_user(name).root / "output" / "scripts")
    return roots


def _resolve_video_path(filename: str) -> Path | None:
    """Find an existing video by bare filename across every brand's videos
    root. Filenames carry a timestamp+format+slug, so collisions across
    brands are not a practical concern."""
    for root in _all_video_roots():
        candidate = root / filename
        if candidate.exists():
            return candidate
    return None

# Every social platform this dashboard can target, short-form side (long-format
# publishes only to "youtube_long" -- separate profiles.yaml entry, unaffected).
_VALID_PLATFORMS = {"snapchat", "tiktok", "instagram", "facebook", "youtube"}

# Background job tracking
_jobs: dict[str, dict] = {}  # job_id → {status, message, result}


@app.route("/api/stats")
def get_stats():
    # Basic job stats
    jobs_summary = {
        "total": len(_jobs),
        "queued": sum(1 for j in _jobs.values() if j["status"] == "queued"),
        "running": sum(1 for j in _jobs.values() if j["status"] == "running"),
        "completed": sum(1 for j in _jobs.values() if j["status"] == "completed"),
        "failed": sum(1 for j in _jobs.values() if j["status"] == "failed"),
    }
    return jsonify(jobs_summary)


def _platform_status() -> dict:
    yt_ok   = (BASE_DIR / YOUTUBE_CLIENT_SECRETS).exists()
    tt_ok   = (BASE_DIR / TIKTOK_PROFILE_DIR / "Default").exists()
    ig_ok   = (BASE_DIR / INSTAGRAM_PROFILE_DIR / "Default").exists()
    return {
        "youtube":   {"ok": yt_ok,  "label": "Connected" if yt_ok  else "Setup required"},
        "tiktok":    {"ok": tt_ok,  "label": "Connected" if tt_ok  else "Setup required"},
        "instagram": {"ok": ig_ok,  "label": "Connected" if ig_ok  else "Setup required"},
    }


def _load_publish_log() -> list:
    if not PUBLISH_LOG.exists():
        return []
    try:
        return json.loads(PUBLISH_LOG.read_text(encoding="utf-8"))
    except Exception:
        return []


def _published_urls(video_path: Path) -> dict:
    """Return {platform: url} for already-published entries for this video."""
    for entry in reversed(_load_publish_log()):
        if Path(entry.get("video", "")).name == video_path.name:
            return {
                p: u for p, u in entry.get("results", {}).items()
                if not u.startswith("ERROR")
            }
    return {}


def _find_script(video_path: Path) -> dict:
    ts = video_path.stem[:15]
    for root in _all_script_roots():
        for f in root.glob(f"{ts}*.json"):
            return json.loads(f.read_text(encoding="utf-8"))
    return {}


def _persist_script_field(video_path: Path, **fields) -> None:
    """
    Write extra key(s) into the script JSON already on disk for this video.
    Used for script["route"] = "manual" (modules/publish/manual_queue.py) -- this
    MUST land on disk, not just stay in the in-memory dict, so that any
    later, unrelated publish action reloading this script (see _find_script)
    still sees the route and modules.publish.publish_config.schedule_video's guard
    still refuses to auto-publish it.
    """
    ts = video_path.stem[:15]
    for root in _all_script_roots():
        for f in root.glob(f"{ts}*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data.update(fields)
                f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                logger.warning(f"_persist_script_field: couldn't update {f}: {e}")
            return


def _video_list() -> list[dict]:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    all_files: list[Path] = []
    for root in _all_video_roots():
        root.mkdir(parents=True, exist_ok=True)
        all_files.extend(root.glob("*.mp4"))

    videos = []
    for f in sorted(all_files, key=lambda x: x.stat().st_mtime, reverse=True):
        script  = _find_script(f)
        urls    = _published_urls(f)
        size_mb = round(f.stat().st_size / 1e6, 1)
        try:
            ts = datetime.strptime(f.stem[:15], "%Y%m%d_%H%M%S").strftime("%d %b %Y %H:%M")
        except Exception:
            ts = f.stem[:15]
        videos.append({
            "name":     f.name,
            "stem":     f.stem,
            "ts":       ts,
            "size_mb":  size_mb,
            "format":   script.get("format", "?"),
            "subject":  script.get("subject", f.stem),
            "hook":     script.get("hook", ""),
            "urls":     urls,
            "brand":    script.get("_user", users.DEFAULT_USER),
        })
    return videos


def _run_in_thread(job_id: str, fn, *args) -> None:
    def _worker():
        try:
            _jobs[job_id]["status"] = "running"
            logger.info(f"Job {job_id}: starting")
            result = fn(*args)
            _jobs[job_id].update({"status": "done", "result": result})
            logger.info(f"Job {job_id}: done → {result}")
        except Exception as e:
            _jobs[job_id].update({"status": "error", "message": str(e)})
            logger.error(f"Job {job_id}: FAILED — {e}", exc_info=True)
    threading.Thread(target=_worker, daemon=True).start()


# Sequential generation queue -- both generation buttons (manual single-video
# AND the batch/"Postiz" panel, mixte or long-format) push onto this ONE
# queue instead of spawning their own thread. A single worker runs jobs
# strictly one at a time: clicking button A then button B fully finishes A's
# entire pipeline before B starts, no parallel ffmpeg/publish runs colliding.
_gen_queue: "queue.Queue[tuple[str, object, tuple]]" = queue.Queue()


def _gen_worker() -> None:
    while True:
        job_id, fn, args = _gen_queue.get()
        try:
            _jobs[job_id]["status"] = "running"
            logger.info(f"Generation job {job_id}: starting")
            result = fn(*args)
            _jobs[job_id].update({"status": "done", "result": result})
            logger.info(f"Generation job {job_id}: done → {result}")
        except Exception as e:
            _jobs[job_id].update({"status": "error", "message": str(e)})
            logger.error(f"Generation job {job_id}: FAILED — {e}", exc_info=True)
        finally:
            _gen_queue.task_done()


threading.Thread(target=_gen_worker, daemon=True).start()


# ── Publish verification worker ─────────────────────────────────────────────
# schedule_video() only confirms the provider ACCEPTED the schedule request --
# real delivery happens later, provider-side, and can still fail silently
# there (2026-09-03: two White Card videos logged "created" but TikTok never
# actually posted them -- Zernio's TikTok delivery failed afterwards with
# "at capacity", invisible until someone checks the post status directly).
# This periodically re-checks recent posts' real status and, on a TikTok
# capacity/account failure, auto-resubmits to TikTok's Creator Inbox (draft)
# so the video lands for a manual tap-to-publish instead of vanishing again.
_PUBLISH_VERIFY_INTERVAL_S = 1800  # 30 min


def _publish_verify_worker() -> None:
    from modules.publish.publish_config import verify_pending_posts
    while True:
        time.sleep(_PUBLISH_VERIFY_INTERVAL_S)
        try:
            problems = verify_pending_posts()
            for p in problems:
                logger.error(f"PUBLISH VERIFY: {p['video']} -> {p['platform']} {p['status']} — {p['detail']}"
                             + (f" | {p['retry']}" if p.get("retry") else ""))
        except Exception as e:
            logger.warning(f"_publish_verify_worker: check failed: {e}")


threading.Thread(target=_publish_verify_worker, daemon=True).start()


# ── Batch checkpointing / crash-resume ──────────────────────────────────────
# 2026-08-12, explicit user ask: a big multi-day batch is 100% in-memory
# (only `_jobs`, a plain dict) -- a PC shutdown, closed tab, or killed python
# process mid-batch loses everything, settings included, forcing a full
# from-scratch re-run. Every batch run through _generate_batch now writes a
# small checkpoint file continuously (params + which slots are done so far),
# so relaunching python and opening a fresh page can offer to pick up exactly
# where it stopped instead of starting over. Deleted the moment a batch
# actually finishes (nothing left to resume) -- see _generate_batch's final
# cleanup.
_BATCH_CHECKPOINTS_DIR = BASE_DIR / "state" / "batch_checkpoints"


def _checkpoint_path(checkpoint_id: str) -> Path:
    return _BATCH_CHECKPOINTS_DIR / f"{checkpoint_id}.json"


def _save_checkpoint(checkpoint_id: str, data: dict) -> None:
    _BATCH_CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat()
    # Write-then-rename instead of writing the target file directly -- a
    # process killed mid-write (the exact scenario this exists to survive)
    # must never leave a half-written, unparseable checkpoint behind.
    tmp = _checkpoint_path(checkpoint_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_checkpoint_path(checkpoint_id))


def _load_checkpoint(checkpoint_id: str) -> dict | None:
    p = _checkpoint_path(checkpoint_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(f"batch checkpoint {checkpoint_id}: unreadable, treating as missing")
        return None


def _delete_checkpoint(checkpoint_id: str) -> None:
    _checkpoint_path(checkpoint_id).unlink(missing_ok=True)


def _list_resumable_checkpoints(user: str | None = None) -> list[dict]:
    """Every checkpoint still status=='running' -- i.e. a batch that started
    but never reached its own final cleanup, whether from a crash/shutdown or
    the process simply not having gotten there yet. `user` (optional) scopes
    to one brand's own batches, since resuming brand A's batch while looking
    at brand B's dashboard tab would be confusing."""
    if not _BATCH_CHECKPOINTS_DIR.exists():
        return []
    out = []
    for f in _BATCH_CHECKPOINTS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") != "running":
            continue
        params = d.get("params", {})
        if user and params.get("user") not in (user, None):
            continue
        out.append({
            "checkpoint_id": d.get("checkpoint_id", f.stem),
            "user":          params.get("user"),
            "mode":          params.get("mode"),
            "kind":          d.get("kind", "mixed"),
            "format":        params.get("format"),   # only set for kind=="manual" (single-format batches)
            "total":         d.get("total", 0),
            "done":          len(d.get("results", [])),
            "created_at":    d.get("created_at"),
        })
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


def _enqueue_generation(job_id: str, fn, *args) -> None:
    _jobs[job_id]["status"] = "queued"
    _gen_queue.put((job_id, fn, args))


# ── Routes ────────────────────────────────────────────────────────────────────

_VIDEOS_PAGE_SIZE = 40


@app.route("/")
def index():
    # _video_list() has no inherent cap -- with hundreds of videos accumulated
    # over time, rendering a <video preload="metadata"> element for every
    # single one made the whole page (scrolling included) crawl in the
    # browser. Paginate via a plain query param + full reload ("Charger
    # plus") -- simplest fix, no new JS/API surface needed.
    all_videos = _video_list()
    limit = max(_VIDEOS_PAGE_SIZE, int(request.args.get("limit", _VIDEOS_PAGE_SIZE)))
    # Brand-scoped format visibility (2026-07-30, explicit user ask: switching
    # brand did nothing before this -- MindFile's own formats leaked into
    # ResetWorth's dropdown and vice versa). One ctx fetch reused for both
    # active_user and the filtered formats dict, preserving FORMATS' declared
    # ordering and defensively dropping any stale id a hand-edited brand
    # config.json might reference.
    ctx = users.get_user()
    scoped_formats = {fid: name for fid, name in FORMATS.items() if fid in ctx.formats}
    return render_template(
        "index.html",
        videos=all_videos[:limit],
        has_more_videos=len(all_videos) > limit,
        next_videos_limit=limit + _VIDEOS_PAGE_SIZE,
        platforms=_platform_status(),
        formats=scoped_formats,
        users=users.list_users(),
        active_user=ctx.name,
        batch_cfg=_batch_panel_config(ctx),
    )


@app.route("/videos/<filename>")
def serve_video(filename):
    path = _resolve_video_path(filename)
    if path is None:
        return "Not found", 404
    return send_file(str(path), mimetype="video/mp4")


_CLIP_OVERRIDE_DIR = BASE_DIR / "scratch" / "clip_overrides"
_CLIP_OVERRIDE_FORMATS = ("format1", "format7")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    # JSON for the plain case; format1/format7's optional clip-file override
    # (§Phase B) needs multipart instead -- get_json(silent=True) returns
    # None rather than raising for a non-JSON content type, so both shapes
    # fall back cleanly to request.form.
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    fmt     = data.get("format", "format1")
    subject = (data.get("subject") or "").strip()
    # format17 always covers the CURRENT week regardless of subject (see
    # script_generator._generate_format17_script) -- never require one.
    if not subject and fmt != "format17":
        return jsonify({"error": "Subject is required"}), 400

    # "auto" = pick one format randomly (same weights as batch scheduler);
    # the schedule's own hook flag (1/3 of format3 slots) applies unless the
    # request already asked for a hook explicitly.
    auto_use_hook = False
    if fmt == "auto":
        fmt, auto_use_hook = _make_format_schedule(1)[0]

    ctx = users.get_user(data.get("user"))
    job_id = f"gen_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    use_hook = (bool(data.get("use_hook", False)) or auto_use_hook) and fmt in _HOOK_COMPATIBLE_FORMATS
    # "Hook recut" -- format3's interrupted-payoff structure (hook -> content ->
    # back to the hook -> content again, see modules.assembler.assemble_with_hook_recut),
    # mutually exclusive with use_hook/beat_match same as pipeline.run itself enforces.
    hook_recut = bool(data.get("hook_recut", False)) and fmt == "format3" and not use_hook
    # "Mode template" -- no hook, no beat matching, exactly 9 equal cuts then
    # split + uploaded to Drive for a CapCut edit (see modules.publish.template_parts).
    # Mutually exclusive with use_hook/hook_recut same as pipeline.run enforces.
    template_mode = bool(data.get("template_mode", False)) and fmt == "format3" and not use_hook and not hook_recut
    if use_hook:
        gen_kwargs = {
            "use_hook": True,
            "hook_transition": str(data.get("hook_transition", "zoom_punch")),
            "hook_cut_style": str(data.get("hook_cut_style", "flash")),
        }
    elif hook_recut:
        gen_kwargs = {"hook_recut": True}
    elif template_mode:
        gen_kwargs = {"template_mode": True}
    else:
        # "Matcher sur le beat" -- format3 without a hook, cut on the music's
        # real beat grid instead of the plain fixed-per-card duration.
        gen_kwargs = {"beat_match": bool(data.get("beat_match", False))} if fmt == "format3" else {}

    # format1/format7 optional clip source override (2026-07-30) -- pipeline.run
    # already accepts clip_path and bypasses the assets/clips/ pool when given
    # one (pipeline.py L97-137); this is just the web-facing path to it. Blank
    # url/file -> untouched existing pool-pick behavior.
    clip_dest = None
    if fmt in _CLIP_OVERRIDE_FORMATS:
        clip_file = request.files.get("clip_file")
        if clip_file and clip_file.filename:
            _CLIP_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
            ext = Path(clip_file.filename).suffix.lower() or ".mp4"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
            clip_dest = _CLIP_OVERRIDE_DIR / f"{ts}{ext}"
            clip_file.save(str(clip_dest))
    clip_url = (data.get("clip_url") or "").strip() if fmt in _CLIP_OVERRIDE_FORMATS else ""

    def _generate():
        kwargs = dict(gen_kwargs)
        clip_path = clip_dest
        if clip_path is None and clip_url:
            from modules.clips.link_download import download_clip
            clip_path = download_clip(clip_url, _CLIP_OVERRIDE_DIR)
        if clip_path is not None:
            kwargs["clip_path"] = clip_path
        path = users.run_generator(ctx, fmt, subject, **kwargs)
        result = {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": fmt}
        if template_mode:
            # Split the 9-equal-cut render back into its 9 segments, upload
            # them to a per-video Drive folder, and remember the original
            # script so the later CapCut-finish step (api_publish_upload's
            # template_pending_id branch) can build the real caption.
            from modules.publish.template_parts import split_into_parts, upload_template_parts, find_template_extras, add_pending
            script = _find_script(path)
            parts = split_into_parts(path) + find_template_extras(path)
            drive_link = upload_template_parts(ctx, path.stem, parts)
            add_pending(ctx, path.stem, script, drive_link)
            result["drive_link"] = drive_link
        return result

    _enqueue_generation(job_id, _generate)
    return jsonify({"job_id": job_id, "format_chosen": fmt})


_FORMAT14_SCRIPTS_DIR = OUTPUT_DIR / "scripts"


def _load_format14_script(script_id: str) -> dict:
    path = _FORMAT14_SCRIPTS_DIR / f"{script_id}.json"
    if not path.exists() or ".." in script_id or "/" in script_id or "\\" in script_id:
        raise FileNotFoundError(f"Unknown script_id: {script_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


@app.route("/api/format14/generate", methods=["POST"])
def api_format14_generate():
    """Dedicated format14 panel, step 1: auto-picks a random character (any
    anime/cartoon/movie/series/game -- see modules.script_generator's
    empty-subject mode) and returns the suspect's lines so the user can
    record/generate that character's voice before building the video."""
    from modules.script_generator import generate_script
    from modules.assembler.interrogation import build_turns

    try:
        script = generate_script("format14", "")
    except Exception as e:
        logger.error(f"format14 script generation failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    script_id = Path(script["_script_path"]).stem
    turns = build_turns(script)
    suspect_lines = [{"turn": i, "text": text} for i, (speaker, text) in enumerate(turns) if speaker == "suspect"]

    return jsonify({
        "script_id": script_id,
        "subject": script["subject"],
        "suspect_name": script.get("suspect_name"),
        "pov_hook": script.get("pov_hook"),
        "hook": script.get("hook"),
        "duration_target": script.get("duration_target"),
        "suspect_lines": suspect_lines,
    })


@app.route("/api/format14/upload_audio", methods=["POST"])
def api_format14_upload_audio():
    """Step 2: the user drops ONE recording with all the suspect's lines
    read in order (same file, re-uploaded, overwrites and gets re-split --
    see interrogation._find_combined_audio's content fingerprinting)."""
    from modules.assembler.interrogation import voice_input_dir

    script_id = request.form.get("script_id", "")
    file = request.files.get("audio")
    if not file or not file.filename:
        return jsonify({"error": "Fichier audio manquant"}), 400

    try:
        script = _load_format14_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    folder = voice_input_dir(script["subject"])
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".mp3"
    dest = folder / f"recording{ext}"
    file.save(str(dest))

    logger.info(f"format14: saved suspect recording for {script['subject']!r} -> {dest}")
    return jsonify({"ok": True, "saved_as": dest.name})


@app.route("/api/format14/build", methods=["POST"])
def api_format14_build():
    """Step 3: assemble the final video -- reuses the same job-queue/polling
    pattern as /api/generate (rendering, background-clip search, and TTS/
    voice-split all take real time)."""
    from modules.assembler import assemble

    data = request.json or {}
    script_id = data.get("script_id", "")
    try:
        script = _load_format14_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    job_id = f"f14_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        path = assemble(script, {}, voice_result=None, timestamps_result=None, music_path=None)
        # Same rename pipeline.run() does after every other format's assembly
        # (see its comment there): the script JSON was saved at STEP 1
        # (script generation) under that timestamp, but this video is built
        # at STEP 3, potentially minutes/hours later once the user has
        # uploaded their audio -- _find_script()/the video grid/publish flow
        # all glob on the VIDEO's own timestamp prefix, so without this
        # rename format14 videos would show up with no subject/hook and
        # publishing would lose their caption/hashtag metadata.
        try:
            script_path = Path(script.get("_script_path") or "")
            if script_path.exists():
                new_path = script_path.parent / (path.stem[:15] + script_path.name[15:])
                if new_path != script_path:
                    script_path.rename(new_path)
                new_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"format14 build: couldn't relocate script after assembly: {e}")
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "format14"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


_FORMAT15_SCRIPTS_DIR = OUTPUT_DIR / "scripts"


def _load_format15_script(script_id: str) -> dict:
    path = _FORMAT15_SCRIPTS_DIR / f"{script_id}.json"
    if not path.exists() or ".." in script_id or "/" in script_id or "\\" in script_id:
        raise FileNotFoundError(f"Unknown script_id: {script_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def _pick_characters_route(fmt: str):
    """Shared body for /api/format15|16/pick_characters -- step "0" of the
    Aléatoire path (2026-08-09): pick 2 characters WITHOUT writing the
    script yet, so the wizard can show the names with a per-character reroll
    button before committing to the (slower, real) script generation call.
    Reused as-is by /api/format16/pick_characters right below."""
    from modules.script_generator import pick_duel_characters, _load_past_characters

    data = request.json or {}
    fix_a = data.get("fix_a") or None
    fix_b = data.get("fix_b") or None
    past = _load_past_characters(fmt, ["char_a_name", "char_b_name"])
    # avoid (2026-08-09): the name being rerolled AWAY from, from THIS
    # session -- past_characters only knows about characters already saved
    # to disk from earlier videos, nothing stops it re-picking the exact
    # same one it just showed the user otherwise.
    avoid = (data.get("avoid") or "").strip()
    if avoid and avoid not in past:
        past = [avoid, *past]
    try:
        picked = pick_duel_characters(fmt, past_characters=past, fix_a=fix_a, fix_b=fix_b)
    except Exception as e:
        logger.error(f"{fmt} character pick failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
    return jsonify(picked)


@app.route("/api/format15/pick_characters", methods=["POST"])
def api_format15_pick_characters():
    return _pick_characters_route("format15")


@app.route("/api/format15/generate", methods=["POST"])
def api_format15_generate():
    """Dedicated format15 panel, step 1: either both characters auto-picked
    (empty subject -> modules.script_generator's auto-pick mode) or both
    given by the user, then returns each character's own lines separately
    so the user can record/generate the two voices before building."""
    from modules.script_generator import generate_script
    from modules.assembler.duel import build_turns

    data = request.json or {}
    mode = data.get("mode", "random")
    ctx = users.get_user(data.get("user"))

    subject = ""
    if mode == "manual":
        name_a = (data.get("char_a_name") or "").strip()
        context_a = (data.get("char_a_context") or "").strip()
        name_b = (data.get("char_b_name") or "").strip()
        context_b = (data.get("char_b_context") or "").strip()
        if not name_a or not name_b:
            return jsonify({"error": "Il faut au moins le nom des deux personnages."}), 400
        subject = f"{name_a} / {context_a} vs {name_b} / {context_b}".strip()

    try:
        script = generate_script("format15", subject)
    except Exception as e:
        logger.error(f"format15 script generation failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    # Tag now (not just at /build) so this brand's own anti-dup history sees
    # it immediately -- see users.tag_and_relocate's docstring for why this
    # wizard needs the same brand handling run_generator gives every other
    # format (2026-08-09: was always writing into the shared default-brand
    # tree regardless of which brand's panel launched it).
    users._tag_script(Path(script["_script_path"]), ctx.name)

    script_id = Path(script["_script_path"]).stem
    turns = build_turns(script)
    a_lines = [{"turn": i, "text": text} for i, (speaker, text) in enumerate(turns) if speaker == "a"]
    b_lines = [{"turn": i, "text": text} for i, (speaker, text) in enumerate(turns) if speaker == "b"]

    return jsonify({
        "script_id": script_id,
        "subject": script["subject"],
        "pov_hook": script.get("pov_hook"),
        "char_a_name": script.get("char_a_name"),
        "char_b_name": script.get("char_b_name"),
        "a_lines": a_lines,
        "b_lines": b_lines,
    })


def _next_recording_path(folder: Path, tag: str, ext: str) -> Path:
    """Next '{tag}_recording_<N>.<ext>' path, N starting at 1 -- lets a
    character's/section's audio be uploaded across several separate takes
    instead of one upload overwriting the last (user feedback, 2026-07-27:
    "je prepare pas forcement tous les audios d'une traite"). Matched on the
    read side by voice_split.find_numbered_recordings."""
    max_n = 0
    for f in folder.glob(f"{tag}_recording_*"):
        m = re.match(rf"^{re.escape(tag)}_recording_(\d+)$", f.stem, re.IGNORECASE)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return folder / f"{tag}_recording_{max_n + 1}{ext}"


@app.route("/api/format15/upload_audio", methods=["POST"])
def api_format15_upload_audio():
    """Step 2: the user drops a character's lines read in order -- can be
    split across several separate uploads (see _next_recording_path), not
    just one continuous take. `side` picks which character ("a" or "b") this
    recording belongs to -- the a_/b_ filename prefix is what
    voice_split.find_numbered_recordings keys off."""
    from modules.assembler.duel import voice_input_dir

    script_id = request.form.get("script_id", "")
    side = request.form.get("side", "")
    file = request.files.get("audio")
    if side not in ("a", "b"):
        return jsonify({"error": "side doit être 'a' ou 'b'"}), 400
    if not file or not file.filename:
        return jsonify({"error": "Fichier audio manquant"}), 400

    try:
        script = _load_format15_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    folder = voice_input_dir(script["subject"])
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".mp3"
    dest = _next_recording_path(folder, side, ext)
    file.save(str(dest))

    logger.info(f"format15: saved character {side!r} recording for {script['subject']!r} -> {dest}")
    return jsonify({"ok": True, "saved_as": dest.name})


@app.route("/api/format15/build", methods=["POST"])
def api_format15_build():
    """Step 3: assemble the final video -- same job-queue/polling pattern as
    format14's build (real footage search + TTS/voice-split + rendering all
    take real time)."""
    from modules.assembler import assemble

    data = request.json or {}
    script_id = data.get("script_id", "")
    ctx = users.get_user(data.get("user"))
    try:
        script = _load_format15_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    job_id = f"f15_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        path = assemble(script, {}, voice_result=None, timestamps_result=None, music_path=None)
        # Same rename as format14's build step -- the script JSON was saved
        # at STEP 1 (script generation), potentially minutes/hours before
        # this video actually gets built once the user's audio is in place.
        try:
            script_path = Path(script.get("_script_path") or "")
            if script_path.exists():
                new_path = script_path.parent / (path.stem[:15] + script_path.name[15:])
                if new_path != script_path:
                    script_path.rename(new_path)
                new_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"format15 build: couldn't relocate script after assembly: {e}")
        # Brand relocation (2026-08-09, see users.tag_and_relocate) -- must run
        # AFTER the script rename above, since it moves whatever script JSON
        # matches path's timestamp prefix.
        path = users.tag_and_relocate(path, ctx)
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "format15"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


_FORMAT16_SCRIPTS_DIR = OUTPUT_DIR / "scripts"


def _load_format16_script(script_id: str) -> dict:
    path = _FORMAT16_SCRIPTS_DIR / f"{script_id}.json"
    if not path.exists() or ".." in script_id or "/" in script_id or "\\" in script_id:
        raise FileNotFoundError(f"Unknown script_id: {script_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


@app.route("/api/format16/pick_characters", methods=["POST"])
def api_format16_pick_characters():
    return _pick_characters_route("format16")


@app.route("/api/format16/generate", methods=["POST"])
def api_format16_generate():
    """Dedicated format16 panel, step 1 -- same mechanism as format15's panel:
    either both characters auto-picked (empty subject -> modules.script_generator's
    auto-pick mode) or both given by the user, then returns each character's
    own lines separately so the user can record/generate the two voices
    before building."""
    from modules.script_generator import generate_script
    from modules.assembler.red_phone import build_turns

    data = request.json or {}
    mode = data.get("mode", "random")
    ctx = users.get_user(data.get("user"))

    subject = ""
    if mode == "manual":
        name_a = (data.get("char_a_name") or "").strip()
        context_a = (data.get("char_a_context") or "").strip()
        name_b = (data.get("char_b_name") or "").strip()
        context_b = (data.get("char_b_context") or "").strip()
        if not name_a or not name_b:
            return jsonify({"error": "Il faut au moins le nom des deux personnages."}), 400
        subject = f"{name_a} / {context_a} calls {name_b} / {context_b}".strip()

    try:
        script = generate_script("format16", subject)
    except Exception as e:
        logger.error(f"format16 script generation failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    users._tag_script(Path(script["_script_path"]), ctx.name)   # see format15/generate's identical comment

    script_id = Path(script["_script_path"]).stem
    turns = build_turns(script)
    a_lines = [{"turn": i, "text": text} for i, (speaker, text) in enumerate(turns) if speaker == "char_a"]
    b_lines = [{"turn": i, "text": text} for i, (speaker, text) in enumerate(turns) if speaker == "char_b"]

    return jsonify({
        "script_id": script_id,
        "subject": script["subject"],
        "char_a_name": script.get("char_a_name"),
        "char_b_name": script.get("char_b_name"),
        "a_lines": a_lines,
        "b_lines": b_lines,
    })


@app.route("/api/format16/upload_audio", methods=["POST"])
def api_format16_upload_audio():
    """Step 2: same mechanism as format15's panel (see red_phone._find_external_audio)
    -- `side` ("a" or "b") picks which character this recording belongs to. Can be
    uploaded across several separate takes (see _next_recording_path), not just one
    continuous take."""
    from modules.assembler.red_phone import voice_input_dir

    script_id = request.form.get("script_id", "")
    side = request.form.get("side", "")
    file = request.files.get("audio")
    if side not in ("a", "b"):
        return jsonify({"error": "side doit être 'a' ou 'b'"}), 400
    if not file or not file.filename:
        return jsonify({"error": "Fichier audio manquant"}), 400

    try:
        script = _load_format16_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    folder = voice_input_dir(script["subject"])
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".mp3"
    dest = _next_recording_path(folder, side, ext)
    file.save(str(dest))

    logger.info(f"format16: saved character {side!r} recording for {script['subject']!r} -> {dest}")
    return jsonify({"ok": True, "saved_as": dest.name})


@app.route("/api/format16/build", methods=["POST"])
def api_format16_build():
    """Step 3: assemble the final video -- same job-queue/polling pattern as
    format15's build."""
    from modules.assembler import assemble

    data = request.json or {}
    script_id = data.get("script_id", "")
    ctx = users.get_user(data.get("user"))
    try:
        script = _load_format16_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    job_id = f"f16_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        path = assemble(script, {}, voice_result=None, timestamps_result=None, music_path=None)
        try:
            script_path = Path(script.get("_script_path") or "")
            if script_path.exists():
                new_path = script_path.parent / (path.stem[:15] + script_path.name[15:])
                if new_path != script_path:
                    script_path.rename(new_path)
                new_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"format16 build: couldn't relocate script after assembly: {e}")
        path = users.tag_and_relocate(path, ctx)   # see format15/build's identical comment
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "format16"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


_FORMAT18_UPLOAD_DIR = BASE_DIR / "scratch" / "format18_uploads"


@app.route("/api/format18/build", methods=["POST"])
def api_format18_build():
    """
    White Card Reaction (format18) -- fully automatic (2026-07-29, explicit
    user ask: "je veux que ce soit toi qui le fasses tout seul"). The user
    supplies only the source clip (an uploaded file OR a link -- Snapchat
    Spotlight/TikTok/IG/YouTube, anywhere yt-dlp has an extractor, see
    modules/clips/link_download.py) and, optionally, a one-line idea to steer
    the joke. Claude looks at the clip's own footage to write the THEM:/ME:/
    POV: caption AND to find/redact any baked-in watermark -- see
    modules/assembler/white_card_reaction.auto_build_white_card_reaction.

    A pasted link is NOT downloaded here in the request handler -- yt-dlp can
    take a while and this would block the Flask worker for the whole request.
    It's deferred into the background build job instead, same place the
    vision calls and the actual ffmpeg render happen.
    """
    file = request.files.get("video")
    url = (request.form.get("url") or "").strip()
    if (not file or not file.filename) and not url:
        return jsonify({"error": "Fournis une vidéo (fichier) ou un lien"}), 400

    idea = (request.form.get("idea") or "").strip() or None
    output_name = (request.form.get("output_name") or "").strip() or "white_card_reaction"
    # "allow_teaser" (2026-08-10): checkbox, default on (unchanged judgment-
    # call behavior) -- "0"/"false" turns the teaser mechanism off entirely
    # for this build, see auto_build_white_card_reaction's docstring.
    allow_teaser = (request.form.get("allow_teaser") or "1").strip().lower() not in ("0", "false")
    # "keep_music" (2026-08-10, explicit user ask): checkbox, default off --
    # the automatic has_background_music() detector + Demucs strip stays the
    # default judgment call; checking this forces force=False into
    # maybe_strip_music below, skipping detection+removal entirely so the
    # clip's original audio (including its music) is kept untouched.
    keep_music = (request.form.get("keep_music") or "0").strip().lower() in ("1", "true")
    ctx = users.get_user(request.form.get("user"))

    dest = None
    if file and file.filename:
        _FORMAT18_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename).suffix.lower() or ".mp4"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        dest = _FORMAT18_UPLOAD_DIR / f"{ts}{ext}"
        file.save(str(dest))

    job_id = f"f18_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        from modules.assembler.white_card_reaction import auto_build_white_card_reaction
        from modules.clips.music_removal import maybe_strip_music

        input_path = dest
        if input_path is None:
            from modules.clips.link_download import download_clip
            input_path = download_clip(url, _FORMAT18_UPLOAD_DIR)

        input_path = maybe_strip_music(Path(input_path), force=(False if keep_music else None))

        path = auto_build_white_card_reaction(str(input_path), idea=idea, output_name=output_name,
                                               allow_teaser=allow_teaser, ctx=ctx)
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "format18"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


_REMIX_UPLOAD_DIR = BASE_DIR / "scratch" / "viral_remix_uploads"

# proposal_id -> {"ctx_name": str, "source_path": Path, "plan": dict}, bridging
# the two-phase remix flow's propose step (api_remix_source) to its finalize
# step (api_remix_finalize) -- see api_remix_source's docstring. One-shot:
# popped the moment a finalize call consumes it. In-memory only (same
# lifetime assumption as `_jobs`) -- fine for this single-user local tool, no
# TTL/cleanup needed since a stray unconsumed entry just sits there harmlessly
# until the process restarts.
_remix_proposals: dict[str, dict] = {}


@app.route("/api/remix_source", methods=["POST"])
def api_remix_source():
    """
    "Remix vidéo virale" -- Publication panel's 3rd input source (2026-08-13):
    take a clip that already buzzed elsewhere (an uploaded file OR a pasted
    link -- same file-or-url pattern as format18's build route above,
    anywhere yt-dlp has an extractor, see modules/clips/link_download.py)
    and hand back a version transformed just enough to dodge reused-content
    detection while keeping the clip's OWN native framing (no white-card
    recomposition -- see modules/assembler/viral_remix.py's module docstring
    for why this is a separate module from format18 rather than a variant
    of it). Brand-agnostic and NOT tied to any one format -- available for
    every brand from the Publication panel.

    Same as format18's build route: a pasted link is NOT downloaded here in
    the request handler (yt-dlp can take a while) -- deferred into the
    background job along with the vision call.

    This is PHASE 1 ONLY as of 2026-08-17 (real user ask): the job here runs
    analyze_remix_source and, for the common case, goes straight on to build
    the finished video exactly like before -- job result shape unchanged,
    {"video", "size_mb", "format"}. But when the source clip is a "quote"
    archetype (a single striking quote/statement card), the job stops SHORT
    of building anything and instead returns {"needs_choice": true,
    "proposal_id": str, "original": str, "candidates": [str, ...]} -- a few
    reworded options (viral_remix.propose_quote_paraphrases) for the user to
    pick from in the UI, since a single auto-picked paraphrase's quality
    varied too much call to call in testing to skip the review step (real
    example: a "...your girl says 'omg, WE got a Lamborghini'" clip). The
    frontend then posts the pick to /api/remix_source/finalize (phase 2)
    to actually build the video. `_remix_proposals` holds the already-
    downloaded source path + decided plan between the two calls so the user
    never re-uploads/re-pastes anything.

    Only ever produces a finished mp4 -- does not publish anything itself.
    The frontend fetches the result back via the existing /videos/<filename>
    route and feeds it into the normal Publication upload flow
    (api_publish_upload), unchanged, so title/description/scheduling all
    still go through the exact same path as any hand-uploaded video.
    """
    file = request.files.get("source_video")
    url = (request.form.get("source_url") or "").strip()
    if (not file or not file.filename) and not url:
        return jsonify({"error": "Fournis une vidéo (fichier) ou un lien"}), 400

    angle_hint = (request.form.get("angle_hint") or "").strip() or None
    ctx = users.get_user(request.form.get("user"))

    dest = None
    if file and file.filename:
        _REMIX_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename).suffix.lower() or ".mp4"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        dest = _REMIX_UPLOAD_DIR / f"{ts}{ext}"
        file.save(str(dest))

    job_id = f"remix_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        from modules.assembler.viral_remix import (
            analyze_remix_source, build_remix_from_plan, propose_quote_paraphrases,
        )

        input_path = dest
        if input_path is None:
            from modules.clips.link_download import download_clip
            input_path = download_clip(url, _REMIX_UPLOAD_DIR)
        input_path = Path(input_path)

        plan = analyze_remix_source(ctx, input_path, angle_hint=angle_hint)

        if plan.get("needs_quote_choice"):
            candidates = propose_quote_paraphrases(plan["quote_original"], n=4)
            proposal_id = f"remixq_{datetime.now().strftime('%H%M%S%f')}"
            _remix_proposals[proposal_id] = {
                "ctx_name": ctx.name, "source_path": input_path, "plan": plan,
            }
            return {"needs_choice": True, "proposal_id": proposal_id,
                    "original": plan["quote_original"], "candidates": candidates}

        path = build_remix_from_plan(ctx, input_path, plan)
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "remix"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


@app.route("/api/remix_source/finalize", methods=["POST"])
def api_remix_finalize():
    """
    Phase 2 of the two-phase "quote" remix flow (see api_remix_source) --
    takes the `proposal_id` from that call's job result plus the user's
    picked (or hand-edited) `quote_choice` line, and actually builds the
    video using the plan + already-downloaded source path stashed in
    `_remix_proposals`. Same job-result shape as api_remix_source's non-
    "quote" fast path: {"video", "size_mb", "format"}.
    """
    data = request.get_json(silent=True) or request.form
    proposal_id = (data.get("proposal_id") or "").strip()
    quote_choice = (data.get("quote_choice") or "").strip()
    if not proposal_id or proposal_id not in _remix_proposals:
        return jsonify({"error": "Proposition introuvable ou expirée -- relance le remix."}), 400
    if not quote_choice:
        return jsonify({"error": "Choisis (ou écris) une citation."}), 400

    proposal = _remix_proposals.pop(proposal_id)
    ctx = users.get_user(proposal["ctx_name"])
    source_path = proposal["source_path"]
    plan = proposal["plan"]

    job_id = f"remixfin_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        from modules.assembler.viral_remix import build_remix_from_plan
        path = build_remix_from_plan(ctx, source_path, plan, quote_choice=quote_choice)
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "remix"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


_LONG1_SCRIPTS_DIR = OUTPUT_DIR / "scripts"
_LONG1_SECTIONS = ["hook", "engrenage", "psychologie", "plan_action"]


def _load_long1_script(script_id: str) -> dict:
    path = _LONG1_SCRIPTS_DIR / f"{script_id}.json"
    if not path.exists() or ".." in script_id or "/" in script_id or "\\" in script_id:
        raise FileNotFoundError(f"Unknown script_id: {script_id!r}")
    return json.loads(path.read_text(encoding="utf-8"))


@app.route("/api/long1/generate", methods=["POST"])
def api_long1_generate():
    """Dedicated long1 panel, step 1: subject optional (empty = Claude picks its
    own real financial ruin/success case, see script_generator._generate_long1_script).
    Returns each section's beat texts so the user knows exactly what to read
    when recording -- this format's narration is the user's own real voice,
    never TTS."""
    from modules.script_generator import generate_script
    from modules.assembler.long1 import build_beats

    data = request.json or {}
    subject = (data.get("subject") or "").strip()

    try:
        script = generate_script("long1", subject)
    except Exception as e:
        logger.error(f"long1 script generation failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

    script_id = Path(script["_script_path"]).stem
    beats = build_beats(script)
    sections = {}
    for sec_id in _LONG1_SECTIONS:
        sec_beats = [b for b in beats if b["section"] == sec_id]
        sections[sec_id] = {
            "label": (script.get("sections", {}).get(sec_id) or {}).get("label", sec_id),
            "lines": [b["text"] for b in sec_beats],
        }

    return jsonify({
        "script_id": script_id,
        "subject": script["subject"],
        "case_subject": script.get("case_subject"),
        "narrator_persona": script.get("narrator_persona"),
        "title": script.get("title"),
        "sections": sections,
    })


@app.route("/api/long1/upload_audio", methods=["POST"])
def api_long1_upload_audio():
    """Step 2: the user reads that section's beats in order -- can be split
    across several separate uploads (see _next_recording_path), not just one
    continuous take. Joined then auto-split into per-beat clips at build
    time via Whisper alignment (see
    modules.assembler.long1._resolve_section_recording / voice_split)."""
    from modules.assembler.long1 import voice_input_dir

    script_id = request.form.get("script_id", "")
    section = request.form.get("section", "")
    file = request.files.get("audio")
    if section not in _LONG1_SECTIONS:
        return jsonify({"error": f"section doit etre l'un de {_LONG1_SECTIONS}"}), 400
    if not file or not file.filename:
        return jsonify({"error": "Fichier audio manquant"}), 400

    try:
        script = _load_long1_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    folder = voice_input_dir(script["subject"])
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".mp3"
    dest = _next_recording_path(folder, section, ext)
    file.save(str(dest))

    logger.info(f"long1: saved {section!r} recording for {script['subject']!r} -> {dest}")
    return jsonify({"ok": True, "saved_as": dest.name})


@app.route("/api/long1/build", methods=["POST"])
def api_long1_build():
    """Step 3: assemble the final documentary -- image search per beat + Whisper
    alignment/splitting per section + rendering all take real time (several
    minutes for a 12-15 min video). Fails with a clear list of missing
    sections rather than a generic 500 if any recording is absent -- this
    format never falls back to synthetic TTS."""
    from modules.assembler import assemble

    data = request.json or {}
    script_id = data.get("script_id", "")
    try:
        script = _load_long1_script(script_id)
    except FileNotFoundError:
        return jsonify({"error": "script_id inconnu"}), 404

    job_id = f"long1_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _build():
        path = assemble(script, {}, voice_result=None, timestamps_result=None, music_path=None)
        try:
            script_path = Path(script.get("_script_path") or "")
            if script_path.exists():
                new_path = script_path.parent / (path.stem[:15] + script_path.name[15:])
                if new_path != script_path:
                    script_path.rename(new_path)
                new_path.write_text(json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"long1 build: couldn't relocate script after assembly: {e}")
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "long1"}

    _enqueue_generation(job_id, _build)
    return jsonify({"job_id": job_id})


@app.route("/api/publish", methods=["POST"])
def api_publish():
    data      = request.json or {}
    video_name = data.get("video")
    platforms  = data.get("platforms", [])

    if not video_name:
        return jsonify({"error": "video is required"}), 400

    video_path = _resolve_video_path(video_name)
    if video_path is None:
        return jsonify({"error": f"Video not found: {video_name}"}), 404

    script = _find_script(video_path)
    job_id = f"pub_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _publish():
        # youtube_long has no Drive+email flow (modules.publish.publisher only knows
        # "youtube"/"instagram"/"tiktok" and would silently no-op) -- it goes
        # direct-to-Zernio instead, same as scheduled batch publishing, using
        # the title/description/thumbnail generated at render time.
        if script.get("format") == "youtube_long" or "youtube_long" in platforms:
            ctx = users.get_user(data.get("user"))
            when_raw = data.get("when_datetime", "")
            if when_raw:
                try:
                    when = datetime.fromisoformat(when_raw)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=_PUBLISH_TZ)
                except ValueError:
                    return {"youtube_long": "ERROR: date/heure invalide"}
            else:
                when = datetime.now(_PUBLISH_TZ)
            res = _schedule_post(video_path, script, when, ctx,
                                 platforms=["youtube_long"])
            if res.status in ("created", "dry-run", "skipped", "partial"):
                return {p: f"scheduled @ {res.scheduled_time}" for p in (res.networks or ["youtube_long"])}
            return {p: f"ERROR: {res.detail}" for p in ["youtube_long"]}

        from modules.publish.publisher import publish
        return publish(video_path, script, platforms=platforms or None)

    _run_in_thread(job_id, _publish)
    return jsonify({"job_id": job_id})


@app.route("/api/format3/template/pending")
def api_format3_template_pending():
    """List format3 'Mode template' videos awaiting their CapCut-edited
    finished export (see modules.publish.template_parts) -- populates the
    Publication panel's attach-to-template dropdown."""
    ctx = users.get_user(request.args.get("user"))
    from modules.publish.template_parts import list_pending
    return jsonify({"pending": list_pending(ctx)})


@app.route("/api/publish_upload", methods=["POST"])
def api_publish_upload():
    """
    Publish one or more already-existing video files the user drops in
    directly (not generated by the pipeline). Each video is published
    independently: its own title/description (modules.publish.upload_meta,
    start+end frame vision), its own script JSON.

    mode (2026-08-06, added alongside the "planification en lot" panel's own
    Manuel(email)/Auto-schedule/Publication modes -- Publication had no email
    option at all before, only direct):
      "direct" (default) -> real Ayrshare/Zernio publish (like auto-schedule).
      "email"            -> same Drive-upload + recap-email flow as "Manuel
                             (email)" elsewhere (modules.publish.publisher.publish),
                             posted by hand instead of via the SaaS APIs. Ignores
                             `when`/spread entirely -- the email flow has no
                             concept of a publish TIME, just a next-available
                             posting-window suggestion per platform.

    when (mode="direct" only):
      "now"    -> exactly one file, published immediately (no spacing needed
                  for a single video).
      "single" -> exactly one file, exact datetime from when_datetime (unchanged
                  single-video behavior).
      "spread" -> multiple files, ALWAYS spread -- never a simultaneous blast,
                  whether triggered from "Maintenant" or "Programmer". Same
                  slot-split mechanism as auto-schedule either way:
                    spread_mode "auto"   -> videos_per_day, 07:00->01:00 window,
                                            >=2h gap (_day_publish_times). Anchored
                                            at start_date if given, else today (US Eastern).
                    spread_mode "manual" -> fixed interval_minutes between posts,
                                            optional videos_per_day cap. Anchored
                                            at manual_start if given, else right now.
                  The last day gets whatever remains even if fewer than videos_per_day.
                  NOTE: this "manual" is spread_mode's fixed-interval timing knob,
                  unrelated to mode="email" above -- same word, different axis.
    """
    mode = request.form.get("mode", "direct")
    if mode not in ("direct", "email"):
        return jsonify({"error": "mode invalide"}), 400
    files = [f for f in request.files.getlist("video") if f and f.filename]
    if not files:
        return jsonify({"error": "Vidéo manquante"}), 400

    platforms_raw = request.form.get("platforms", "")
    platforms = [p.strip() for p in platforms_raw.split(",") if p.strip()]
    if not platforms or not set(platforms).issubset(_VALID_PLATFORMS):
        return jsonify({"error": f"Plateformes invalides (valides: {sorted(_VALID_PLATFORMS)})"}), 400

    # mode="email" ignores when/spread entirely -- the Drive+email flow has no
    # concept of an exact publish time, just a next-available-window SUGGESTION
    # per platform (modules.publish.publisher.publish -> get_post_time), same as
    # "Manuel (email)" elsewhere never asks for one either.
    when_mode = request.form.get("when", "now") if mode == "direct" else "now"
    if mode == "direct":
        if when_mode not in ("now", "single", "spread"):
            return jsonify({"error": "when invalide"}), 400
        if when_mode == "single" and len(files) != 1:
            return jsonify({"error": "'single' n'est valide que pour une seule vidéo"}), 400
        if when_mode == "now" and len(files) > 1:
            return jsonify({"error": "'now' n'est valide que pour une seule vidéo (utilise 'spread' pour plusieurs -- jamais de publication simultanée en masse)"}), 400

    user = request.form.get("user") or users.active_user()
    ctx  = users.get_user(user)

    # format3 "Mode template" finish step: attach this upload (the user's
    # CapCut export built from the 9 parts we handed them) to a pending
    # template-mode video instead of treating it as a brand-new upload --
    # skips the vision-based meta guess (modules.publish.upload_meta) in
    # favor of the ORIGINAL script's real hook/subject (see
    # modules.publish.template_parts.add_pending), and blurs CapCut's
    # watermark before anything gets published. Only meaningful for a single
    # file (there's exactly one pending video to attach to).
    template_pending_id = request.form.get("template_pending_id", "").strip()
    blur_corner = request.form.get("blur_corner", "top-right")
    try:
        blur_w_pct = float(request.form.get("blur_w_pct", 0.25))
        blur_h_pct = float(request.form.get("blur_h_pct", 0.10))
    except ValueError:
        return jsonify({"error": "Zone de floutage invalide"}), 400

    pending_record = None
    if template_pending_id:
        if len(files) != 1:
            return jsonify({"error": "Le rattachement à un template ne fonctionne que pour une seule vidéo"}), 400
        from modules.assembler.watermark import _CORNERS
        if blur_corner not in _CORNERS:
            return jsonify({"error": f"Coin invalide (valides: {list(_CORNERS)})"}), 400
        from modules.publish.template_parts import get_pending
        pending_record = get_pending(ctx, template_pending_id)
        if pending_record is None:
            return jsonify({"error": "Template inconnu ou déjà traité"}), 404

    # Save all files first, each with its OWN unique 15-char timestamp prefix
    # (base + i seconds) -- _find_script()/app dashboard glob on that exact
    # prefix, a shared batch timestamp would make every video in the same
    # upload match every other video's script.
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    base_dt = datetime.now()
    saved: list[Path] = []
    for i, f in enumerate(files):
        ts   = (base_dt + timedelta(seconds=i)).strftime("%Y%m%d_%H%M%S")
        ext  = Path(f.filename).suffix or ".mp4"
        dest = VIDEOS_DIR / f"{ts}_upload{ext}"
        f.save(str(dest))
        saved.append(dest)

    finish_at: datetime | None = None
    if when_mode == "now":
        whens = [datetime.now(_PUBLISH_TZ) for _ in saved]
    elif when_mode == "single":
        raw = request.form.get("when_datetime", "")
        try:
            when = datetime.fromisoformat(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=_PUBLISH_TZ)
        except ValueError:
            return jsonify({"error": "Date/heure invalide"}), 400
        whens = [when]
        finish_at = when
    else:  # spread -- either "auto" (existing) or "manual" (fixed interval, see _compute_manual_whens)
        spread_mode = request.form.get("spread_mode", "auto")
        if spread_mode == "auto":
            start_raw = request.form.get("start_date", "")
            if start_raw:
                try:
                    start_dt = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=_PUBLISH_TZ)
                except ValueError:
                    return jsonify({"error": "Date de début invalide"}), 400
            else:
                # "Maintenant" + plusieurs vidéos: pas de date choisie -- l'étalement
                # démarre aujourd'hui (heure US Eastern). _day_publish_times expects
                # day0 at midnight (it adds window-start-of-day minute offsets on top).
                start_dt = datetime.now(_PUBLISH_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                vpd = int(request.form.get("videos_per_day", 1))
            except (ValueError, TypeError):
                return jsonify({"error": "vidéos/jour invalide"}), 400
            if vpd < 1:
                return jsonify({"error": "vidéos/jour doit être >= 1"}), 400

            whens = []
            remaining = len(saved)
            day = 0
            while remaining > 0:
                day_count = min(vpd, remaining)
                whens.extend(_day_publish_times(start_dt + timedelta(days=day), day_count))
                remaining -= day_count
                day += 1

        elif spread_mode == "manual":
            manual_raw = request.form.get("manual_start", "")
            if manual_raw:
                try:
                    manual_start = datetime.fromisoformat(manual_raw)
                    if manual_start.tzinfo is None:
                        manual_start = manual_start.replace(tzinfo=_PUBLISH_TZ)
                except ValueError:
                    return jsonify({"error": "Date/heure de départ invalide"}), 400
            else:
                # "Maintenant" + plusieurs vidéos: le départ est implicitement maintenant.
                manual_start = datetime.now(_PUBLISH_TZ)
            try:
                interval_minutes = int(request.form.get("interval_minutes", 0))
            except (ValueError, TypeError):
                return jsonify({"error": "Intervalle invalide"}), 400
            if interval_minutes < 1:
                return jsonify({"error": "Intervalle doit être >= 1 minute"}), 400

            vpd_raw = request.form.get("videos_per_day", "").strip()
            vpd = None   # None = illimité
            if vpd_raw:
                try:
                    vpd = int(vpd_raw)
                except ValueError:
                    return jsonify({"error": "vidéos/jour invalide"}), 400
                if vpd < 1:
                    return jsonify({"error": "vidéos/jour doit être >= 1 (ou vide pour illimité)"}), 400

            whens = _compute_manual_whens(manual_start, interval_minutes, vpd, len(saved))
        else:
            return jsonify({"error": "spread_mode invalide"}), 400

        finish_at = whens[-1]

    job_id = f"upload_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status": "queued", "message": "", "result": None,
        "progress": {"done": 0, "total": len(saved), "current": "En attente…"},
    }

    def _run():
        from modules.publish.upload_meta import generate_meta_from_video
        results = []
        for i, (video_path, when) in enumerate(zip(saved, whens)):
            _jobs[job_id]["progress"] = {
                "done": i, "total": len(saved),
                "current": f"Vidéo {i + 1}/{len(saved)} — {video_path.name}",
            }
            try:
                if pending_record is not None:
                    from modules.assembler.watermark import blur_corner as _blur_corner_region
                    blurred = video_path.with_name(f"{video_path.stem}_blurred{video_path.suffix}")
                    _blur_corner_region(video_path, blurred, corner=blur_corner,
                                        w_pct=blur_w_pct, h_pct=blur_h_pct)
                    video_path = blurred   # published file is the blurred one; script.json below matches its stem
                    script = pending_record["script"]
                    from modules.publish.template_parts import mark_done
                    mark_done(ctx, template_pending_id)
                else:
                    meta   = generate_meta_from_video(video_path)
                    script = {"format": "uploaded", "subject": meta["title"], "hook": "", "_meta": meta}

                SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
                (SCRIPTS_DIR / f"{video_path.stem}.json").write_text(
                    json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8"
                )

                if mode == "email":
                    # Same Drive-upload + recap-email flow "Manuel (email)" uses
                    # elsewhere (modules.publish.publisher.publish) -- results
                    # dict is {platform: drive_link} on success, {platform: "ERROR: ..."} on failure.
                    from modules.publish.publisher import publish as publish_manual
                    pub_results = publish_manual(video_path, script, platforms=platforms, days_ahead=1)
                    failed = {p: r for p, r in pub_results.items() if str(r).startswith("ERROR:")}
                    status = "error" if failed else "manual"
                    results.append({"video": video_path.name, "status": status,
                                    "networks": list(pub_results.keys()),
                                    "when": None, "detail": "; ".join(failed.values()) if failed else ""})
                    logger.info(f"Publish-upload {i + 1}/{len(saved)} email/{status} — {video_path.name}")
                else:
                    res = _schedule_post(video_path, script, when, ctx, platforms=platforms)
                    results.append({"video": video_path.name, "status": res.status,
                                    "networks": res.networks, "when": res.scheduled_time})
                    logger.info(f"Publish-upload {i + 1}/{len(saved)} {res.status} — {video_path.name}")
            except Exception as e:
                results.append({"video": video_path.name, "status": "error", "error": str(e)})
                logger.error(f"Publish-upload {i + 1}/{len(saved)} FAILED: {e}", exc_info=True)

        _jobs[job_id]["progress"] = {"done": len(saved), "total": len(saved), "current": "Terminé"}
        ok = sum(1 for r in results if r["status"] != "error")
        return {"results": results, "total": len(saved), "ok": ok,
                "finish_at": finish_at.isoformat() if finish_at else None}

    _run_in_thread(job_id, _run)
    return jsonify({"job_id": job_id, "total": len(saved),
                    "finish_at": finish_at.isoformat() if finish_at else None})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/setup_session", methods=["POST"])
def api_setup_session():
    platform = (request.json or {}).get("platform")
    if platform not in ("tiktok", "instagram"):
        return jsonify({"error": "Invalid platform"}), 400
    subprocess.Popen(
        [sys.executable, str(BASE_DIR / "tools" / "publishing" / "setup_sessions.py"), "--platform", platform],
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
    )
    return jsonify({"ok": True, "message": f"Browser opening for {platform} login..."})


@app.route("/api/videos")
def api_videos():
    return jsonify(_video_list())


# ── Brands (multi-user) ─────────────────────────────────────────────────────

@app.route("/api/users", methods=["GET"])
def api_users():
    return jsonify({"users": users.list_users(), "active": users.active_user()})


@app.route("/api/users/active", methods=["POST"])
def api_set_active_user():
    name = (request.json or {}).get("user", "")
    try:
        users.set_active_user(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "active": name})


@app.route("/api/users", methods=["POST"])
def api_create_user():
    data = request.json or {}
    name = data.get("name", "")
    try:
        ctx = users.create_user(
            name,
            generator=data.get("generator", users.DEFAULT_USER),
            default_networks=data.get("default_networks"),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    users.set_active_user(ctx.name)
    return jsonify({"ok": True, "user": ctx.name})


# ── Brand factory: one-click "new brand" automation (2026-08-12) ───────────
# See modules/brand_factory.py's module docstring for the full pipeline.
# Runs in a background thread (mirrors the batch-generation pattern already
# used elsewhere in this file); the dashboard polls /api/brand_factory/status.

@app.route("/api/brand_factory/start", methods=["POST"])
def api_brand_factory_start():
    from modules import brand_factory
    # Block a second concurrent run -- a job survives independently of the
    # tab/request that launched it (see brand_factory.run_async), so nothing
    # previously stopped a double-click or a retry-after-error from firing two
    # full research+book Claude runs in parallel for the same intent (found
    # live 2026-08-12 during UI testing). The dashboard reattaches its poller
    # to the existing job_id instead of erroring out.
    existing = brand_factory.active_job()
    if existing:
        return jsonify({"error": "Une marque automatique est déjà en cours de création.",
                         "job_id": existing.get("job_id")}), 409
    hint = (request.json or {}).get("hint", "") if request.is_json else ""
    job_id = brand_factory.run_async(hint)
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/brand_factory/status")
def api_brand_factory_status():
    from modules import brand_factory
    job_id = request.args.get("job_id", "")
    if job_id:
        status = brand_factory.get_status(job_id)
        if status is None:
            return jsonify({"error": "unknown job_id"}), 404
        return jsonify(status)
    return jsonify({"jobs": brand_factory.list_jobs()})


# ── Batch generation ──────────────────────────────────────────────────────────

_BATCH_SUBJECTS = [
    "how compound interest makes you rich while you sleep",
    "the 3 financial habits of self-made millionaires",
    "why the poor trade time for money and the rich don't",
    "how to go from zero to 1000 dollars saved in 90 days",
    "the rule of 72 — how long to double your money",
    "why most people stay broke despite a good salary",
    "how to buy assets instead of liabilities",
    "the investment that changed everything for me",
    "stop upgrading your lifestyle every time you earn more",
    "the difference between income and wealth",
    "how to build passive income from scratch with nothing",
    "the one mindset shift that separates the wealthy from everyone else",
    "why saving money alone will never make you rich",
    "the debt trap most people never escape",
    "how to think like an investor not a consumer",
    "why your salary is your worst financial plan",
    "the three accounts every wealthy person keeps",
    "how small daily habits create massive financial results",
    "the first 1000 dollars — why it changes everything",
    "why the rich get richer while the middle class shrinks",
]

# format6 (ranking list) is a different niche entirely (people/habits/mindset,
# not wealth) — kept in its own pool so it never gets a money-themed subject
# from _BATCH_SUBJECTS.
_RANKING_SUBJECTS = [
    "types of toxic people to stay away from",
    "signs someone is a fake friend",
    "habits that secretly kill your dreams",
    "red flags in a relationship you should never ignore",
    "types of people who drain your energy",
    "mistakes that keep you poor and stuck",
    "signs you're surrounded by the wrong people",
    "excuses that are quietly ruining your life",
    "types of negative people to cut off immediately",
    "warning signs of a manipulator",
    "habits of people who never grow",
    "signs someone doesn't respect you",
    "things confident people never do",
    "reasons most people never reach their goals",
    "signs you're in your own way",
    "types of friends you should never trust",
    "habits that destroy your discipline",
    "signs of a one-sided friendship",
    "things that silently kill motivation",
    "traits of people who never succeed",
]

# MindFile (psychology/human-behavior brand, see users/mindfile/) -- its OWN
# subject pool, distinct from ResetWorth's wealth/ranking pools above. Same
# generator (config.json: "generator": "resetworth" -- literally pipeline.run
# under the hood, just different format ids), so _pick_subjects's existing
# similarity/anti-dup logic works unchanged with this as pool_source.
_MINDFILE_SUBJECTS = [
    "the reason you keep replaying that one embarrassing memory",
    "why your brain remembers insults better than compliments",
    "the psychology behind why silence feels like rejection",
    "why you people-please even when you hate it",
    "the real reason you sabotage things that are going well",
    "why criticism from strangers hurts more than it should",
    "the trauma response hiding behind your need for control",
    "why you overexplain yourself to people who barely asked",
    "the psychology of why you miss people who hurt you",
    "why your inner voice sounds like your harshest critic",
    "the reason you feel guilty for resting",
    "why you attract the same type of person over and over",
    "the psychology behind doomscrolling at 2am",
    "why validation from others never actually fixes anything",
    "the real reason you can't accept a compliment",
    "why you overthink texts that took 5 seconds to read",
    "the psychology of why comparison ruins your mood instantly",
    "why you stay loyal to people who never chose you back",
    "the reason your anxiety shows up as anger",
    "why you feel most alone in a room full of people",
]


def _make_format_schedule(n: int, mode: str = "auto") -> list[tuple[str, bool]]:
    """
    Build a format schedule for n videos: (format, use_hook) pairs.
    mode="format1" → all format1 (clip edit), no hook
    mode="format7" → all format7 (clip edit 2.0), no hook
    mode="format6" → all format6 (ranking list), no hook
    mode="format3" → all format3 (luxury, mixed image/video pool), no hook
    mode="auto"    → the real mix the user asked for: 1/4 format1, 1/4
                     format3 PLAIN (no hook), 1/4 format3 WITH a hook, 1/4
                     format6 (ranking list, dark B-roll). e.g. 32 → 8 f1 +
                     8 f3(plain) + 8 f3(hook) + 8 f6. Earlier version was a
                     1/3 split without format6 -- format6 added as its own
                     even slice alongside the format3/luxury slots, not
                     folded into them (different niche, different visual
                     style, needs its own share of the rotation).
    Types are spread evenly in a cycling pattern — never bunched.
    """
    if mode == "format1":
        return [("format1", False)] * n
    if mode == "format7":
        return [("format7", False)] * n
    if mode == "format6":
        return [("format6", False)] * n
    if mode == "format3":
        return [("format3", False)] * n

    # auto: 1/4 format1, 1/4 format3 plain, 1/4 format3 WITH hook, 1/4 format6
    n_f1       = max(0, min(n, round(n / 4)))
    n_f6       = max(0, min(n - n_f1, round(n / 4)))
    n_f3_total = n - n_f1 - n_f6
    n_f3_hook  = n_f3_total // 2
    n_f3_plain = n_f3_total - n_f3_hook

    queues: dict[str, list[tuple[str, bool]]] = {
        "format1":      [("format1", False)] * n_f1,
        "format3plain": [("format3", False)] * n_f3_plain,
        "format3hook":  [("format3", True)] * n_f3_hook,
        "format6":      [("format6", False)] * n_f6,
    }
    order = ["format1", "format3plain", "format3hook", "format6"]
    result: list[tuple[str, bool]] = []
    t = 0
    while len(result) < n:
        key = order[t % len(order)]
        if queues[key]:
            result.append(queues[key].pop(0))
        else:
            # Queue empty — drain any non-empty queue
            for k in order:
                if queues[k]:
                    result.append(queues[k].pop(0))
                    break
        t += 1
    return result[:n]


_CUSTOM_MIX_FORMAT_KEYS = ["format1", "format7", "format6", "format15", "format16", "format18", "format3"]


_FORMAT3_MODES = ("none", "hook", "beat_match", "hook_recut", "template")


def _flatten_day_plans(
    day_plans: list[dict], format_keys: list[str], hook_field: bool = True,
) -> tuple[list[tuple[str, bool]], list[int], list[int], list[list[str | None]], list[int],
           list[str], list[dict | None]]:
    """
    Flatten a day_plans list (see _generate_batch's day_plans docstring) into
    the flat per-slot structures the batch generators / manual-queue /
    publish-time code consume. Shared between ResetWorth's and MindFile's
    batch routes -- identical shape/logic, only the mix keys and hook
    eligibility differ (MindFile has no hook-eligible format). Replaces the
    old _make_custom_schedule/_make_mindfile_custom_schedule, which forced
    the exact same per-day pattern to repeat every day -- each day here is
    fully independent, including summing to 0 (no video that day).

    format_keys: mix keys in flattening order, hook-eligible format (if any)
    LAST -- ResetWorth passes _CUSTOM_MIX_FORMAT_KEYS (format3 last),
    MindFile passes _MINDFILE_SHORT_FORMATS with hook_field=False.

    format3_mode_per_slot (2026-08-06, "mix hook/beat-match/hook-recut per
    video" user ask): each format3 occurrence gets its OWN mode instead of
    one global choice for the whole batch -- day["hook_modes"], a list
    aligned with THAT day's format3 slots only (not the full day pattern),
    each one of _FORMAT3_MODES. Falls back to the OLD hook_count-based
    scheme (first hook_count format3 slots -> "hook", rest -> "none") when
    hook_modes is absent, so a stale/older request body still works.
    schedule's `use_hook` bool stays True iff mode=="hook" specifically
    (nothing downstream that reads schedule directly needs to change) --
    format3_mode_per_slot is the new parallel source of truth for the fuller
    4-way choice, consulted by _generate_batch's _render_slot.

    publish_override_per_slot (2026-08-06, "per-video publish mode" user
    ask): day["publish_overrides"], a list aligned with the FULL day pattern
    (same convention as `times`) of {"mode": "auto"|"manual", "networks":
    [...]} | null -- a per-slot override of the batch-global mode/platforms,
    consulted by _dispatch_auto/_dispatch_manual_week. null/absent means
    "use the batch-global setting", so days/slots that don't set this are
    completely unaffected.

    Returns (schedule, day_index_per_slot, slot_index_within_day,
    fixed_times_by_day, day_vpd, format3_mode_per_slot, publish_override_per_slot):
      schedule               list[(fmt, use_hook)] -- see format3_mode_per_slot above.
      day_index_per_slot     0-based day index per slot (replaces the old
                              `i // vpd` math -- vpd is no longer uniform
                              across days).
      slot_index_within_day  0-based position of each slot within its day.
      fixed_times_by_day     one list per day of "HH:MM"|"now"|None, aligned with
                              that day's own slot order -- passed straight
                              to _day_publish_times' `fixed` param.
      day_vpd                videos that day, len == len(day_plans).
      format3_mode_per_slot  one of _FORMAT3_MODES per schedule slot ("none"
                              for every non-format3 slot).
      publish_override_per_slot  {"mode", "networks"} | None per schedule slot.
    """
    hook_key = format_keys[-1] if hook_field else None
    schedule: list[tuple[str, bool]] = []
    day_index_per_slot: list[int] = []
    slot_index_within_day: list[int] = []
    fixed_times_by_day: list[list[str | None]] = []
    day_vpd: list[int] = []
    format3_mode_per_slot: list[str] = []
    publish_override_per_slot: list[dict | None] = []

    for day_idx, day in enumerate(day_plans):
        mix = day.get("mix") or {}
        hook_count = int(day.get("hook_count", 0)) if hook_field else 0
        raw_modes = day.get("hook_modes") if hook_field else None

        day_pattern: list[tuple[str, bool]] = []
        day_f3_modes: list[str] = []
        for key in format_keys:
            n = int(mix.get(key, 0))
            if hook_field and key == hook_key:
                if raw_modes is not None:
                    modes = [(m if m in _FORMAT3_MODES else "none") for m in raw_modes][:n]
                    modes += ["none"] * (n - len(modes))
                else:
                    modes = ["hook" if idx < hook_count else "none" for idx in range(n)]
                day_f3_modes.extend(modes)
                day_pattern.extend((key, m == "hook") for m in modes)
            else:
                day_pattern.extend([(key, False)] * n)

        day_vpd.append(len(day_pattern))
        times = list(day.get("times") or [])
        fixed_times_by_day.append((times + [None] * len(day_pattern))[:len(day_pattern)])

        overrides = list(day.get("publish_overrides") or [])
        overrides = (overrides + [None] * len(day_pattern))[:len(day_pattern)]

        f3_i = 0
        for local_idx, slot in enumerate(day_pattern):
            schedule.append(slot)
            day_index_per_slot.append(day_idx)
            slot_index_within_day.append(local_idx)
            publish_override_per_slot.append(overrides[local_idx])
            if hook_field and slot[0] == hook_key:
                format3_mode_per_slot.append(day_f3_modes[f3_i])
                f3_i += 1
            else:
                format3_mode_per_slot.append("none")

    return (schedule, day_index_per_slot, slot_index_within_day, fixed_times_by_day, day_vpd,
            format3_mode_per_slot, publish_override_per_slot)


def _load_past_subjects() -> set[str]:
    """Load all past subjects from generated scripts + publish_log (lowercased)."""
    subjects: set[str] = set()

    # 1. From every generated script JSON
    scripts_dir = BASE_DIR / "output" / "scripts"
    if scripts_dir.exists():
        for f in scripts_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s = data.get("subject", "").strip().lower()
                if s:
                    subjects.add(s)
            except Exception:
                continue

    # 2. From publish_log (belt-and-suspenders)
    log_file = BASE_DIR / "state" / "publish_log.json"
    if log_file.exists():
        try:
            entries = json.loads(log_file.read_text(encoding="utf-8"))
            for e in entries:
                s = e.get("subject", "").strip().lower()
                if s:
                    subjects.add(s)
        except Exception:
            pass

    return subjects


def _subjects_too_similar(a: str, b: str, threshold: float = 0.70) -> bool:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > threshold


def _pick_subjects(n: int, past_subjects: set[str] | None = None, pool_source: list[str] | None = None) -> list[str]:
    """
    Pick n subjects avoiding recently published topics (difflib similarity check).
    Falls back to random pool entry if all candidates are too similar.
    pool_source defaults to _BATCH_SUBJECTS; pass _RANKING_SUBJECTS for format6.
    """
    past = past_subjects or set()
    pool = list(pool_source if pool_source is not None else _BATCH_SUBJECTS)
    random.shuffle(pool)

    result: list[str] = []
    used_in_batch: list[str] = []
    pool_idx  = 0
    attempts  = 0
    max_att   = len(pool) * 3

    while len(result) < n and attempts < max_att:
        if pool_idx >= len(pool):
            random.shuffle(pool)
            pool_idx = 0
        candidate = pool[pool_idx]
        pool_idx += 1
        attempts += 1

        all_used = list(past) + used_in_batch
        if any(_subjects_too_similar(candidate, u) for u in all_used):
            continue

        result.append(candidate)
        used_in_batch.append(candidate.lower())

    # Fallback if pool exhausted — just fill remaining
    while len(result) < n:
        result.append(random.choice(pool))

    return result


def _rand_minute_not_mult5(lo: int, hi: int) -> int:
    """Random integer minute in [lo, hi) that is NOT a multiple of 5. lo/hi are minutes."""
    lo = int(lo)
    hi = int(hi)
    if hi <= lo:
        hi = lo + 1
    for _ in range(60):
        m = random.randint(lo, hi - 1)
        if m % 5 != 0:
            return m
    # Fallback: nudge off a multiple of 5
    m = lo
    while m % 5 == 0:
        m += 1
    return m


def _day_publish_times(day0: datetime, vpd: int, fixed: list[str | None] | None = None) -> list[datetime]:
    """
    Compute vpd publish datetimes for one calendar day.

    - Window 07:00 -> 01:00 next day (19h) split into vpd equal slots.
    - Each video randomised inside its slot, minute never a multiple of 5.
    - >= 2h between consecutive videos; if vpd is too high for the window,
      the gap is enforced sequentially even past slot boundaries.

    day0 = US-Eastern-midnight datetime of the publish day.

    fixed: optional per-slot "HH:MM" overrides (len == vpd, None entries
    stay auto). A fixed slot is trusted as given -- never nudged for the 2h
    gap, that's the whole point of a deliberate override -- but it still
    updates prev_min, so an auto slot right after it still gets pushed out
    by the gap rule same as if that fixed time had been picked randomly.

    A slot can also be the literal string "now" (2026-08-23, per-slot
    "🚀 Publier maintenant" button in the day-by-day "Personnalisé" planner --
    see renderDayPlanSlots' dp-now-btn) -- that slot publishes at the actual
    current instant instead of anywhere in day0's window, and does NOT touch
    prev_min: it's a one-off override unrelated to that day's spread, so it
    must not distort the gap spacing of the day's other (real) slots.
    """
    width = _WINDOW_TOTAL_MIN / vpd
    times: list[datetime] = []
    prev_min: float | None = None

    for k in range(vpd):
        fixed_hhmm = fixed[k] if fixed else None
        if fixed_hhmm == "now":
            times.append(datetime.now(_PUBLISH_TZ))
            continue
        if fixed_hhmm:
            hh, mm = fixed_hhmm.split(":")
            chosen = int(hh) * 60 + int(mm)
            times.append(day0 + timedelta(minutes=chosen))
            prev_min = chosen
            continue

        slot_start = _WINDOW_START_MIN + k * width
        slot_end   = _WINDOW_START_MIN + (k + 1) * width

        lo = slot_start
        if prev_min is not None:
            lo = max(lo, prev_min + _MIN_GAP_MIN)
        hi = slot_end

        if hi <= lo:
            # Window too tight for 2h spacing (very high vpd): force min gap.
            chosen = int(round(lo))
            if chosen % 5 == 0:
                chosen += 1
        else:
            chosen = _rand_minute_not_mult5(lo, hi)

        times.append(day0 + timedelta(minutes=chosen))
        prev_min = chosen

    return times


def _compute_manual_whens(manual_start: datetime, interval_minutes: int,
                          vpd: int | None, total: int) -> list[datetime]:
    """
    Publication mode's "Manuel" spread -- day 1 is a fixed interval starting
    exactly at manual_start; from day 2 onward it falls back to the same
    _day_publish_times mechanism as auto-schedule (07:00->01:00 window,
    randomized, >=2h gap), advancing one calendar day at a time, last day
    takes the remainder.

    vpd=None means "illimité": no day quota, so the day-2+ fallback never
    triggers -- the whole sequence stays a pure fixed interval from
    manual_start, however many videos land on the same calendar day.
    """
    if vpd is None:
        return [manual_start + timedelta(minutes=interval_minutes * i) for i in range(total)]

    day1_count = min(vpd, total)
    whens = [manual_start + timedelta(minutes=interval_minutes * i) for i in range(day1_count)]

    remaining = total - day1_count
    day = 1
    midnight = manual_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while remaining > 0:
        day_count = min(vpd, remaining)
        whens.extend(_day_publish_times(midnight + timedelta(days=day), day_count))
        remaining -= day_count
        day += 1

    return whens


def _schedule_post(video_path: Path, script: dict, when: datetime, ctx,
                   platforms: list[str] | None = None):
    """
    Build caption, then schedule the video direct-to-SaaS (Ayrshare/Zernio) for brand `ctx`.
    platforms: explicit network list from the UI picker; falls back to the
    brand's configured default_networks when not given (old behavior).
    """
    from modules.content_meta import generate_meta, build_social_caption, youtube_title
    meta    = generate_meta(script)
    caption = build_social_caption(meta)
    title   = youtube_title(meta)          # same "Comment WORD" line the caption leads with -- see youtube_title's docstring

    thumbnail_path = None
    if script.get("thumbnail"):
        candidate = OUTPUT_DIR / script["thumbnail"]
        if candidate.exists():
            thumbnail_path = candidate

    from modules.publish.publish_config import schedule_video, load_publish_config
    cfg = load_publish_config(ctx.profiles_file)
    networks = platforms or ctx.default_networks
    return schedule_video(video_path, networks, when,
                          caption=caption, title=title, thumbnail_path=thumbnail_path, cfg=cfg,
                          route=script.get("route"), profiles_file=ctx.profiles_file)


def _parse_start_date(start_date: str | None) -> datetime:
    """Parse 'YYYY-MM-DD' to US-Eastern-midnight datetime. Default = tomorrow."""
    if start_date:
        try:
            d = datetime.strptime(start_date, "%Y-%m-%d")
            return d.replace(tzinfo=_PUBLISH_TZ)
        except ValueError:
            pass
    tomorrow = (datetime.now(_PUBLISH_TZ) + timedelta(days=1))
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_start_datetime(start_datetime: str | None) -> datetime:
    """
    Parse a 'YYYY-MM-DDTHH:MM' (datetime-local input) to a US-Eastern-aware
    datetime, keeping the user-chosen hour/minute -- unlike _parse_start_date
    (day-only, hour always forced to 00:00). Default = tomorrow same hour as now.
    """
    if start_datetime:
        try:
            d = datetime.fromisoformat(start_datetime)
            return d.replace(tzinfo=_PUBLISH_TZ) if d.tzinfo is None else d.astimezone(_PUBLISH_TZ)
        except ValueError:
            pass
    return datetime.now(_PUBLISH_TZ) + timedelta(days=1)


_HOOK_COMPATIBLE_FORMATS = {"format3"}


def _generate_batch(days: int, vpd: int, job_id: str, format_mode: str = "auto",
                    mode: str = "manual",
                    start_date: str | None = None, user: str | None = None,
                    hooks_enabled: bool = False, hook_transition: str = "zoom_punch",
                    hook_cut_style: str = "flash", beat_match: bool = False,
                    hook_recut_enabled: bool = False, template_mode_enabled: bool = False,
                    day_plans: list[dict] | None = None,
                    platforms: list[str] | None = None,
                    checkpoint_id: str | None = None, resume_skip: int = 0,
                    prior_results: list[dict] | None = None) -> dict:
    """
    Generate videos sequentially (same anti-dup generation for both modes).
    Sink per video:
      mode="manual"   → Drive upload + recap email (days_ahead = day index)
      mode="schedule" → schedule direct-to-SaaS (Ayrshare/Zernio) at a computed date/time
    Everything is scoped to brand `user` (generator, history, publish config).

    hooks_enabled: master on/off switch.

    beat_match: applies to format3 slots that do NOT get a hook ("Matcher
    sur le beat" toggle) -- cuts the mixed image/video pool on the music's
    real beat grid instead of the plain fixed-per-card duration. Hook slots
    are always beat-matched regardless of this flag (mandatory there).

    hook_recut_enabled / template_mode_enabled (2026-08-07): same uniform,
    batch-global toggle as hooks_enabled/beat_match, mutually exclusive with
    them and each other (enforced client-side, same precedence server-side:
    hook > hook_recut > template > beat_match/plain). Only meaningful for
    format3 slots, only for the non-custom (no day_plans) path -- day_plans'
    own per-slot format3_mode already supports "hook_recut"/"template" via
    _FORMAT3_MODES. template_mode_enabled additionally splits each rendered
    slot into 9 parts + uploads them to Drive (see
    modules.publish.template_parts), same as the single-video route.

    day_plans (list[dict] or None): when given (only valid with
    format_mode="auto"), replaces the proportional 1/4 auto-split AND the
    old "same mix every day" custom_mix with a genuinely independent plan
    per day -- one dict per day, each shaped:
        {"mix": {format1,format7,format3,format6,format15,format16,format18: int},
         "hook_count": int, "hook_assignments": [{"transition","cut_style"}, ...],
         "times": ["HH:MM"|"now"|None, ...],
         "format15_slots"/"format16_slots"/"format18_slots": [dict, ...]}
    `mix` can sum to 0 (no video that day). `times` is parallel to that
    day's flattened slot order (_CUSTOM_MIX_FORMAT_KEYS order: f1,f7,f6,
    f15,f16,f18 then f3) -- None/missing entries stay auto-placed in the
    07:00-01:00 window same as before; given entries are passed straight to
    _day_publish_times' `fixed` param. Flattened via _flatten_day_plans into
    the same (fmt, use_hook) schedule shape _make_format_schedule produces,
    so nothing downstream needs to know which path built it.
    `days`/`vpd` are ignored when day_plans is given (both are derived from
    it) but still required for the format_mode="auto" WITHOUT day_plans
    proportional-split path (1/4 format1, 1/4 format3 plain, 1/4 format3
    WITH a hook, 1/4 format6 -- unchanged, see _make_format_schedule) and
    for single-format batches (format_mode="format1"/"format3"/etc, no
    per-day customization concept).

    format15/16/18 slots need manual per-occurrence input instead of a
    random subject (duel character pick, White Card Reaction source clip);
    sourced from each day's format15_slots/format16_slots/format18_slots
    when day_plans is given. Looked up via a precomputed schedule-index ->
    position map (see _manual_slot_pos below), the exact same one-slot-at-a-
    time pattern _generate_batch_manual/_build_manual_slot already use for
    these formats' OWN separate manual-batch mode -- reused directly here,
    not reimplemented.

    checkpoint_id / resume_skip / prior_results (2026-08-12, crash-resume):
    when checkpoint_id is given, this run's progress is written to
    state/batch_checkpoints/<checkpoint_id>.json after every slot -- a crash/
    shutdown mid-batch leaves that file with whatever finished so far, so a
    later call can resume by passing resume_skip = len(prior results) and
    prior_results = that same list. resume_skip only ever skips slots by
    their position in `schedule` (0..resume_skip-1) -- it does NOT re-derive
    `schedule`/`subjects`/`routes` from a saved plan, they're recomputed the
    same way a fresh run would (subjects naturally avoid whatever's already
    in past_subjects by the time of the resume; the (rare) automatic manual-
    quota routing may reshuffle which slots land on the manual queue vs
    auto-schedule slightly differently than the original run would have --
    acceptable drift for a crash-recovery path, never a functional break).
    `_manual_slot_pos`/`_hook_slot_pos` below exist SPECIFICALLY so skipping
    doesn't corrupt state that used to only ever advance via execution order
    (a plain iterator for format15/16/18 slots, a plain counter for which
    hook_assignments entry a day's Nth hook slot gets) -- both must stay
    correct regardless of which slot indices this particular call actually
    renders.
    """
    ctx  = users.get_user(user)
    past = users.past_subjects(ctx)

    if day_plans is not None:
        (schedule, day_index_per_slot, slot_index_within_day, fixed_times_by_day, day_vpd_list,
         format3_mode_per_slot, publish_override_per_slot) = (
            _flatten_day_plans(day_plans, _CUSTOM_MIX_FORMAT_KEYS, hook_field=True)
        )
        total  = sum(day_vpd_list)
        days   = len(day_plans)
        hook_assignments_by_day = [d.get("hook_assignments") or [] for d in day_plans]
        format15_slots = [s for d in day_plans for s in (d.get("format15_slots") or [])]
        format16_slots = [s for d in day_plans for s in (d.get("format16_slots") or [])]
        format18_slots = [s for d in day_plans for s in (d.get("format18_slots") or [])]
        is_custom = True
    else:
        total = days * vpd
        schedule = _make_format_schedule(total, mode=format_mode)   # list[(fmt, use_hook)]
        # No per-slot customization on this path -- derive the 4-way mode
        # from the existing bool (True -> "hook", else "none"; "beat_match"
        # is applied uniformly later via the `beat_match` param, same as
        # before) and leave every publish override unset (batch-global
        # mode/platforms apply to every slot, unchanged behavior).
        format3_mode_per_slot      = ["hook" if uh else "none" for _, uh in schedule]
        publish_override_per_slot  = [None] * total
        day_index_per_slot      = [i // vpd for i in range(total)]
        slot_index_within_day   = [i % vpd for i in range(total)]
        fixed_times_by_day      = [[None] * vpd for _ in range(days)]
        day_vpd_list            = [vpd] * days
        hook_assignments_by_day = []
        format15_slots = format16_slots = format18_slots = []
        is_custom = False

    subjects  = _pick_subjects(total, past_subjects=past)

    # format6 is a different niche (people/habits, not wealth) — swap its
    # slots to ranking-topic subjects instead of the wealth pool above.
    n_f6 = sum(1 for fmt, _ in schedule if fmt == "format6")
    if n_f6:
        ranking_subjects = _pick_subjects(n_f6, past_subjects=past, pool_source=_RANKING_SUBJECTS)
        ri = 0
        for i, (fmt, _) in enumerate(schedule):
            if fmt == "format6":
                subjects[i] = ranking_subjects[ri]
                ri += 1

    # Auto mode: precompute publish times per day (from chosen start date).
    start_dt   = _parse_start_date(start_date)
    day_times: dict[int, list[datetime]] = {}   # day index -> [datetime per slot]
    day_starts: list[int] = []   # day index -> first flat slot index that day
    _acc = 0
    for _v in day_vpd_list:
        day_starts.append(_acc)
        _acc += _v

    # Clip availability check — format1 AND format7 both draw from the same
    # assets/clips/ pool (clip_selector.select_clip) — convert excess slots
    # (from either) → format3 when there aren't enough clips to go around.
    clips_dir = BASE_DIR / "assets" / "clips"
    clip_exts  = {".mp4", ".mov", ".mkv"}
    n_clips    = sum(1 for f in clips_dir.glob("*") if f.suffix.lower() in clip_exts) if clips_dir.exists() else 0
    n_f1       = sum(1 for fmt, _ in schedule if fmt in ("format1", "format7"))
    warning    = None
    if n_f1 > n_clips:
        excess  = n_f1 - n_clips
        warning = f"Seulement {n_clips} clip(s) disponible(s) pour {n_f1} vidéos format1/format7 — {excess} slot(s) convertis en format3."
        converted = 0
        for i in range(len(schedule) - 1, -1, -1):
            if schedule[i][0] in ("format1", "format7") and converted < excess:
                schedule[i] = ("format3", False)   # format1/format7 slots never carry a hook flag
                converted += 1

    # Manual publish queue (modules/publish/manual_queue.py, see project spec): a
    # per-week quota of eligible slots is PULLED from the schedule built
    # above (never added — total stays fixed) and flipped to route="manual".
    # Only meaningful for mode="schedule" -- mode="manual" batches are
    # already 100% posted by hand, there's nothing to pull out of.
    from config import MANUAL_ENABLED, MANUAL_ELIGIBLE_FORMATS
    routes: list[str] = ["auto"] * total
    if any(o is not None for o in publish_override_per_slot):
        # Explicit per-video routing (2026-08-06, "Personnalisé" pmode) --
        # the user picked auto-schedule/manuel for each slot directly, so the
        # automated weekly-quota system below doesn't apply at all here: it
        # would just second-guess an explicit choice. A slot with no override
        # in an otherwise-custom batch defaults to "auto" (unaffected).
        routes = ["manual" if (o and o.get("mode") == "manual") else "auto" for o in publish_override_per_slot]
        for i in range(total):
            if routes[i] == "manual":
                fmt_i, _ = schedule[i]
                schedule[i] = (fmt_i, False)   # same rule n°4: hook always removed on a manual slot
        n_manual = routes.count("manual")
        if n_manual:
            logger.info(f"per-video routing: {n_manual}/{total} slot(s) explicitly set to manual")
    elif MANUAL_ENABLED and mode == "schedule":
        from modules.publish.manual_queue import assign_manual_routes
        routes, manual_warnings = assign_manual_routes(schedule, day_vpd_list, days)
        for i in range(total):
            if routes[i] == "manual":
                fmt_i, _ = schedule[i]
                schedule[i] = (fmt_i, False)   # rule n°4: hook always removed on a manual slot
        for w in manual_warnings:
            logger.warning(f"manual queue: {w}")
        n_manual = routes.count("manual")
        if n_manual:
            logger.info(f"manual queue: {n_manual}/{total} slot(s) routed to manual publish")

    # Precomputed, order-independent positions for state _render_slot used to
    # track by MUTATING a counter / consuming an iterator as slots rendered in
    # sequence -- broke the instant a slot could be SKIPPED (crash-resume,
    # below) rather than always rendered strictly in order. Each slot's
    # position here depends only on `schedule`/`format3_mode_per_slot`/
    # `day_index_per_slot`, fixed regardless of which slots this particular
    # call actually renders.
    _manual_slot_pos: dict[int, int] = {}
    _manual_counters = {"format15": 0, "format16": 0, "format18": 0}
    _hook_slot_pos: dict[int, int] = {}
    _hook_counters: dict[int, int] = {}
    for _i in range(total):
        _fmt_i = schedule[_i][0]
        if _fmt_i in _manual_counters:
            _manual_slot_pos[_i] = _manual_counters[_fmt_i]
            _manual_counters[_fmt_i] += 1
        if is_custom:
            _is_hook_i = (format3_mode_per_slot[_i] == "hook" and hooks_enabled
                         and _fmt_i in _HOOK_COMPATIBLE_FORMATS)
        else:
            _use_hook_here_i = schedule[_i][1] or format_mode != "auto"
            _is_hook_i = hooks_enabled and _use_hook_here_i and _fmt_i in _HOOK_COMPATIBLE_FORMATS
        if _is_hook_i:
            _d = day_index_per_slot[_i]
            _hook_slot_pos[_i] = _hook_counters.get(_d, 0)
            _hook_counters[_d] = _hook_counters.get(_d, 0) + 1

    results: list[dict] = list(prior_results or [])
    processed = resume_skip   # running count across weeks/phases, for the progress bar (monotonic)

    def _persist_progress() -> None:
        """Checkpoint after every finished slot -- see checkpoint_id's
        docstring above. No-op when this call has no checkpoint_id (retries,
        the manual-format batch path, etc never pass one)."""
        if not checkpoint_id:
            return
        ckpt = _load_checkpoint(checkpoint_id) or {
            "checkpoint_id": checkpoint_id, "created_at": datetime.now().isoformat(),
            "params": {}, "total": total,
        }
        ckpt["results"] = results
        ckpt["status"]  = "running"
        ckpt["warning"] = warning
        _save_checkpoint(checkpoint_id, ckpt)

    def _render_slot(i: int):
        """Render slot i. Returns (video_path, script) on success, (None, error_str) on failure."""
        nonlocal processed
        fmt, sched_use_hook = schedule[i]
        subject = subjects[i]
        day_idx = day_index_per_slot[i]
        day_num = day_idx + 1

        processed += 1
        _jobs[job_id]["progress"] = {
            "done":    processed - 1,
            "total":   total,
            "current": f"Vidéo {processed}/{total} — {fmt} — {subject[:45]}…"
                       + (" [manuelle]" if routes[i] == "manual" else ""),
        }

        try:
            gen_kwargs = {}
            if is_custom:
                # Per-video format3 mode (2026-08-06 user ask: mix hook/
                # beat-match/hook-recut/plain within ONE batch instead of one
                # global choice) -- format3_mode_per_slot[i] is "none" for
                # every non-format3 slot, see _flatten_day_plans.
                mode3 = format3_mode_per_slot[i]
                if mode3 == "hook" and hooks_enabled and fmt in _HOOK_COMPATIBLE_FORMATS:
                    t, c = hook_transition, hook_cut_style
                    slot_idx = _hook_slot_pos.get(i, 0)
                    if hook_assignments_by_day and day_idx < len(hook_assignments_by_day):
                        day_list = hook_assignments_by_day[day_idx]
                        if slot_idx < len(day_list):
                            t = day_list[slot_idx].get("transition", t)
                            c = day_list[slot_idx].get("cut_style", c)
                    gen_kwargs = {"use_hook": True, "hook_transition": t, "hook_cut_style": c}
                elif mode3 == "beat_match":
                    gen_kwargs = {"beat_match": True}
                elif mode3 == "hook_recut":
                    gen_kwargs = {"hook_recut": True}
                elif mode3 == "template":
                    gen_kwargs = {"template_mode": True}
                # mode3 == "none" (or "hook" but hooks_enabled is off / format
                # ineligible) -> plain assembly, gen_kwargs stays {}.
            else:
                use_hook_here = sched_use_hook or format_mode != "auto"
                if hooks_enabled and use_hook_here and fmt in _HOOK_COMPATIBLE_FORMATS:
                    gen_kwargs = {"use_hook": True, "hook_transition": hook_transition, "hook_cut_style": hook_cut_style}
                elif hook_recut_enabled and fmt == "format3":
                    gen_kwargs = {"hook_recut": True}
                elif template_mode_enabled and fmt == "format3":
                    gen_kwargs = {"template_mode": True}
                elif fmt == "format3":
                    # No hook/recut/template on this slot -- "Matcher sur le
                    # beat" still applies if the toggle is on.
                    gen_kwargs = {"beat_match": beat_match}

            if routes[i] == "manual":
                gen_kwargs["manual"] = True

            if fmt in ("format15", "format16", "format18"):
                # No random subject here -- needs the manual per-occurrence
                # input supplied for THIS specific slot (see
                # format15_slots/format16_slots/format18_slots docstring
                # above). Reuses _build_manual_slot exactly as these formats'
                # own separate manual-batch mode does.
                slot_list = {"format15": format15_slots, "format16": format16_slots, "format18": format18_slots}[fmt]
                pos = _manual_slot_pos.get(i)
                if pos is None or pos >= len(slot_list):
                    raise RuntimeError(
                        f"{fmt}_slots ran out -- fewer manual inputs than {fmt} slots in the mix"
                    )
                manual_slot = slot_list[pos]
                if manual_slot.get("video_path"):
                    # format15/16 only (2026-08-09): already built via the
                    # Personnalisé inline wizard -- script + optional real
                    # audio already collected there, see renderDuelFields'
                    # full-wizard rewrite and duelBuild's noReload path.
                    # Resolve the existing file (already brand-relocated by
                    # /api/format15|16/build, see users.tag_and_relocate) and
                    # skip generation entirely -- same helper /videos/<name>
                    # uses, so it finds the file regardless of which brand's
                    # output tree it landed in.
                    video_path = _resolve_video_path(manual_slot["video_path"])
                    if video_path is None:
                        raise RuntimeError(f"{fmt}: vidéo pré-construite introuvable : {manual_slot['video_path']!r}")
                    subject = manual_slot.get("subject", "")
                else:
                    video_path, subject = _build_manual_slot(fmt, manual_slot, ctx)
                subjects[i] = subject
            else:
                video_path = users.run_generator(ctx, fmt, subject, **gen_kwargs)

            # ctx.root/output/scripts, NOT BASE_DIR/output/scripts (2026-08-10
            # fix -- that path never existed at all: scripts live under
            # config.OUTPUT_DIR = RESETWORTH_DIR/output/scripts for the
            # default brand, and users.run_generator/_relocate_to_brand move
            # them to ctx.root/output/scripts for every other brand. This
            # glob was silently finding nothing for EVERY brand, every
            # format, always falling back to the bare {"format","subject"}
            # stub below -- which _schedule_post's generate_meta() then had
            # to write a title/description from with an empty hook and body,
            # degrading every auto-scheduled batch video's caption quality.
            scripts_dir = ctx.root / "output" / "scripts"
            ts_prefix   = video_path.stem[:15]
            candidates  = list(scripts_dir.glob(f"{ts_prefix}*.json"))
            script      = (
                json.loads(candidates[0].read_text(encoding="utf-8"))
                if candidates else {"format": fmt, "subject": subject}
            )
            script["route"] = routes[i]
            if routes[i] == "manual":
                _persist_script_field(video_path, route="manual")
            # "Mode template" post-processing -- same split-into-9 + Drive
            # upload + pending-state tracking as the single-video route (see
            # api_generate). Skipped when routes[i]=="manual": pipeline.run
            # silently drops template_mode in favor of the manual-queue's own
            # beat-matched assembler when both are set (same precedence
            # hook_recut already has), so the render ISN'T actually a 9-equal-
            # cut video in that case -- nothing to split.
            if gen_kwargs.get("template_mode") and routes[i] != "manual":
                from modules.publish.template_parts import split_into_parts, upload_template_parts, find_template_extras, add_pending
                parts = split_into_parts(video_path) + find_template_extras(video_path)
                drive_link = upload_template_parts(ctx, video_path.stem, parts)
                add_pending(ctx, video_path.stem, script, drive_link)
                script["_template_drive_link"] = drive_link
            return video_path, script
        except Exception as e:
            logger.error(f"Batch slot {i + 1}/{total} render FAILED: {e}", exc_info=True)
            return None, str(e)

    def _dispatch_auto(i: int, video_path: Path, script: dict) -> None:
        fmt     = schedule[i][0]
        subject = subjects[i]
        day_idx = day_index_per_slot[i]
        day_num = day_idx + 1
        # Per-video publish override (2026-08-06, "Personnalisé" pmode) --
        # this slot's OWN network selection instead of the batch-global one,
        # when the user set one for it. None (no override) -> unchanged
        # behavior, uses the batch-global `platforms`.
        override = publish_override_per_slot[i]
        slot_platforms = (override.get("networks") or platforms) if override else platforms
        if mode == "schedule":
            g    = day_idx
            slot = slot_index_within_day[i]
            if g not in day_times:
                day0 = start_dt + timedelta(days=g)
                day_times[g] = _day_publish_times(day0, day_vpd_list[g], fixed=fixed_times_by_day[g])
            when = day_times[g][slot]
            res  = _schedule_post(video_path, script, when, ctx, platforms=slot_platforms)
            if res.status in ("created", "skipped", "partial"):
                results.append({
                    "video": video_path.name, "subject": subject, "day": day_num, "format": fmt,
                    "status": "ok", "when": res.scheduled_time, "networks": res.networks,
                    "publish": res.status,
                })
                logger.info(f"Batch slot {i + 1}/{total} scheduled ({res.status}) "
                            f"— J+{day_num} — {res.scheduled_time} — {video_path.name}")
            else:
                results.append({
                    "video": video_path.name, "subject": subject, "day": day_num, "format": fmt,
                    "status": "error", "error": res.detail,
                })
                logger.error(f"Batch slot {i + 1}/{total} publish FAILED: {res.detail}")
        else:
            from modules.publish.publisher import publish
            publish(video_path, script, platforms=slot_platforms, days_ahead=day_num)
            results.append({
                "video": video_path.name, "subject": subject, "day": day_num, "format": fmt,
                "status": "ok",
            })
            logger.info(f"Batch slot {i + 1}/{total} OK — J+{day_num} — {video_path.name}")

    def _dispatch_manual_week(week_idx: int, indices: list[tuple[int, Path, dict]]) -> None:
        """
        Rule n°3: render (already done by the caller) then upload to Drive
        and send ONE recap email for this week's manual videos, immediately --
        before the week's auto videos even start rendering.

        Networks + Snapchat handling fixed 2026-08-06 (see
        [[project_snapchat_manual_email_gap]] memory) -- this used to hardcode
        instagram/tiktok/youtube only, same bug modules.publish.publisher.publish
        had until the same day's fix. Now uses each slot's own network
        selection (publish_override_per_slot, "Personnalisé" pmode) falling
        back to the batch-global `platforms`, and gives Snapchat its own
        150-char-capped caption in the recap, same as publisher.py.
        """
        from modules.content_meta import generate_meta, build_social_caption, youtube_title, FIXED_HASHTAGS
        from modules.publish.scheduler import get_post_time, upload_manual_video, send_recap_email
        from modules.publish.manual_queue import pick_trending_sound
        from modules.publish.publisher import _SCHEDULABLE_PLATFORMS

        try:
            from modules.music.music_updater import get_trending_sounds
            trending_pool = get_trending_sounds()
        except Exception as e:
            logger.warning(f"manual queue: trending sounds unavailable ({e})")
            trending_pool = []

        week_recap: list[dict] = []
        used_names: set[str] = set()

        for i, video_path, script in indices:
            fmt     = schedule[i][0]
            subject = subjects[i]
            day_num = day_index_per_slot[i] + 1
            try:
                meta     = generate_meta(script)
                caption  = build_social_caption(meta)
                title    = youtube_title(meta)   # same "Comment WORD" line the caption leads with -- see youtube_title's docstring; must run AFTER build_social_caption
                hashtags = " ".join(FIXED_HASHTAGS)

                bpm  = (script.get("_manual_music_meta") or {}).get("bpm")
                sound = pick_trending_sound(bpm, trending_pool, exclude_names=used_names)
                if sound:
                    used_names.add(sound.get("name", ""))
                    music_suggestion = {**sound, "type": "trending"}
                else:
                    music_suggestion = meta.get("music_suggestion", {})

                override = publish_override_per_slot[i]
                slot_networks = (override.get("networks") or platforms) if override else platforms
                slot_networks = [p for p in (slot_networks or []) if p in _SCHEDULABLE_PLATFORMS] or \
                    ["instagram", "tiktok", "youtube"]   # unchanged default when nothing is configured
                times = {p: get_post_time(p, days_ahead=day_num) for p in slot_networks}
                ig_time, tt_time, yt_time = times.get("instagram"), times.get("tiktok"), times.get("youtube")
                snap_time, fb_time = times.get("snapchat"), times.get("facebook")

                snap_caption = None
                if "snapchat" in slot_networks:
                    from publishers.ayrshare_client import _truncate_caption_for_snapchat
                    snap_caption = _truncate_caption_for_snapchat(f"{caption}\n\n{hashtags}".strip())

                link = upload_manual_video(video_path, ig_time, tt_time, yt_time, title, caption,
                                           hashtags, music_suggestion,
                                           snap_time=snap_time, fb_time=fb_time, snap_caption=snap_caption)
                week_recap.append({
                    "platform": "manual", "status": "manual", "ig_time": ig_time,
                    "tt_time": tt_time, "yt_time": yt_time, "snap_time": snap_time, "fb_time": fb_time,
                    "title": title, "caption": caption, "hashtags": hashtags, "snap_caption": snap_caption,
                    "music_suggestion": music_suggestion, "drive_link": link,
                })
                results.append({
                    "video": video_path.name, "subject": subject, "day": day_num, "format": fmt,
                    "status": "ok", "route": "manual",
                })
                logger.info(f"Batch slot {i + 1}/{total} manual — Drive OK — {video_path.name}")
            except Exception as e:
                logger.error(f"Batch slot {i + 1}/{total} manual dispatch FAILED: {e}", exc_info=True)
                week_recap.append({"platform": "manual", "status": "error",
                                   "title": script.get("subject", subject), "error": str(e)})
                results.append({
                    "video": video_path.name, "subject": subject, "day": day_num, "format": fmt,
                    "status": "error", "error": str(e), "route": "manual",
                })

        try:
            send_recap_email(week_recap)
            logger.info(f"manual queue: week {week_idx} recap email sent ({len(week_recap)} video(s))")
        except Exception as e:
            logger.warning(f"manual queue: week {week_idx} recap email failed (non-fatal): {e}")

    from modules.publish.manual_queue import week_day_ranges
    for week_idx, (day_lo, day_hi) in enumerate(week_day_ranges(days), start=1):
        lo_i = day_starts[day_lo - 1]
        hi_i = min(day_starts[day_hi - 1] + day_vpd_list[day_hi - 1], total)
        week_indices = list(range(lo_i, hi_i))
        manual_indices = [i for i in week_indices if routes[i] == "manual"]
        auto_indices   = [i for i in week_indices if routes[i] != "manual"]

        # Phase 1 (rule n°3): manual slots first. On a render failure, try
        # the next eligible slot in this same week instead of just dropping
        # the quota — never block the auto phase either way.
        rendered_manual: list[tuple[int, Path, dict]] = []
        # i >= resume_skip filter (2026-08-12): a fallback promotion must
        # never target a slot from BEFORE the resume point -- that slot was
        # already rendered+dispatched in a previous run, "promoting" it here
        # would re-render and re-publish it a second time.
        fallback_pool = [i for i in auto_indices if i >= resume_skip and schedule[i][0] in MANUAL_ELIGIBLE_FORMATS]
        for i in manual_indices:
            if i < resume_skip:
                continue
            video_path, script_or_err = _render_slot(i)
            if video_path is None:
                logger.warning(f"manual queue: slot {i + 1} render failed ({script_or_err}), "
                               "trying next eligible slot this week instead")
                results.append({"subject": subjects[i], "day": day_index_per_slot[i] + 1, "format": schedule[i][0],
                                "status": "error", "error": script_or_err, "route": "manual"})
                _persist_progress()
                if fallback_pool:
                    fb = fallback_pool.pop(0)
                    auto_indices.remove(fb)
                    routes[fb] = "manual"
                    schedule[fb] = (schedule[fb][0], False)
                    fb_path, fb_script = _render_slot(fb)
                    if fb_path is not None:
                        rendered_manual.append((fb, fb_path, fb_script))
                    else:
                        results.append({"subject": subjects[fb], "day": day_index_per_slot[fb] + 1,
                                        "format": schedule[fb][0], "status": "error",
                                        "error": fb_script, "route": "manual"})
                        _persist_progress()
                continue
            rendered_manual.append((i, video_path, script_or_err))

        if rendered_manual:
            _dispatch_manual_week(week_idx, rendered_manual)
            _persist_progress()

        # Phase 2: this week's auto slots, unchanged behavior.
        for i in auto_indices:
            if i < resume_skip:
                continue
            video_path, script_or_err = _render_slot(i)
            if video_path is None:
                results.append({"subject": subjects[i], "day": day_index_per_slot[i] + 1, "format": schedule[i][0],
                                "status": "error", "error": script_or_err})
                _persist_progress()
                continue
            # Mode template: NOT published now -- it's raw 9-equal-cut source
            # material sitting as "pending" (Drive link already recorded in
            # _render_slot) until the user drops back a finished CapCut edit
            # via the Publication panel's template_pending_id flow. Skip the
            # normal publish dispatch entirely, same as the single-video route.
            if isinstance(script_or_err, dict) and script_or_err.get("_template_drive_link"):
                results.append({
                    "video": video_path.name, "subject": subjects[i], "day": day_index_per_slot[i] + 1,
                    "format": schedule[i][0], "status": "ok",
                    "template_drive_link": script_or_err["_template_drive_link"],
                })
                logger.info(f"Batch slot {i + 1}/{total} template-mode — pending CapCut, "
                            f"not dispatched — {video_path.name}")
                _persist_progress()
                continue
            _dispatch_auto(i, video_path, script_or_err)
            _persist_progress()

    _jobs[job_id]["progress"] = {"done": total, "total": total, "current": "Terminé"}
    if checkpoint_id:
        # Batch actually finished (including a resumed continuation reaching
        # the end) -- nothing left to resume, delete the checkpoint so it
        # stops being offered on the next page load.
        _delete_checkpoint(checkpoint_id)
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {"results": results, "warning": warning, "total": total, "ok": ok_count}


def _retry_batch_failed(failed_entries: list[dict], req: dict, job_id: str) -> dict:
    """
    Re-generate + re-publish exactly the videos that failed in a previous
    batch (same subject/format), reusing that batch's mode/hooks/platforms.
    Schedule-mode retries publish immediately -- the original computed slot
    time is long past by the time someone clicks retry.
    """
    ctx             = users.get_user(req.get("user"))
    mode            = req.get("mode", "manual")
    hooks_enabled   = req.get("hooks_enabled", False)
    hook_transition = req.get("hook_transition", "zoom_punch")
    hook_cut_style  = req.get("hook_cut_style", "flash")
    beat_match      = req.get("beat_match", False)
    hook_recut_enabled    = req.get("hook_recut_enabled", False)
    template_mode_enabled = req.get("template_mode_enabled", False)
    platforms       = req.get("platforms")

    results: list[dict] = []
    total = len(failed_entries)

    for i, entry in enumerate(failed_entries):
        fmt     = entry.get("format")
        subject = entry.get("subject")
        day_num = entry.get("day", 1)

        _jobs[job_id]["progress"] = {
            "done": i, "total": total,
            "current": f"Retry {i + 1}/{total} — {fmt} — {(subject or '')[:45]}…",
        }

        try:
            gen_kwargs = {}
            if hooks_enabled and fmt in _HOOK_COMPATIBLE_FORMATS:
                gen_kwargs = {"use_hook": True, "hook_transition": hook_transition,
                              "hook_cut_style": hook_cut_style}
            elif hook_recut_enabled and fmt == "format3":
                gen_kwargs = {"hook_recut": True}
            elif template_mode_enabled and fmt == "format3":
                gen_kwargs = {"template_mode": True}
            elif fmt == "format3":
                gen_kwargs = {"beat_match": beat_match}
            video_path = users.run_generator(ctx, fmt, subject, **gen_kwargs)

            scripts_dir = ctx.root / "output" / "scripts"   # see the main batch loop's identical fix/comment
            ts_prefix   = video_path.stem[:15]
            candidates  = list(scripts_dir.glob(f"{ts_prefix}*.json"))
            script      = (
                json.loads(candidates[0].read_text(encoding="utf-8"))
                if candidates else {"format": fmt, "subject": subject}
            )

            if gen_kwargs.get("template_mode"):
                # Same as the main batch loop -- not published, split + Drive
                # + pending instead.
                from modules.publish.template_parts import split_into_parts, upload_template_parts, find_template_extras, add_pending
                parts = split_into_parts(video_path) + find_template_extras(video_path)
                drive_link = upload_template_parts(ctx, video_path.stem, parts)
                add_pending(ctx, video_path.stem, script, drive_link)
                results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                "format": fmt, "status": "ok", "template_drive_link": drive_link})
            elif mode == "schedule":
                res = _schedule_post(video_path, script, datetime.now(_PUBLISH_TZ), ctx, platforms=platforms)
                if res.status in ("created", "skipped", "partial"):
                    results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                    "format": fmt, "status": "ok",
                                    "when": res.scheduled_time, "networks": res.networks})
                else:
                    results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                    "format": fmt, "status": "error", "error": res.detail})
            else:
                from modules.publish.publisher import publish
                publish(video_path, script, platforms=platforms, days_ahead=day_num)
                results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                "format": fmt, "status": "ok"})

            logger.info(f"Retry {i + 1}/{total} OK — {video_path.name}")
        except Exception as e:
            results.append({"subject": subject, "day": day_num, "format": fmt,
                            "status": "error", "error": str(e)})
            logger.error(f"Retry {i + 1}/{total} FAILED: {e}", exc_info=True)

    _jobs[job_id]["progress"] = {"done": total, "total": total, "current": "Terminé"}
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {"results": results, "total": total, "ok": ok_count}


@app.route("/api/batch_retry", methods=["POST"])
def api_batch_retry():
    data           = request.json or {}
    original_job_id = data.get("job_id")
    original_job    = _jobs.get(original_job_id)
    if not original_job or original_job.get("status") != "done":
        return jsonify({"error": "Batch introuvable ou pas terminé"}), 400

    original_result = original_job.get("result") or {}
    failed = [r for r in original_result.get("results", []) if r.get("status") == "error"]
    if not failed:
        return jsonify({"error": "Aucun échec à réessayer dans ce batch"}), 400

    req = original_job.get("request") or {}
    job_id = f"retry_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status": "queued", "message": "", "result": None,
        "progress": {"done": 0, "total": len(failed), "current": "En attente…"},
    }

    def _run():
        return _retry_batch_failed(failed, req, job_id)

    _enqueue_generation(job_id, _run)
    return jsonify({"job_id": job_id, "total": len(failed)})


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _valid_hhmm(s) -> bool:
    return isinstance(s, str) and bool(_HHMM_RE.match(s))


def _parse_mix_counts(mix, label: str) -> tuple[dict[str, int] | None, str | None]:
    """Parse+validate a {format1..format18: count} dict against
    _CUSTOM_MIX_FORMAT_KEYS -- shared by /api/batch's day_plans (per-day) and
    legacy custom_mix (uniform) request parsing. Returns (counts, None) with
    all 7 keys present as non-negative ints, or (None, error_message)."""
    if not isinstance(mix, dict):
        return None, f"{label}: mix invalide"
    try:
        counts = {k: int(mix.get(k, 0)) for k in _CUSTOM_MIX_FORMAT_KEYS}
    except (ValueError, TypeError, AttributeError):
        return None, f"{label}: mix invalide"
    if any(v < 0 for v in counts.values()):
        return None, f"{label}: les compteurs ne peuvent pas être négatifs"
    return counts, None


@app.route("/api/batch", methods=["POST"])
def api_batch():
    data = request.json or {}
    mode = str(data.get("mode", "manual"))
    try:
        days        = int(data.get("days", 1))
        vpd         = int(data.get("videos_per_day", 1))
        format_mode = str(data.get("format_mode", "auto"))
        start_date  = data.get("start_date") or None
        user        = data.get("user") or users.active_user()
        hooks_enabled   = bool(data.get("hooks_enabled", False))
        hook_transition = str(data.get("hook_transition", "zoom_punch"))
        hook_cut_style  = str(data.get("hook_cut_style", "flash"))
        beat_match      = bool(data.get("beat_match", False))
        hook_recut_enabled    = bool(data.get("hook_recut_enabled", False))
        template_mode_enabled = bool(data.get("template_mode_enabled", False))
    except (ValueError, TypeError):
        return jsonify({"error": "Paramètres invalides"}), 400

    platforms = data.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            return jsonify({"error": "Sélectionne au moins une plateforme"}), 400
        if not set(platforms).issubset(_VALID_PLATFORMS):
            return jsonify({"error": f"Plateformes invalides (valides: {sorted(_VALID_PLATFORMS)})"}), 400

    if mode not in ("manual", "schedule"):
        return jsonify({"error": "mode invalide (manual|schedule)"}), 400
    # format_mode: "auto" or any of this brand's OWN non-manual-input mix
    # formats (see app.py's _batch_panel_config -- "simpleFormats" is the
    # exact same set the single-format-mode buttons offer client-side).
    # Was hardcoded to ("format1","format3","auto") only -- the UI already
    # offered format6/15/16/18 buttons too, all of which 400'd if clicked;
    # fixed 2026-08-07 alongside the batch-panel interface unification.
    _fm_ctx = users.get_user(data.get("user"))
    _simple_formats = {"auto"} | {f["id"] for f in _batch_panel_config(_fm_ctx)["simpleFormats"]}
    if format_mode not in _simple_formats:
        return jsonify({"error": f"format_mode invalide (valides: {sorted(_simple_formats)})"}), 400
    from modules.transitions import TRANSITION_TYPES
    if hook_transition not in TRANSITION_TYPES:
        return jsonify({"error": "hook_transition invalide"}), 400
    if hook_cut_style not in ("flash", "glitch", "none"):
        return jsonify({"error": "hook_cut_style invalide"}), 400

    # Per-day custom plan -- either sent directly as `day_plans` (new "jour
    # par jour" UI, each day fully independent) or built here from the
    # legacy flat `custom_mix` fields (old "same mix every day" UI, still
    # fully supported) -- either way _generate_batch only ever sees the
    # normalized `day_plans` list (or None for the plain proportional
    # 1/4-split path, unchanged). Only valid alongside format_mode="auto".
    day_plans_raw = data.get("day_plans")
    custom_mix    = data.get("custom_mix")
    day_plans: list[dict] | None = None

    if day_plans_raw is not None:
        if format_mode != "auto":
            return jsonify({"error": "day_plans seulement disponible en mode auto"}), 400
        if not isinstance(day_plans_raw, list) or not day_plans_raw:
            return jsonify({"error": "day_plans invalide"}), 400

        day_plans = []
        for idx, raw in enumerate(day_plans_raw, start=1):
            label = f"day_plans[{idx}]"
            if not isinstance(raw, dict):
                return jsonify({"error": f"{label}: invalide"}), 400
            counts, err = _parse_mix_counts(raw.get("mix"), label)
            if err:
                return jsonify({"error": err}), 400
            day_vpd_i = sum(counts.values())

            # hook_modes (2026-08-06, per-video hook/beat-match/hook-recut mix,
            # see _flatten_day_plans) -- when given, it's the source of truth
            # for which format3 slots get which mode; hook_count_i is then
            # DERIVED from it (how many are "hook" specifically) purely to
            # keep the existing hook_assignments validation below unchanged.
            # Legacy requests without hook_modes keep the old hook_count-only
            # behavior exactly as before.
            hook_modes_i = raw.get("hook_modes")
            if hook_modes_i is not None:
                if not isinstance(hook_modes_i, list) or len(hook_modes_i) != counts["format3"]:
                    return jsonify({"error": f"{label}: hook_modes doit contenir {counts['format3']} entrée(s)"}), 400
                if not all(m in _FORMAT3_MODES for m in hook_modes_i):
                    return jsonify({"error": f"{label}: hook_modes invalide (valides: {_FORMAT3_MODES})"}), 400
                hook_count_i = hook_modes_i.count("hook")
            else:
                try:
                    hook_count_i = int(raw.get("hook_count", 0) or 0)
                except (ValueError, TypeError):
                    return jsonify({"error": f"{label}: hook_count invalide"}), 400
                if not (0 <= hook_count_i <= counts["format3"]):
                    return jsonify({"error": f"{label}: hook_count doit être entre 0 et {counts['format3']}"}), 400

            times = raw.get("times")
            if times is not None:
                if not isinstance(times, list) or len(times) != day_vpd_i:
                    return jsonify({"error": f"{label}: times doit contenir {day_vpd_i} entrée(s)"}), 400
                for t in times:
                    if t is not None and not _valid_hhmm(t):
                        return jsonify({"error": f"{label}: heure invalide ({t})"}), 400

            # publish_overrides (2026-08-06, "Personnalisé" pmode, per-video
            # auto-schedule+networks vs manuel) -- same alignment convention
            # as `times`: one entry per flattened slot in this day, null
            # means "use the batch-global mode/platforms" for that slot.
            publish_overrides_i = raw.get("publish_overrides")
            if publish_overrides_i is not None:
                if not isinstance(publish_overrides_i, list) or len(publish_overrides_i) != day_vpd_i:
                    return jsonify({"error": f"{label}: publish_overrides doit contenir {day_vpd_i} entrée(s)"}), 400
                for o in publish_overrides_i:
                    if o is None:
                        continue
                    if not isinstance(o, dict) or o.get("mode") not in ("auto", "manual"):
                        return jsonify({"error": f"{label}: publish_overrides invalide"}), 400
                    if o.get("mode") == "auto":
                        nets = o.get("networks")
                        if not isinstance(nets, list) or not nets or not set(nets).issubset(_VALID_PLATFORMS):
                            return jsonify({"error": f"{label}: publish_overrides.networks invalide"}), 400

            hook_assignments_i = None
            if hooks_enabled and hook_count_i > 0:
                hook_assignments_i = raw.get("hook_assignments")
                if not isinstance(hook_assignments_i, list) or len(hook_assignments_i) != hook_count_i:
                    return jsonify({"error": f"{label}: hook_assignments doit avoir {hook_count_i} entrée(s)"}), 400
                for a in hook_assignments_i:
                    t = (a or {}).get("transition")
                    c = (a or {}).get("cut_style")
                    if t not in TRANSITION_TYPES:
                        return jsonify({"error": f"{label}: transition invalide: {t}"}), 400
                    if c not in ("flash", "glitch", "none"):
                        return jsonify({"error": f"{label}: cut_style invalide: {c}"}), 400

            day_dict: dict = {"mix": counts, "hook_count": hook_count_i}
            if hook_modes_i is not None:
                day_dict["hook_modes"] = hook_modes_i
            if times is not None:
                day_dict["times"] = times
            if hook_assignments_i is not None:
                day_dict["hook_assignments"] = hook_assignments_i
            if publish_overrides_i is not None:
                day_dict["publish_overrides"] = publish_overrides_i

            for fmt_key, slots_field in (
                ("format15", "format15_slots"), ("format16", "format16_slots"), ("format18", "format18_slots"),
            ):
                n = counts[fmt_key]
                if n <= 0:
                    continue
                slots = raw.get(slots_field)
                if not isinstance(slots, list) or len(slots) != n:
                    return jsonify({"error": f"{label}: {slots_field} doit contenir {n} entrée(s)"}), 400
                day_dict[slots_field] = slots

            day_plans.append(day_dict)
        days = len(day_plans)

    elif custom_mix is not None:
        if format_mode != "auto":
            return jsonify({"error": "custom_mix seulement disponible en mode auto"}), 400
        counts, err = _parse_mix_counts(custom_mix, "custom_mix")
        if err:
            return jsonify({"error": err}), 400
        if sum(counts.values()) != vpd:
            return jsonify({"error": f"custom_mix doit sommer à {vpd} (vidéos/jour)"}), 400
        try:
            hook_count = int(data.get("hook_count", 0))
        except (ValueError, TypeError):
            return jsonify({"error": "hook_count invalide"}), 400
        if not (0 <= hook_count <= counts["format3"]):
            return jsonify({"error": f"hook_count doit être entre 0 et {counts['format3']}"}), 400

        # format15/16/18 need manual per-occurrence input (see
        # _generate_batch's day_plans docstring) -- one dict per slot across
        # ALL days, in schedule order (day 1's slots first, ...), sliced per
        # day below to build each day's own format15_slots/16/18.
        per_day_manual_slots: dict[str, list] = {}
        for fmt_key, slots_field in (
            ("format15", "format15_slots"), ("format16", "format16_slots"), ("format18", "format18_slots"),
        ):
            n = counts[fmt_key]
            if n <= 0:
                continue
            slots = data.get(slots_field)
            expected = n * days
            if not isinstance(slots, list) or len(slots) != expected:
                return jsonify({
                    "error": f"{slots_field} doit contenir {expected} entrée(s) "
                             f"({n} x {days} jour(s))"
                }), 400
            per_day_manual_slots[slots_field] = slots

        hook_assignments = None
        if hooks_enabled and hook_count > 0:
            hook_assignments = data.get("hook_assignments")
            if not isinstance(hook_assignments, list) or len(hook_assignments) != days:
                return jsonify({"error": f"hook_assignments doit avoir {days} jour(s)"}), 400
            for day_list in hook_assignments:
                if not isinstance(day_list, list) or len(day_list) != hook_count:
                    return jsonify(
                        {"error": f"chaque jour de hook_assignments doit avoir {hook_count} entrée(s)"}
                    ), 400
                for a in day_list:
                    t = (a or {}).get("transition")
                    c = (a or {}).get("cut_style")
                    if t not in TRANSITION_TYPES:
                        return jsonify({"error": f"transition invalide: {t}"}), 400
                    if c not in ("flash", "glitch", "none"):
                        return jsonify({"error": f"cut_style invalide: {c}"}), 400

        # Normalize into a day_plans list (same day-dict repeated `days`
        # times) -- _generate_batch only ever consumes day_plans from here.
        day_plans = []
        for d in range(days):
            day_dict = {"mix": counts, "hook_count": hook_count}
            if hook_assignments is not None:
                day_dict["hook_assignments"] = hook_assignments[d]
            for fmt_key, slots_field in (
                ("format15", "format15_slots"), ("format16", "format16_slots"), ("format18", "format18_slots"),
            ):
                n = counts[fmt_key]
                if n <= 0:
                    continue
                flat = per_day_manual_slots[slots_field]
                day_dict[slots_field] = flat[d * n:(d + 1) * n]
            day_plans.append(day_dict)

    # Manual mode keeps its preset days; schedule mode allows free day/vpd fields.
    if mode == "manual" and days not in (1, 3, 7, 30):
        return jsonify({"error": "days doit être 1, 3, 7 ou 30 en mode manuel"}), 400
    if not (1 <= days <= 90):
        return jsonify({"error": "days doit être entre 1 et 90"}), 400

    if day_plans is None:
        if not (1 <= vpd <= 30):
            return jsonify({"error": "vidéos/jour doit être entre 1 et 30"}), 400
        total = days * vpd
    else:
        for idx, dp in enumerate(day_plans, start=1):
            dv = sum(dp["mix"].values())
            if dv > 30:
                return jsonify({"error": f"day_plans[{idx}]: max 30 vidéos/jour ({dv} demandées)"}), 400
        total = sum(sum(dp["mix"].values()) for dp in day_plans)

    # A single day summing to 0 is legitimate ("no video that day"), but
    # every day at 0 means an empty plan -- used to sail through and
    # "succeed" at 0/0 instead of telling the caller nothing was asked for.
    if total == 0:
        return jsonify({"error": "Aucune vidéo dans le plan (tous les jours sont à 0)"}), 400

    cap = 100 if mode == "schedule" else 60
    if total > cap:
        return jsonify({"error": f"Maximum {cap} vidéos par batch ({total} demandées)"}), 400

    if mode == "schedule":
        from modules import users as _users
        ctx = _users.get_user(user)
        if not ctx.profiles_file.exists():
            return jsonify({"error": f"profiles.yaml manquant: {ctx.profiles_file}"}), 400
        if not start_date:
            return jsonify({"error": "Choisis une date de début (mode schedule)"}), 400

    job_id = f"batch_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status":   "queued",
        "message":  "",
        "result":   None,
        "progress": {"done": 0, "total": total, "current": "En attente…"},
        # kept so /api/batch_retry can replay the same publish routing for
        # just the failed videos, without needing the whole form resent.
        "request": {
            "mode": mode, "user": user, "hooks_enabled": hooks_enabled,
            "hook_transition": hook_transition, "hook_cut_style": hook_cut_style,
            "beat_match": beat_match,
            "hook_recut_enabled": hook_recut_enabled, "template_mode_enabled": template_mode_enabled,
            "platforms": platforms,
        },
    }

    # Crash-resume checkpoint (2026-08-12) -- same job_id doubles as the
    # checkpoint's own id for a fresh batch (no ambiguity yet, nothing to
    # resume from). See _generate_batch's checkpoint_id docstring and
    # api_batch_resume below.
    checkpoint_id = job_id
    _save_checkpoint(checkpoint_id, {
        "checkpoint_id": checkpoint_id,
        "created_at": datetime.now().isoformat(),
        "status": "running",
        "kind": "mixed",
        "params": {
            "days": days, "vpd": vpd, "format_mode": format_mode, "mode": mode,
            "start_date": start_date, "user": user,
            "hooks_enabled": hooks_enabled, "hook_transition": hook_transition,
            "hook_cut_style": hook_cut_style, "beat_match": beat_match,
            "hook_recut_enabled": hook_recut_enabled, "template_mode_enabled": template_mode_enabled,
            "day_plans": day_plans, "platforms": platforms,
        },
        "total": total,
        "results": [],
        "warning": None,
    })

    def _run():
        return _generate_batch(days, vpd, job_id, format_mode=format_mode,
                               mode=mode,
                               start_date=start_date, user=user,
                               hooks_enabled=hooks_enabled, hook_transition=hook_transition,
                               hook_cut_style=hook_cut_style, beat_match=beat_match,
                               hook_recut_enabled=hook_recut_enabled,
                               template_mode_enabled=template_mode_enabled,
                               day_plans=day_plans,
                               platforms=platforms,
                               checkpoint_id=checkpoint_id)

    _enqueue_generation(job_id, _run)

    start_dt = _parse_start_date(start_date)
    end_dt   = start_dt + timedelta(days=days - 1)
    return jsonify({
        "job_id": job_id, "total": total, "mode": mode,
        "start_date": start_dt.strftime("%Y-%m-%d"),
        "end_date":   end_dt.strftime("%Y-%m-%d"),
    })


@app.route("/api/batch/resumable")
def api_batch_resumable():
    """Batches that started but never reached their own finish -- crash,
    PC shutdown, or a closed tab/killed python. Called on page load so the
    dashboard can offer to pick up right where it stopped (see
    checkResumableBatch in index.html)."""
    return jsonify({"resumable": _list_resumable_checkpoints(request.args.get("user"))})


@app.route("/api/batch/resume", methods=["POST"])
def api_batch_resume():
    data          = request.json or {}
    checkpoint_id = data.get("checkpoint_id")
    ckpt = _load_checkpoint(checkpoint_id) if checkpoint_id else None
    if ckpt is None:
        return jsonify({"error": "Batch introuvable (déjà terminé ou supprimé)"}), 404
    if ckpt.get("status") != "running":
        return jsonify({"error": "Ce batch est déjà terminé"}), 400

    p             = ckpt.get("params", {})
    prior_results = ckpt.get("results", [])
    total         = ckpt.get("total", 0)
    resume_skip   = len(prior_results)
    if resume_skip >= total:
        _delete_checkpoint(checkpoint_id)
        return jsonify({"error": "Ce batch était déjà complet, rien à reprendre"}), 400

    kind   = ckpt.get("kind", "mixed")
    job_id = f"batch_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status":   "queued",
        "message":  "",
        "result":   None,
        "progress": {"done": resume_skip, "total": total,
                     "current": f"Reprise — {resume_skip}/{total} déjà faites…"},
        "request": {k: p.get(k) for k in
                    ("mode", "user", "hooks_enabled", "hook_transition", "hook_cut_style",
                     "beat_match", "hook_recut_enabled", "template_mode_enabled", "platforms")},
    }

    if kind == "manual":
        # format15/16/18 "X uniquement" single-format batch (see
        # _generate_batch_manual's own checkpoint contract) -- a flat `slots`
        # list, no day_plans/hooks/mix machinery at all.
        def _run():
            return _generate_batch_manual(
                p.get("format"), p.get("slots", []), job_id,
                mode=p.get("mode", "manual"), start_date=p.get("start_date"),
                user=p.get("user"), platforms=p.get("platforms"),
                checkpoint_id=checkpoint_id, resume_skip=resume_skip, prior_results=prior_results,
                times=p.get("times"),
            )
    else:
        def _run():
            return _generate_batch(
                p.get("days", 1), p.get("vpd", 1), job_id,
                format_mode=p.get("format_mode", "auto"), mode=p.get("mode", "manual"),
                start_date=p.get("start_date"), user=p.get("user"),
                hooks_enabled=p.get("hooks_enabled", False),
                hook_transition=p.get("hook_transition", "zoom_punch"),
                hook_cut_style=p.get("hook_cut_style", "flash"),
                beat_match=p.get("beat_match", False),
                hook_recut_enabled=p.get("hook_recut_enabled", False),
                template_mode_enabled=p.get("template_mode_enabled", False),
                day_plans=p.get("day_plans"), platforms=p.get("platforms"),
                checkpoint_id=checkpoint_id, resume_skip=resume_skip, prior_results=prior_results,
            )

    _enqueue_generation(job_id, _run)
    return jsonify({"job_id": job_id, "total": total, "resumed_from": resume_skip, "mode": p.get("mode", "manual")})


@app.route("/api/batch/resume_discard", methods=["POST"])
def api_batch_resume_discard():
    """User explicitly doesn't want to continue an interrupted batch --
    delete its checkpoint so it stops being offered."""
    checkpoint_id = (request.json or {}).get("checkpoint_id")
    if checkpoint_id:
        _delete_checkpoint(checkpoint_id)
    return jsonify({"ok": True})


# ── Manual-format batch (format15/16/18) ─────────────────────────────────────
# Deliberately NOT built on _generate_batch's schedule/custom_mix/hook/manual-
# queue machinery -- none of that applies here (no wealth-subject pool, no
# hooks, no clip-availability juggling), it would just be dead complexity
# along for the ride. Reuses the SAME _jobs progress/result shape _generate_batch
# produces (so the existing _pollBatch JS needs zero changes) and the SAME
# publish primitives (_schedule_post, _day_publish_times) -- one slot = one day.

_MANUAL_BATCH_FORMATS = ("format15", "format16", "format18")


def _build_manual_slot(fmt: str, slot: dict, ctx) -> tuple[Path, str]:
    """Builds ONE video for a manual-format batch slot. Returns (video_path, label_for_log)."""
    if fmt == "format18":
        from modules.assembler.white_card_reaction import auto_build_white_card_reaction
        from modules.clips.music_removal import maybe_strip_music

        url = (slot.get("url") or "").strip()
        file_token = slot.get("file_token")
        idea = (slot.get("idea") or "").strip() or None
        output_name = (slot.get("output_name") or "").strip() or "white_card_reaction"
        # "allow_teaser" checkbox (2026-08-10) -- see api_format18_build's identical comment.
        allow_teaser = slot.get("allow_teaser", True) not in (False, "0", "false")
        # "keep_music" checkbox (2026-08-10) -- see api_format18_build's identical comment.
        keep_music = slot.get("keep_music") in (True, "1", "true")

        input_path = (_CLIP_OVERRIDE_DIR / file_token) if file_token else None
        if input_path is None:
            if not url:
                raise ValueError("Aucune vidéo/lien fourni pour ce slot")
            from modules.clips.link_download import download_clip
            input_path = download_clip(url, _FORMAT18_UPLOAD_DIR)

        input_path = maybe_strip_music(Path(input_path), force=(False if keep_music else None))
        path = auto_build_white_card_reaction(str(input_path), idea=idea, output_name=output_name,
                                               allow_teaser=allow_teaser, ctx=ctx)
        return path, output_name

    if fmt in ("format15", "format16"):
        # Batch = always TTS (assemble_format15/16 fall back to archetype TTS
        # per line when no human recording exists -- see duel.py/red_phone.py),
        # no per-slot audio-upload step -- real-voice recording doesn't fit an
        # unattended batch. users.run_generator reuses the exact same brand-
        # tagging/relocation path every other format goes through.
        mode = slot.get("mode", "random")
        subject = ""
        if mode == "manual":
            name_a = (slot.get("char_a_name") or "").strip()
            context_a = (slot.get("char_a_context") or "").strip()
            name_b = (slot.get("char_b_name") or "").strip()
            context_b = (slot.get("char_b_context") or "").strip()
            if not name_a or not name_b:
                raise ValueError("Il faut au moins le nom des deux personnages")
            subject = f"{name_a} / {context_a} vs {name_b} / {context_b}".strip()
        path = users.run_generator(ctx, fmt, subject)
        return path, (subject or fmt)

    raise ValueError(f"Format non supporté pour le batch manuel : {fmt}")


def _generate_batch_manual(fmt: str, slots: list[dict], job_id: str,
                           mode: str = "manual", start_date: str | None = None,
                           user: str | None = None, platforms: list[str] | None = None,
                           checkpoint_id: str | None = None, resume_skip: int = 0,
                           prior_results: list[dict] | None = None,
                           times: list[str | None] | None = None) -> dict:
    """checkpoint_id/resume_skip/prior_results: same crash-resume contract as
    _generate_batch (2026-08-12) -- MUCH simpler here since every slot is a
    flat, self-contained dict (no hook-assignment counters or format15/16/18
    position maps to keep order-independent, unlike the mixed batch).

    times: optional per-slot "HH:MM"|"now"|None override (2026-08-23, same
    per-video time control as the day-by-day planner's dp-time/dp-now-btn --
    see renderManualSlots' manual-slot-time/manual-slot-now-btn), aligned
    1:1 with `slots`. Only consulted when mode == "schedule" -- irrelevant
    for mode == "manual" (the email/Drive drop-off flow has no publish
    instant to control)."""
    ctx = users.get_user(user)
    total = len(slots)
    start_dt = _parse_start_date(start_date)
    results: list[dict] = list(prior_results or [])

    def _persist_progress() -> None:
        if not checkpoint_id:
            return
        ckpt = _load_checkpoint(checkpoint_id) or {
            "checkpoint_id": checkpoint_id, "created_at": datetime.now().isoformat(),
            "kind": "manual", "params": {}, "total": total,
        }
        ckpt["results"] = results
        ckpt["status"]  = "running"
        _save_checkpoint(checkpoint_id, ckpt)

    for i, slot in enumerate(slots):
        if i < resume_skip:
            continue
        _jobs[job_id]["progress"] = {
            "done": i, "total": total,
            "current": f"Vidéo {i + 1}/{total} — {fmt}",
        }
        try:
            video_path, label = _build_manual_slot(fmt, slot, ctx)
        except Exception as e:
            logger.error(f"Manual batch slot {i + 1}/{total} render FAILED: {e}", exc_info=True)
            results.append({"video": None, "subject": "", "day": i + 1, "format": fmt,
                             "status": "error", "error": str(e)})
            _persist_progress()
            continue

        scripts_dir = ctx.root / "output" / "scripts"   # see the main batch loop's identical fix/comment
        ts_prefix = video_path.stem[:15]
        candidates = list(scripts_dir.glob(f"{ts_prefix}*.json"))
        script = (
            json.loads(candidates[0].read_text(encoding="utf-8"))
            if candidates else {"format": fmt, "subject": label}
        )

        try:
            if mode == "schedule":
                day0 = start_dt + timedelta(days=i)
                fixed = times[i] if times and i < len(times) else None
                when = _day_publish_times(day0, 1, fixed=[fixed])[0]
                res = _schedule_post(video_path, script, when, ctx, platforms=platforms)
                if res.status in ("created", "skipped", "partial"):
                    results.append({"video": video_path.name, "subject": label, "day": i + 1,
                                     "format": fmt, "status": "ok", "when": res.scheduled_time,
                                     "networks": res.networks, "publish": res.status})
                else:
                    results.append({"video": video_path.name, "subject": label, "day": i + 1,
                                     "format": fmt, "status": "error", "error": res.detail})
            else:
                from modules.publish.publisher import publish
                publish(video_path, script, platforms=platforms, days_ahead=i + 1)
                results.append({"video": video_path.name, "subject": label, "day": i + 1,
                                 "format": fmt, "status": "ok"})
        except Exception as e:
            logger.error(f"Manual batch slot {i + 1}/{total} publish FAILED: {e}", exc_info=True)
            results.append({"video": video_path.name, "subject": label, "day": i + 1,
                             "format": fmt, "status": "error", "error": str(e)})
        _persist_progress()

    ok = sum(1 for r in results if r["status"] == "ok")
    if checkpoint_id:
        _delete_checkpoint(checkpoint_id)
    return {"ok": ok, "results": results}


@app.route("/api/batch_slot_upload", methods=["POST"])
def api_batch_slot_upload():
    """
    Uploads ONE file ahead of a manual batch's "Lancer" click -- Lancer itself
    posts JSON carrying all N slots at once, so a file can't ride along
    directly; the client uploads it here first and references the returned
    token in that slot's payload.
    """
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Fichier manquant"}), 400
    _CLIP_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".mp4"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
    dest = _CLIP_OVERRIDE_DIR / f"{ts}{ext}"
    file.save(str(dest))
    return jsonify({"token": dest.name})


@app.route("/api/batch_manual", methods=["POST"])
def api_batch_manual():
    data = request.json or {}
    fmt = str(data.get("format", ""))
    if fmt not in _MANUAL_BATCH_FORMATS:
        return jsonify({"error": f"format invalide (valides: {_MANUAL_BATCH_FORMATS})"}), 400

    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        return jsonify({"error": "Aucun slot fourni"}), 400
    if len(slots) > 60:
        return jsonify({"error": f"Maximum 60 vidéos par batch ({len(slots)} demandées)"}), 400

    mode = str(data.get("mode", "manual"))
    if mode not in ("manual", "schedule"):
        return jsonify({"error": "mode invalide (manual|schedule)"}), 400
    start_date = data.get("start_date") or None
    if mode == "schedule" and not start_date:
        return jsonify({"error": "Choisis une date de début (mode schedule)"}), 400
    user = data.get("user") or users.active_user()

    # Per-slot "HH:MM"|"now"|None time override (2026-08-23) -- see
    # _generate_batch_manual's docstring. Only meaningful for mode=="schedule";
    # sent-but-ignored for mode=="manual" is harmless, so no mode-gate here.
    times = data.get("times")
    if times is not None:
        if not isinstance(times, list) or len(times) != len(slots):
            return jsonify({"error": "times invalide (doit avoir autant d'entrées que slots)"}), 400

    platforms = data.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            return jsonify({"error": "Sélectionne au moins une plateforme"}), 400
        if not set(platforms).issubset(_VALID_PLATFORMS):
            return jsonify({"error": f"Plateformes invalides (valides: {sorted(_VALID_PLATFORMS)})"}), 400

    total = len(slots)
    job_id = f"batchmanual_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status": "queued", "message": "", "result": None,
        "progress": {"done": 0, "total": total, "current": "En attente…"},
    }

    # Crash-resume checkpoint (2026-08-12) -- same contract as the mixed
    # batch's own checkpoint (see _generate_batch's docstring), "kind":
    # "manual" tells api_batch_resume which generator function owns it.
    checkpoint_id = job_id
    _save_checkpoint(checkpoint_id, {
        "checkpoint_id": checkpoint_id,
        "created_at": datetime.now().isoformat(),
        "status": "running",
        "kind": "manual",
        "params": {
            "format": fmt, "slots": slots, "mode": mode, "start_date": start_date,
            "user": user, "platforms": platforms, "times": times,
        },
        "total": total,
        "results": [],
    })

    def _run():
        return _generate_batch_manual(fmt, slots, job_id, mode=mode, start_date=start_date,
                                      user=user, platforms=platforms, checkpoint_id=checkpoint_id,
                                      times=times)

    _enqueue_generation(job_id, _run)
    return jsonify({"job_id": job_id, "total": total})


# ── Long-format (YouTube compilation) ────────────────────────────────────────
# Own counters (state/long_format_counter.json + long_format_clip_history.json,
# see modules/long_format/long_format_state.py), fully separate from the mixte batch above.
# No auto-schedule here — generation only; publish via the existing per-video
# buttons (routed to the "youtube_long" profiles.yaml platform).

@app.route("/api/generate_long", methods=["POST"])
def api_generate_long():
    data    = request.json or {}
    subject = data.get("subject", "").strip()
    if not subject:
        return jsonify({"error": "Subject is required"}), 400

    assembly_mode = str(data.get("assembly_mode", "random"))
    if assembly_mode not in ("random", "context"):
        return jsonify({"error": "assembly_mode invalide (random|context)"}), 400

    ctx = users.get_user(data.get("user"))
    if "youtube_long" not in ctx.formats:
        return jsonify({"error": f"'{ctx.name}' n'a pas le format youtube_long"}), 400

    job_id = f"genlong_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {"status": "queued", "message": "", "result": None}

    def _generate():
        path = users.run_generator(ctx, "youtube_long", subject, assembly_mode=assembly_mode)
        return {"video": path.name, "size_mb": round(path.stat().st_size / 1e6, 1), "format": "youtube_long"}

    _enqueue_generation(job_id, _generate)
    return jsonify({"job_id": job_id})


def _generate_batch_long(total: int, job_id: str, assembly_mode: str = "random",
                         user: str | None = None, mode: str = "manual",
                         days: int = 1, vpd: int = 1,
                         start_date: str | None = None) -> dict:
    """
    mode="manual"   → generation only (existing behavior, publish via the
                       per-video button on the card).
    mode="schedule" → also auto-schedules each video direct-to-Zernio
                      ("youtube_long" platform) at a computed date/time,
                      spread across `days`/`vpd` the same way the mixte
                      batch's schedule mode does (_day_publish_times).
    """
    ctx     = users.get_user(user)
    results: list[dict] = []

    # start_date carries a full datetime-local value ("YYYY-MM-DDTHH:MM") so the
    # user can pick the exact hour, not just the day -- with vpd==1 (the common
    # long-format case) that exact hour is reused every day; with vpd>1 a single
    # chosen hour can't serve multiple slots/day, so those still fall back to
    # the randomized 07:00->01:00 spread (_day_publish_times), same as the
    # mixte batch's schedule mode.
    start_dt  = _parse_start_datetime(start_date) if mode == "schedule" else None
    day_times: dict[int, list[datetime]] = {}

    for i in range(total):
        subject = random.choice(_BATCH_SUBJECTS)
        _jobs[job_id]["progress"] = {
            "done": i, "total": total,
            "current": f"Vidéo {i + 1}/{total} — youtube_long — {subject[:45]}…",
        }
        try:
            video_path = users.run_generator(ctx, "youtube_long", subject, assembly_mode=assembly_mode)

            if mode == "schedule":
                scripts_dir = ctx.root / "output" / "scripts"   # see the main batch loop's identical fix/comment
                ts_prefix   = video_path.stem[:15]
                candidates  = list(scripts_dir.glob(f"{ts_prefix}*.json"))
                script      = (
                    json.loads(candidates[0].read_text(encoding="utf-8"))
                    if candidates else {"format": "youtube_long", "subject": subject}
                )
                g, slot = i // vpd, i % vpd
                if vpd == 1:
                    when = start_dt + timedelta(days=g)   # exact chosen hour, every day
                else:
                    if g not in day_times:
                        day0 = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        day_times[g] = _day_publish_times(day0 + timedelta(days=g), vpd)
                    when = day_times[g][slot]
                res  = _schedule_post(video_path, script, when, ctx, platforms=["youtube_long"])
                if res.status in ("created", "skipped", "partial"):
                    results.append({"video": video_path.name, "subject": subject, "status": "ok",
                                    "when": res.scheduled_time})
                    logger.info(f"Long-format batch {i + 1}/{total} scheduled ({res.status}) "
                                f"— {res.scheduled_time} — {video_path.name}")
                else:
                    results.append({"video": video_path.name, "subject": subject, "status": "error",
                                    "error": res.detail})
                    logger.error(f"Long-format batch {i + 1}/{total} publish FAILED: {res.detail}")
            else:
                results.append({"video": video_path.name, "subject": subject, "status": "ok"})
                logger.info(f"Long-format batch {i + 1}/{total} OK — {video_path.name}")
        except Exception as e:
            results.append({"subject": subject, "status": "error", "error": str(e)})
            logger.error(f"Long-format batch {i + 1}/{total} FAILED: {e}", exc_info=True)

    _jobs[job_id]["progress"] = {"done": total, "total": total, "current": "Terminé"}
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {"results": results, "total": total, "ok": ok_count}


@app.route("/api/batch_long", methods=["POST"])
def api_batch_long():
    data = request.json or {}
    try:
        days          = int(data.get("days", 1))
        vpd           = int(data.get("videos_per_day", 1))
        assembly_mode = str(data.get("assembly_mode", "random"))
        mode          = str(data.get("mode", "manual"))
        start_date    = data.get("start_date")
        user          = data.get("user") or users.active_user()
    except (ValueError, TypeError):
        return jsonify({"error": "Paramètres invalides"}), 400

    if assembly_mode not in ("random", "context"):
        return jsonify({"error": "assembly_mode invalide (random|context)"}), 400
    if mode not in ("manual", "schedule"):
        return jsonify({"error": "mode invalide (manual|schedule)"}), 400
    if not (1 <= days <= 90):
        return jsonify({"error": "days doit être entre 1 et 90"}), 400
    if not (1 <= vpd <= 30):
        return jsonify({"error": "vidéos/jour doit être entre 1 et 30"}), 400

    total = days * vpd
    if total > 30:   # long-form renders are far heavier than shorts -- keep batch sizes sane
        return jsonify({"error": f"Maximum 30 vidéos long-format par batch ({total} demandées)"}), 400

    job_id = f"batchlong_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status":   "queued",
        "message":  "",
        "result":   None,
        "progress": {"done": 0, "total": total, "current": "En attente…"},
    }

    def _run():
        return _generate_batch_long(total, job_id, assembly_mode=assembly_mode, user=user,
                                    mode=mode, days=days, vpd=vpd, start_date=start_date)

    _enqueue_generation(job_id, _run)
    return jsonify({"job_id": job_id, "total": total})


# ── MindFile brand batch (mf1/mf2/mf3/mf5/mf6 short-form, mf_long long-form) ─
# Deliberately NOT built on the ResetWorth mixte batch's (_generate_batch)
# schedule/custom_mix/hook/beat-match/manual-queue machinery -- none of that
# applies here (no format3-style hook-eligible format, no clip-pool
# availability to juggle), same "don't drag in dead complexity" call already
# made for _generate_batch_manual (format15/16/18) above. MindFile's own
# config.json declares "generator": "resetworth", so users.run_generator(ctx,
# fmt, subject) is literally the same pipeline.run() dispatch ResetWorth
# uses underneath -- only the format ids and subject pool differ.
# 2026-07-30, explicit user ask: MindFile should get the same 3 homes
# ResetWorth has (short-form batch, long-form batch, single generate) but
# scoped to ITS OWN formats -- single-generate already worked (brand-scoped
# format dropdown), these two batch routes were the missing pieces.

_MINDFILE_SHORT_FORMATS = ["mf1", "mf2", "mf3", "mf5", "mf6"]


def _make_mindfile_schedule(n: int, formats: list[str]) -> list[str]:
    """Even cycling rotation across `formats` -- same spread-not-bunched
    pattern _make_format_schedule's auto mode uses, just without a hook flag
    (none of MindFile's formats have a hook-eligible concept)."""
    return [formats[i % len(formats)] for i in range(n)]


# ── Brand-agnostic batch panel (2026-08-07) ─────────────────────────────────
#
# User pushback: "le panneau est censé être identique c'est juste le contenu
# qui change pour chaque marque" -- ResetWorth's batch panel and MindFile's
# had drifted into two hand-duplicated implementations (own JS functions, own
# DOM ids, own hardcoded network/format lists) that had to be kept in sync by
# hand -- exactly the kind of drift feedback_no_new_dedicated_panels_brand_
# scoping already warned about, and exactly what broke when "Personnalisé"
# only got added to one of them at first. templates/index.html now renders
# ONE shared Jinja block + ONE shared JS implementation for the batch panel;
# this config is the single per-brand data source it reads from -- content
# (which formats/networks a brand has) varies, the interface code doesn't.
#
# Combined mix-format registry: EVERY format id that can appear in a batch
# mix, across every brand, in one fixed order. A brand's own `formats` list
# (config.json) filters this down to what it actually owns -- same pattern
# _CUSTOM_MIX_FORMAT_KEYS/_MINDFILE_SHORT_FORMATS already used, just merged
# into one registry instead of two brand-specific constants. format3 (the
# only hook-eligible entry today) is deliberately positioned right before
# the mf* entries -- _flatten_day_plans requires the hook-eligible key to be
# the LAST element of whatever format_keys list it's given, and _batch_panel_
# config's hook_eligible re-sort below enforces that regardless of this
# registry's own order, so this position is a readability choice, not a load-
# bearing one.
_BATCH_MIX_LABELS: dict[str, str] = {
    "format1":  "format1",
    "format7":  "Clip Edit 2.0",
    "format6":  "Ranking List",
    "format15": "Duel Financier",
    "format16": "Téléphone Rouge",
    "format18": "White Card Reaction",
    "format3":  "Luxury",
    "mf1":      "POV Reenactment",
    "mf2":      "Decode",
    "mf3":      "Which One Are You",
    "mf5":      "Two Minds",
    "mf6":      "The Exchange",
}
_BATCH_MIX_ORDER            = list(_BATCH_MIX_LABELS.keys())
_NETWORK_LABELS: dict[str, str] = {
    "snapchat": "Snapchat", "tiktok": "TikTok", "instagram": "Instagram",
    "facebook": "Facebook", "youtube": "YouTube",
}
_BATCH_CLIP_POOL_FORMATS    = {"format1", "format7"}   # draw from assets/clips/, need the low-clips warning
_BATCH_MANUAL_INPUT_FORMATS = {"format15", "format16", "format18"}   # need a per-slot FORMAT_FIELDS mini-form
# _HOOK_COMPATIBLE_FORMATS (defined above, = {"format3"}) doubles as "which
# mix format(s) are hook-eligible" -- one shared set, not a second one here.


def _batch_panel_config(ctx) -> dict:
    """
    Single source of truth for the (brand-agnostic) batch panel's rendering.
    Returned dict is injected into the template as JSON (Jinja `batch_cfg`,
    also a `window.BATCH_CFG` JS const) -- see the module comment above.
    """
    owned = [f for f in _BATCH_MIX_ORDER if f in ctx.formats]
    hook_eligible = next((f for f in owned if f in _HOOK_COMPATIBLE_FORMATS), None)
    # Enforce "hook-eligible key is last" programmatically (see registry
    # comment above) rather than trusting _BATCH_MIX_ORDER's manual ordering
    # to never regress this -- _flatten_day_plans silently mis-assigns
    # otherwise (whatever's last gets treated as the hook key).
    if hook_eligible:
        owned = [f for f in owned if f != hook_eligible] + [hook_eligible]

    # ctx.generator is NOT a reliable signal here -- MindFile's own
    # config.json sets "generator": "resetworth" too (that field means
    # something else, per-video content generation routing, not which batch
    # route to call), confirmed live before this shipped. Format membership
    # is the actual signal that already distinguishes the two batch
    # pipelines everywhere else (the Jinja gates this replaces used it too).
    is_mindfile_pipeline = any(f in ctx.formats for f in _MINDFILE_SHORT_FORMATS)
    # Manual-input formats (format15/16/18) need a per-slot mini-form
    # (character names, clip source) that /api/batch's day_plans/custom_mix
    # machinery collects but the plain 1/4-split scheduler (_make_format_
    # schedule) never did -- their "X uniquement" button used to 400 through
    # THAT route for exactly this reason. Kept out of simpleFormats (which
    # feeds api_batch's format_mode validation) for that reason, but they
    # DO still get their own "X uniquement" button in the template (a
    # separate Jinja loop over manualFormats, 2026-08-08) -- selectMode()/
    # launchBatch() already special-case _MANUAL_BATCH_FORMATS client-side
    # and redirect straight to the dedicated /api/batch_manual route
    # (renderManualSlots()'s per-slot mini-forms + launchBatchManual()),
    # which never goes through api_batch's format_mode check at all.
    simple_formats = [f for f in owned if f not in _BATCH_MANUAL_INPUT_FORMATS]
    return {
        "mixFormats": [{"id": f, "label": _BATCH_MIX_LABELS.get(f, f)} for f in owned],
        # list[dict] for the Jinja "X uniquement" buttons (needs labels);
        # api_batch's format_mode validation below pulls .id back out of
        # this same list -- one list, not two, so they can't drift apart.
        "simpleFormats": [{"id": f, "label": _BATCH_MIX_LABELS.get(f, f)} for f in simple_formats],
        "hookEligible": hook_eligible,
        "hookEligibleLabel": _BATCH_MIX_LABELS.get(hook_eligible, hook_eligible or ""),
        "manualFormats": [
            {"id": f, "label": _BATCH_MIX_LABELS.get(f, f)}
            for f in owned if f in _BATCH_MANUAL_INPUT_FORMATS
        ],
        "clipPoolFormats": [f for f in owned if f in _BATCH_CLIP_POOL_FORMATS],
        "networks": ctx.default_networks,
        "networkLabels": {n: _NETWORK_LABELS.get(n, n.capitalize()) for n in ctx.default_networks},
        # Publication (direct upload + publish) is genuinely brand-agnostic
        # server-side already (/api/publish_upload only ever reads `ctx`, no
        # ResetWorth-specific assumption in it) -- true for every brand.
        "hasPublication": True,
        # The one place this config still branches on the brand's underlying
        # generation PIPELINE rather than pure content -- which endpoint the
        # batch panel posts to genuinely differs (own _generate_batch_xxx
        # function per pipeline). A 3rd pipeline needs a 3rd branch here,
        # same as it would need its own _generate_batch_xxx function anyway.
        "endpoint": "/api/batch_mindfile" if is_mindfile_pipeline else "/api/batch",
    }


def _generate_batch_mindfile(total: int, job_id: str, formats: list[str],
                             mode: str = "manual", days: int = 1, vpd: int = 1,
                             start_date: str | None = None, user: str | None = None,
                             platforms: list[str] | None = None,
                             day_plans: list[dict] | None = None) -> dict:
    """
    day_plans (list[dict] or None): same per-day-independent-plan concept as
    ResetWorth's _generate_batch (see its day_plans docstring) -- one dict
    per day, {"mix": {mf1,mf2,mf3,mf5,mf6: int}, "times": ["HH:MM"|"now"|None, ...]}.
    No hook concept for MindFile's formats, so no hook_count/hook_assignments
    key here. Flattened via the same shared _flatten_day_plans ResetWorth
    uses. When None, falls back to the old even-rotation across `formats`
    with a single uniform `vpd`/`days`.
    """
    ctx  = users.get_user(user)
    past = users.past_subjects(ctx)

    if day_plans is not None:
        # MindFile has no hook-eligible format (hook_field=False) -- the
        # format3_mode_per_slot return value is meaningless here, discarded.
        # publish_override_per_slot (2026-08-06, "🧩 Personnalisé" pmode,
        # same feature as ResetWorth's -- see project_batch_per_video_controls_pending
        # memory) IS used here.
        schedule, day_index_per_slot, slot_index_within_day, fixed_times_by_day, day_vpd_list, _, publish_override_per_slot = (
            _flatten_day_plans(day_plans, _MINDFILE_SHORT_FORMATS, hook_field=False)
        )
        total = sum(day_vpd_list)
        days  = len(day_plans)
    else:
        schedule = [(fmt, False) for fmt in _make_mindfile_schedule(total, formats)]
        day_index_per_slot    = [i // vpd for i in range(total)]
        slot_index_within_day = [i % vpd for i in range(total)]
        fixed_times_by_day    = [[None] * vpd for _ in range(days)]
        day_vpd_list          = [vpd] * days
        publish_override_per_slot = [None] * total

    subjects = _pick_subjects(total, past_subjects=past, pool_source=_MINDFILE_SUBJECTS)

    start_dt: datetime | None = _parse_start_date(start_date) if mode == "schedule" else None
    day_times: dict[int, list[datetime]] = {}
    results: list[dict] = []
    # run_generator relocates the video AND its script JSON into the brand's
    # own users/<name>/output/ tree for any non-default brand (see
    # modules/users.py's _relocate_to_brand) -- MindFile's script JSONs live
    # there, not in the shared root output/scripts/ this file's other batch
    # functions read from.
    scripts_dir = (BASE_DIR / "output" / "scripts") if ctx.is_default else (ctx.root / "output" / "scripts")

    for i in range(total):
        fmt, _  = schedule[i]
        subject = subjects[i]
        day_idx = day_index_per_slot[i]
        day_num = day_idx + 1
        _jobs[job_id]["progress"] = {
            "done": i, "total": total,
            "current": f"Vidéo {i + 1}/{total} — {fmt} — {subject[:45]}…",
        }
        try:
            video_path = users.run_generator(ctx, fmt, subject)
            ts_prefix  = video_path.stem[:15]
            candidates = list(scripts_dir.glob(f"{ts_prefix}*.json"))
            script     = (
                json.loads(candidates[0].read_text(encoding="utf-8"))
                if candidates else {"format": fmt, "subject": subject}
            )
            # Per-video publish override (2026-08-06, "🧩 Personnalisé" pmode) --
            # this slot's OWN mode/networks instead of the batch-global ones,
            # when the user set one for it. None -> unchanged behavior. Note
            # the override's own vocabulary is {"auto","manual"} (matches the
            # UI/ResetWorth's routes[] convention) while this function's own
            # `mode` local is {"schedule","manual"} -- translated here, not
            # the same axis by coincidence of both using the word "manual".
            override = publish_override_per_slot[i]
            if override:
                slot_mode      = "schedule" if override.get("mode") == "auto" else "manual"
                slot_platforms = override.get("networks") or platforms
            else:
                slot_mode      = mode
                slot_platforms = platforms

            if slot_mode == "schedule":
                g, slot = day_idx, slot_index_within_day[i]
                if g not in day_times:
                    day_times[g] = _day_publish_times(start_dt + timedelta(days=g), day_vpd_list[g],
                                                        fixed=fixed_times_by_day[g])
                when = day_times[g][slot]
                res  = _schedule_post(video_path, script, when, ctx, platforms=slot_platforms)
                if res.status in ("created", "skipped", "partial"):
                    results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                     "format": fmt, "status": "ok", "when": res.scheduled_time,
                                     "networks": res.networks, "publish": res.status})
                    logger.info(f"MindFile batch {i + 1}/{total} scheduled ({res.status}) "
                                f"— J+{day_num} — {video_path.name}")
                else:
                    results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                     "format": fmt, "status": "error", "error": res.detail})
                    logger.error(f"MindFile batch {i + 1}/{total} publish FAILED: {res.detail}")
            else:
                from modules.publish.publisher import publish
                publish(video_path, script, platforms=slot_platforms, days_ahead=day_num)
                results.append({"video": video_path.name, "subject": subject, "day": day_num,
                                 "format": fmt, "status": "ok"})
                logger.info(f"MindFile batch {i + 1}/{total} OK — J+{day_num} — {video_path.name}")
        except Exception as e:
            logger.error(f"MindFile batch slot {i + 1}/{total} FAILED: {e}", exc_info=True)
            results.append({"subject": subject, "day": day_num, "format": fmt,
                             "status": "error", "error": str(e)})

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"ok": ok, "results": results}


@app.route("/api/batch_mindfile", methods=["POST"])
def api_batch_mindfile():
    """Short-form MindFile batch. format_mode="auto" (default) rotates evenly
    across mf1/mf2/mf3/mf5/mf6 -- same "Mélange auto" concept ResetWorth's
    batch panel has; any single format id in _MINDFILE_SHORT_FORMATS restricts
    the whole batch to just that format ("mf1 uniquement" etc, same pattern
    as ResetWorth's format1/format3/... "uniquement" buttons). _make_mindfile_
    schedule already handles a 1-element formats list as "always that one",
    so no new schedule logic is needed here -- just which list gets passed."""
    data = request.json or {}
    try:
        days        = int(data.get("days", 1))
        vpd         = int(data.get("videos_per_day", 1))
        mode        = str(data.get("mode", "manual"))
        format_mode = str(data.get("format_mode", "auto"))
        start_date  = data.get("start_date") or None
        user        = data.get("user") or users.active_user()
    except (ValueError, TypeError):
        return jsonify({"error": "Paramètres invalides"}), 400

    if mode not in ("manual", "schedule"):
        return jsonify({"error": "mode invalide (manual|schedule)"}), 400
    if format_mode != "auto" and format_mode not in _MINDFILE_SHORT_FORMATS:
        return jsonify({"error": f"format_mode invalide (auto ou {_MINDFILE_SHORT_FORMATS})"}), 400

    platforms = data.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            return jsonify({"error": "Sélectionne au moins une plateforme"}), 400
        if not set(platforms).issubset(_VALID_PLATFORMS):
            return jsonify({"error": f"Plateformes invalides (valides: {sorted(_VALID_PLATFORMS)})"}), 400

    # Per-day custom plan -- same day_plans/custom_mix normalization concept
    # as ResetWorth's /api/batch (see there for the full rationale), scoped
    # to MindFile's own formats and with no hook concept.
    day_plans_raw = data.get("day_plans")
    custom_mix    = data.get("custom_mix")
    day_plans: list[dict] | None = None

    if day_plans_raw is not None:
        if format_mode != "auto":
            return jsonify({"error": "day_plans seulement disponible en mode auto"}), 400
        if not isinstance(day_plans_raw, list) or not day_plans_raw:
            return jsonify({"error": "day_plans invalide"}), 400

        day_plans = []
        for idx, raw in enumerate(day_plans_raw, start=1):
            label = f"day_plans[{idx}]"
            if not isinstance(raw, dict):
                return jsonify({"error": f"{label}: invalide"}), 400
            mix = raw.get("mix")
            if not isinstance(mix, dict):
                return jsonify({"error": f"{label}: mix invalide"}), 400
            try:
                counts = {f: int(mix.get(f, 0)) for f in _MINDFILE_SHORT_FORMATS}
            except (ValueError, TypeError, AttributeError):
                return jsonify({"error": f"{label}: mix invalide"}), 400
            if any(v < 0 for v in counts.values()):
                return jsonify({"error": f"{label}: les compteurs ne peuvent pas être négatifs"}), 400
            day_vpd_i = sum(counts.values())
            if day_vpd_i > 30:
                return jsonify({"error": f"{label}: max 30 vidéos/jour ({day_vpd_i} demandées)"}), 400

            times = raw.get("times")
            if times is not None:
                if not isinstance(times, list) or len(times) != day_vpd_i:
                    return jsonify({"error": f"{label}: times doit contenir {day_vpd_i} entrée(s)"}), 400
                for t in times:
                    if t is not None and not _valid_hhmm(t):
                        return jsonify({"error": f"{label}: heure invalide ({t})"}), 400

            # publish_overrides (2026-08-06, "🧩 Personnalisé" pmode -- same
            # feature as ResetWorth's /api/batch, see there for the fuller
            # comment): per-video auto-schedule+networks vs manuel.
            publish_overrides_i = raw.get("publish_overrides")
            if publish_overrides_i is not None:
                if not isinstance(publish_overrides_i, list) or len(publish_overrides_i) != day_vpd_i:
                    return jsonify({"error": f"{label}: publish_overrides doit contenir {day_vpd_i} entrée(s)"}), 400
                for o in publish_overrides_i:
                    if o is None:
                        continue
                    if not isinstance(o, dict) or o.get("mode") not in ("auto", "manual"):
                        return jsonify({"error": f"{label}: publish_overrides invalide"}), 400
                    if o.get("mode") == "auto":
                        nets = o.get("networks")
                        if not isinstance(nets, list) or not nets or not set(nets).issubset(_VALID_PLATFORMS):
                            return jsonify({"error": f"{label}: publish_overrides.networks invalide"}), 400

            day_dict: dict = {"mix": counts}
            if times is not None:
                day_dict["times"] = times
            if publish_overrides_i is not None:
                day_dict["publish_overrides"] = publish_overrides_i
            day_plans.append(day_dict)
        days = len(day_plans)

    elif custom_mix is not None:
        if format_mode != "auto":
            return jsonify({"error": "custom_mix seulement disponible en mode auto"}), 400
        try:
            counts = {f: int(custom_mix.get(f, 0)) for f in _MINDFILE_SHORT_FORMATS}
        except (ValueError, TypeError, AttributeError):
            return jsonify({"error": "custom_mix invalide"}), 400
        if sum(counts.values()) != vpd:
            return jsonify({"error": f"custom_mix doit sommer à {vpd} (vidéos/jour)"}), 400
        day_plans = [{"mix": counts} for _ in range(days)]

    if not (1 <= days <= 90):
        return jsonify({"error": "days doit être entre 1 et 90"}), 400

    if day_plans is None:
        if not (1 <= vpd <= 30):
            return jsonify({"error": "vidéos/jour doit être entre 1 et 30"}), 400
        total = days * vpd
    else:
        total = sum(sum(dp["mix"].values()) for dp in day_plans)

    if total > 60:
        return jsonify({"error": f"Maximum 60 vidéos par batch ({total} demandées)"}), 400
    if mode == "schedule" and not start_date:
        return jsonify({"error": "Choisis une date de début (mode schedule)"}), 400

    batch_formats = _MINDFILE_SHORT_FORMATS if format_mode == "auto" else [format_mode]

    job_id = f"batchmf_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status": "queued", "message": "", "result": None,
        "progress": {"done": 0, "total": total, "current": "En attente…"},
    }

    def _run():
        return _generate_batch_mindfile(total, job_id, batch_formats,
                                        mode=mode, days=days, vpd=vpd,
                                        start_date=start_date, user=user, platforms=platforms,
                                        day_plans=day_plans)

    _enqueue_generation(job_id, _run)
    start_dt = _parse_start_date(start_date)
    end_dt   = start_dt + timedelta(days=days - 1)
    return jsonify({
        "job_id": job_id, "total": total, "mode": mode,
        "start_date": start_dt.strftime("%Y-%m-%d"), "end_date": end_dt.strftime("%Y-%m-%d"),
    })


@app.route("/api/batch_mindfile_long", methods=["POST"])
def api_batch_mindfile_long():
    """Long-form MindFile batch (mf_long only) -- same shape as the short-form
    route above, single-format schedule, lower cap (heavier renders, same
    reasoning as /api/batch_long's youtube_long cap)."""
    data = request.json or {}
    try:
        days       = int(data.get("days", 1))
        vpd        = int(data.get("videos_per_day", 1))
        mode       = str(data.get("mode", "manual"))
        start_date = data.get("start_date") or None
        user       = data.get("user") or users.active_user()
    except (ValueError, TypeError):
        return jsonify({"error": "Paramètres invalides"}), 400

    if mode not in ("manual", "schedule"):
        return jsonify({"error": "mode invalide (manual|schedule)"}), 400
    if not (1 <= days <= 90):
        return jsonify({"error": "days doit être entre 1 et 90"}), 400
    if not (1 <= vpd <= 30):
        return jsonify({"error": "vidéos/jour doit être entre 1 et 30"}), 400
    if mode == "schedule" and not start_date:
        return jsonify({"error": "Choisis une date de début (mode schedule)"}), 400

    platforms = data.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list) or not platforms:
            return jsonify({"error": "Sélectionne au moins une plateforme"}), 400
        if not set(platforms).issubset(_VALID_PLATFORMS):
            return jsonify({"error": f"Plateformes invalides (valides: {sorted(_VALID_PLATFORMS)})"}), 400

    total = days * vpd
    if total > 30:   # long-form renders are far heavier than shorts -- keep batch sizes sane
        return jsonify({"error": f"Maximum 30 vidéos long-format par batch ({total} demandées)"}), 400

    job_id = f"batchmflong_{datetime.now().strftime('%H%M%S%f')}"
    _jobs[job_id] = {
        "status": "queued", "message": "", "result": None,
        "progress": {"done": 0, "total": total, "current": "En attente…"},
    }

    def _run():
        return _generate_batch_mindfile(total, job_id, ["mf_long"],
                                        mode=mode, days=days, vpd=vpd,
                                        start_date=start_date, user=user, platforms=platforms)

    _enqueue_generation(job_id, _run)
    start_dt = _parse_start_date(start_date)
    end_dt   = start_dt + timedelta(days=days - 1)
    return jsonify({
        "job_id": job_id, "total": total, "mode": mode,
        "start_date": start_dt.strftime("%Y-%m-%d"), "end_date": end_dt.strftime("%Y-%m-%d"),
    })


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  Dashboard -> http://localhost:5000")
    print("=" * 50 + "\n")
    app.run(debug=False, port=5000, threaded=True)
