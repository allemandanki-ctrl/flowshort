"""
B-roll for the format1 clip-edit: theme keywords extracted from the speech
transcript, then matching video clips fetched from Pexels.

Reference technique: "most of these accounts... use Pinterest for this...
find animations, all different types of concept... download six seven
different clips that you think suit the video." Pinterest has no reliable
API for automated fetching (unofficial scraping is ToS-fragile) -- Pexels'
free video API gives the same real-footage-not-generated result without
the reliability risk, keyed off themes pulled from the actual speech
instead of manual browsing.
"""

import logging
import random
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_N_KEYWORDS   = 7
_FALLBACK_KEYWORDS = [
    "cinematic clouds slow motion", "city lights night timelapse",
    "ocean waves aerial", "mountain landscape drone", "forest sunlight rays",
]


def _has_source_audio(path: str) -> bool:
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return bool(r.stdout.strip())


def extract_broll_keywords(full_text: str, n: int = _N_KEYWORDS) -> list[str]:
    """
    Read the speech transcript, return N short visual search phrases (2-4
    words each) capturing THEMES/IMAGERY the speech evokes -- not literal
    transcription. E.g. a speech about resilience might yield "storm ocean
    waves", "mountain climber silhouette", "phoenix fire embers".
    Falls back to a generic cinematic set on any failure.
    """
    from config import CLAUDE_MODEL
    if not full_text.strip():
        return list(_FALLBACK_KEYWORDS)

    prompt = (
        "Read this motivational speech transcript. Suggest "
        f"{n} short stock-footage search phrases (2-4 words each) for B-roll "
        "that would visually match its THEMES and IMAGERY -- cinematic, "
        "emotionally resonant shots (nature, urban, abstract, movement), NOT "
        "literal illustrations of the words. Think like a film editor picking "
        "cutaways for a trailer, not a slideshow.\n\n"
        f"Transcript: {full_text[:800]}\n\n"
        'Answer with JSON only: {"keywords": ["...", "...", ...]}'
    )
    try:
        from modules.claude_cli import call_claude
        import json
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        m = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(m.group(0)) if m else {}
        keywords = [str(k).strip() for k in result.get("keywords", []) if str(k).strip()]
        if not keywords:
            return list(_FALLBACK_KEYWORDS)
        logger.info(f"clip_broll: keywords -> {keywords}")
        return keywords[:n]
    except Exception as e:
        logger.warning(f"clip_broll: keyword extraction failed ({e}); using fallback set")
        return list(_FALLBACK_KEYWORDS)


_PINTEREST_DIR = None   # set lazily below (needs BASE_DIR from config)
_PINTEREST_USED_FILE = None
_PINTEREST_EXCLUDE_LAST_N = 40   # Significantly expanded to avoid back-to-back repeats over a longer period
_MOOD_VOCAB = ("dark", "radiant")
_MOOD_MANIFEST_FILE = None


def _pinterest_pool() -> list:
    from pathlib import Path
    from config import ASSETS_DIR
    global _PINTEREST_DIR, _PINTEREST_USED_FILE
    if _PINTEREST_DIR is None:
        _PINTEREST_DIR = ASSETS_DIR / "broll_pinterest"
    if not _PINTEREST_DIR.exists():
        return []
    return sorted(_PINTEREST_DIR.rglob("*.mp4"))


def classify_speech_mood(full_text: str) -> str | None:
    """
    dark (tense/dramatic/somber) vs radiant (bright/uplifting/triumphant) --
    user: "quand tu vois que l'univers est dark, associe du dark, quand tu
    vois que ça commence à être rayonnant met du rayonnant" (match the
    B-roll's visual tone to the speech's tone, not a random mood every
    time). Returns None on any failure -- caller then skips mood filtering
    and picks from the full pool instead of narrowing incorrectly.
    """
    from config import CLAUDE_MODEL
    if not full_text.strip():
        return None
    try:
        from modules.claude_cli import call_claude
        prompt = (
            "Classify the overall emotional tone of this speech into EXACTLY ONE "
            f"of: {', '.join(_MOOD_VOCAB)}. 'dark' = tense, dramatic, somber, "
            "confrontational. 'radiant' = bright, uplifting, triumphant, hopeful.\n"
            f"Speech: {full_text[:500]}\nAnswer with only the word, nothing else."
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        mood = raw.strip().lower()
        return mood if mood in _MOOD_VOCAB else None
    except Exception as e:
        logger.warning(f"clip_broll: speech mood classification failed ({e})")
        return None


def _classify_clip_mood(clip_path) -> str | None:
    """
    Same dark/radiant vocab, one frame per pool clip, result cached forever
    in state/pinterest_clip_moods.json (clips don't change mood between
    runs -- classify once, reuse). Returns None on failure (clip excluded
    from mood-filtered picks but still available in the unfiltered pool).
    """
    import subprocess
    import tempfile
    from pathlib import Path
    from config import CLAUDE_MODEL

    frame = Path(tempfile.gettempdir()) / "vid_pipeline_f1_broll" / f"mood_{Path(clip_path).stem}.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", "1.5", "-i", str(clip_path),
         "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(frame)],
        capture_output=True)
    if r.returncode != 0 or not frame.exists():
        return None
    try:
        from modules.claude_cli import call_claude
        prompt = (
            f"@{frame}\n"
            "Classify this shot's visual tone into EXACTLY ONE of: "
            f"{', '.join(_MOOD_VOCAB)}. 'dark' = night, shadows, muted/desaturated, "
            "somber. 'radiant' = bright, golden hour, vivid, sunlit. "
            "Answer with only the word."
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        mood = raw.strip().lower()
        return mood if mood in _MOOD_VOCAB else None
    except Exception as e:
        logger.warning(f"clip_broll: clip mood classification failed for {clip_path} ({e})")
        return None
    finally:
        frame.unlink(missing_ok=True)


def _pool_moods() -> dict:
    """Loads/builds the clip->mood manifest, classifying any new pool clips once."""
    import json
    from config import STATE_DIR
    global _MOOD_MANIFEST_FILE
    if _MOOD_MANIFEST_FILE is None:
        _MOOD_MANIFEST_FILE = STATE_DIR / "pinterest_clip_moods.json"

    moods: dict = {}
    if _MOOD_MANIFEST_FILE.exists():
        try:
            moods = json.loads(_MOOD_MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            moods = {}

    changed = False
    for clip in _pinterest_pool():
        key = str(clip)
        if key not in moods:
            moods[key] = _classify_clip_mood(clip)
            changed = True
    if changed:
        _MOOD_MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MOOD_MANIFEST_FILE.write_text(json.dumps(moods, indent=2), encoding="utf-8")
    return moods


def _fetch_pinterest_clips(count: int, mood: str | None = None) -> list[str]:
    """
    User-curated B-roll pool (assets/broll_pinterest/, hand-picked from
    Pinterest -- silhouette/moody aesthetic, screened for visible stock
    watermarks) -- the PRIMARY source now, replacing Pexels after the user
    judged Pexels B-roll quality/fluidity as bad. Small pool: anti-repeat
    only excludes the last N picks, not a days-based window (would starve
    the pool immediately at that size).

    mood ("dark"/"radiant"/None) narrows the pool to matching clips first
    (see _pool_moods) -- falls back to the FULL pool if narrowing would
    leave fewer clips than requested, so a mood mismatch never blocks the
    burst from filling.
    """
    import json
    import random
    from pathlib import Path
    from config import STATE_DIR
    global _PINTEREST_USED_FILE

    pool = _pinterest_pool()
    if not pool:
        return []

    if _PINTEREST_USED_FILE is None:
        _PINTEREST_USED_FILE = STATE_DIR / "pinterest_broll_used.json"

    used: list[str] = []
    if _PINTEREST_USED_FILE.exists():
        try:
            used = json.loads(_PINTEREST_USED_FILE.read_text(encoding="utf-8"))
        except Exception:
            used = []

    recent = set(used[-_PINTEREST_EXCLUDE_LAST_N:])
    available = [p for p in pool if str(p) not in recent] or list(pool)

    if mood:
        # Prioritize mood-matched clips, but a small pool relative to burst
        # size means strict mood-only would starve the burst -- fill any
        # remainder from the rest of the pool rather than going all-or-nothing.
        moods = _pool_moods()
        matched   = [p for p in available if moods.get(str(p)) == mood]
        unmatched = [p for p in available if moods.get(str(p)) != mood]
        random.shuffle(matched)
        random.shuffle(unmatched)
        available = matched + unmatched
        logger.info(f"clip_broll: mood='{mood}' -> {len(matched)} matching clip(s) prioritized "
                    f"(of {len(available)} eligible)")
    else:
        random.shuffle(available)

    picks = available[:count]

    used.extend(str(p) for p in picks)
    _PINTEREST_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PINTEREST_USED_FILE.write_text(json.dumps(used[-200:], indent=2), encoding="utf-8")

    logger.info(f"clip_broll: {len(picks)} clip(s) from curated Pinterest pool "
                f"({len(pool)} eligible)")
    return [str(p) for p in picks]


def _score_clip_relevance(clip_path, keywords: list[str]) -> float:
    """
    Ranks a clip's relevance to keywords (0.0 to 1.0). Cached by keyword set? 
    No, just cache by clip to keep it manageable. Re-run per-keyword batch.
    """
    import subprocess
    import tempfile
    import json
    from config import CLAUDE_MODEL

    # Cache mechanism: simple file for now
    from config import STATE_DIR
    cache_file = STATE_DIR / "pinterest_clip_relevance.json"
    cache = {}
    if cache_file.exists():
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    
    # Generate a unique key for this clip + keywords combo
    cache_key = f"{Path(clip_path).name}_{hash(tuple(sorted(keywords)))}"
    if cache_key in cache:
        return cache[cache_key]

    # Analyze frame
    frame = Path(tempfile.gettempdir()) / "vid_pipeline_f1_broll" / f"score_{Path(clip_path).stem}.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", "1.5", "-i", str(clip_path),
         "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(frame)],
        capture_output=True)
    if r.returncode != 0 or not frame.exists():
        return 0.5 # Default middle score

    try:
        from modules.claude_cli import call_claude
        prompt = (
            f"@{frame}\n"
            f"On a scale of 0.0 to 1.0, how relevant is this B-roll shot "
            f"to these concepts: {', '.join(keywords)}? "
            "Answer with ONLY the number."
        )
        raw, _, _ = call_claude("", prompt, model=CLAUDE_MODEL)
        score = float(raw.strip())
        cache[cache_key] = score
        cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        return score
    except Exception as e:
        logger.warning(f"clip_broll: relevance scoring failed for {clip_path} ({e})")
        return 0.5
    finally:
        frame.unlink(missing_ok=True)


def fetch_broll_clips(keywords: list[str], count: int, dest_dir, full_text: str = "") -> list[str]:
    """
    Primary source: the user's curated Pinterest B-roll pool, mood-matched
    AND semantically ranked by relevance to keywords. Recently-used clips
    (last _PINTEREST_EXCLUDE_LAST_N picks, tracked in the same
    state/pinterest_broll_used.json _fetch_pinterest_clips writes to) are
    excluded first so relevance ranking never just picks the same handful
    of "best" clips on every render.
    """
    import json
    from config import STATE_DIR
    global _PINTEREST_USED_FILE

    mood = classify_speech_mood(full_text) if full_text else None

    # 1. Get initial candidate pool
    pool = _pinterest_pool()
    if not pool:
        # Fallback to Pexels
        from modules.visuals import _fetch_pexels_videos
        from pathlib import Path
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        return _fetch_pexels_videos(keywords, count, dest_dir)

    if _PINTEREST_USED_FILE is None:
        _PINTEREST_USED_FILE = STATE_DIR / "pinterest_broll_used.json"

    used: list[str] = []
    if _PINTEREST_USED_FILE.exists():
        try:
            used = json.loads(_PINTEREST_USED_FILE.read_text(encoding="utf-8"))
        except Exception:
            used = []

    recent = set(used[-_PINTEREST_EXCLUDE_LAST_N:])
    available = [p for p in pool if str(p) not in recent] or list(pool)

    # 2. Filter by mood if applicable
    moods = _pool_moods()
    if mood:
        matched   = [p for p in available if moods.get(str(p)) == mood]
        unmatched = [p for p in available if moods.get(str(p)) != mood]
        # Prioritize matched, then shuffle within
        random.shuffle(matched)
        random.shuffle(unmatched)
        available = matched + unmatched
    else:
        random.shuffle(available)

    # 3. Rank top N by relevance score
    # We only score the top X candidates to keep API costs sane
    scoring_pool = available[:count * 2]
    scored = [(p, _score_clip_relevance(p, keywords)) for p in scoring_pool]
    scored.sort(key=lambda x: x[1], reverse=True)

    picks = [p[0] for p in scored[:count]]

    used.extend(str(p) for p in picks)
    _PINTEREST_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PINTEREST_USED_FILE.write_text(json.dumps(used[-200:], indent=2), encoding="utf-8")

    logger.info(f"clip_broll: picked {len(picks)} clips using semantic relevance scoring "
                f"({len(recent)} recent excluded, {len(pool)} in pool)")
    return [str(p) for p in picks]


_CORNER_RADIUS   = 36
_BURST_SUBCLIP_DUR = 0.85  # each clip's play time WITHIN the burst -- snappy, not lingering
_BURST_SIZE        = 12    # how many different B-roll clips play back-to-back in one burst


def _build_rounded_mask_png(width: int, height: int, out_path) -> None:
    """
    RGBA overlay PNG: opaque BLACK everywhere except a rounded-rectangle hole
    in the middle (fully transparent there). Overlaying this on top of any
    footage punches rounded black corners onto it -- the reference video's
    signature "curved edge frame" effect, achieved as a plain PNG overlay
    (same pattern as the existing quote/watermark PNGs) instead of needing
    an alpha-preserving video codec.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    hole = Image.new("L", (width, height), 0)
    hd = ImageDraw.Draw(hole)
    hd.rounded_rectangle([0, 0, width - 1, height - 1], radius=_CORNER_RADIUS, fill=255)
    # alpha = 255 - hole (opaque black outside the rounded rect, transparent inside)
    alpha = Image.eval(hole, lambda v: 255 - v)
    img.putalpha(alpha)
    img.save(str(out_path), "PNG")


def build_framed_clip(
    clip_path, broll_paths: list[str], duration: float, out_path,
    width: int = 1080, height: int = 608, keywords: list[str] | None = None,
) -> tuple[float, float] | None:
    """
    Produces a single video (width x height, rounded black corners baked in,
    ORIGINAL clip audio preserved, ambient SFX mixed in under the burst)
    where the main clip's video cuts to a BURST of several different B-roll
    clips playing back-to-back (not one isolated cutaway at a time), then
    returns to the main clip -- the reference technique's core edit: "start
    placing [B-roll] where you think it's relevant... really start to build
    that story" over continuous narration. User feedback that replaced the
    earlier "scattered single cutaways" design: "il en faut bcp plus
    d'affilé, pas de manière random... faut plusieurs images et vidéos
    stylés d'affilé" (need several clips IN A ROW, not randomly scattered
    one at a time) -- a proper rapid-cut montage burst, not sparse inserts.

    ONE burst window, placed in the middle of the clip (25%-75% usable
    range), containing up to _BURST_SIZE different B-roll clips at
    _BURST_SUBCLIP_DUR each. Speech audio plays continuously underneath the
    whole burst, unaffected -- only the video track cuts away.

    Returns (burst_start, burst_end) in seconds on this clip's timeline, or
    None if no B-roll was available -- the caller uses this to caption ONLY
    the burst window when the source clip's own footage already has
    burned-in captions elsewhere (B-roll never has its own captions, so it
    still needs ours regardless of what the main clip carries).
    """
    import subprocess
    import tempfile
    from pathlib import Path

    broll_paths = broll_paths[:_BURST_SIZE]
    n_clips = len(broll_paths)
    keywords = keywords or []

    mask_path = Path(tempfile.gettempdir()) / "vid_pipeline_f1_broll" / "rounded_mask.png"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    _build_rounded_mask_png(width, height, mask_path)

    if n_clips == 0:
        # No B-roll available -- still apply the frame mask to the plain clip.
        cmd = [
            "ffmpeg", "-y", "-i", str(clip_path), "-i", str(mask_path),
            "-filter_complex",
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p[base];"
            f"[base][1:v]overlay=0:0,format=yuv420p[outv]",
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}",
            str(out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"build_framed_clip (no B-roll) failed: {r.stderr[-800:]}")
        return None
        return

    burst_dur = _BURST_SUBCLIP_DUR * n_clips
    usable_start = duration * 0.25
    usable_end   = duration * 0.75
    # Center the burst in the usable range; shrink it if the clip is too
    # short to fit the full burst rather than overflowing past usable_end.
    burst_dur = min(burst_dur, max(_BURST_SUBCLIP_DUR, usable_end - usable_start))
    n_clips = max(1, round(burst_dur / _BURST_SUBCLIP_DUR))
    broll_paths = broll_paths[:n_clips]
    burst_start = usable_start + (usable_end - usable_start - burst_dur) / 2
    burst_end = burst_start + burst_dur

    # Single ambient SFX bed for the whole burst (one texture, not one per
    # subclip -- switching SFX every 1.1s would sound chaotic under speech).
    sfx_path = None
    if keywords:
        from modules.clips.clip_sfx import fetch_ambient_sfx
        sfx_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f1_sfx"
        sfx_path = fetch_ambient_sfx(keywords[0], sfx_dir / "burst_sfx.mp3")

    cmd = ["ffmpeg", "-y", "-i", str(clip_path)]
    for bp in broll_paths:
        cmd += ["-i", str(bp)]
    cmd += ["-i", str(mask_path)]
    mask_idx = len(broll_paths) + 1
    sfx_idx = None
    if sfx_path:
        cmd += ["-i", str(sfx_path)]
        sfx_idx = mask_idx + 1

    # Main clip is referenced twice (before the burst, after it) unless the
    # burst sits flush against one edge -- split() into exactly that many
    # explicit outputs, since ffmpeg's implicit multi-reference silently
    # bypasses the scale/crop chain for references after the first (same
    # fan-out bug hit earlier building the hook-transition speed ramp).
    has_head = burst_start > 0.0
    has_tail = burst_end < duration
    n_main_refs = max(1, int(has_head) + int(has_tail))

    filters = []
    main_split_labels = "".join(f"[main{i}]" for i in range(n_main_refs))
    filters.append(
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,format=yuv420p,split={n_main_refs}{main_split_labels}"
    )
    for i, bp in enumerate(broll_paths):
        filters.append(
            f"[{i+1}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,format=yuv420p,"
            f"trim=0:{_BURST_SUBCLIP_DUR:.3f},setpts=PTS-STARTPTS[broll{i}]"
        )

    segments = []
    main_ref_i = 0
    if has_head:
        filters.append(f"[main{main_ref_i}]trim=0:{burst_start:.3f},setpts=PTS-STARTPTS[seghead]")
        segments.append("seghead")
        main_ref_i += 1
    segments.extend(f"broll{i}" for i in range(len(broll_paths)))
    if has_tail:
        filters.append(f"[main{main_ref_i}]trim={burst_end:.3f}:{duration:.3f},setpts=PTS-STARTPTS[segtail]")
        segments.append("segtail")

    concat_inputs = "".join(f"[{s}]" for s in segments)
    filters.append(f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[composited]")
    filters.append(f"[composited][{mask_idx}:v]overlay=0:0,format=yuv420p[outv]")

    # Ambient SFX mixed in LOW under the original speech for the burst
    # window only -- delayed to the burst's start time, faded in/out.
    audio_map = "0:a?"
    if sfx_idx is not None and _has_source_audio(str(clip_path)):
        delay_ms = int(burst_start * 1000)
        fade_out_st = max(0.0, burst_dur - 0.3)
        filters.append(
            f"[{sfx_idx}:a]atrim=0:{burst_dur:.3f},asetpts=PTS-STARTPTS,"
            f"volume=0.16,afade=t=in:d=0.3,afade=t=out:st={fade_out_st:.3f}:d=0.3,"
            f"adelay={delay_ms}|{delay_ms}[sfx]"
        )
        filters.append("[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0[outa]")
        audio_map = "[outa]"

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-map", audio_map,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}",
        str(out_path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"build_framed_clip failed: {r.stderr[-1200:]}")
    logger.info(f"clip_broll: framed clip with {len(broll_paths)}-clip B-roll burst "
                f"({burst_start:.1f}s-{burst_end:.1f}s) -> {out_path}")
    return burst_start, burst_end
