"""
Format 11 assembler -- "Dialogue Mentor/Eleve" (two real TTS voices).

The reusable piece here is the two-voice turn-taking engine itself, not
just this one format: two distinct edge-tts voices (free, no API key)
alternate speaker turns, each synthesized separately and concatenated with
a short pause, driving a text card timed to that turn's real spoken
duration. This is what actually answers "the long-form voice-over sounds
too robotic" -- a single droning narrator vs two voices trading lines reads
as a conversation, not a lecture. Same engine will drive the later
character-debate formats (see BESOIN_TON_AVIS.md) -- just swap the two
voice IDs and the persona colors/labels.

No B-roll, no music -- the two voices carry the whole audio track.
"""

import asyncio
import logging
import re
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, OUTPUT_DIR
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text, _FADE_DUR,
    _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD,
    _TEXT_COLOR, _NAIVE_COLOR, _REAL_COLOR,
)
from modules.transitions import probe_duration

logger = logging.getLogger(__name__)

_TURN_GAP = 0.35   # silence between turns, seconds -- natural conversational beat

# Distinct voices per persona -- this pairing (not the specific format) is
# the reusable part. "student" / "mentor" here; future formats reuse this
# dict shape with different keys/voices/labels for their own two personas.
_VOICES = {
    "student": "en-US-AriaNeural",   # younger-register female voice
    "mentor":  "en-US-GuyNeural",    # deep, authoritative male voice
}
_LABELS = {"student": "Student", "mentor": "Mentor"}
_COLORS = {"student": _NAIVE_COLOR, "mentor": _REAL_COLOR}   # reuse the existing 2-tone brand language

_SPEAKER_PREFIX_RE = re.compile(r"^\s*(mentor|student)\s*[:\-]\s*", re.IGNORECASE)


def _strip_speaker_prefix(text: str) -> str:
    """
    Defensive cleanup: the prompt tells Claude never to prefix lines with a
    speaker label (position in the turns list already encodes who's
    speaking), but LLM output on this kind of instruction is inconsistent
    enough that a literal "Mentor: ..."/"Student: ..." occasionally still
    slips through -- and since every line goes straight to TTS, an
    unstripped label would get spoken out loud verbatim.
    """
    return _SPEAKER_PREFIX_RE.sub("", text).strip()


def assemble_format11(script, visuals_result, voice_result, timestamps_result, music_path):
    """Pure two-voice dialogue assembler -- ignores visuals_result/voice_result/timestamps_result
    (this format generates its OWN multi-voice audio internally, see build_dialogue_track)."""
    hook = _strip_speaker_prefix(script.get("hook", ""))
    body = [_strip_speaker_prefix(b) for b in script.get("body", []) if b.strip()]
    cta  = _strip_speaker_prefix(script.get("cta") or "")

    lines = [hook] + body[:4] + ([cta] if cta else [])
    speakers = ["student", "mentor", "student", "mentor", "student", "mentor"][:len(lines)]
    turns = list(zip(speakers, lines))

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f11"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    audio_path, turn_durations = build_dialogue_track(turns, tmp_dir, _VOICES)

    bg_png = tmp_dir / "bg.png"
    _build_bg(bg_png)

    overlay_specs: list[tuple[Path, float, float]] = []
    t = 0.0
    for i, ((speaker, text), dur) in enumerate(zip(turns, turn_durations)):
        png = tmp_dir / f"turn_{i}.png"
        _render_turn(speaker, text, png, _LABELS, _COLORS)
        overlay_specs.append((png, t, t + dur))
        t += dur + _TURN_GAP
    total = probe_duration(str(audio_path))

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format11_{slug}.mp4"

    cmd = _dialogue_cmd(bg_png, overlay_specs, audio_path, total, str(out))
    logger.info(f"Assembling format11 (dialogue mentor/eleve) | turns={len(turns)} | total={total:.1f}s")
    _run_ffmpeg(cmd)

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out


def build_dialogue_track(
    turns: list[tuple[str, str]], tmp_dir: Path, voices: dict[str, str],
    external_audio: dict[int, Path] | None = None,
) -> tuple[Path, list[float]]:
    """
    Synthesize each turn with its speaker's voice, concatenate with a short
    silence gap between turns. Returns (final_audio_path, [duration_per_turn]).
    Reusable as-is by any future two-voice format -- pass a different
    `turns` list and a `voices` dict mapping that format's own persona keys
    to edge-tts voice IDs to drive a different dialogue.

    external_audio, if given, maps a turn index to a pre-made audio file
    (e.g. a line the user generated themselves outside this pipeline) --
    that file is re-encoded to match the synthesized clips and used for
    that turn instead of calling TTS. Turns not in the mapping still fall
    back to `voices` as usual.
    """
    external_audio = external_audio or {}
    clip_paths = []
    durations = []
    for i, (speaker, text) in enumerate(turns):
        clip_path = tmp_dir / f"voice_{i}.mp3"
        if i in external_audio:
            _run_ffmpeg([
                "ffmpeg", "-y", "-i", str(external_audio[i]),
                "-ar", "24000", "-ac", "1", "-q:a", "9", str(clip_path),
            ])
        else:
            asyncio.run(_synthesize(text, str(clip_path), voices[speaker]))
        clip_paths.append(clip_path)
        durations.append(probe_duration(str(clip_path)))

    silence_path = tmp_dir / "silence.mp3"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono",
        "-t", str(_TURN_GAP), "-q:a", "9", str(silence_path),
    ])

    concat_list = tmp_dir / "concat.txt"
    lines = []
    for i, clip_path in enumerate(clip_paths):
        lines.append(f"file '{clip_path.name}'")
        if i < len(clip_paths) - 1:
            lines.append(f"file '{silence_path.name}'")
    concat_list.write_text("\n".join(lines), encoding="utf-8")

    final_audio = tmp_dir / "dialogue.mp3"
    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-c", "copy", "dialogue.mp3",
    ], cwd=str(tmp_dir))

    return final_audio, durations


_kokoro_pipelines: dict[str, "KPipeline"] = {}


def _get_kokoro_pipeline(lang_code: str):
    if lang_code not in _kokoro_pipelines:
        from kokoro import KPipeline
        _kokoro_pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _kokoro_pipelines[lang_code]


def _synthesize_kokoro(text: str, output_path: str, voice: str) -> None:
    """Kokoro (open-weight, free, runs locally, no cloning involved --
    original trained voices, not modeled on any specific real person)."""
    import numpy as np
    import soundfile as sf

    pipeline = _get_kokoro_pipeline(voice[0])
    chunks = [audio for _, _, audio in pipeline(text, voice=voice)]
    full = np.concatenate(chunks)
    wav_path = output_path.rsplit(".", 1)[0] + ".wav"
    sf.write(wav_path, full, 24000)
    _run_ffmpeg(["ffmpeg", "-y", "-i", wav_path, "-q:a", "9", output_path])


async def _synthesize(text: str, output_path: str, voice: str) -> None:
    """voice IDs prefixed "kokoro:" route to the local Kokoro model; anything
    else is an edge-tts voice ID, synthesized via Microsoft's free API."""
    if voice.startswith("kokoro:"):
        _synthesize_kokoro(text, output_path, voice.split(":", 1)[1])
        return
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def _build_bg(out_path: Path) -> None:
    """Same soft charcoal gradient as the rest of the motion-graphics family."""
    top, bottom = (58, 59, 63), (16, 16, 18)
    img  = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), bottom)
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(top[c] + (bottom[c] - top[c]) * f) for c in range(3))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    img.save(str(out_path), quality=92)


def _paste_circular_avatar(overlay: Image.Image, avatar_path: Path, center_x: int, bottom_y: int, size: int = 190) -> None:
    """Crop `avatar_path` to a circle and paste it centered on center_x, with
    its bottom edge at bottom_y (so it sits just above the label below it)."""
    try:
        photo = Image.open(avatar_path).convert("RGB")
    except Exception:
        return
    w, h = photo.size
    side = min(w, h)
    photo = photo.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)).resize((size, size))

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)

    ring = size + 10
    framed = Image.new("RGBA", (ring, ring), (0, 0, 0, 0))
    ImageDraw.Draw(framed).ellipse((0, 0, ring, ring), fill=(255, 255, 255, 230))
    framed.paste(photo, (5, 5), mask)

    x = center_x - ring // 2
    y = bottom_y - ring
    overlay.alpha_composite(framed, (x, y))


def render_pov_card(text: str, avatar_path: Path | None, out_path: Path) -> None:
    """Opening title card -- character photo (large, centered) + a "POV: ..."
    line, shown ALONE before the interrogation turns start. Visually
    distinct from the regular turn cards (no speaker label/name), narrated
    by the interrogator but framed as the video's own hook rather than
    part of the back-and-forth."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        pov_font = ImageFont.truetype(_FONT_SEMIBOLD, size=58)
    except OSError:
        pov_font = ImageFont.load_default()

    if avatar_path and Path(avatar_path).exists():
        _paste_circular_avatar(overlay, Path(avatar_path), VIDEO_WIDTH // 2, int(VIDEO_HEIGHT * 0.46), size=360)

    wrapped = textwrap.fill(text, width=22, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=pov_font, align="center", spacing=16)
    bx = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    by = int(VIDEO_HEIGHT * 0.58)
    _soft_text(overlay, (bx, by), wrapped, pov_font, _TEXT_COLOR, align="center", spacing=16, stroke_width=3)

    overlay.save(str(out_path), format="PNG")


def _render_turn(
    speaker: str, text: str, out_path: Path,
    labels: dict[str, str], colors: dict[str, tuple],
    avatars: dict[str, Path] | None = None, outline: bool = False,
) -> None:
    """One full-frame card: speaker label (colored per persona) + their line, centered.
    `labels`/`colors` are passed in (not read off the module-level dicts) so
    other formats -- e.g. format14, where the second persona's label is a
    dynamic character name -- can reuse this renderer as-is.

    avatars, if given, pastes a small circular image above that speaker's
    label (only visible during their own turn, since each turn is its own
    PNG). outline adds a hard text stroke -- needed over busy real footage
    (format14) instead of a flat gradient, where the default soft shadow
    alone isn't enough contrast."""
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    color = colors[speaker]

    try:
        label_font = ImageFont.truetype(_FONT_SEMIBOLD, size=40)
        body_font  = ImageFont.truetype(_FONT_LIGHT, size=52)
    except OSError:
        label_font = body_font = ImageFont.load_default()

    label = labels[speaker].upper()
    lbbox = draw.textbbox((0, 0), label, font=label_font)
    lx = (VIDEO_WIDTH - (lbbox[2] - lbbox[0])) // 2
    ly = int(VIDEO_HEIGHT * 0.36)

    avatar_path = (avatars or {}).get(speaker)
    if avatar_path and Path(avatar_path).exists():
        _paste_circular_avatar(overlay, Path(avatar_path), VIDEO_WIDTH // 2, ly - 20)

    _soft_text(overlay, (lx, ly), label, label_font, color, multiline=False,
               stroke_width=3 if outline else 0)

    wrapped = textwrap.fill(text, width=24, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=body_font, align="center", spacing=14)
    bx = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    by = ly + (lbbox[3] - lbbox[1]) + 50
    _soft_text(overlay, (bx, by), wrapped, body_font, _TEXT_COLOR, align="center", spacing=14,
               stroke_width=3 if outline else 0)

    overlay.save(str(out_path), format="PNG")


def _dialogue_cmd(
    bg_png: Path,
    overlay_specs: list[tuple[Path, float, float]],
    audio_path: Path,
    total_dur: float,
    output_path: str,
) -> list[str]:
    """Same looped-bg + per-turn cross-fade pattern as the rest of the family,
    but audio is the real dialogue track (mapped directly), not music."""
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(bg_png)]
    for png, _, _ in overlay_specs:
        cmd += ["-loop", "1", "-i", str(png)]
    audio_idx = 1 + len(overlay_specs)
    cmd += ["-i", str(audio_path)]

    filters = [f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p[base]"]
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
