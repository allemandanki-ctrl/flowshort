"""
remove_music.py  <video_or_audio>
Supprime la musique de fond en gardant voix + effets.
Utilise Demucs htdemucs (Facebook) -- bien meilleur que MVSEP sur parole.

Usage:
    python tools/media/remove_music.py ma_video.mp4
    python tools/media/remove_music.py mon_audio.wav

Sortie:
    ma_video_no_music.mp4  (même dossier que l'entrée)
"""

import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def remove_music(input_path: str) -> str:
    inp = Path(input_path).resolve()
    if not inp.exists():
        print(f"ERREUR : fichier introuvable : {inp}")
        sys.exit(1)

    is_video = inp.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".webm"}
    out_path = inp.parent / (inp.stem + "_no_music" + inp.suffix)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wav_in  = tmp / "input.wav"
        sep_dir = tmp / "separated"
        sep_dir.mkdir()
        wav_out = tmp / "vocals.wav"

        # 1. Extraire audio en WAV 44.1kHz stéréo
        print("1/4  Extraction audio...")
        _run(["ffmpeg", "-y", "-i", str(inp),
              "-ar", "44100", "-ac", "2", "-vn", str(wav_in)])

        # 2. Séparation Demucs (vocals vs no_vocals)
        # --mp3 contourne torchaudio/torchcodec (utilise lameenc)
        print("2/4  Separation Demucs htdemucs (CPU ~5-15 min)...")
        _run([
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "--mp3",
            "--out", str(sep_dir),
            str(wav_in),
        ])

        # 3. Trouver le stem vocals produit par Demucs
        print("3/4  Recuperation stem vocals...")
        # Demucs cree : sep_dir/<model>/<input_stem>/vocals.mp3
        vocals_files = list(sep_dir.rglob("vocals.mp3"))
        if not vocals_files:
            print(f"ERREUR : vocals.mp3 introuvable dans {sep_dir}")
            print("Fichiers trouves :", list(sep_dir.rglob("*")))
            sys.exit(1)

        vocals_path = vocals_files[0]
        print(f"     Stem: {vocals_path}")
        import shutil
        # Convertir MP3 -> WAV pour l'assemblage final
        wav_out_vocals = tmp / "vocals_converted.wav"
        _run(["ffmpeg", "-y", "-i", str(vocals_path), str(wav_out_vocals)])
        shutil.copy(str(wav_out_vocals), str(wav_out))

        # 4. Recombiner avec la vidéo
        print("4/4  Assemblage final...")
        if is_video:
            _run([
                "ffmpeg", "-y",
                "-i", str(inp), "-i", str(wav_out),
                "-c:v", "copy",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                str(out_path),
            ])
        else:
            shutil.copy(str(wav_out), str(out_path))

    print(f"\nTermine : {out_path}")
    return str(out_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    remove_music(sys.argv[1])
