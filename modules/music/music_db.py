"""
Music database — SQLite index of royalty-free tracks + usage history.

Tables:
  tracks(track_id, source, license, title, filename, url, path, bpm, duration,
         genre, feel, instruments, description, embedding BLOB)
  usage_history(id, track_id, video, platform, posted_at)

Embeddings are float32 numpy arrays stored as BLOB (sentence-transformers).
Audio files are downloaded just-in-time on selection (path filled then).
"""

import logging
import sqlite3
from pathlib import Path

import numpy as np

from config import BASE_DIR

logger = logging.getLogger(__name__)

_DB_PATH = BASE_DIR / "state" / "music.db"   # shared track catalog, not ResetWorth's own STATE_DIR


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                track_id     TEXT PRIMARY KEY,
                source       TEXT,
                license      TEXT,
                title        TEXT,
                filename     TEXT,
                url          TEXT,
                path         TEXT,
                bpm          INTEGER,
                duration     REAL,
                genre        TEXT,
                feel         TEXT,
                instruments  TEXT,
                description  TEXT,
                embedding    BLOB,
                banned       INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS usage_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id   TEXT,
                video      TEXT,
                platform   TEXT,
                posted_at  TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS ix_usage_track ON usage_history(track_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_usage_time  ON usage_history(posted_at)")
        # Migration for DBs created before `banned` existed.
        try:
            c.execute("ALTER TABLE tracks ADD COLUMN banned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists


def ban_track(track_id: str) -> None:
    """Permanently exclude a track from future selection (user disliked it)."""
    init_db()   # self-contained: safe even if called before any other music_db function
    with _conn() as c:
        c.execute("UPDATE tracks SET banned=1 WHERE track_id=?", (track_id,))
    logger.info(f"music_db: banned {track_id}")


def unban_track(track_id: str) -> None:
    init_db()
    with _conn() as c:
        c.execute("UPDATE tracks SET banned=0 WHERE track_id=?", (track_id,))


def ban_track_by_title(title: str) -> int:
    """Ban all tracks whose title matches exactly (case-insensitive). Returns count banned."""
    init_db()
    with _conn() as c:
        cur = c.execute("UPDATE tracks SET banned=1 WHERE LOWER(title)=LOWER(?)", (title,))
        return cur.rowcount


def ban_by_feel_keywords(keywords: set[str]) -> int:
    """
    Ban (not delete) every track whose 'feel' field contains any of the given
    keywords, even if it also carries other relevant tags. Used to purge
    pop/bright-leaning tracks that slipped in via a co-occurring dark tag
    (e.g. feel="Driving, Action, Intense, Bright" -- still sounds pop-ish).
    """
    init_db()
    with _conn() as c:
        rows = c.execute("SELECT track_id, feel FROM tracks WHERE COALESCE(banned,0)=0").fetchall()
        to_ban = [r["track_id"] for r in rows
                 if any(k.lower() in (r["feel"] or "").lower() for k in keywords)]
        if to_ban:
            c.executemany("UPDATE tracks SET banned=1 WHERE track_id=?",
                          [(t,) for t in to_ban])
    return len(to_ban)


def upsert_track(track: dict) -> None:
    emb = track.get("embedding")
    blob = np.asarray(emb, dtype=np.float32).tobytes() if emb is not None else None
    with _conn() as c:
        c.execute("""
            INSERT INTO tracks (track_id, source, license, title, filename, url, path,
                                bpm, duration, genre, feel, instruments, description, embedding)
            VALUES (:track_id,:source,:license,:title,:filename,:url,:path,
                    :bpm,:duration,:genre,:feel,:instruments,:description,:embedding)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, filename=excluded.filename, url=excluded.url,
                bpm=excluded.bpm, genre=excluded.genre, feel=excluded.feel,
                instruments=excluded.instruments, description=excluded.description,
                embedding=COALESCE(excluded.embedding, tracks.embedding)
        """, {
            "track_id": track["track_id"], "source": track.get("source"),
            "license": track.get("license"), "title": track.get("title"),
            "filename": track.get("filename"), "url": track.get("url"),
            "path": track.get("path"), "bpm": track.get("bpm"),
            "duration": track.get("duration"), "genre": track.get("genre"),
            "feel": track.get("feel"), "instruments": track.get("instruments"),
            "description": track.get("description"), "embedding": blob,
        })


def count_tracks(only_embedded: bool = False) -> int:
    q = "SELECT COUNT(*) FROM tracks" + (" WHERE embedding IS NOT NULL" if only_embedded else "")
    with _conn() as c:
        return c.execute(q).fetchone()[0]


def tracks_without_embedding() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM tracks WHERE embedding IS NULL").fetchall()
    return [dict(r) for r in rows]


def load_embeddings() -> tuple[list[str], np.ndarray]:
    """Return (track_ids, matrix[N,D]) for all embedded, non-banned tracks."""
    with _conn() as c:
        rows = c.execute(
            "SELECT track_id, embedding FROM tracks "
            "WHERE embedding IS NOT NULL AND COALESCE(banned, 0) = 0"
        ).fetchall()
    ids = [r["track_id"] for r in rows]
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    return ids, mat


def get_track(track_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM tracks WHERE track_id=?", (track_id,)).fetchone()
    return dict(r) if r else None


def set_track_path(track_id: str, path: str) -> None:
    with _conn() as c:
        c.execute("UPDATE tracks SET path=? WHERE track_id=?", (path, track_id))


def set_embedding(track_id: str, emb) -> None:
    blob = np.asarray(emb, dtype=np.float32).tobytes()
    with _conn() as c:
        c.execute("UPDATE tracks SET embedding=? WHERE track_id=?", (blob, track_id))


def record_usage(track_id: str, video: str, platform: str = "all") -> None:
    from datetime import datetime, timezone
    with _conn() as c:
        c.execute(
            "INSERT INTO usage_history (track_id, video, platform, posted_at) VALUES (?,?,?,?)",
            (track_id, video, platform, datetime.now(timezone.utc).isoformat()),
        )


def recently_used(days: int = 30, last_n: int = 50) -> set[str]:
    """track_ids used in the last `days` OR among the last `last_n` usages."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        by_time = c.execute(
            "SELECT DISTINCT track_id FROM usage_history WHERE posted_at >= ?", (cutoff,)
        ).fetchall()
        by_count = c.execute(
            "SELECT track_id FROM usage_history ORDER BY id DESC LIMIT ?", (last_n,)
        ).fetchall()
    return {r["track_id"] for r in by_time} | {r["track_id"] for r in by_count}
