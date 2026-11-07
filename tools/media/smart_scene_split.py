"""
smart_scene_split.py  <video.mp4> [output_dir] [prefix]

Découpage intelligent par 4 signaux corroborés:
  1. Changement de locuteur   (pyannote.audio)
  2. Changement visuel fort   (AdaptiveDetector)
  3. Changement de contexte   (Whisper - similarité sémantique)
  4. Changement de source     (statistiques frames)

Coupe seulement quand les 4 signaux s'accordent dans une fenêtre de WINDOW secondes.

Usage:
    python tools/media/smart_scene_split.py video.mp4
    python tools/media/smart_scene_split.py video.mp4 output_dir/ prefix
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# Force UTF-8 sur Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Config ────────────────────────────────────────────────────────────────────
HF_TOKEN            = os.environ.get("HF_TOKEN", "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")
WINDOW              = 2.0    # secondes: fenêtre de corrélation entre signaux
MIN_CLIP_DURATION   = 8.0    # secondes: durée minimale d'un clip
MAX_CLIP_DURATION   = 45.0   # secondes: hard cap — coupe forcée au-delà
SIGNALS_REQUIRED    = 4      # nb de signaux requis (coupe propre)
SIGNALS_FORCED      = 2      # nb de signaux pour coupe forcée (hard cap)
ADAPTIVE_THRESHOLD  = 3.0    # sensibilité AdaptiveDetector
MIN_SCENE_FRAMES    = 90     # ~3s @ 30fps
WHISPER_MODEL       = "base" # tiny/base/small/medium
QUALITY_THRESHOLD   = 0.25   # seuil changement qualité (0-1)
SEMANTIC_THRESHOLD  = 0.35   # seuil dissimilarité sémantique (0-1)
VIDEO_CODEC         = "libx264"
AUDIO_CODEC         = "aac"
CRF                 = "18"
PRESET              = "fast"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def _video_fps(path: Path) -> float:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    return fps


def _video_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


# ── Signal 1: Speaker change (pyannote) ───────────────────────────────────────

def signal_speaker(wav: Path) -> list[float]:
    """Retourne timestamps où le locuteur change."""
    print("  [Signal 1] Diarisation pyannote...", flush=True)
    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=HF_TOKEN
    )

    # Charger WAV via soundfile pour éviter torchcodec
    audio_data, sample_rate = sf.read(str(wav), dtype="float32", always_2d=True)
    waveform = torch.from_numpy(audio_data.T)  # (channels, time)
    audio_dict = {"waveform": waveform, "sample_rate": sample_rate}
    output = pipeline(audio_dict)
    # pyannote 4.x: DiarizeOutput.speaker_diarization = Annotation
    diarization = output.speaker_diarization

    events: list[float] = []
    prev_speaker = None
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if prev_speaker is not None and speaker != prev_speaker:
            events.append(turn.start)
        prev_speaker = speaker

    print(f"    -> {len(events)} changements de locuteur", flush=True)
    return events


# ── Signal 2: Visual change (AdaptiveDetector) ────────────────────────────────

def signal_visual(video: Path) -> list[float]:
    """Retourne timestamps de changements visuels forts."""
    print("  [Signal 2] Détection visuelle...", flush=True)
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import AdaptiveDetector

    vid = open_video(str(video))
    sm  = SceneManager()
    sm.add_detector(AdaptiveDetector(
        adaptive_threshold=ADAPTIVE_THRESHOLD,
        min_scene_len=MIN_SCENE_FRAMES,
    ))
    sm.detect_scenes(vid, show_progress=False)
    events = [s.seconds for s, _ in sm.get_scene_list()[1:]]  # skip start
    print(f"    -> {len(events)} changements visuels", flush=True)
    return events


# ── Signal 3: Context change (Whisper) ────────────────────────────────────────

def _word_overlap(text_a: str, text_b: str) -> float:
    """Retourne dissimilarité (0=identique, 1=complètement différent)."""
    stop = {"the","a","an","is","it","in","on","at","to","and","or","but",
            "i","you","he","she","we","they","of","for","with","that","this",
            "le","la","les","un","une","des","de","du","et","en","à","au",
            "je","tu","il","elle","on","nous","vous","ils","elles","que","qui"}
    def words(t):
        return {w.lower() for w in t.split() if w.lower() not in stop and len(w) > 2}
    a, b = words(text_a), words(text_b)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    return 1.0 - len(a & b) / len(a | b)


def signal_context(wav: Path) -> list[float]:
    """Retourne timestamps de changements de sujet/contexte."""
    print("  [Signal 3] Transcription Whisper...", flush=True)
    import whisper
    model = whisper.load_model(WHISPER_MODEL)
    result = model.transcribe(str(wav), word_timestamps=False, verbose=False)
    segments = result.get("segments", [])
    print(f"    -> {len(segments)} segments transcrits", flush=True)

    events: list[float] = []
    for i in range(1, len(segments)):
        prev_text = segments[i - 1].get("text", "")
        curr_text = segments[i].get("text", "")
        dissim = _word_overlap(prev_text, curr_text)
        if dissim >= SEMANTIC_THRESHOLD:
            events.append(segments[i]["start"])

    print(f"    -> {len(events)} changements de contexte", flush=True)
    return events


# ── Signal 4: Source quality change ──────────────────────────────────────────

def _frame_stats(video: Path, t: float) -> Optional[np.ndarray]:
    """Extrait stats couleur/qualité d'une frame à t secondes."""
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = np.array([
        cv2.calcHist([hsv], [c], None, [32], [0, 256]).flatten()
        for c in range(3)
    ]).flatten()
    hist /= (hist.sum() + 1e-9)
    # Ajoute sharpness (variance Laplacien)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
    return np.append(hist, sharp / 10000.0)


def _stats_diff(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> float:
    if a is None or b is None:
        return 1.0
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 1.0
    cos = np.dot(a, b) / (na * nb)
    return float(1.0 - cos)


def signal_quality(video: Path, candidate_times: list[float]) -> list[float]:
    """Parmi les temps candidats, retourne ceux avec un vrai changement de source."""
    print("  [Signal 4] Analyse qualité/source frames...", flush=True)
    events: list[float] = []
    for t in candidate_times:
        before = _frame_stats(video, max(0.0, t - 0.5))
        after  = _frame_stats(video, t + 0.5)
        diff   = _stats_diff(before, after)
        if diff >= QUALITY_THRESHOLD:
            events.append(t)
    print(f"    -> {len(events)} changements de source", flush=True)
    return events


# ── Corrélation des signaux ───────────────────────────────────────────────────

def correlate_signals(
    sig1: list[float],
    sig2: list[float],
    sig3: list[float],
    sig4: list[float],
    duration: float,
) -> list[float]:
    """
    Deux niveaux de coupe :
    - Coupe propre  : >= SIGNALS_REQUIRED (4/4) signaux dans ±WINDOW
    - Coupe forcée  : >= SIGNALS_FORCED   (2/4) signaux si segment > MAX_CLIP_DURATION
    """
    all_sigs = [sig1, sig2, sig3, sig4]
    all_events = sorted(set(sig1 + sig2 + sig3 + sig4))

    def score_at(t: float) -> int:
        return sum(1 for sig in all_sigs if any(abs(e - t) <= WINDOW for e in sig))

    # Coupes propres (4/4)
    cuts: list[float] = []
    for t in all_events:
        if score_at(t) >= SIGNALS_REQUIRED:
            if not cuts or t - cuts[-1] > MIN_CLIP_DURATION:
                cuts.append(t)

    print(f"  {len(cuts)} coupes propres (4/4 signaux)", flush=True)

    # Coupes forcées : hard cap sur les segments trop longs
    forced = 0
    boundaries = [0.0] + cuts + [duration]
    extra_cuts: list[float] = []

    for i in range(len(boundaries) - 1):
        seg_start, seg_end = boundaries[i], boundaries[i + 1]
        seg_dur = seg_end - seg_start

        while seg_dur > MAX_CLIP_DURATION:
            # Cherche meilleur point 2/4 dans la fenêtre [seg_start+MIN, seg_start+MAX]
            search_start = seg_start + MIN_CLIP_DURATION
            search_end   = seg_start + MAX_CLIP_DURATION
            candidates = [t for t in all_events if search_start < t < search_end]

            if not candidates:
                # Aucun signal dans la fenêtre → coupe au milieu
                mid = seg_start + MAX_CLIP_DURATION / 2
                extra_cuts.append(mid)
                forced += 1
                seg_start = mid
            else:
                # Prend le candidat avec le meilleur score
                best = max(candidates, key=score_at)
                if score_at(best) >= SIGNALS_FORCED:
                    extra_cuts.append(best)
                    forced += 1
                    seg_start = best
                else:
                    # Coupe au meilleur point disponible quand même
                    extra_cuts.append(best)
                    forced += 1
                    seg_start = best

            seg_dur = seg_end - seg_start

    cuts = sorted(set(cuts + extra_cuts))
    # Filtrer doublons trop proches
    deduped: list[float] = []
    for t in cuts:
        if not deduped or t - deduped[-1] > MIN_CLIP_DURATION:
            deduped.append(t)

    print(f"  {forced} coupes forcées (hard cap {MAX_CLIP_DURATION}s)", flush=True)
    print(f"  Total: {len(deduped)} coupes", flush=True)
    return deduped


# ── Encodage clips ────────────────────────────────────────────────────────────

def encode_clip(video: Path, start: float, end: float, out: Path) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.6f}", "-i", str(video),
        "-to", f"{end - start:.6f}",
        "-c:v", VIDEO_CODEC, "-crf", CRF, "-preset", PRESET,
        "-c:a", AUDIO_CODEC,
        str(out),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


# ── Main ──────────────────────────────────────────────────────────────────────

def split_video(
    video_path: str,
    output_dir: Optional[str] = None,
    prefix: Optional[str] = None,
) -> dict:
    video   = Path(video_path).resolve()
    out_dir = Path(output_dir).resolve() if output_dir else video.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    pfx     = prefix or video.stem
    dur     = _video_duration(video)

    print(f"\n=== Smart Scene Split: {video.name} ({dur:.1f}s) ===", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wav = tmp / "audio.wav"

        print("\n[0/5] Extraction audio WAV...", flush=True)
        _run(["ffmpeg", "-y", "-i", str(video), "-ar", "16000", "-ac", "1", str(wav)])

        print("\n[1/5] Signal 1 — Locuteur...", flush=True)
        sig1 = signal_speaker(wav)

        print("\n[2/5] Signal 2 — Visuel...", flush=True)
        sig2 = signal_visual(video)

        print("\n[3/5] Signal 3 — Contexte...", flush=True)
        sig3 = signal_context(wav)

        print("\n[4/5] Signal 4 — Qualité source...", flush=True)
        all_candidates = sorted(set(sig1 + sig2 + sig3))
        sig4 = signal_quality(video, all_candidates)

        print("\n[5/5] Corrélation + encodage clips...", flush=True)
        cut_times = correlate_signals(sig1, sig2, sig3, sig4, dur)

        # Créer les segments
        boundaries = [0.0] + cut_times + [dur]
        clips_created = []
        clip_num = 0

        for i in range(len(boundaries) - 1):
            start, end = boundaries[i], boundaries[i + 1]
            d = end - start
            if d < MIN_CLIP_DURATION:
                continue
            clip_num += 1
            out_file = out_dir / f"{pfx}_clip_{clip_num:02d}.mp4"
            ok = encode_clip(video, start, end, out_file)
            status = "OK" if ok else "ERR"
            print(f"  [{status}] {out_file.name} ({d:.1f}s)", flush=True)
            clips_created.append({"clip": out_file.name, "start": start, "end": end, "ok": ok})

    print(f"\nTermine: {len(clips_created)} clips dans {out_dir}", flush=True)
    return {"clips": clips_created, "output_dir": str(out_dir)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    split_video(
        sys.argv[1],
        sys.argv[2] if len(sys.argv) >= 3 else None,
        sys.argv[3] if len(sys.argv) >= 4 else None,
    )
