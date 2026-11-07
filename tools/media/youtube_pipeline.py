"""
youtube_pipeline.py  <youtube_url>

Pipeline complet YouTube -> clips sans musique:
  1. Telechargement MP4 HD (yt-dlp)
  2. Suppression musique (remove_music.py)
  3. Decoupage scenes (scene_split.py)
  4. Ouvre le dossier de sortie

Usage:
    python tools/media/youtube_pipeline.py "https://www.youtube.com/watch?v=XXXXX"
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Chemins ───────────────────────────────────────────────────────────────────
PROJECT = Path(__file__).parent.parent.parent
CLIPS_ROOT = PROJECT / "assets" / "clips"
CLIPS_ROOT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "MVSEP-CDX23-Cinematic-Sound-Demixing"))
sys.path.insert(0, str(PROJECT / ".claude" / "skills" / "scene-split"))
os.environ.setdefault("HF_TOKEN", "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")

# Patch PyTorch 2.6 weights_only
import torch as _torch
_orig_load = _torch.load
def _patched_load(f, *a, **k):
    k.setdefault("weights_only", False)
    return _orig_load(f, *a, **k)
_torch.load = _patched_load


def sanitize_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:60]


def step1_download(url: str, tmp_dir: Path) -> Path:
    print("\n[ETAPE 1/3] Telechargement YouTube (yt-dlp)...")
    out_tmpl = str(tmp_dir / "%(title)s.%(ext)s")
    subprocess.run(
        [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", out_tmpl,
            url,
        ],
        check=True,
    )
    mp4_files = list(tmp_dir.glob("*.mp4"))
    if not mp4_files:
        print("ERREUR : aucun MP4 telecharge")
        sys.exit(1)
    return mp4_files[0]


def step2_remove_music(video: Path, tmp_dir: Path) -> Path:
    print("\n[ETAPE 2/3] Suppression musique (MVSEP)...")
    from remove_music import remove_music
    # remove_music() ecrit <stem>_no_music.mp4 dans le meme dossier
    out_path = remove_music(str(video))
    return Path(out_path)


def step3_split(video_no_music: Path, out_dir: Path, prefix: str) -> None:
    print("\n[ETAPE 3/3] Decoupage scenes (4 signaux)...")
    from smart_scene_split import split_video
    split_video(str(video_no_music), str(out_dir), prefix)


def open_folder(path: Path) -> None:
    os.startfile(str(path))


def main(url: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # 1. Telechargement
        video = step1_download(url, tmp_dir)
        title = sanitize_name(video.stem)
        print(f"  Titre: {title}")

        # 2. Suppression musique
        video_no_music = step2_remove_music(video, tmp_dir)

        # 3. Dossier de sortie frais
        out_dir = CLIPS_ROOT / title
        out_dir.mkdir(parents=True, exist_ok=True)

        # 4. Decoupage
        step3_split(video_no_music, out_dir, title)

    print(f"\n=== TERMINE ===")
    print(f"Clips dans : {out_dir}")
    open_folder(out_dir)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
