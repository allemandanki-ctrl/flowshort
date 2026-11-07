"""
Real film/show clip sourcing for format14's background -- searches YouTube
for a scene clip matching the character/context and downloads a short
section of it, used as moving background footage instead of a static
gradient/photo.

Same acceptance as character_image.py's case-file photo: using real
footage from the source material is a copyright-risk call the user has
already made for themselves (see BESOIN_TON_AVIS.md). This only points the
yt-dlp download mechanism youtube_pipeline.py already relies on (for
long-format compilations) at a search query instead of a known URL.
"""

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_MIN_DUR, _MAX_DUR = 20, 900   # seconds -- skip live/0-length and full-episode-length results
_SKIP_INTRO = 8                # seconds -- avoid channel intros/logos at the very start
_STOPWORDS = {"the", "and", "mr", "mrs", "dr", "of", "de", "la"}
_MIN_MEAN_LUMA = 45.0           # 0-255 sampled-frame brightness floor, checked on the RAW
                                # download. duel.py's own footage overlay applies a further
                                # eq=brightness=-0.10 grade on top at render time, so this
                                # floor has to sit well above "just barely visible" -- a raw
                                # clip that's merely dim can still end up near-black once that
                                # grade lands (real incident, 2026-08-26: a raw clip passed a
                                # naive near-black check but was unwatchable after grading)


def _distinctive_tokens(name: str) -> list[str]:
    """All identifying name tokens (real words, 3+ letters, not a
    stopword) -- e.g. ["jay", "gatsby"] for "Jay Gatsby". A real clip/photo
    title usually only uses ONE of a character's names, and it's often the
    SURNAME rather than the first name ("Gatsby's party scene", not "Jay's
    party scene") -- matching on just the first word missed these
    entirely, discarding every otherwise-correct candidate."""
    return [t.lower() for t in re.findall(r"[A-Za-z']+", name) if len(t) >= 3 and t.lower() not in _STOPWORDS]


def _filter_by_relevance(candidates: list[tuple], name: str | None, query: str) -> list[tuple]:
    """Keep only candidates whose title mentions ANY of the character's
    name tokens. If nothing matches, return an EMPTY list rather than
    silently keeping unrelated footage -- the caller already falls back to
    a static gradient card when there are no clips, which reads far better
    than a background that visibly has nothing to do with the character."""
    if not name:
        return candidates
    tokens = _distinctive_tokens(name)
    if not tokens:
        return candidates
    filtered = [c for c in candidates if any(tok in c[2].lower() for tok in tokens)]
    if not filtered:
        logger.warning(f"No clip titles matched {name!r} (tokens {tokens!r}) for query {query!r} -- discarding all candidates")
        return []
    return filtered


def _query_variants(query: str, name: str | None) -> list[str]:
    """Query phrasings to try in sequence until one actually turns up
    usable, on-character footage. Real incident, 2026-08-26: an animated
    character's first-choice query ("<name> <context>") returned nothing
    that passed the title-relevance filter, so EVERY line of that
    character's dialogue silently fell back to a flat gradient text card
    for the whole video -- leaving it at a single query and giving up is
    not an acceptable outcome (same standing rule as
    duel._fetch_photo_retrying: keep trying phrasings, never just settle
    for blank/broken)."""
    variants = [query]
    if name:
        variants += [name, f"{name} clip", f"{name} scene", f"{name} movie scene", f"{name} scene compilation"]
    return list(dict.fromkeys(v for v in variants if v))


def _mean_luma(path: Path) -> float | None:
    """Sampled brightness (0-255) of one frame ~1s into the clip -- catches
    downloads that are a technically valid video stream but visually
    unusable (a near-black interior/night scene). Returns None if a frame
    can't be sampled (caller treats that as inconclusive, not a rejection)."""
    frame_path = path.with_suffix(".luma_check.jpg")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1", "-q:v", "4", str(frame_path)],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or not frame_path.exists():
            return None
        from PIL import Image, ImageStat
        with Image.open(frame_path) as img:
            return ImageStat.Stat(img.convert("L")).mean[0]
    except Exception:
        return None
    finally:
        frame_path.unlink(missing_ok=True)


def _is_watchable(path: Path) -> bool:
    """Combines the existing decode-sanity check with the brightness floor
    -- both must pass for a downloaded clip to actually be used."""
    if not _has_valid_video_stream(path):
        return False
    luma = _mean_luma(path)
    if luma is not None and luma < _MIN_MEAN_LUMA:
        logger.debug(f"Discarding too-dark clip {path.name} (mean luma {luma:.1f} < {_MIN_MEAN_LUMA})")
        return False
    return True


def _run_yt_dlp(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """subprocess.run's own `timeout` only kills the direct child (yt-dlp
    itself) on Windows -- yt-dlp spawns ffmpeg as a grandchild to merge
    bestvideo+bestaudio, and if that ffmpeg survives the kill, it keeps the
    stdout/stderr pipe open, so `communicate()` hangs forever waiting for
    it to close (seen live: an orphaned ffmpeg.exe stuck at 0% CPU with the
    parent call never returning, well past its 90s timeout). `taskkill /T`
    kills the whole process tree instead of just the one PID."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _has_valid_video_stream(path: Path) -> bool:
    """yt-dlp occasionally writes an mp4 with a valid-looking moov atom
    (duration/metadata intact) but truncated/corrupted actual frame data --
    e.g. a download killed mid-merge by _run_yt_dlp's timeout. Duration
    alone (ffprobe/probe_duration) passes on these; actually decoding a
    moment of the file is what catches them before they reach the ffmpeg
    concat later and take the whole render down.

    2026-08-14, real bug caught live: this used to be JUST the decode probe
    below, which only checks that ffmpeg doesn't ERROR while decoding
    whatever streams exist -- an AUDIO-ONLY download (bestvideo track
    missing/failed to merge, bestaudio alone still succeeded) decodes
    cleanly with `-f null` (it's a generic muxer, happy to discard audio
    frames with nothing to complain about) and sailed straight through,
    even though there's no video to actually show. format15's assembler
    (duel.py's _seg_footage_line) builds its filtergraph assuming a `[0:v]`
    input unconditionally and crashed with "Stream specifier ':v' ...
    matches no streams" the first time a fetched character clip like that
    reached it. An explicit stream-count check catches this BEFORE the
    decode probe, which cannot tell "no video stream" apart from "silently
    fine, nothing to decode"."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if not result.stdout.strip():
            return False
    except Exception:
        return False

    try:
        from modules.transitions import probe_duration
        if probe_duration(str(path)) <= 0.1:
            return False
    except Exception:
        return False

    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-t", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0 and not result.stderr.strip()
    except Exception:
        return False


def _search_candidates(query: str, max_candidates: int, name: str | None) -> list[tuple]:
    """One search+duration-filter+relevance-filter pass for `query`. Returns
    an empty list (never raises) when the search fails or nothing passes
    the filters -- callers try the next query variant in that case."""
    try:
        listing = _run_yt_dlp(
            [
                "yt-dlp", f"ytsearch{max_candidates}:{query} scene", "--flat-playlist",
                "--print", "%(id)s|%(duration)s|%(title)s", "--no-warnings",
            ],
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"Character clip search failed for {query!r}: {e}")
        return []

    candidates = []
    for line in listing.stdout.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        vid, dur, title = parts
        try:
            dur_f = float(dur)
        except ValueError:
            continue
        if _MIN_DUR <= dur_f <= _MAX_DUR:
            candidates.append((vid, dur_f, title))

    return _filter_by_relevance(candidates, name, query)


def fetch_character_clip(
    query: str, out_path: Path, segment_duration: float, max_candidates: int = 5, name: str | None = None
) -> Path | None:
    """
    Search YouTube for `query` (retrying alternate phrasings if the first
    one turns up nothing usable -- see _query_variants), download a
    `segment_duration`-second section of the first usable, watchable result
    to out_path. Returns None (caller falls back to a static image/gradient)
    if every variant fails.
    """
    tried_vids: set[str] = set()
    for variant in _query_variants(query, name):
        candidates = [c for c in _search_candidates(variant, max_candidates, name) if c[0] not in tried_vids]
        if not candidates:
            continue

        for vid, dur_f, title in candidates:
            tried_vids.add(vid)
            start = _SKIP_INTRO if dur_f > segment_duration + _SKIP_INTRO + 2 else 0
            end = min(dur_f, start + segment_duration + 2)
            try:
                _run_yt_dlp(
                    [
                        "yt-dlp", f"https://www.youtube.com/watch?v={vid}",
                        "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
                        "--merge-output-format", "mp4",
                        "--download-sections", f"*{start:.0f}-{end:.0f}",
                        "--no-warnings", "-o", str(out_path),
                    ],
                    timeout=90,
                )
            except Exception as e:
                logger.debug(f"Character clip download failed for {variant!r} ({title}): {e}")
                continue

            if out_path.exists() and _is_watchable(out_path):
                logger.info(f"Character clip fetched for {variant!r}: {title} [{vid}]")
                return out_path
            out_path.unlink(missing_ok=True)

    logger.warning(f"No usable video candidates for {query!r} after trying alternate phrasings")
    return None


def fetch_character_clips(
    query: str, tmp_dir: Path, count: int, segment_duration: float, max_candidates: int = 8, name: str | None = None,
    file_prefix: str = "clip",
) -> list[Path]:
    """Same search as fetch_character_clip, but downloads up to `count` DISTINCT
    videos (one short section each) for a multi-clip cycling background,
    format3a-style, instead of a single looped clip.

    file_prefix MUST be distinct per character when this is called twice
    into the same tmp_dir (format15's two fighters) -- both calls used to
    write "clip_0.mp4", "clip_1.mp4", ... regardless of which character,
    so character B's fetch silently overwrote character A's clips on disk
    after both calls finished, and A's own footage never actually played
    (every "A" line and every "B" line ended up pointing at the same,
    B-only files).

    Also retries alternate query phrasings (see _query_variants) whenever
    the current one runs dry -- real incident, 2026-08-26: an animated
    character's only query returned nothing that passed relevance
    filtering, so this returned an empty list and EVERY line of that
    character's dialogue silently fell back to a flat gradient text card
    for the whole video. Leaving a character with zero real footage after
    one failed phrasing is not acceptable -- keep trying until either
    `count` clips are found or every variant is exhausted."""
    clips: list[Path] = []
    tried_vids: set[str] = set()
    i = 0
    for variant in _query_variants(query, name):
        if len(clips) >= count:
            break
        candidates = [c for c in _search_candidates(variant, max_candidates, name) if c[0] not in tried_vids]
        for vid, dur_f, title in candidates:
            if len(clips) >= count:
                break
            tried_vids.add(vid)
            start = _SKIP_INTRO if dur_f > segment_duration + _SKIP_INTRO + 2 else 0
            end = min(dur_f, start + segment_duration + 2)
            out_path = tmp_dir / f"{file_prefix}_{i}.mp4"
            i += 1
            try:
                _run_yt_dlp(
                    [
                        "yt-dlp", f"https://www.youtube.com/watch?v={vid}",
                        "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
                        "--merge-output-format", "mp4",
                        "--download-sections", f"*{start:.0f}-{end:.0f}",
                        "--no-warnings", "-o", str(out_path),
                    ],
                    timeout=90,
                )
            except Exception as e:
                logger.debug(f"Skipping clip candidate {title!r}: {e}")
                continue
            if out_path.exists() and _is_watchable(out_path):
                clips.append(out_path)
                logger.info(f"Character clip fetched for {variant!r}: {title} [{vid}]")
            else:
                out_path.unlink(missing_ok=True)
                logger.debug(f"Discarding unusable download for {title!r} (broken stream or too dark)")

    if not clips:
        logger.warning(f"No usable video candidates for {query!r} after trying alternate phrasings")
    return clips
