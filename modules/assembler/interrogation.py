"""
Format 14 assembler -- "Interrogatoire" (fixed interrogator voice + an
archetype-matched voice for the fictional character being questioned).

Reuses format11's two-voice turn-taking engine (see dialogue.py) -- the
only new piece is resolving the second voice from an archetype description
(gender/age/tone/accent) via voice_bank instead of a hardcoded voice ID.
The interrogator's voice is fixed, like the mentor's in format11.

See BESOIN_TON_AVIS.md: this format never targets recognition of the real
actor/performer behind a character -- suspect_voice describes an archetype
for the ROLE (age register, tone, accent), not an impression of a specific
real person's timbre.
"""

import logging
import math
import random
import re
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, ASSETS_DIR, RESETWORTH_ASSETS_DIR
from modules.assembler import _run_ffmpeg, _VIDEOS_DIR, _slugify, _FADE_DUR, _NAIVE_COLOR, _REAL_COLOR
from modules.assembler.character_clip import fetch_character_clips
from modules.assembler.character_image import fetch_character_image, fetch_character_images
from modules.assembler.dialogue import build_dialogue_track, _build_bg, _dialogue_cmd, _render_turn, render_pov_card
from modules.assembler.voice_bank import select_voice
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

_TURN_GAP = 0.35
_INTERROGATOR_VOICE = "kokoro:bm_george"  # fixed brand voice: Kokoro (free, non-cloned), confirmed sounding good
_VOICE_INPUT_ROOT = ASSETS_DIR / "voices" / "format14"
_BG_SEG_DUR = 4.5  # per-clip/image display time in the cycling background, format3a-style
_BRANDING_DIR = RESETWORTH_ASSETS_DIR / "branding"
_INCOGNITO_ICON = _BRANDING_DIR / "incognito_icon.png"


def _ensure_incognito_icon() -> Path:
    """Fixed, procedurally-drawn generic "hidden identity" badge for the
    interrogator -- reused across every video, generated once and cached.
    There's no real person behind this role, so unlike the suspect there's
    nothing to source per-character here."""
    if _INCOGNITO_ICON.exists():
        return _INCOGNITO_ICON
    _BRANDING_DIR.mkdir(parents=True, exist_ok=True)

    size = 190
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=(30, 30, 34, 255))

    white = (225, 225, 225, 255)
    draw.pieslice((size * 0.10, size * 0.02, size * 0.90, size * 0.66), start=180, end=360, fill=white)
    draw.rectangle((size * 0.28, size * 0.06, size * 0.72, size * 0.30), fill=white)

    eye_y = size * 0.52
    draw.ellipse((size * 0.16, eye_y, size * 0.46, eye_y + size * 0.16), fill=white)
    draw.ellipse((size * 0.54, eye_y, size * 0.84, eye_y + size * 0.16), fill=white)
    draw.line((size * 0.46, eye_y + size * 0.08, size * 0.54, eye_y + size * 0.08), fill=white, width=6)

    img.save(str(_INCOGNITO_ICON), format="PNG")
    return _INCOGNITO_ICON


_SPEAKER_PREFIX_RE = re.compile(r"^\s*(interrogator|suspect)\s*[:\-]\s*", re.IGNORECASE)


def _strip_speaker_prefix(text: str) -> str:
    return _SPEAKER_PREFIX_RE.sub("", text).strip()


def voice_input_dir(subject: str) -> Path:
    """Where a user-generated suspect voice line goes for a given subject --
    drop turn_<i>.mp3/.wav here (i = that line's index in the full turn
    list -- see prepare_format14_voice.py for the exact numbers for a given
    script, since it shifts with pov_hook/body length) before running this
    format to use it instead of the auto-selected TTS voice for that line."""
    return _VOICE_INPUT_ROOT / _slugify(subject)


_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
_TURN_FILE_RE = re.compile(r"^turn_\d+\.")


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def _find_combined_audio(folder: Path) -> Path | None:
    """A single audio file dropped in the folder that ISN'T already a
    turn_<i> split -- e.g. the user recorded/generated all of the
    suspect's lines in one take instead of exporting them separately.

    Skips anything already marked .consumed FOR ITS CURRENT CONTENT -- a
    combined take is only ever auto-split once per (size, mtime). Without
    this, a script regeneration (fresh suspect lines every time) would
    re-run Whisper alignment against a recording of the OLD lines every
    single run: slow, and the alignment is nonsense since the words
    genuinely don't match. The fingerprint (not just filename) means
    dropping a NEW recording under the SAME filename -- e.g. re-exporting
    over "walter-white-....mp3" -- is still picked up as fresh."""
    candidates = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _AUDIO_EXT and not _TURN_FILE_RE.match(f.name)
    ]
    usable = []
    for f in candidates:
        marker = f.with_name(f.name + ".consumed")
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == _file_fingerprint(f):
            continue
        usable.append(f)

    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1:
        logger.warning(f"Multiple non-turn_N audio files in {folder}, expected exactly one combined take -- ignoring")
    return None


def _mark_combined_audio_consumed(combined: Path) -> None:
    combined.with_name(combined.name + ".consumed").write_text(_file_fingerprint(combined), encoding="utf-8")


def _find_external_suspect_audio(subject: str, turns: list[tuple[str, str]]) -> dict[int, Path]:
    """Every script regeneration writes fresh suspect lines, so a turn_<i>
    file left over from a PREVIOUS script for the same character would
    silently play the wrong words under the new on-screen text. Each usable
    file gets a turn_<i>.txt sidecar recording the exact line it was matched
    against -- a mismatch against the current script's text means the file
    is stale, so it's skipped (falls back to TTS) instead of playing
    mismatched audio."""
    folder = voice_input_dir(subject)
    if not folder.is_dir():
        return {}

    suspect_turns = [(i, text) for i, (speaker, text) in enumerate(turns) if speaker == "suspect"]

    found: dict[int, Path] = {}
    for i, text in suspect_turns:
        matches = [f for f in folder.glob(f"turn_{i}.*") if f.suffix.lower() != ".txt"]
        if not matches:
            continue
        manifest = folder / f"turn_{i}.txt"
        if manifest.exists() and manifest.read_text(encoding="utf-8").strip() != text.strip():
            logger.warning(
                f"turn_{i}{matches[0].suffix} in {folder} was recorded for a different script's "
                f"text -- ignoring stale file, falling back to auto TTS for this line"
            )
            continue
        found[i] = matches[0]

    missing = [(i, text) for i, text in suspect_turns if i not in found]
    if missing:
        combined = _find_combined_audio(folder)
        if combined:
            out_paths = [folder / f"turn_{i}.mp3" for i, _ in missing]
            from modules.assembler.voice_split import split_combined_audio
            success = split_combined_audio(combined, [text for _, text in missing], out_paths)
            _mark_combined_audio_consumed(combined)
            if success:
                for (i, _), out_path in zip(missing, out_paths):
                    found[i] = out_path

    for i, text in suspect_turns:
        if i in found:
            (folder / f"turn_{i}.txt").write_text(text, encoding="utf-8")

    if found:
        logger.info(f"Using {len(found)} user-supplied suspect audio file(s) from {folder}")
    return found


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def _render_bg_track(media_items: list[Path], seg_dur: float, out_path: Path) -> None:
    """Concatenate real clips/images of the character into one continuous,
    silent background track (format3a-style cycling) -- each item gets
    `seg_dur` seconds, videos trimmed, images held static. No audio is
    mapped from any of these, only from the dialogue track later."""
    cmd = ["ffmpeg", "-y"]
    kinds = []
    for m in media_items:
        if m.suffix.lower() in _IMAGE_EXT:
            cmd += ["-loop", "1", "-t", str(seg_dur), "-i", str(m)]
            kinds.append("image")
        else:
            cmd += ["-i", str(m)]
            kinds.append("video")

    filters = []
    for i, kind in enumerate(kinds):
        if kind == "video":
            filters.append(
                f"[{i}:v]trim=duration={seg_dur},setpts=PTS-STARTPTS,"
                f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,fps={VIDEO_FPS},format=yuv420p[s{i}]"
            )
        else:
            # real video clips already carry their own motion; a still image
            # held flat for seg_dur reads as dead air, so give it a mild
            # Ken Burns zoom instead -- capped low (1.12x) to stay subtle.
            filters.append(
                f"[{i}:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,"
                f"zoompan=z='min(zoom+0.0015,1.12)':d={int(seg_dur * VIDEO_FPS)}:"
                f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS},format=yuv420p[s{i}]"
            )
    concat_in = "".join(f"[s{i}]" for i in range(len(kinds)))
    filters.append(f"{concat_in}concat=n={len(kinds)}:v=1:a=0[vout]")

    cmd += [
        "-filter_complex", ";\n".join(filters), "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS), str(out_path),
    ]
    _run_ffmpeg(cmd)


def _dialogue_cmd_video_bg(
    video_bg: Path,
    overlay_specs: list[tuple[Path, float, float]],
    audio_path: Path,
    total_dur: float,
    output_path: str,
) -> list[str]:
    """Same per-turn overlay/fade pattern as _dialogue_cmd, but the base
    layer is real looped footage (dimmed/desaturated) instead of a static
    gradient -- text legibility comes from the turn cards' own stroked
    text (see _render_turn's outline=True), not a band over the footage."""
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video_bg)]
    for png, _, _ in overlay_specs:
        cmd += ["-loop", "1", "-i", str(png)]
    audio_idx = 1 + len(overlay_specs)
    cmd += ["-i", str(audio_path)]

    filters = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p,"
        f"eq=brightness=-0.12:saturation=0.65[base]"
    ]
    prev = "base"
    for j, (_, start, end) in enumerate(overlay_specs):
        png_idx = 1 + j
        lbl = "vout" if j == len(overlay_specs) - 1 else f"vo{j}"
        filters.append(
            f"[{png_idx}:v]format=rgba,fade=t=in:st={start:.3f}:d={_FADE_DUR}:alpha=1[fov{j}]"
        )
        filters.append(
            f"[{prev}][fov{j}]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[{lbl}]"
        )
        prev = lbl

    cmd += ["-filter_complex", ";\n".join(filters)]
    cmd += ["-map", "[vout]", "-map", f"{audio_idx}:a"]
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-t", str(total_dur),
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def build_turns(script: dict) -> list[tuple[str, str]]:
    """Speaker/text pairs in spoken order: optional pov_hook (interrogator,
    turn 0 -- an interrogator-narrated title card shown ALONE before the
    interrogation starts, gets special rendering via render_pov_card
    rather than the regular turn card), hook (interrogator), body
    (alternating starting with the suspect, whatever its length -- 4 lines
    in the original 20-30s cut, 8 in the 60-75s TikTok-monetization cut,
    never hardcode the count), cta (suspect).

    The one place this pattern lives -- shared by assemble_format14,
    prepare_format14_voice.py, and the FlowShort app's format14 panel."""
    pov_hook = _strip_speaker_prefix(script.get("pov_hook") or "")
    hook = _strip_speaker_prefix(script.get("hook", ""))
    body = [_strip_speaker_prefix(b) for b in script.get("body", []) if b.strip()]
    cta  = _strip_speaker_prefix(script.get("cta") or "")

    lines = ([pov_hook] if pov_hook else []) + [hook] + body + ([cta] if cta else [])
    speakers = (["interrogator"] if pov_hook else []) + ["interrogator"]
    speakers += ["suspect" if i % 2 == 0 else "interrogator" for i in range(len(body))]
    speakers += (["suspect"] if cta else [])
    return list(zip(speakers, lines))


def assemble_format14(script, visuals_result, voice_result, timestamps_result, music_path):
    """Interrogation-room two-voice assembler -- ignores visuals_result/voice_result/
    timestamps_result (generates its own dialogue audio, like format11)."""
    turns = build_turns(script)
    pov_turn_index = 0 if script.get("pov_hook") else None

    suspect_profile = script.get("suspect_voice") or {}
    suspect_voice_id = select_voice(
        gender=suspect_profile.get("gender", "male"),
        accent=suspect_profile.get("accent", ""),
        age_register=suspect_profile.get("age_register", ""),
        tone=suspect_profile.get("tone") or [],
    )
    voices = {"interrogator": _INTERROGATOR_VOICE, "suspect": suspect_voice_id}

    suspect_name = (script.get("suspect_name") or script["subject"].split("/")[0]).strip().upper()
    labels = {"interrogator": "INTERROGATOR", "suspect": suspect_name}
    colors = {"interrogator": _REAL_COLOR, "suspect": _NAIVE_COLOR}

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f14"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    external_audio = _find_external_suspect_audio(script["subject"], turns)
    audio_path, turn_durations = build_dialogue_track(turns, tmp_dir, voices, external_audio)
    estimated_total = sum(turn_durations) + _TURN_GAP * (len(turns) - 1)

    query = script["subject"].replace("/", " ")

    avatar_path = tmp_dir / "avatar.jpg"
    if not fetch_character_image(query, avatar_path, name=suspect_name):
        avatar_path = None
    avatars = {"interrogator": _ensure_incognito_icon(), "suspect": avatar_path}

    n_segments = max(1, math.ceil(estimated_total / _BG_SEG_DUR) + 1)  # +1 safety segment
    clips = fetch_character_clips(
        query, tmp_dir, count=min(7, n_segments), segment_duration=_BG_SEG_DUR, max_candidates=12, name=suspect_name
    )
    if clips:
        # Real clips only when we have any -- repeating a couple of real
        # scenes (cycling covers that) reads far better than padding with a
        # generic/mismatched image just to hit segment count.
        media_pool = clips
        random.shuffle(media_pool)
    else:
        media_pool = fetch_character_images(query, tmp_dir, count=n_segments, name=suspect_name)

    video_bg = None
    if media_pool:
        cycled = [media_pool[i % len(media_pool)] for i in range(n_segments)]
        video_bg = tmp_dir / "bg_track.mp4"
        _render_bg_track(cycled, _BG_SEG_DUR, video_bg)
        logger.info(f"Built cycling background from {len(media_pool)} real clip(s)/image(s) for {query!r}")
    else:
        logger.info(f"No real clips/images found for {query!r} -- falling back to case-file photo/gradient")

    if video_bg is None:
        bg_png = tmp_dir / "bg.png"
        _build_bg(bg_png)

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0
    for i, ((speaker, text), dur) in enumerate(zip(turns, turn_durations)):
        png = tmp_dir / f"turn_{i}.png"
        if i == pov_turn_index:
            render_pov_card(text, avatars.get("suspect"), png)
        else:
            _render_turn(speaker, text, png, labels, colors, avatars=avatars, outline=True)
        overlay_specs.append((png, t, t + dur))
        t += dur + _TURN_GAP
    total = probe_duration(str(audio_path))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format14_{slug}.mp4"

    if video_bg is not None:
        cmd = _dialogue_cmd_video_bg(video_bg, overlay_specs, audio_path, total, str(out))
    else:
        cmd = _dialogue_cmd(bg_png, overlay_specs, audio_path, total, str(out))
    logger.info(
        f"Assembling format14 (interrogatoire) | suspect={suspect_name} "
        f"voice={suspect_voice_id} | bg={'cycling media' if video_bg else 'photo/gradient'} "
        f"| turns={len(turns)} | total={total:.1f}s"
    )
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out
