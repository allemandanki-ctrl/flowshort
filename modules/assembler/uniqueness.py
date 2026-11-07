"""
Anti-duplicate-content transform, shared across any renderer that needs to
defeat reused-content detection on TikTok/IG/FB/Snapchat -- extracted from
modules/assembler/white_card_reaction.py's `_apply_uniqueness_pass`
(2026-08-12 original, format18-only) so a second caller (viral_remix.py,
2026-08-13 -- remixing an externally-sourced viral clip while KEEPING its own
native framing, as opposed to format18's white-card recomposition) can reuse
the exact same transform without duplicating the ffmpeg filter graph.

format18 still owns its own box-detection call (detect_content_box, stays in
white_card_reaction.py -- moving it here would create a circular import,
since white_card_reaction.py needs to import FROM here) and passes its own
white-card content box in. A caller with no static background to protect
(the normal case: a full-bleed source clip, nothing baked around the real
footage) passes box=None and the whole probed frame is treated as content.
"""

import logging
import random
import subprocess
from pathlib import Path

from modules.assembler import _duration_ffprobe, _has_audio, _run_ffmpeg

logger = logging.getLogger(__name__)


def _probe_dims(path: str) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    w_str, h_str = r.stdout.strip().split(",")
    return int(w_str), int(h_str)


def _probe_fps(path: str) -> float:
    """r_frame_rate comes back as a fraction ("30000/1001", "25/1") -- not a
    plain float -- and zoompan needs an explicit fps= or it silently falls
    back to 25, which would drift duration/AV-sync on anything shot at 30/60
    (see project's segment-based AV sync history: frame-rate mismatches
    accumulate into real drift, never a purely cosmetic issue)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    raw = r.stdout.strip()
    try:
        if "/" in raw:
            num, den = raw.split("/")
            return float(num) / float(den) if float(den) else 30.0
        return float(raw)
    except (ValueError, ZeroDivisionError):
        return 30.0


_STRENGTH_RANGES = {
    # (crop_px_range, brightness_range, saturation_range, noise_range,
    #  zoom_target_range, crf_range, pitch_range, noise_bed_vol_range)
    "normal": ((4, 9), (0.008, 0.02), (1.01, 1.03), (4, 8), (1.02, 1.045), (18, 21), (1.004, 1.008), (0.018, 0.03)),
    # 2026-08-15, explicit user call: for a source that's an already-viral
    # clip the user WANTS to keep riding (not swap out) -- "changer assez
    # pour que ça soit pas répertorié comme reused content mais pas trop
    # pour que ça reste à peu près la même chose" -- the "normal" tier above
    # is deliberately invisible-to-a-human, which also means it's weak
    # against anything more robust than the crudest hash matching. "strong"
    # pushes every knob roughly 2-3x further while staying recognizably the
    # same footage (not a scramble) -- still short of visibly distorting the
    # clip, just no longer imperceptible.
    # noise capped at (7, 11), NOT pushed as far as the other knobs -- live
    # testing found the original (10, 18) draft inflated a 15s 1080x1920
    # clip to ~90MB (vs ~15MB at "normal") because per-pixel grain is far
    # more expensive to compress than crop/zoom/color are; way too heavy for
    # an actual upload. A moderate bump over "normal"'s (4, 8) still adds
    # real anti-fingerprint texture without that blowup.
    "strong": ((14, 26), (0.03, 0.06), (1.05, 1.12), (7, 11), (1.06, 1.10), (16, 20), (1.015, 1.03), (0.04, 0.07)),
}


def apply_uniqueness_pass(
    video_path: Path, box: tuple[int, int, int, int] | None = None, mirror: bool = True,
    strength: str = "normal",
) -> None:
    """
    Re-encodes the finished render in place through a set of transformations
    aimed squarely at reused-content/duplicate detection on TikTok/IG/FB --
    2026-08-12, explicit user ask after noticing white-card posts consistently
    get suppressed from For You/recommendation feeds (repost source footage
    reads as duplicate content). Platforms match reused content on (at least)
    3 independent layers -- file hash, PERCEPTUAL video hash, and AUDIO
    fingerprint -- and simply re-encoding only defeats the first. This defeats
    the other two as well, while staying invisible/inaudible to a human
    viewer (strength="normal" -- see `strength` below for the other tier):
      - the actual content (the full frame, or just `box` when given) is
        horizontally mirrored, given a slow barely-visible zoom drift
        (frame-by-frame crop window shrinking toward center, so no two frames
        share the same exact crop the way a static crop would), and gets a
        tiny brightness/saturation/grain shift -- breaks perceptual hash
        matching against the original repost
      - a near-silent low-level pink-noise bed is mixed under the clip's own
        (pitch-shifted) audio -- breaks waveform-correlation audio matching,
        on top of the pitch shift alone which only defeats spectral matching
      - the whole frame gets a small asymmetric crop+rescale (geometry, not
        color -- see below for why color must stay out of this part) and a
        randomized CRF -- changes the file hash even beyond the first encode
      - metadata is stripped

    Every numeric knob is RANDOMIZED per call, not a fixed constant -- a
    hardcoded "crop 6px, hue 3deg" would itself become a recognizable house
    signature across every video this channel posts, defeating the point.

    box: sub-rectangle (bx, by, bw, bh) to confine the color/grain/zoom step
    to -- needed when there's a static background around the real footage
    (format18's own white card: applying color/grain to the WHOLE canvas
    visibly tints the plain white background). None (the typical case for a
    full-bleed source clip with nothing baked around it, e.g. a downloaded
    viral clip) treats the whole probed frame as content -- no recomposite
    step needed in that case, the box IS the frame.

    mirror: set False to skip the horizontal-flip step -- 2026-08-13, real
    user catch: flipping ALSO flips any burned-in text already in the
    content (captions, subtitles, bold on-screen words, UI text), which
    reads backwards and unreadable. format18 never hit this in practice (its
    box is re-detected on ITS OWN render, which only ever has its own THEM:/
    ME:/POV: text -- rendered separately, outside this box, never mirrored)
    so its call site leaves this at the default True. A caller compositing
    onto an EXTERNAL source clip that might carry its own baked-in text
    (viral_remix.py) should check first and pass False when in doubt -- the
    other layers (zoom drift, grain, pitch-shift, crop, noise, CRF, metadata
    strip) still provide real anti-duplication defense without it.

    Two mistakes found live while testing this by hand before landing on this
    version, both worth keeping as comments so they don't get reintroduced:
      1. Color/grain/zoom must be confined to ONLY the real-footage region
         (never a coordinate reused from an earlier render if `box` moves
         between calls). Applying them to a WHOLE canvas that includes a
         static background visibly tints it -- invisible on a busy video
         frame, glaring on a large flat area. The outer whole-frame pass
         below is deliberately geometry-only (crop+rescale) for the same
         reason.
      2. rubberband for the pitch shift, not asetrate+aresample -- asetrate
         changes playback SPEED along with pitch, which drifts audio out of
         sync with video over the clip's duration. rubberband shifts pitch
         while holding tempo fixed, so duration matches to the frame.

    strength: "normal" (default) or "strong" -- see _STRENGTH_RANGES. "normal"
    is tuned to be imperceptible, which caps how much it can actually defeat
    -- fine for a channel's OWN renders (format18's white-card recomposition,
    where there's no recognizable third-party footage to begin with).
    "strong" is for when the source itself is a licensed/recognizable clip
    the user explicitly wants to keep RIDING (not swap out) -- pushes every
    knob further, still the same recognizable footage, just no longer
    invisible-to-the-eye. Neither tier is a guaranteed bypass of a
    rights-holder's own fingerprint matching (see viral_remix.py's module
    docstring) -- "strong" is a real increase in how much gets perturbed,
    not a promise.

    Fails soft: any error here leaves `video_path` exactly as the caller's
    render finished it -- a missed uniqueness pass is never worse than
    skipping it.
    """
    if strength not in _STRENGTH_RANGES:
        logger.warning(f"uniqueness pass: unknown strength {strength!r}, falling back to 'normal'")
        strength = "normal"
    crop_range, brightness_range, saturation_range, noise_range, zoom_range, crf_range, pitch_range, noise_bed_range = \
        _STRENGTH_RANGES[strength]
    try:
        w, h = _probe_dims(str(video_path))
        if w <= 0 or h <= 0:
            logger.warning("uniqueness pass skipped, could not probe video dimensions")
            return
        bx, by, bw, bh = box if box else (0, 0, w, h)
        if bw <= 0 or bh <= 0:
            logger.warning("uniqueness pass skipped, empty content box")
            return
        dur = _duration_ffprobe(str(video_path))
        has_audio = _has_audio(str(video_path))

        # Randomized per render (see docstring) -- ranges come from `strength`
        # (_STRENGTH_RANGES): "normal"'s were tuned by eye against real
        # renders to stay imperceptible; "strong"'s push further on purpose.
        left, top, right, bottom = (random.randint(*crop_range) for _ in range(4))
        outer_w, outer_h = w - left - right, h - top - bottom
        brightness = round(random.uniform(*brightness_range), 4)
        saturation = round(random.uniform(*saturation_range), 3)
        noise_strength = random.randint(*noise_range)
        zoom_target = round(random.uniform(*zoom_range), 4)
        fps = _probe_fps(str(video_path))
        total_frames = max(1, round(dur * fps))
        zoom_inc = (zoom_target - 1.0) / total_frames
        crf = random.randint(*crf_range)
        pitch = round(random.uniform(*pitch_range), 5)
        noise_bed_vol = round(random.uniform(*noise_bed_range), 4)

        tmp_out = video_path.with_name(f"{video_path.stem}_uniq.mp4")
        flip_step = "hflip," if mirror else ""

        if box is None:
            # Whole frame IS the content -- no recomposite over a protected
            # background needed, mirror/zoom/grain apply directly to [0:v].
            filters = (
                f"[0:v]{flip_step}zoompan=z='min(zoom+{zoom_inc:.8f},{zoom_target})':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={bw}x{bh}:fps={fps:.6f}[boxzoom];"
                f"[boxzoom]eq=brightness={brightness}:saturation={saturation}[boxeq];"
                f"[boxeq]noise=alls={noise_strength}:allf=t+u[recomposited];"
                f"[recomposited]crop={outer_w}:{outer_h}:{left}:{top},scale={w}:{h},setsar=1[vout]"
            )
        else:
            filters = (
                f"[0:v]crop={bw}:{bh}:{bx}:{by}[boxsrc];"
                f"[boxsrc]{flip_step}zoompan=z='min(zoom+{zoom_inc:.8f},{zoom_target})':d=1:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={bw}x{bh}:fps={fps:.6f}[boxzoom];"
                f"[boxzoom]eq=brightness={brightness}:saturation={saturation}[boxeq];"
                f"[boxeq]noise=alls={noise_strength}:allf=t+u[boxfinal];"
                f"[0:v][boxfinal]overlay={bx}:{by}[recomposited];"
                f"[recomposited]crop={outer_w}:{outer_h}:{left}:{top},scale={w}:{h},setsar=1[vout]"
            )

        cmd = ["ffmpeg", "-y", "-i", str(video_path)]
        if has_audio:
            cmd += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=44100:duration={dur:.3f}"]
            filters += (
                f";[0:a]rubberband=pitch={pitch}[apitch];"
                f"[1:a]volume={noise_bed_vol}[anoiseq];"
                f"[apitch][anoiseq]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
        cmd += ["-filter_complex", filters, "-map", "[vout]"]
        if has_audio:
            cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-map_metadata", "-1", "-movflags", "+faststart",
            str(tmp_out),
        ]
        _run_ffmpeg(cmd)
        tmp_out.replace(video_path)
        logger.info(
            f"uniqueness pass applied | strength={strength} | box={box or 'full frame'} | mirror={mirror} "
            f"| crop=L{left}T{top}R{right}B{bottom} "
            f"| brightness={brightness} saturation={saturation} noise={noise_strength} "
            f"| zoom->{zoom_target} | crf={crf}"
            + (f" | pitch={pitch} noise_bed={noise_bed_vol}" if has_audio else " | no audio")
        )
    except Exception as e:
        logger.warning(f"uniqueness pass failed ({e}), keeping render as-is")
