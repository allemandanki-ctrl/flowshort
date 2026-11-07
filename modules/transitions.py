"""
Transitions — hook-to-content cut styles + music beat detection.

All cut styles are built on FFmpeg's native `xfade` filter (present since
ffmpeg 4.3). This ffmpeg build ships 58 native xfade transitions (-1..57,
`ffmpeg -h filter=xfade`) -- many of them are direct C ports of the same
concepts found in the gl-transitions.com GLSL library (circlecrop, distance,
pixelize, diagonal wipes, squeeze, wind, cover/reveal, radial, slice), so a
wide, "developed" transition catalog is available without compiling a custom
GLSL-capable ffmpeg (fragile on Windows -- no libEGL/ANGLE by default). A
`gl-transitions/gl-transitions` clone was inspected for reference/parity
checking; its 125 shaders are richer, but porting the GLSL math shader-by-
shader is a much larger, slower-to-verify effort than what's below.

  hard_cut     : instant cut, no blend (plain concat, no xfade)
  whip_pan     : xfade=hblur       -- horizontal blur mimics whip-pan motion
  zoom_punch   : xfade=zoomin      -- punch-in zoom on the cut
  flash_white  : xfade=fadewhite
  match_cut    : best-effort hard cut (see note below)
  distance     : xfade=distance    -- pixel-distance morph (gl-transitions ColourDistance/CrossZoom feel)
  circle_crop  : xfade=circlecrop
  pixelize     : xfade=pixelize    -- mosaic dissolve (gl-transitions Mosaic/AdvancedMosaic feel)
  dissolve     : xfade=dissolve
  diagonal     : xfade=diagtl
  squeeze      : xfade=squeezeh
  wind         : xfade=hlwind
  radial       : xfade=radial
  slice        : xfade=hlslice
  cover        : xfade=coverleft
  reveal       : xfade=revealup
  prop_wipe    : a pre-rendered growing-object clip inserted between hook and
                 content (assets/transitions/), not an xfade blend at all --
                 see build_prop_wipe_video_filters. Built with HyperFrames
                 (heygen-com/hyperframes, GSAP+Chrome rendering, MIT), the
                 real prop-wipe technique from the reference examples (an
                 object grows to fully occlude the frame, hiding the cut
                 behind it) rather than an ffmpeg filter approximation.

match_cut honesty note: a *true* match cut (matching subject silhouette/motion
across the two shots) needs frame-level scene analysis this pipeline doesn't
have. Faking it with a generic blend would look worse than a clean cut, so
match_cut currently IS a hard_cut. If real match-cutting matters later, it
needs per-hook manual "match point" metadata (a coordinate/pose to align to),
which is a separate feature -- flagged here rather than silently faked.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TRANSITION_TYPES = (
    "hard_cut", "whip_pan", "zoom_punch", "flash_white", "match_cut",
    "distance", "circle_crop", "pixelize", "dissolve", "diagonal",
    "squeeze", "wind", "radial", "slice", "cover", "reveal", "prop_wipe",
)

# Pre-rendered via HyperFrames (see modules.transitions module docstring).
# v2: a directional light streak + soft feathered white/warm burst, blurred
# throughout (no hard geometric edge) -- v1 was a flat gold sphere with a
# crisp boundary, rejected in user testing as "horrible", not smooth.
# 2x speed via ffmpeg setpts (0.47s -> 0.30s) -- user feedback said the
# original length read as too long/slow.
PROP_WIPE_ASSET = Path(__file__).resolve().parent.parent / "assets" / "transitions" / "prop_wipe_light_burst.mp4"
PROP_WIPE_DURATION = 0.30

_XFADE_NAME = {
    "whip_pan":    "hblur",
    "zoom_punch":  "zoomin",
    "flash_white": "fadewhite",
    "distance":    "distance",
    "circle_crop": "circlecrop",
    "pixelize":    "pixelize",
    "dissolve":    "dissolve",
    "diagonal":    "diagtl",
    "squeeze":     "squeezeh",
    "wind":        "hlwind",
    "radial":      "radial",
    "slice":       "hlslice",
    "cover":       "coverleft",
    "reveal":      "revealup",
}

DEFAULT_TRANSITION_DURATION = 0.20   # seconds -- short enough to read as a "cut", not a fade


def is_hard(transition: str) -> bool:
    """hard_cut and match_cut (best-effort) both render as a plain concat, no xfade."""
    return transition in ("hard_cut", "match_cut")


def is_prop_wipe(transition: str) -> bool:
    """prop_wipe inserts a third pre-rendered clip -- neither plain concat nor xfade."""
    return transition == "prop_wipe"


def build_prop_wipe_video_filters(
    hook_label: str, wipe_input_idx: int, content_label: str, out_label: str,
) -> list[str]:
    """
    3-way concat: hook -> prop-wipe asset (scaled/normalized to 1080x1920) ->
    content. The wipe clip's growing object fully occludes the frame by
    roughly its midpoint, hiding the join behind it -- see PROP_WIPE_ASSET.
    """
    from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS
    return [
        f"[{wipe_input_idx}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p,fps={VIDEO_FPS}[wipev]",
        f"[{hook_label}][wipev][{content_label}]concat=n=3:v=1:a=0[{out_label}]",
    ]


def xfade_name(transition: str) -> str:
    if transition not in TRANSITION_TYPES:
        raise ValueError(f"Unknown transition '{transition}'. Valid: {TRANSITION_TYPES}")
    if is_hard(transition):
        raise ValueError(f"'{transition}' has no xfade name -- it's a hard cut, check is_hard() first")
    return _XFADE_NAME[transition]


def detect_first_beat(music_path: str, offset_search_window: float = 15.0) -> float:
    """
    Detect the first strong beat/onset in a music track (librosa), searched within
    the first `offset_search_window` seconds. Used to trim the track so playback
    -- started exactly at the hook->content cut -- lands its first beat right on
    the cut ("the drop hits on the transition").

    Returns the beat time in seconds. Falls back to 0.0 (start of file, i.e. no
    trim) on any failure -- a missed beat-sync is far less bad than a crash.
    """
    try:
        import librosa
        y, sr = librosa.load(music_path, sr=22050, mono=True,
                             duration=offset_search_window)
        onset_frames = librosa.onset.onset_detect(y=y, sr=sr, units="time",
                                                   backtrack=True)
        if len(onset_frames) > 0:
            beat_time = float(onset_frames[0])
            # Ignore a near-zero "onset" (that's just the track starting, not a beat).
            if beat_time < 0.05:
                onset_frames = onset_frames[onset_frames > 0.05] if len(onset_frames) > 1 else []
                beat_time = float(onset_frames[0]) if len(onset_frames) else 0.0
            logger.info(f"transitions: first beat detected at {beat_time:.3f}s in {Path(music_path).name}")
            return beat_time
    except Exception as e:
        logger.warning(f"transitions: beat detection failed ({e}); using track start (no trim)")
    return 0.0


def detect_tempo(music_path: str, max_duration: float = 30.0) -> float:
    """
    Detect a track's tempo (BPM) via librosa. Used to derive a fast-cut
    montage's clip-change interval from the music's ACTUAL rhythm (e.g. one
    cut every 2 beats) instead of an arbitrary fixed duration -- "images et
    vidéos qui défilent à la vitesse du bit" (cuts landing on the beat) needs
    the cut rate to track the track's real tempo, not a guessed constant.

    Returns BPM, or 0.0 on any failure -- caller falls back to a fixed
    default interval.
    """
    try:
        import librosa
        y, sr = librosa.load(music_path, sr=22050, mono=True, duration=max_duration)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(tempo) if hasattr(tempo, "__float__") else float(tempo[0])
        logger.info(f"transitions: tempo detected at {bpm:.1f} BPM in {Path(music_path).name}")
        return bpm
    except Exception as e:
        logger.warning(f"transitions: tempo detection failed ({e}); using fixed cut interval")
        return 0.0


def detect_beat_grid(
    music_path: str, start_offset: float = 0.0, max_duration: float = 25.0,
    beats_per_cut: int = 1,
) -> list[float]:
    """
    Real beat-grid cut durations -- not detect_tempo's fixed-interval
    approximation. Detects actual beat timestamps (librosa beat_track) inside
    the track starting at start_offset (the point music playback actually
    begins, e.g. after detect_first_beat's trim), groups every beats_per_cut
    beats into one cut boundary, and returns the resulting durations so
    _loop_clips_to_duration can cut clips exactly ON the beat rather than at
    an evenly-spaced guess.

    Returns [] on failure or too few detected beats -- caller falls back to
    detect_tempo's fixed interval.

    Uses the REAL per-beat intervals, in chronological order -- not a single
    constant period repeated. A track's tempo is never perfectly rigid (human
    performance, rubato, mastering), so locking every cut to one fixed
    average interval let cuts drift further and further from the actual
    beat the longer the montage ran: the first few cuts looked fine, but by
    the end of a ~16s beat-synced segment the accumulated drift made the
    visual cut land visibly off the music's real beat. Outlier intervals
    (e.g. a silent intro confusing the tracker) are clamped to a sane range
    around the median so a single bad gap still can't produce one oversized
    segment.

    Anchored at t=0, not at beat_track's own first detected timestamp: audio
    playback already starts exactly on a beat (start_offset comes from
    detect_first_beat's onset trim), but beat_track is a DIFFERENT algorithm
    (periodic beat tracking, not onset detection) and its first detected beat
    is often a bit later than true t=0 -- filling that lead-in with
    median-period-sized steps keeps the very first clip on-rhythm too.
    """
    try:
        import librosa
        y, sr = librosa.load(music_path, sr=22050, mono=True,
                             offset=start_offset, duration=max_duration)
        _, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        if len(beat_times) < beats_per_cut + 1:
            return []
        raw_intervals = [beat_times[i + 1] - beat_times[i] for i in range(len(beat_times) - 1)]
        raw_intervals = [d for d in raw_intervals if d > 0.05]
        if not raw_intervals:
            return []
        sorted_intervals = sorted(raw_intervals)
        median = sorted_intervals[len(sorted_intervals) // 2]   # outlier-resistant reference
        lo, hi = median * 0.5, median * 2.0
        clamped = [min(max(d, lo), hi) for d in raw_intervals]

        if beats_per_cut > 1:
            grouped = [
                sum(clamped[i:i + beats_per_cut])
                for i in range(0, len(clamped), beats_per_cut)
            ]
        else:
            grouped = clamped
        if not grouped:
            return []

        period = median * beats_per_cut
        n_lead = max(1, round(beat_times[0] / period)) if beat_times[0] > 0.05 else 0
        durations = ([period] * n_lead) + grouped

        # Cycle the real detected pattern (not a flat repeat of one number)
        # to cover max_duration if the analysis window ran out of beats.
        total = sum(durations)
        i = 0
        while total < max_duration:
            d = grouped[i % len(grouped)]
            durations.append(d)
            total += d
            i += 1

        if durations:
            logger.info(f"transitions: beat grid -> {len(durations)} cuts, "
                        f"avg {sum(durations)/len(durations):.2f}s, in {Path(music_path).name}")
        return durations
    except Exception as e:
        logger.warning(f"transitions: beat grid detection failed ({e}); using fixed interval")
        return []


def build_transition_filters(
    hook_label: str,
    content_label: str,
    out_label: str,
    transition: str,
    hook_duration: float,
    duration: float = DEFAULT_TRANSITION_DURATION,
) -> list[str]:
    """
    Build the FFmpeg filter_complex line(s) that join [hook_label] and
    [content_label] video streams into [out_label], per the chosen transition.

    hard_cut/match_cut: plain concat (instant cut, frame N -> frame N+1).
    Others: xfade with offset = hook_duration - duration (transition finishes
    exactly when the hook ends, so the cut still reads as landing at hook_duration).
    """
    if is_hard(transition):
        return [f"[{hook_label}][{content_label}]concat=n=2:v=1:a=0[{out_label}]"]

    name = xfade_name(transition)
    offset = max(0.0, hook_duration - duration)
    return [
        f"[{hook_label}][{content_label}]xfade=transition={name}:"
        f"duration={duration:.3f}:offset={offset:.3f}[{out_label}]"
    ]


def probe_duration(path: str) -> float:
    """ffprobe wrapper -- duration in seconds, 0.0 on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ── Transition "polish" layer ────────────────────────────────────────────
#
# Modular by design, independent of the cut STYLE above (hard_cut/whip_pan/
# zoom_punch/flash_white/match_cut): a speed ramp (video), a white flash, and
# a percussive impact hit (audio) can be layered onto ANY of those cut
# styles, or onto a future "prop_wipe" style once real transition-object
# assets are sourced. Neither function below cares which cut style is used.

DEFAULT_RAMP_DURATION = 0.5     # seconds of hook tail sped up before the cut
DEFAULT_RAMP_FACTOR   = 1.35    # playback speed multiplier during the ramp
DEFAULT_WHOOSH_DURATION = 0.4   # seconds of riser SFX layered under the cut


def build_speed_ramp_filters(
    in_label: str, out_label: str, hook_dur: float,
    ramp_duration: float = DEFAULT_RAMP_DURATION,
    ramp_factor: float = DEFAULT_RAMP_FACTOR,
    fps: int | None = None,
) -> tuple[list[str], float]:
    """
    Split [in_label] into a normal-speed head + a sped-up tail (the last
    ramp_duration seconds of the ORIGINAL hook), concatenated into
    [out_label]. Speeding up the tail makes the final instant before the cut
    play faster -- reads as punch/anticipation building into the transition.

    Returns (filter_lines, effective_duration). effective_duration is SHORTER
    than hook_dur (the sped-up tail plays back in less time) -- every
    downstream consumer of "hook duration" (POV overlay window, transition
    offset, audio cut point) MUST use effective_duration, not hook_dur, or
    sync breaks exactly like the two bugs already fixed in this system today.

    No-op (returns hook_dur unchanged, empty filter list) if the ramp doesn't
    fit within the hook -- a 1.5s hook can't lose 0.5s to a ramp and still be
    a coherent clip.

    fps, if given, re-locks the concat output to a clean constant-framerate
    timebase. Required for xfade transitions specifically: the setpts speed
    change on the tail leaves concat's output on a high-precision (1/1000000)
    timebase instead of the expected 1/fps, and xfade -- unlike concat itself
    -- refuses to join two inputs on mismatched timebases ("First input link
    main timebase do not match the corresponding second input link xfade
    timebase"). Reproduced with a real hook + zoom_punch transition.
    """
    if ramp_duration <= 0 or ramp_factor <= 1.0 or ramp_duration >= hook_dur * 0.8:
        return [], hook_dur

    head_dur = hook_dur - ramp_duration
    tail_effective = ramp_duration / ramp_factor
    effective_dur = head_dur + tail_effective

    fps_lock = f",fps={fps}" if fps else ""
    filters = [
        # Explicit split (not two implicit references to [in_label]) --
        # some HEVC sources trigger an ffmpeg fan-out bug where the second
        # implicit reference bypasses the scale/crop chain entirely, feeding
        # concat raw decoder dimensions instead of the normalized 1080x1920,
        # which fails with "Input link parameters do not match". Reproduced
        # and confirmed fixed with a real transitionalhooks.com HEVC clip.
        f"[{in_label}]split=2[rh_src1][rh_src2]",
        f"[rh_src1]trim=0:{head_dur:.3f},setpts=PTS-STARTPTS[rh_head]",
        f"[rh_src2]trim={head_dur:.3f}:{hook_dur:.3f},"
        f"setpts=(PTS-STARTPTS)/{ramp_factor:.3f}[rh_tail]",
        f"[rh_head][rh_tail]concat=n=2:v=1:a=0{fps_lock}[{out_label}]",
    ]
    return filters, effective_dur


DEFAULT_FLASH_DURATION = 0.10   # seconds of white-flash fade on each side of the cut
DEFAULT_IMPACT_DURATION = 0.18  # seconds of the impact-hit SFX


def build_flash_cut_filters(
    hook_label: str, content_label: str,
    hook_out_label: str, content_out_label: str,
    hook_duration: float,
    flash_duration: float = DEFAULT_FLASH_DURATION,
) -> list[str]:
    """
    Brief white-flash bridging the cut: the hook's last `flash_duration`
    seconds fade TO white, the content's first `flash_duration` seconds fade
    IN FROM white. Combined, a short flash covers the join -- independent of
    and layered on top of whichever cut STYLE is used (hard_cut/whip_pan/
    zoom_punch/flash_white/match_cut), so it works universally rather than
    being its own transition type.

    hook_duration positions the fade-out at the hook's TAIL, not its start --
    fade's `st` defaults to 0 (the clip's beginning), which would flash the
    hook white on its very first frame instead of right before the cut.

    Caller must feed `hook_out_label`/`content_out_label` (not the raw
    hook_label/content_label) into build_transition_filters.
    """
    fade_start = max(0.0, hook_duration - flash_duration)
    return [
        f"[{hook_label}]fade=t=out:st={fade_start:.3f}:d={flash_duration:.3f}:color=white[{hook_out_label}]",
        f"[{content_label}]fade=t=in:st=0.000:d={flash_duration:.3f}:color=white[{content_out_label}]",
    ]


DEFAULT_GLITCH_DURATION = 0.12   # seconds of RGB-shift pulse on each side of the cut
DEFAULT_GLITCH_SHIFT_PX = 14     # pixel offset of the red/blue channel split


def build_glitch_cut_filters(
    hook_label: str, content_label: str,
    hook_out_label: str, content_out_label: str,
    hook_duration: float,
    glitch_duration: float = DEFAULT_GLITCH_DURATION,
    shift_px: int = DEFAULT_GLITCH_SHIFT_PX,
) -> list[str]:
    """
    Brief RGB-channel-split (chromatic aberration / "signal glitch") pulse
    bridging the cut, via ffmpeg's native `rgbashift` filter -- no GLSL/shader
    compilation needed. Same visual family as gl-transitions' popular
    GlitchDisplace/GlitchMemories shaders, built from a filter this ffmpeg
    binary already ships. Alternative to build_flash_cut_filters -- same
    "polish layer" slot, pick one per render via `cut_style`.
    """
    g_start = max(0.0, hook_duration - glitch_duration)
    return [
        f"[{hook_label}]rgbashift=rh={shift_px}:bh=-{shift_px}:edge=smear:"
        f"enable='between(t,{g_start:.3f},{hook_duration:.3f})'[{hook_out_label}]",
        f"[{content_label}]rgbashift=rh={shift_px}:bh=-{shift_px}:edge=smear:"
        f"enable='between(t,0,{glitch_duration:.3f})'[{content_out_label}]",
    ]


def synthesize_impact_hit(duration: float, out_path: str) -> Path:
    """
    Short percussive impact SFX: a low sub-thump (decaying sine) plus a brief
    noise click for attack transient. Loud immediately, decays fast -- the
    opposite envelope shape from a riser/whoosh (quiet building to loud).

    Replaces an earlier synthesized "whoosh" riser that user testing flagged
    as reading like random meaningless noise. An impact hit lands ON the cut
    (attack = the cut itself) instead of building INTO it, which is the
    "audio-visual edit" character actually being asked for -- paired with
    build_flash_cut_filters so the hit and the flash land on the same frame.

    Self-contained (no external asset/download needed, always available).
    """
    d = max(0.08, duration)
    click_d = min(0.03, d * 0.3)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"aevalsrc='0.9*sin(2*PI*90*t)*exp(-18*t)':s=44100:d={d:.3f}",
        "-f", "lavfi", "-i", f"anoisesrc=color=white:duration={click_d:.3f}:sample_rate=44100:amplitude=0.7",
        "-filter_complex",
        f"[1:a]apad=whole_dur={d:.3f}[click];"
        f"[0:a][click]amix=inputs=2:duration=first:normalize=0[out]",
        "-map", "[out]", "-t", f"{d:.3f}",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"impact hit synthesis failed: {result.stderr[-400:]}")
    return Path(out_path)
