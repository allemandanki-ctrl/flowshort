"""
Background-music-presence heuristic for format1/format7/format18's source
clips (see modules/clips/music_removal.py for what happens once this says
yes). Restricted to those three formats -- see their call sites in
pipeline.py and app.py.

There's no Claude-vision-equivalent escape hatch for audio in this codebase
(modules/claude_cli.py's call_claude only takes image @path embeds, never
audio), so this has to be a numeric heuristic, and numeric heuristics on
music-vs-speech-vs-ambience are inherently fuzzy -- same honest framing as
modules/clips/watermark_scan.py's docstring for an analogous problem on the
video side. Ambiguous cases (a droning ambient pad, a quiet bed under loud
speech, a strong rhythmic SFX with no tonal content) can go either way. A
wrong "skip" just matches today's existing behavior (no regression -- music
stays); a wrong "run anyway" only costs time (Demucs, ~5-15 min per
tools/media/remove_music.py's own estimate) -- that asymmetry is why the
threshold below leans lenient (skip) rather than trigger-happy. Every one of
format1/format7/format18's inline fields carries a manual override checkbox
next to the auto-verdict for exactly this reason -- this heuristic is a
default, not a final word.
"""

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
_ANALYSIS_DURATION = 30.0   # seconds -- same capped-duration convention as
                            # modules/transitions.py's detect_tempo/detect_beat_grid
_SCORE_THRESHOLD = 0.55   # calibrated 2026-07-30 -- see the score-weighting comment
                          # below; 0.5 left one confirmed pure-speech sample sitting
                          # exactly on the boundary (score 0.50), 0.55 gives clean margin


def _extract_audio_wav(path: str) -> Path:
    """librosa can't reliably decode video containers -- extract to WAV
    first, the same first step tools/media/remove_music.py's own pipeline
    uses (ffmpeg -ar -ac -vn)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="music_presence_")
    import os
    os.close(fd)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-t", str(_ANALYSIS_DURATION),
         "-ar", "22050", "-ac", "1", "-vn", tmp_path],
        capture_output=True, check=True,
    )
    return Path(tmp_path)


def has_background_music(path: str) -> tuple[bool, float]:
    """
    Returns (decision, composite_score in [0,1]). Fails soft -> (False, 0.0)
    on any error (corrupt/silent input, missing dependency, extraction
    failure) -- skipping Demucs is always the safe failure mode here, never
    worth crashing a build over.

    Three signals, combined into one score:
      - harmonic energy ratio (librosa.effects.hpss) -- sustained tonal
        content (chords/bassline/pads) scores high; speech-over-ambience
        sits mostly percussive/broadband, not sustained-harmonic.
      - beat-grid regularity (librosa.beat.beat_track, same primitive
        modules/transitions.py's detect_tempo already uses) -- a real music
        bed tracks a fairly regular grid; speech/ambience produces an
        erratic one.
      - sustained-energy coverage -- fraction of 1s windows whose RMS stays
        above a low floor for the WHOLE clip. A continuous bed under speech
        rarely drops to near-silence between phrases; pure speech does.
    """
    wav_path = None
    try:
        import numpy as np
        import librosa

        needs_extraction = Path(path).suffix.lower() in _VIDEO_EXTS
        wav_path = _extract_audio_wav(path) if needs_extraction else None
        load_path = str(wav_path) if wav_path else path

        y, sr = librosa.load(load_path, sr=22050, mono=True, duration=_ANALYSIS_DURATION)
        if y.size == 0:
            return False, 0.0

        harmonic, percussive = librosa.effects.hpss(y)
        h_energy = float(np.mean(harmonic ** 2))
        p_energy = float(np.mean(percussive ** 2))
        harmonic_ratio = h_energy / (h_energy + p_energy + 1e-9)

        _, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)
        if len(beat_times) >= 3:
            intervals = np.diff(beat_times)
            beat_regularity = float(max(0.0, 1.0 - (np.std(intervals) / (np.mean(intervals) + 1e-9))))
        else:
            beat_regularity = 0.0

        hop = sr  # 1-second windows
        n_windows = max(1, len(y) // hop)
        window_rms = [
            float(np.sqrt(np.mean(y[i * hop:(i + 1) * hop] ** 2)))
            for i in range(n_windows)
        ]
        floor = 0.02   # empirical low-energy threshold, not full silence
        sustained_coverage = sum(1 for r in window_rms if r > floor) / n_windows

        # Weights calibrated 2026-07-30 against real material, not guessed:
        # harmonic_ratio alone cleanly separated 3 confirmed ground-truth
        # categories (pure TTS voice, no music: ~0.33-0.43 / real clip-edit
        # footage, likely pre-scored with music: ~0.55-0.67 / obvious music
        # tracks: ~0.80-0.95). beat_regularity and sustained_coverage, at
        # equal weight, were BOTH found to saturate near 1.0 even for
        # confirmed pure-speech TTS audio (regular speaking cadence reads as
        # a "regular beat" to librosa.beat.beat_track, and TTS narration
        # rarely has long silences) -- an equal-weight blend produced a false
        # positive on all 3 known-clean speech samples tested. Kept as minor
        # tie-breakers only, not load-bearing.
        score = 0.85 * harmonic_ratio + 0.10 * beat_regularity + 0.05 * sustained_coverage
        decision = score > _SCORE_THRESHOLD
        logger.info(
            f"music_presence: {Path(path).name} -> harmonic={harmonic_ratio:.2f} "
            f"beat_reg={beat_regularity:.2f} sustained={sustained_coverage:.2f} "
            f"score={score:.2f} -> {'MUSIC' if decision else 'no music'}"
        )
        return decision, round(score, 3)
    except Exception as e:
        logger.warning(f"music_presence: detection failed for {path} ({e}); assuming no music")
        return False, 0.0
    finally:
        if wav_path:
            wav_path.unlink(missing_ok=True)
