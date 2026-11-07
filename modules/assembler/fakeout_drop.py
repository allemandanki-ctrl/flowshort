"""
Format 13 -- "Fake-out Drop" assembler.

100% JSON-config-driven, no-voice, no Claude script generation: a sober
finance-advice setup (0-4s) hard-cuts to black (4-4.5s, music cut dead + SFX
stinger) then flips into a comedic "drop" (4.5-12s, different music + B-roll
burst + shake text + overlay captions), closing on a small deadpan punchline
(12-14s). Standalone entry point -- see format13_fakeout_drop.py at the repo
root -- bypasses pipeline.run()/generate_script() entirely since every line
of text and every asset path comes from the config JSON.

Timing (all overridable via config["timing"], defaults match the spec this
format was built against):
  0.0                     -- phase1 starts (sober bg + hook, then 2nd line + curve)
  phase1_duration (4.0)   -- cut to black, music dies dead (no fade), SFX stinger
  + cut_duration (0.5)    -- drop music starts, shake flashes, then B-roll burst
  drop_end (12.0)         -- punchline: small text, music ducked
  punchline_duration (2.0)-- total
"""

import logging
import random
import re
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from config import ASSETS_DIR, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from modules.assembler import _FONT_BOLD_PATH, _FONT_IMPACT_PATH, _run_ffmpeg, _slugify, _VIDEOS_DIR

logger = logging.getLogger(__name__)

# Vertical/horizontal safe zone -- keeps text clear of the TikTok/Shorts top
# bar and the bottom caption/share-button UI (same margin philosophy as
# format1's quote band, just expressed as a text-safe box instead of a
# fixed band height since this format's text isn't confined to one zone).
_SAFE_TOP    = 180
_SAFE_BOTTOM = 1640
_SIDE_MARGIN = 60
_SAFE_WIDTH  = VIDEO_WIDTH - 2 * _SIDE_MARGIN

_STROKE_WIDTH = 5
_FILL  = (255, 255, 255, 255)
_STROKE_FILL = (0, 0, 0, 255)

# Shake jitter (phase3 flash texts only) -- amplitude/frequency tuned by eye
# against a spike test, not exposed in config: it's a fixed visual signature
# of this format, not something a punchline swap needs to touch.
_SHAKE_AMP_X, _SHAKE_FREQ_X = 12, 10
_SHAKE_AMP_Y, _SHAKE_FREQ_Y = 8, 13

_PUNCH_GAP_PX = 24   # vertical gap between punchline line 1 and line 2

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif")
_MIN_BEAT_SLOT = 0.12   # floor so a very fast tempo never produces unreadable micro-cuts


def build_fakeout_drop(config: dict) -> Path:
    timing = config.get("timing", {})
    p1_dur     = float(timing.get("phase1_duration", 4.0))
    cut_dur    = float(timing.get("cut_duration", 0.5))
    drop_end   = float(timing.get("drop_end", 12.0))
    punch_dur  = float(timing.get("punchline_duration", 2.0))
    cut_start  = p1_dur
    drop_start = cut_start + cut_dur
    total_dur  = drop_end + punch_dur

    phase1 = config["phase1"]
    cut    = config.get("cut", {})
    drop   = config["drop"]
    punch  = config["punchline"]

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f13_fakeout"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    # ---- Phase 1 text PNGs ----
    hook_png = tmp_dir / "hook.png"
    _fit_text_png(phase1["hook_text"], _FONT_BOLD_PATH, hook_png, max_size=76)
    second_png = tmp_dir / "second.png"
    _fit_text_png(phase1["second_text"], _FONT_BOLD_PATH, second_png, max_size=76)

    # ---- Phase 3 shake-flash PNGs (config-driven text + timing) ----
    shake_pngs = []
    for i, entry in enumerate(drop.get("shake_texts", [])):
        png = tmp_dir / f"shake_{i:02d}.png"
        _fit_text_png(entry["text"], _FONT_IMPACT_PATH, png, max_size=180, min_size=56)
        shake_pngs.append((png, float(entry["start"]), float(entry["end"])))

    # ---- Phase 3 body overlay PNGs ----
    overlay_pngs = []
    for i, entry in enumerate(drop.get("overlays", [])):
        png = tmp_dir / f"overlay_{i:02d}.png"
        _fit_text_png(entry["text"], _FONT_IMPACT_PATH, png, max_size=88, min_size=40)
        overlay_pngs.append((png, float(entry["start"]), float(entry["end"])))

    # ---- Phase 4 punchline PNGs -- line 2 anchored BELOW line 1's own
    # rendered block, not independently centered (independent centering was
    # the bug caught by the pre-implementation spike test: both lines landed
    # on the same vertical midpoint and overlapped). ----
    punch1_png = tmp_dir / "punch1.png"
    p1_box = _fit_text_png(punch["text_1"], _FONT_BOLD_PATH, punch1_png,
                           max_size=44, min_size=28, vertical="above_center")
    punch2_png = tmp_dir / "punch2.png"
    _fit_text_png(punch["text_2"], _FONT_BOLD_PATH, punch2_png,
                  max_size=44, min_size=28, fixed_y=p1_box["bottom"] + _PUNCH_GAP_PX)

    black_png = tmp_dir / "black.png"
    Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0)).save(str(black_png))

    # ---- Cut-phase text (e.g. "SIIIIKKKKKKKE") -- shown+shaken over the
    # black screen for the cut's whole duration, timed to the SFX/drop hit.
    # Optional: some punchlines might want a silent black beat instead.
    cut_text = cut.get("text")
    cut_png = None
    if cut_text:
        cut_png = tmp_dir / "cut_text.png"
        _fit_text_png(cut_text, _FONT_IMPACT_PATH, cut_png, max_size=170, min_size=56)

    hook_bg = phase1.get("hook_background_path") or _pick_luxury_drive_bg()

    # ---- phase1's 2nd shot (2-4s, "DCA...") -- a DIFFERENT luxury asset than
    # the hook's, not a frozen/zoomed frame of the same clip (repeating the
    # same shot for both halves of phase1 reads as cheap/static). If the
    # config gives an explicit compound_curve_path (an actual chart image),
    # that opts into the slow-zoom chart treatment via ffmpeg zoompan instead
    # -- matplotlib-generated animated frames would be inconsistent with the
    # rest of this ffmpeg/Pillow pipeline (no other format uses matplotlib),
    # so "simple static chart asset + slow zoom" is the fallback the spec
    # itself offered for that case.
    explicit_curve = phase1.get("compound_curve_path")
    explicit_second = phase1.get("second_background_path")
    if explicit_curve:
        curve_path = explicit_curve
        curve_is_chart = True
    elif explicit_second:
        curve_path = explicit_second
        curve_is_chart = False
    else:
        curve_path = _pick_luxury_drive_bg(exclude=frozenset({hook_bg}))
        curve_is_chart = False

    calm_music = phase1.get("calm_music_path") or _pick_calm_music()
    sfx_path   = cut.get("sfx_path")
    drop_music = drop["drop_music_path"]
    drop_music_offset = float(drop.get("drop_music_offset", 0.0))
    drop_sync_to_cut  = bool(drop.get("drop_music_sync_to_cut", False))

    bg_clips = [_normalize_bg_asset(c, tmp_dir, i) for i, c in enumerate(drop["background_clips"])]
    drop_span = drop_end - drop_start
    bg_segments = _beat_synced_segments(bg_clips, drop_music, drop_span, offset=drop_music_offset)

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(config.get("output_name", "fakeout_drop"))
    out  = _VIDEOS_DIR / f"{ts}_format13_{slug}.mp4"
    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    cmd = _build_cmd(
        hook_bg=hook_bg, curve_path=curve_path, curve_is_chart=curve_is_chart,
        hook_png=hook_png, second_png=second_png, black_png=black_png,
        cut_png=cut_png,
        shake_pngs=shake_pngs, overlay_pngs=overlay_pngs,
        punch1_png=punch1_png, punch2_png=punch2_png,
        bg_segments=bg_segments,
        calm_music=calm_music, sfx_path=sfx_path, drop_music=drop_music,
        drop_music_offset=drop_music_offset, drop_sync_to_cut=drop_sync_to_cut,
        p1_dur=p1_dur, cut_start=cut_start, cut_dur=cut_dur,
        drop_start=drop_start, drop_end=drop_end, total_dur=total_dur,
        punch_start=drop_end,
        punch2_start=float(punch.get("text_2_start", drop_end + 1.0)),
        out_path=str(out),
    )
    logger.info(
        f"Assembling format13 (fake-out drop) | total={total_dur:.1f}s "
        f"| shake={len(shake_pngs)} overlays={len(overlay_pngs)} "
        f"bg_assets={len(bg_clips)} bg_cuts={len(bg_segments)}"
    )
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


_LOCAL_LUXURY_MOODS = ("money_wealth", "golden_hour", "night_drive", "silhouette_urban")


def _pick_luxury_drive_bg(exclude: frozenset[str] = frozenset()) -> str:
    """
    Sober/luxury background, sourced from the user's Drive library -- same
    footage pool format3a's luxury video background pulls from, scoped to
    folders tagged "luxury" specifically (format3a's stage_candidates() mixes
    several random themes together for a montage session, which is more than
    this format needs -- phase1 only ever shows one clip at a time). Falls
    back to a local broll_pinterest mood clip if Drive is unreachable or has
    no luxury folder, and only raises if that also comes up empty -- phase1
    always needs SOME background, unlike the punchline SFX/music which can
    silently go without.

    `exclude` (already-picked paths/ids) keeps the two phase1 shots (hook +
    "DCA..." second beat) from landing on the same clip twice.
    """
    try:
        from modules.drive_clips import load_folders, _list_folder, _download_file, _STAGING_DIR
        folders = [f for f in load_folders() if f.get("tag") == "luxury"]
        random.shuffle(folders)
        for folder in folders:
            try:
                entries = _list_folder(folder["id"])
            except Exception as e:
                logger.warning(f"format13: luxury folder {folder['id'][:12]} inaccessible ({e})")
                continue
            entries = [e for e in entries if e["name"].lower().endswith(
                (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp")) and e["id"] not in exclude]
            if not entries:
                continue
            random.shuffle(entries)
            _STAGING_DIR.mkdir(parents=True, exist_ok=True)
            for e in entries[:5]:
                dest = _STAGING_DIR / f"f13_{e['id'][:12]}_{re.sub(r'[^\w.-]', '_', e['name'])[:60]}"
                got = _download_file(e["id"], dest)
                if got:
                    logger.info(f"format13: luxury bg <- Drive '{e['name']}'")
                    return str(got)
    except Exception as e:
        logger.warning(f"format13: Drive luxury fetch failed ({e}); falling back to local broll")

    from config import ASSETS_DIR
    # Try moods in priority order (money_wealth first -- closest to "luxury
    # car/cash" thematically) rather than pooling every mood together, which
    # would let the 11 generic night/sunset clips drown out the 2 money ones.
    for mood in _LOCAL_LUXURY_MOODS:
        pool = list((ASSETS_DIR / "broll_pinterest" / mood).glob("*.mp4"))
        pool = [p for p in pool if str(p) not in exclude]
        if pool:
            chosen = str(random.choice(pool))
            logger.info(f"format13: luxury bg <- local fallback '{mood}/{Path(chosen).name}'")
            return chosen

    raise RuntimeError(
        "format13: no phase1 background available -- provide phase1.hook_background_path, "
        "configure a Drive 'luxury' folder, or add clips to assets/broll_pinterest/"
    )


def _pick_calm_music() -> str | None:
    """
    Phase1 "calme piano" bed. No Freesound involvement (that bank is
    dark_ambient/aggressive-trap only, explicitly blacklists calm/piano
    terms) -- uses the existing non-Freesound fetcher instead, mood
    "inspirational" (Mixkit "uplifting piano" + Jamendo "piano+emotional").
    Falls back to silence (returns None) if nothing is cached and the fetch
    fails -- SFX/music is a polish layer here, never worth blocking the video.
    """
    try:
        from modules.music.music_fetcher import download_mood_pack, list_available
        existing = list_available("inspirational")
        if not existing:
            download_mood_pack(["inspirational"])
            existing = list_available("inspirational")
        if existing:
            return random.choice(existing)
    except Exception as e:
        logger.warning(f"format13: calm music fetch failed ({e}); phase1 will be silent")
    return None


def _normalize_bg_asset(path: str, tmp_dir: Path, index: int) -> str:
    """
    Re-encodes exotic-but-common phone/export image formats (AVIF/HEIC/...)
    to plain PNG before they ever reach the "-loop 1" input path -- ffmpeg's
    image2 demuxer (which "-loop" targets) rejects some of these container
    types outright even though ffmpeg CAN decode a single frame from them via
    a normal "-i" input. Videos and already-plain image formats pass through
    untouched. Users will hand this format all sorts of random export
    formats over time, so normalizing up front is more robust than chasing
    per-format "-loop" compatibility one bug report at a time.
    """
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return path
    if ext not in _IMAGE_EXTS:
        return path   # video -- untouched

    out_png = tmp_dir / f"bgimg_{index:03d}.png"
    try:
        _run_ffmpeg(["ffmpeg", "-y", "-i", str(path), "-frames:v", "1", str(out_png)])
        return str(out_png)
    except Exception as e:
        logger.warning(f"format13: could not normalize {path} ({e}); using as-is")
        return path


def _beat_synced_segments(
    assets: list[str], music_path: str, span: float, offset: float = 0.0,
) -> list[tuple[str, float]]:
    """
    Snaps the B-roll burst's hard cuts to drop_music's tempo instead of an
    arbitrary equal split -- detects BPM via librosa, picks whichever
    subdivision (whole/half/third/quarter beat) lands closest to "one cut
    per asset" so all of them get shown, then locks every cut to that
    grid (cycling back through the asset list if the grid ends up with
    more slots than assets, dropping the tail if fewer). Falls back to a
    plain equal split across all assets if tempo detection fails for any
    reason (corrupt/tiny file, librosa missing) -- beat-sync is a polish
    layer, never worth failing the whole render over.
    """
    n = len(assets)
    ideal_slot = span / n

    try:
        import librosa
        y, sr = librosa.load(music_path, sr=None, mono=True, offset=offset)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        tempo = float(tempo[0] if hasattr(tempo, "__len__") else tempo)
        if tempo <= 0:
            raise ValueError(f"non-positive tempo {tempo}")
        beat_dur = 60.0 / tempo
        candidates = [beat_dur / k for k in (1, 2, 3, 4)]
        slot_dur = min(candidates, key=lambda d: abs(d - ideal_slot))
        slot_dur = max(slot_dur, _MIN_BEAT_SLOT)
        n_slots = max(1, round(span / slot_dur))
        slot_dur = span / n_slots   # rescale so cuts exactly fill the span, no gap/overrun
        logger.info(
            f"format13: beat-synced B-roll -- tempo={tempo:.1f}bpm, "
            f"slot={slot_dur:.3f}s, cuts={n_slots} (assets={n})"
        )
        return [(assets[i % n], slot_dur) for i in range(n_slots)]
    except Exception as e:
        logger.warning(f"format13: beat detection failed ({e}); falling back to equal split")
        return [(a, ideal_slot) for a in assets]


def _fit_text_png(
    text: str,
    font_path: str,
    out_path: Path,
    max_size: int,
    min_size: int = 32,
    fill=_FILL,
    vertical: str = "center",   # "center" | "above_center"
    fixed_y: int | None = None,
) -> dict:
    """
    Renders `text` centered (horizontally always; vertically per `vertical`,
    or at a caller-supplied `fixed_y` -- used to stack punchline line 2 under
    line 1's actual rendered block instead of re-centering it independently).
    Shrinks font size until the wrapped text fits the safe box, so a config
    swapped in with a much longer punchline never overflows/clips instead of
    silently breaking layout. Returns the rendered block's pixel bbox
    (in overlay coordinates) so callers can anchor further text off it.
    """
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    size = max_size
    while size >= min_size:
        font = ImageFont.truetype(font_path, size=size)
        probe = draw.textbbox((0, 0), "M", font=font)
        char_w = max(1, probe[2] - probe[0])
        wrap_width = max(3, int(_SAFE_WIDTH / (char_w * 0.62)))
        wrapped = textwrap.fill(text, width=wrap_width, break_long_words=False)

        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center",
                                        spacing=14, stroke_width=_STROKE_WIDTH)
        block_w = bbox[2] - bbox[0]
        block_h = bbox[3] - bbox[1]

        fits_width = block_w <= _SAFE_WIDTH
        fits_height = fixed_y is not None or block_h <= (_SAFE_BOTTOM - _SAFE_TOP)
        if fits_width and fits_height:
            break
        size -= 6
    else:
        size = min_size
        font = ImageFont.truetype(font_path, size=size)
        wrapped = textwrap.fill(text, width=max(3, int(_SAFE_WIDTH / (size * 0.55))),
                                 break_long_words=False)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center",
                                        spacing=14, stroke_width=_STROKE_WIDTH)
        block_w = bbox[2] - bbox[0]
        block_h = bbox[3] - bbox[1]

    x = (VIDEO_WIDTH - block_w) // 2 - bbox[0]
    if fixed_y is not None:
        y = fixed_y
    elif vertical == "above_center":
        y = _SAFE_TOP + (_SAFE_BOTTOM - _SAFE_TOP) // 3 - bbox[1]
    else:
        y = _SAFE_TOP + (_SAFE_BOTTOM - _SAFE_TOP - block_h) // 2 - bbox[1]

    draw.multiline_text((x, y), wrapped, font=font, fill=fill, align="center",
                         spacing=14, stroke_width=_STROKE_WIDTH, stroke_fill=_STROKE_FILL)
    overlay.save(str(out_path), format="PNG")
    return {"top": y + bbox[1], "bottom": y + bbox[1] + block_h, "height": block_h}


def _build_cmd(
    hook_bg, curve_path, curve_is_chart, hook_png, second_png, black_png, cut_png,
    shake_pngs, overlay_pngs, punch1_png, punch2_png,
    bg_segments, calm_music, sfx_path, drop_music,
    drop_music_offset, drop_sync_to_cut,
    p1_dur, cut_start, cut_dur, drop_start, drop_end, total_dur,
    punch_start, punch2_start, out_path,
) -> list[str]:
    cmd = ["ffmpeg", "-y"]

    # 0: phase1 hook bg (0-2s window, but we source the full 4s span from it
    #    then trim) -- image or clip, Drive/local fetch can hand back either.
    if str(hook_bg).lower().endswith(_IMAGE_EXTS):
        cmd += ["-loop", "1", "-i", str(hook_bg)]
    else:
        cmd += ["-stream_loop", "-1", "-i", str(hook_bg)]
    # 1: phase1's 2nd-beat asset (2-4s) -- a static chart image (zoompan) if
    #    curve_is_chart, otherwise a normal image/video luxury shot like input 0.
    if curve_is_chart:
        cmd += ["-loop", "1", "-i", str(curve_path)]
    elif str(curve_path).lower().endswith(_IMAGE_EXTS):
        cmd += ["-loop", "1", "-i", str(curve_path)]
    else:
        cmd += ["-stream_loop", "-1", "-i", str(curve_path)]
    # 2: hook text png
    cmd += ["-loop", "1", "-i", str(hook_png)]
    # 3: second text png
    cmd += ["-loop", "1", "-i", str(second_png)]
    # 4: black png (reused for cut + punchline)
    cmd += ["-loop", "1", "-i", str(black_png)]

    cut_text_idx = None
    if cut_png is not None:
        cut_text_idx = 5
        cmd += ["-loop", "1", "-i", str(cut_png)]

    bg_start_idx = 6 if cut_png is not None else 5
    for path, _ in bg_segments:
        if str(path).lower().endswith(_IMAGE_EXTS):
            cmd += ["-loop", "1", "-i", str(path)]
        else:
            cmd += ["-stream_loop", "-1", "-i", str(path)]

    shake_start_idx = bg_start_idx + len(bg_segments)
    for png, _, _ in shake_pngs:
        cmd += ["-loop", "1", "-i", str(png)]

    overlay_start_idx = shake_start_idx + len(shake_pngs)
    for png, _, _ in overlay_pngs:
        cmd += ["-loop", "1", "-i", str(png)]

    punch1_idx = overlay_start_idx + len(overlay_pngs)
    cmd += ["-loop", "1", "-i", str(punch1_png)]
    punch2_idx = punch1_idx + 1
    cmd += ["-loop", "1", "-i", str(punch2_png)]

    audio_idx = punch2_idx + 1
    has_calm = bool(calm_music)
    has_sfx  = bool(sfx_path)
    if has_calm:
        calm_idx = audio_idx; audio_idx += 1
        cmd += ["-i", str(calm_music)]
    if has_sfx:
        sfx_idx = audio_idx; audio_idx += 1
        cmd += ["-i", str(sfx_path)]
    drop_idx = audio_idx
    if drop_music_offset:
        cmd += ["-ss", str(drop_music_offset)]
    cmd += ["-i", str(drop_music)]

    filters = []

    # --- video: 4 background stages concatenated, then text overlaid on top ---
    filters.append(
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,trim=duration={p1_dur},"
        f"setpts=PTS-STARTPTS[p1bg]"
    )

    curve_dur = p1_dur - 2.0
    if curve_is_chart:
        curve_frames = max(1, int(curve_dur * VIDEO_FPS))
        filters.append(
            f"[1:v]scale=8000:-1,zoompan=z='min(zoom+0.0006,1.12)':d={curve_frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:"
            f"fps={VIDEO_FPS},setsar=1,trim=duration={curve_dur},setpts=PTS-STARTPTS[curvebg]"
        )
    else:
        filters.append(
            f"[1:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,trim=duration={curve_dur},"
            f"setpts=PTS-STARTPTS[curvebg]"
        )
    filters.append(f"[p1bg]trim=duration=2,setpts=PTS-STARTPTS[p1a]")
    # p1a covers 0-2s of the hook bg; curvebg covers 2-4s as its own separate
    # asset (not a continuation of hook_bg).
    filters.append(f"[4:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
                   f"trim=duration={cut_dur},setpts=PTS-STARTPTS[cutbg]")

    n_bg = len(bg_segments)
    drop_segs = []
    for i, (_, seg_dur) in enumerate(bg_segments):
        idx = bg_start_idx + i
        filters.append(
            f"[{idx}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,trim=duration={seg_dur:.3f},"
            f"setpts=PTS-STARTPTS[drop{i}]"
        )
        drop_segs.append(f"[drop{i}]")
    filters.append(f"{''.join(drop_segs)}concat=n={n_bg}:v=1:a=0[dropbg]")

    filters.append(f"[4:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
                   f"trim=duration={total_dur - punch_start},setpts=PTS-STARTPTS[punchbg]")

    filters.append("[p1a][curvebg][cutbg][dropbg][punchbg]concat=n=5:v=1:a=0[base]")
    prev = "base"

    # phase1 text
    filters.append(f"[2:v]format=rgba[hkt]")
    filters.append(f"[{prev}][hkt]overlay=0:0:enable='between(t,0,2)'[v01]")
    prev = "v01"
    filters.append(f"[3:v]format=rgba[snt]")
    filters.append(f"[{prev}][snt]overlay=0:0:enable='between(t,2,{p1_dur})'[v02]")
    prev = "v02"

    # cut-phase text (e.g. "SIIIIKKKKKKKE") -- same shake jitter as phase3 flashes
    if cut_text_idx is not None:
        cut_end = cut_start + cut_dur
        filters.append(f"[{cut_text_idx}:v]format=rgba[cutt]")
        filters.append(
            f"[{prev}][cutt]overlay="
            f"x='(W-w)/2+{_SHAKE_AMP_X}*sin(2*PI*{_SHAKE_FREQ_X}*t)':"
            f"y='(H-h)/2+{_SHAKE_AMP_Y}*cos(2*PI*{_SHAKE_FREQ_Y}*t)':"
            f"enable='between(t,{cut_start},{cut_end})'[vcut]"
        )
        prev = "vcut"

    # phase3 shake flashes -- jittered position, only while enabled
    for i, (_, start, end) in enumerate(shake_pngs):
        idx = shake_start_idx + i
        lbl_in = f"sh{i}"
        lbl_out = f"vsh{i}"
        filters.append(f"[{idx}:v]format=rgba[{lbl_in}]")
        filters.append(
            f"[{prev}][{lbl_in}]overlay="
            f"x='(W-w)/2+{_SHAKE_AMP_X}*sin(2*PI*{_SHAKE_FREQ_X}*t)':"
            f"y='(H-h)/2+{_SHAKE_AMP_Y}*cos(2*PI*{_SHAKE_FREQ_Y}*t)':"
            f"enable='between(t,{start},{end})'[{lbl_out}]"
        )
        prev = lbl_out

    # phase3 body overlays
    for i, (_, start, end) in enumerate(overlay_pngs):
        idx = overlay_start_idx + i
        lbl_in = f"ov{i}"
        lbl_out = f"vov{i}"
        filters.append(f"[{idx}:v]format=rgba[{lbl_in}]")
        filters.append(
            f"[{prev}][{lbl_in}]overlay=0:0:enable='between(t,{start},{end})'[{lbl_out}]"
        )
        prev = lbl_out

    # phase4 punchline
    filters.append(f"[{punch1_idx}:v]format=rgba[pc1]")
    filters.append(f"[{prev}][pc1]overlay=0:0:enable='between(t,{punch_start},{total_dur})'[vpc1]")
    prev = "vpc1"
    filters.append(f"[{punch2_idx}:v]format=rgba[pc2]")
    filters.append(f"[{prev}][pc2]overlay=0:0:enable='between(t,{punch2_start},{total_dur})'[vout]")

    audio_filters = []
    audio_labels = []
    if has_calm:
        audio_filters.append(f"[{calm_idx}:a]atrim=0:{cut_start},asetpts=PTS-STARTPTS[calm]")
        audio_labels.append("[calm]")
    if has_sfx:
        delay_ms = int(cut_start * 1000)
        audio_filters.append(f"[{sfx_idx}:a]adelay={delay_ms}|{delay_ms}[sfx]")
        audio_labels.append("[sfx]")
    drop_music_start = cut_start if drop_sync_to_cut else drop_start
    drop_delay_ms = int(drop_music_start * 1000)
    duck_gain = 0.316   # ~-10dB
    audio_filters.append(
        f"[{drop_idx}:a]adelay={drop_delay_ms}|{drop_delay_ms},"
        f"volume={duck_gain}:enable='between(t,{punch_start},{total_dur})'[drop]"
    )
    audio_labels.append("[drop]")

    if len(audio_labels) > 1:
        audio_filters.append(
            f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:normalize=0[aout]"
        )
        audio_out = "[aout]"
    else:
        audio_out = audio_labels[0]

    all_filters = filters + audio_filters
    cmd += ["-filter_complex", ";\n".join(all_filters)]
    cmd += ["-map", "[vout]", "-map", audio_out]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(total_dur),
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd
