"""
Format 16 assembler -- "Le Telephone Rouge" (a panicked character from one
fictional universe calls a totally different character from ANOTHER
universe for urgent financial/life advice). Reuses format14/15's building
blocks -- build_dialogue_track for the two-voice audio, voice_bank for
archetype voice matching, character_image for a real photo per character --
but the visual language is new: ultra-fast alternating full-screen cuts,
each character's own portrait duotone-toned in their own neon color (red
for the panicked caller, blue for the calm one -- same color language as
format9's red-pill/blue-pill), with an animated waveform pulsing to the
actual amplitude of whichever line is currently playing.

Frame-by-frame Pillow rendering piped into ffmpeg as rawvideo (same
technique as format8's _encode_frames) -- needed for the waveform's
per-frame motion, unlike the other two-voice formats' static PNG-overlay
stages.
"""

import logging
import math
import random
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, ASSETS_DIR
from modules.assembler import (
    _run_ffmpeg, _VIDEOS_DIR, _slugify, _soft_text,
    _FONT_REGULAR, _FONT_LIGHT, _FONT_SEMIBOLD, _TEXT_COLOR,
)
from modules.assembler.character_image import fetch_character_image
from modules.assembler.dialogue import build_dialogue_track
from modules.assembler.voice_bank import select_voice
from modules.transitions import probe_duration

_VOICE_INPUT_ROOT = ASSETS_DIR / "voices" / "format16"

logger = logging.getLogger(__name__)

_TURN_GAP = 0.3   # snappier than format11/14's 0.35 -- this format is a rapid-fire montage

# Neon duotone per speaker -- char_a (the panicked caller) = red, char_b (the calm advisor) =
# blue. Same red/blue color language as format9's red-pill/blue-pill so the channel's "urgent
# choice/tension" visual identity stays consistent across formats.
_NEON_RED       = (255, 70, 60)
_NEON_RED_DARK  = (26, 4, 6)
_NEON_BLUE      = (60, 150, 255)
_NEON_BLUE_DARK = (4, 10, 28)

_N_BARS       = 26
_BAR_MAX_H    = 220
_BAR_GAP      = 8
_BAR_BASE_Y_FRAC = 0.88
_FLASH_DUR    = 0.10   # quick white flash at each cut -- sells the "ultra-fast montage" cut energy

_SPEAKER_PREFIX_RE = re.compile(r"^\s*(char\s*_?a|char\s*_?b|caller|receiver)\s*[:\-]\s*", re.IGNORECASE)


def _strip_speaker_prefix(text: str) -> str:
    return _SPEAKER_PREFIX_RE.sub("", text or "").strip()


def build_turns(script: dict) -> list[tuple[str, str]]:
    """Speaker/text pairs in spoken order: hook (char_a's panicked opening call), body
    (8 lines alternating starting with char_b's first response), cta (char_b's closing
    one-liner) -- so the exchange opens AND closes on the calm character, bookending
    the caller's panic."""
    hook = _strip_speaker_prefix(script.get("hook", ""))
    body = [_strip_speaker_prefix(b) for b in script.get("body", []) if b.strip()]
    cta  = _strip_speaker_prefix(script.get("cta") or "")

    turns: list[tuple[str, str]] = [("char_a", hook)]
    for i, line in enumerate(body):
        turns.append(("char_b" if i % 2 == 0 else "char_a", line))
    if cta:
        turns.append(("char_b", cta))
    return turns


# ── Manual voice (same pattern as format14/format15 -- see interrogation.py /
# duel.py's own copies of this logic) -- lets the FlowShort panel's user drop
# real recordings for either character instead of the auto-picked TTS voice. ──

def voice_input_dir(subject: str) -> Path:
    return _VOICE_INPUT_ROOT / _slugify(subject)


_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def _resolve_combined_audio(folder: Path, tag: str, tmp_dir: Path) -> Path | None:
    """A character's audio may be spread across several separate uploads (see
    voice_split.find_numbered_recordings -- user feedback, 2026-07-27: "je
    prepare pas forcement tous les audios d'une traite") -- joins them into
    one continuous take before splitting."""
    from modules.assembler.voice_split import concat_recordings, find_numbered_recordings
    pieces = find_numbered_recordings(folder, tag)
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0]
    combined = tmp_dir / f"{tag}_combined_input.wav"
    concat_recordings(pieces, combined)
    logger.info(f"format16: joined {len(pieces)} recording piece(s) for character {tag!r}")
    return combined


def _find_external_audio(subject: str, turns: list[tuple[str, str]], tmp_dir: Path) -> dict[int, Path]:
    """Same two-tier resolution as duel.py's _find_external_audio: an exact
    turn_<i> file per line if present, else one combined per-character take
    (a_*/b_* prefix, possibly split across several uploads) auto-split
    against that character's own lines."""
    folder = voice_input_dir(subject)
    if not folder.is_dir():
        return {}

    found: dict[int, Path] = {}
    for speaker, tag in (("char_a", "a"), ("char_b", "b")):
        char_turns = [(i, text) for i, (spk, text) in enumerate(turns) if spk == speaker]
        for i, text in char_turns:
            matches = [f for f in folder.glob(f"turn_{i}.*") if f.suffix.lower() != ".txt"]
            if not matches:
                continue
            manifest = folder / f"turn_{i}.txt"
            if manifest.exists() and manifest.read_text(encoding="utf-8").strip() != text.strip():
                logger.warning(f"format16: turn_{i} audio in {folder} is stale for the current script -- falling back to TTS")
                continue
            found[i] = matches[0]

        missing = [(i, text) for i, text in char_turns if i not in found]
        if missing:
            combined = _resolve_combined_audio(folder, tag, tmp_dir)
            if combined:
                out_paths = [folder / f"turn_{i}.mp3" for i, _ in missing]
                from modules.assembler.voice_split import split_combined_audio
                success = split_combined_audio(combined, [text for _, text in missing], out_paths)
                if success:
                    for (i, _), out_path in zip(missing, out_paths):
                        found[i] = out_path

    for i, (speaker, text) in enumerate(turns):
        if i in found:
            (folder / f"turn_{i}.txt").write_text(text, encoding="utf-8")

    if found:
        logger.info(f"format16: using {len(found)} user-supplied audio file(s) from {folder}")
    return found


def _cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_h, new_w = h, int(h * src_ratio)
    else:
        new_w, new_h = w, int(w / src_ratio)
    img = img.resize((new_w, new_h))
    x0 = (new_w - w) // 2
    y0 = (new_h - h) // 2
    return img.crop((x0, y0, x0 + w, y0 + h))


def _duotone(img: Image.Image, dark: tuple, light: tuple) -> Image.Image:
    gray = ImageOps.grayscale(img)
    return ImageOps.colorize(gray, black=dark, white=light).convert("RGB")


def _flat_gradient(dark: tuple, light: tuple) -> Image.Image:
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), dark)
    draw = ImageDraw.Draw(img)
    for y in range(VIDEO_HEIGHT):
        f = y / (VIDEO_HEIGHT - 1)
        row = tuple(int(light[c] + (dark[c] - light[c]) * f) for c in range(3))
        draw.line([(0, y), (VIDEO_WIDTH, y)], fill=row)
    return img


def _build_turn_bg(avatar_path: Path | None, speaker: str) -> Image.Image:
    """Full-bleed neon duotone portrait -- falls back to a flat neon gradient
    (same color, no photo) if no real photo was found, never a blank frame."""
    dark, light = (_NEON_RED_DARK, _NEON_RED) if speaker == "char_a" else (_NEON_BLUE_DARK, _NEON_BLUE)
    if avatar_path and Path(avatar_path).exists():
        try:
            photo = Image.open(avatar_path).convert("RGB")
            photo = _cover_resize(photo, VIDEO_WIDTH, VIDEO_HEIGHT)
            toned = _duotone(photo, dark, light)
        except Exception:
            toned = _flat_gradient(dark, light)
    else:
        toned = _flat_gradient(dark, light)

    # Darken further for caption legibility -- duotone alone still reads too bright/busy
    # under white text in the brighter frames.
    arr = np.asarray(toned).astype(np.float32) * 0.55
    toned = Image.fromarray(arr.astype(np.uint8), mode="RGB")
    return toned.convert("RGBA")


def _render_caption(speaker_label: str, text: str, speaker: str) -> Image.Image:
    """Transparent overlay: speaker name + line, big and punchy for a fast-cut montage
    (larger type than the other two-voice formats -- each card is on screen for a much
    shorter beat here)."""
    import textwrap
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    color = _NEON_RED if speaker == "char_a" else _NEON_BLUE

    try:
        label_font = ImageFont.truetype(_FONT_SEMIBOLD, size=42)
        body_font  = ImageFont.truetype(_FONT_REGULAR, size=54)
    except OSError:
        label_font = body_font = ImageFont.load_default()

    label = speaker_label.upper()
    lbbox = draw.textbbox((0, 0), label, font=label_font)
    lx = (VIDEO_WIDTH - (lbbox[2] - lbbox[0])) // 2
    ly = int(VIDEO_HEIGHT * 0.30)
    _soft_text(overlay, (lx, ly), label, label_font, color, multiline=False, stroke_width=3)

    wrapped = textwrap.fill(text, width=22, break_long_words=False)
    bbox = draw.textbbox((0, 0), wrapped, font=body_font, align="center", spacing=14)
    bx = (VIDEO_WIDTH - (bbox[2] - bbox[0])) // 2
    by = ly + (lbbox[3] - lbbox[1]) + 44
    _soft_text(overlay, (bx, by), wrapped, body_font, _TEXT_COLOR, align="center", spacing=14, stroke_width=3)

    return overlay


_BAR_WEIGHTS = [0.35 + 0.65 * math.sin(math.pi * i / (_N_BARS - 1)) for i in range(_N_BARS)]
_bar_rng = random.Random(7)
_BAR_JITTER = [0.8 + 0.35 * _bar_rng.random() for _ in range(_N_BARS)]


def _draw_waveform(frame: Image.Image, value: float, t: float, color: tuple) -> None:
    """Vertical bar waveform, amplitude-reactive (see _amplitude_envelope) with a small
    idle sine wobble during silence gaps so it never looks frozen between turns."""
    draw = ImageDraw.Draw(frame)
    total_w = VIDEO_WIDTH * 0.72
    bar_w = (total_w - _BAR_GAP * (_N_BARS - 1)) / _N_BARS
    x0 = (VIDEO_WIDTH - total_w) / 2
    base_y = int(VIDEO_HEIGHT * _BAR_BASE_Y_FRAC)

    for i in range(_N_BARS):
        idle = 0.12 + 0.10 * math.sin(i * 0.4 + t * 3.2)
        h = max(6.0, _BAR_MAX_H * _BAR_WEIGHTS[i] * (idle + value * _BAR_JITTER[i]))
        x = x0 + i * (bar_w + _BAR_GAP)
        draw.rounded_rectangle(
            [x, base_y - h, x + bar_w, base_y], radius=bar_w / 2,
            fill=(color[0], color[1], color[2], 235),
        )


def _amplitude_envelope(audio_path: Path, n_frames: int, sr: int = 4000) -> np.ndarray:
    """RMS amplitude per video frame, normalized 0..1 against the track's own peak --
    drives _draw_waveform so the bars actually pulse with the currently-playing voice."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-f", "f32le", "-ar", str(sr), "-ac", "1", "-"],
        capture_output=True,
    )
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    window = max(1, sr // VIDEO_FPS)
    envelope = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        chunk = samples[i * window:(i + 1) * window]
        if len(chunk):
            envelope[i] = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))
    peak = float(envelope.max()) or 1.0
    return envelope / peak


def _encode_frames(frame_fn, n_frames: int, audio_path: Path, output_path: str) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "-r", str(VIDEO_FPS), "-i", "-",
        "-i", str(audio_path),
        "-filter_complex", "[0:v]format=yuv420p[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-r", str(VIDEO_FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]
    with tempfile.TemporaryFile() as log_f:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log_f, stderr=log_f)
        try:
            for i in range(n_frames):
                proc.stdin.write(frame_fn(i).tobytes())
            proc.stdin.close()
            proc.wait()
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()

        if proc.returncode != 0:
            log_f.seek(0)
            log = log_f.read().decode(errors="replace")
            tail = "\n".join(log.splitlines()[-20:])
            raise RuntimeError(f"FFmpeg failed (exit {proc.returncode}):\n{tail}")


def assemble_format16(script, visuals_result, voice_result, timestamps_result, music_path):
    """Red-phone crossover-call assembler -- ignores visuals_result/voice_result/
    timestamps_result (generates its own two-voice dialogue audio, like format11/14/15)."""
    turns = build_turns(script)

    char_a_name = (script.get("char_a_name") or script["subject"].split("/")[0]).strip()
    char_b_name = (script.get("char_b_name") or script["subject"].split("/")[-1]).strip()
    char_a_context = (script.get("char_a_context") or "").strip()
    char_b_context = (script.get("char_b_context") or "").strip()

    a_profile = script.get("char_a_voice") or {}
    b_profile = script.get("char_b_voice") or {}
    voices = {
        "char_a": select_voice(
            gender=a_profile.get("gender", "male"), accent=a_profile.get("accent", ""),
            age_register=a_profile.get("age_register", ""), tone=a_profile.get("tone") or [],
        ),
        "char_b": select_voice(
            gender=b_profile.get("gender", "male"), accent=b_profile.get("accent", ""),
            age_register=b_profile.get("age_register", ""), tone=b_profile.get("tone") or [],
        ),
    }
    labels = {"char_a": char_a_name.upper(), "char_b": char_b_name.upper()}

    tmp_dir = Path(tempfile.gettempdir()) / "vid_pipeline_f16"
    tmp_dir.mkdir(exist_ok=True)
    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)

    external_audio = _find_external_audio(script["subject"], turns, tmp_dir)
    audio_path, turn_durations = build_dialogue_track(turns, tmp_dir, voices, external_audio)
    total = probe_duration(str(audio_path))

    avatar_a = tmp_dir / "avatar_a.jpg"
    if not fetch_character_image(f"{char_a_name} {char_a_context}".strip(), avatar_a, name=char_a_name):
        avatar_a = None
    avatar_b = tmp_dir / "avatar_b.jpg"
    if not fetch_character_image(f"{char_b_name} {char_b_context}".strip(), avatar_b, name=char_b_name):
        avatar_b = None
    avatars = {"char_a": avatar_a, "char_b": avatar_b}

    # Precompute each speaker's neon bg + caption ONCE (not per-frame) -- only the
    # waveform bars actually need fresh per-frame rendering.
    bgs = {s: _build_turn_bg(avatars[s], s) for s in ("char_a", "char_b")}
    turn_captions = [_render_caption(labels[speaker], text, speaker) for speaker, text in turns]

    turn_bounds: list[tuple[float, float, int]] = []  # (start, end, turn_index)
    t = 0.0
    for i, dur in enumerate(turn_durations):
        turn_bounds.append((t, t + dur, i))
        t += dur + _TURN_GAP

    n_frames = int(round(total * VIDEO_FPS))
    envelope = _amplitude_envelope(audio_path, n_frames)

    def turn_at(t_now: float) -> int:
        for start, end, idx in turn_bounds:
            if t_now < end + _TURN_GAP / 2:
                return idx
        return turn_bounds[-1][2]

    def frame_fn(i: int) -> Image.Image:
        t_now = i / VIDEO_FPS
        idx = turn_at(t_now)
        speaker, _ = turns[idx]
        frame = bgs[speaker].copy()
        frame.alpha_composite(turn_captions[idx])
        _draw_waveform(frame, float(envelope[i]) if i < len(envelope) else 0.0, t_now,
                        _NEON_RED if speaker == "char_a" else _NEON_BLUE)

        turn_start = turn_bounds[idx][0]
        since_start = t_now - turn_start
        if 0 <= since_start < _FLASH_DUR:
            alpha = int(200 * (1 - since_start / _FLASH_DUR))
            flash = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (255, 255, 255, alpha))
            frame.alpha_composite(flash)
        return frame

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(script["subject"])
    out  = _VIDEOS_DIR / f"{ts}_format16_{slug}.mp4"

    logger.info(
        f"Assembling format16 (telephone rouge) | {char_a_name} <-> {char_b_name} "
        f"| turns={len(turns)} | total={total:.1f}s"
    )
    _encode_frames(frame_fn, n_frames, audio_path, str(out))

    size_mb = out.stat().st_size / (1024 * 1024)
    logger.info(f"Video ready: {out} ({size_mb:.1f} MB)")
    return out
