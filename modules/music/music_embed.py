"""
Music embeddings — sentence-transformers text encoder over each track's mood
metadata (feel + description + instruments + BPM). Light (~80MB, CPU), reliable.

Why text-embedding, not CLAP audio:
  The Incompetech catalog ships human-written mood descriptions + feel tags per
  track. Matching Claude's mood query against those curated descriptions is more
  reliable than CLAP audio embeddings and needs no audio decode / 2GB model / GPU.
  Same cosine-similarity mechanic, far lighter — fits this machine.

Run:  python -m modules.music.music_embed        # embeds all tracks missing an embedding
"""

import logging

import numpy as np

from modules.music import music_db

logger = logging.getLogger(__name__)

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading text encoder '{_MODEL_NAME}'...")
        _model = SentenceTransformer(_MODEL_NAME, device="cpu")
        logger.info("Encoder loaded")
    return _model


def track_text(track: dict) -> str:
    """Build the text profile embedded for a track."""
    bpm = track.get("bpm") or 0
    tempo = "fast" if bpm >= 120 else "mid-tempo" if bpm >= 90 else "slow"
    return (
        f"{track.get('feel','')}. {track.get('description','')}. "
        f"Instruments: {track.get('instruments','')}. "
        f"{tempo} {bpm} BPM instrumental."
    ).strip()


def embed_texts(texts: list[str]) -> np.ndarray:
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, batch_size=64,
                        show_progress_bar=False).astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    return embed_texts([text])[0]


def embed_all_missing() -> int:
    todo = music_db.tracks_without_embedding()
    if not todo:
        logger.info("All tracks already embedded.")
        return 0
    logger.info(f"Embedding {len(todo)} tracks...")
    texts = [track_text(t) for t in todo]
    vecs = embed_texts(texts)
    for t, v in zip(todo, vecs):
        music_db.set_embedding(t["track_id"], v)
    logger.info(f"Embedded {len(todo)} tracks.")
    return len(todo)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = embed_all_missing()
    print(f"\nEmbedded now: {n} | total embedded: {music_db.count_tracks(only_embedded=True)}")
