"""
Splits a single "everything recorded in one take" audio file -- e.g. the
user pasting all of a character's lines into an external voice generator
at once instead of exporting one file per line -- into the per-turn
segments format14/format15/format16/long1 need.

Two earlier approaches both failed for the same underlying reason (reported
2026-07-27, format16 Luffy/Sasuke, a Japanese voice-over performed under an
English on-screen script):

  1. Word-COUNT alignment: count N tokenized words off the target English
     line, consume N transcribed words from the recording. Breaks completely
     once the spoken language differs from the target text's language --
     word counts between two languages for the same content have no
     numeric relationship to each other.

  2. Pure PAUSE-based alignment: cut at the largest silence gaps in the
     recording, language-agnostic but content-blind -- it cuts wherever
     there happens to be silence, which includes a mid-sentence breath, not
     only the pause that follows a line's actual content.

This version instead: transcribes with task="translate" (Whisper translates
whatever language was spoken INTO English, with per-word timestamps still
aligned to the original audio), walks that English translation matching
each target line's own keywords in order to find WHERE that line's content
has actually been said, then cuts at the next real pause AFTER that point --
content-aware AND language-agnostic AND breath-safe.
"""

import logging
import re
from pathlib import Path

from faster_whisper import WhisperModel

from modules.assembler import _run_ffmpeg

logger = logging.getLogger(__name__)

_model_cache: WhisperModel | None = None


def _get_model() -> WhisperModel:
    """"base" was too weak for the translate-then-match approach this module relies
    on -- confirmed 2026-07-27 on a real Japanese recording: "base"'s translation was
    loose/hallucinated ("secret weapon was found in the secret weapon"), barely
    overlapping the target lines' actual wording, so content matching kept falling
    back to a plain proportional split instead of a real match. "small" produced a
    close, largely literal translation of the same audio ("you used the treasure as
    a meat again... couldn't get out of this island" for a target line about treasure/
    meat/leaving the island) -- worth the extra CPU time for what this module is
    actually trying to do."""
    global _model_cache
    if _model_cache is None:
        logger.info("Loading Whisper model 'small' for voice-split alignment...")
        _model_cache = WhisperModel("small", device="cpu", compute_type="int8")
    return _model_cache


_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def find_numbered_recordings(folder: Path, tag: str) -> list[Path]:
    """
    All audio files for `tag` (e.g. "hook", "a", "char_a"), in upload order --
    supports recording/uploading a character's or section's audio across
    several separate takes instead of requiring one continuous recording in
    a single sitting (user feedback, 2026-07-27: "je prepare pas forcement
    tous les audios d'une traite"). Expects '{tag}_recording_<N>.<ext>',
    N starting at 1 -- sorted NUMERICALLY, not alphabetically (alphabetic
    sort would order 1, 10, 11, 2, ... instead of 1, 2, ..., 10, 11). Falls
    back to a single legacy un-numbered '{tag}_recording.<ext>' file for
    anything saved before this existed.
    """
    if not folder.is_dir():
        return []
    numbered: list[tuple[int, Path]] = []
    for f in folder.iterdir():
        if not f.is_file() or f.suffix.lower() not in _AUDIO_EXT:
            continue
        m = re.match(rf"^{re.escape(tag)}_recording_(\d+)$", f.stem, re.IGNORECASE)
        if m:
            numbered.append((int(m.group(1)), f))
    if numbered:
        numbered.sort(key=lambda t: t[0])
        return [f for _, f in numbered]
    legacy = [f for f in folder.glob(f"{tag}_recording.*") if f.suffix.lower() in _AUDIO_EXT]
    return legacy[:1]


def concat_recordings(paths: list[Path], out_path: Path) -> None:
    """Join multiple recording pieces (possibly different formats/sample rates --
    separate takes recorded on different days/devices often are) into one
    continuous take -- concat FILTER, not the demuxer, specifically so mismatched
    source encodings don't need to match exactly."""
    if len(paths) == 1:
        _run_ffmpeg(["ffmpeg", "-y", "-i", str(paths[0]), "-ar", "44100", "-ac", "2", str(out_path)])
        return
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", str(p)]
    filter_in = "".join(f"[{i}:a]" for i in range(len(paths)))
    cmd += ["-filter_complex", f"{filter_in}concat=n={len(paths)}:v=0:a=1[aout]", "-map", "[aout]", str(out_path)]
    _run_ffmpeg(cmd)


_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to", "of", "in",
    "on", "at", "it", "its", "and", "or", "but", "for", "with", "this", "that", "these",
    "those", "i", "you", "he", "she", "we", "they", "my", "your", "his", "her", "our",
    "their", "im", "youre", "hes", "shes", "were", "theyre", "not", "no", "so", "as",
    "if", "do", "does", "did", "just", "up", "out", "all", "can", "will", "have", "has",
}


def _normalize_word(w: str) -> str:
    return re.sub(r"[^\w]", "", w).lower()


def _keywords_similar(a: str, b: str) -> bool:
    """True if `a` and `b` are plausibly the same word in different forms
    (plural, -ing/-ed, etc). Hand-written suffix-stripping rules (tried first,
    confirmed 2026-07-27) kept breaking on irregular English morphology --
    "controlled" minus "ed" leaves a doubled consonant ("controll") that no
    longer equals "control"; "enemies" minus a blind "es"/"s" strip doesn't
    equal "enemy" (y/ies pluralization). Prefix containment sidesteps that:
    one word fully starting with the other covers plurals/-ing/-ed additions
    regardless of spelling changes at the join (control/controlled, resource/
    resources, weak/weakness, panic/panicking); a "differ by at most the last
    character" fallback (restricted to longer words to avoid short-word false
    positives like one/once) covers the remaining y/ies cases (enemy/enemies,
    berry/berries)."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    # A 3-letter floor let "can" (modal verb) fuzzy-match keyword "cant" (can't,
    # i.e. "cannot") purely because "cant".startswith("can") -- opposite meanings,
    # false match (confirmed 2026-07-27). Short words need exact equality; only
    # longer-enough words are safe for containment-based matching.
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    if len(shorter) >= 5:
        common = 0
        for x, y in zip(shorter, longer):
            if x != y:
                break
            common += 1
        if common >= len(shorter) - 1:
            return True
    return False


def _line_keywords(line: str) -> set[str]:
    words = (_normalize_word(w) for w in line.split())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


_MATCH_THRESHOLD = 0.55   # fraction of a line's TOTAL KEYWORD WEIGHT (character length, see
                          # _content_matched_cut_points) that must be covered before we consider
                          # a line's content actually said. History: was 0.55 with count-based
                          # matching and no fuzzy word-form matching -- too strict for loose
                          # translations, under-matched and fell back to proportional splits too
                          # often, so lowered to 0.40. Then fuzzy plural/tense matching was added
                          # (see _keywords_similar) and 0.40 flipped to the OPPOSITE failure:
                          # confirmed 2026-07-27 on "Stop panicking. A captain who can't control
                          # spending will never reach his destination" -- "panicking"+"captain"+
                          # "who"+"cant"+"control" alone (all early in the sentence) already
                          # covered 40% of the weighted total, cutting right after "control the
                          # dead body" and BEFORE "spending will never reach his destination" was
                          # even said. Raised back up so a line's later/distinctive words
                          # ("destination") generally have to show up too, not just whatever
                          # happens to appear first.
_LOOKAHEAD_PAUSE_WORDS = 6
_MIN_PAUSE = 0.12
_FALLBACK_BUFFER = 0.25


def _find_pause_after(words: list, start_idx: int) -> float:
    """First reasonably-sized silence gap at/after `start_idx` -- places the
    actual cut at the next natural breath/pause once a line's content has
    been matched, rather than cutting the instant the last keyword lands
    (would risk clipping the line's own trailing words) or at some arbitrary
    distant gap that has nothing to do with this line ending."""
    hi = min(len(words) - 1, start_idx + _LOOKAHEAD_PAUSE_WORDS)
    for i in range(max(0, start_idx - 1), hi):
        gap = words[i + 1].start - words[i].end
        if gap >= _MIN_PAUSE:
            return (words[i].end + words[i + 1].start) / 2
    idx = min(start_idx, len(words) - 1)
    return words[idx].end + _FALLBACK_BUFFER


def _content_matched_cut_points(words: list, lines: list[str], duration: float) -> list[float]:
    """Walk the translated-to-English word stream (see split_combined_audio),
    matching each target line's own significant keywords in order. Once a
    line's content has genuinely been said (not just "some pause happened"),
    cut at the next natural pause after that point -- content-aware, so a
    dramatic mid-sentence breath doesn't get mistaken for a line boundary.

    Whisper's translation of a non-English recording is loose, not a literal
    match to the target line's exact wording -- if a line's keywords never
    reach the match threshold, an UNCAPPED search would walk all the way to
    the end of the recording, swallowing every remaining line into one
    (reported 2026-07-27: "tu as rien cut", every later cut collapsed onto
    the last one). Each line's search is capped to a generous multiple of
    its fair share of whatever audio is left, so a bad match degrades to a
    reasonable proportional split instead of consuming everything."""
    cut_points = [0.0]
    word_idx = 0
    n_lines = len(lines)
    all_keywords = [_line_keywords(l) for l in lines]
    for i, line in enumerate(lines[:-1]):
        remaining_words = len(words) - word_idx
        fair_share = max(3, round(remaining_words / (n_lines - i)))
        cap_idx = min(len(words), word_idx + fair_share * 3)

        # A keyword shared with the NEXT line ("now" said in both "and now the crew
        # can't leave" and "and now she can only get") is ambiguous evidence -- if the
        # translation happens to say it near/inside the next line's own content instead
        # of this line's, matching it here drags this line's cut far past where it
        # should end (confirmed 2026-07-27). Drop it from this line's own matching so
        # only genuinely distinguishing words can move the boundary.
        keywords = all_keywords[i] - (all_keywords[i + 1] if i + 1 < n_lines else set())
        end_idx = word_idx
        reached = False
        if keywords:
            # Weight by character length, not raw count -- a line's short/common
            # keywords ("who", "cant") routinely show up within the first few words
            # of ANY line and would satisfy a count-based threshold long before the
            # line's actual distinctive content ("destination", "panicking") has
            # been said, cutting mid-sentence (confirmed 2026-07-27: "captain who
            # can't control" alone hit 4/10 matched keywords and cut before "reach
            # his destination" was even said). Longer, rarer words carry more of a
            # line's real identity, so they should carry more of the threshold too.
            matched: set[str] = set()
            total_weight = sum(len(kw) for kw in keywords)
            needed_weight = max(1, round(total_weight * _MATCH_THRESHOLD))
            matched_weight = 0
            last_match_idx = None
            while end_idx < cap_idx:
                w = _normalize_word(words[end_idx].word)
                if len(w) >= 3:
                    for kw in keywords:
                        if kw not in matched and _keywords_similar(w, kw):
                            matched.add(kw)
                            matched_weight += len(kw)
                            last_match_idx = end_idx + 1
                            break
                end_idx += 1
                if matched_weight >= needed_weight:
                    reached = True
                    break
        if not reached:
            if last_match_idx is not None:
                # Threshold not reached, but SOME of this line's keywords genuinely
                # matched -- a paraphrase-heavy translation (idioms, mistransliterated
                # names) can easily leave real content under the weight threshold even
                # though the words that DID match are real evidence of where the line's
                # content actually was. That position is a far better guess than a blind
                # proportional split -- e.g. matching only "treasure"/"meat"/"island" out
                # of a line's 11 keywords still correctly lands right after "this island!"
                # is said, since "island" is genuinely the line's last word (2026-07-27).
                end_idx = last_match_idx
            else:
                # No keyword of this line matched anything -- no usable evidence at all,
                # fall back to this line's fair share of whatever audio is left.
                end_idx = min(len(words), word_idx + fair_share)

        cut_time = _find_pause_after(words, end_idx)
        cut_points.append(cut_time)
        word_idx = max(end_idx, word_idx)

    cut_points.append(duration + 0.3)
    # Cut points must be non-decreasing -- content matching can occasionally stall
    # (a mistranslated or unmatched line) and leave two lines resolving close together.
    for i in range(1, len(cut_points)):
        if cut_points[i] < cut_points[i - 1]:
            cut_points[i] = cut_points[i - 1] + 0.05
    return cut_points


def split_combined_audio(audio_path: Path, lines: list[str], out_paths: list[Path]) -> bool:
    """
    Transcribe `audio_path` (one continuous recording of `lines` spoken in
    order, in ANY language) and cut it into len(lines) segments saved to
    `out_paths`, matching each line's content in Whisper's own English
    translation of the audio before cutting at the pause that follows it.
    Returns False (no files written) if transcription found implausibly few
    words -- alignment would be unreliable, caller should fall back to the
    auto-selected TTS voice instead.
    """
    model = _get_model()
    # task="translate" + language=None: Whisper auto-detects whatever language was
    # actually spoken and translates its output to English, word timestamps still
    # aligned to the original audio -- this is what lets us match spoken CONTENT
    # against the target/on-screen English lines regardless of the recording's own
    # language (see module docstring).
    segments, _ = model.transcribe(str(audio_path), word_timestamps=True, language=None, task="translate")
    words = [w for seg in segments if seg.words for w in seg.words]

    # A language-agnostic sanity floor -- NOT a word-count comparison against the
    # target lines (the whole reason this module no longer aligns by word count).
    if len(words) < len(lines):
        logger.warning(
            f"Voice split: translated only {len(words)} word(s) for {len(lines)} line(s) in "
            f"{audio_path.name} -- alignment too unreliable, skipping split"
        )
        return False

    duration = words[-1].end
    cut_points = _content_matched_cut_points(words, lines, duration)

    for i, out_path in enumerate(out_paths):
        start, end = cut_points[i], cut_points[i + 1]
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ss", f"{max(0.0, start - 0.05):.3f}", "-to", f"{end:.3f}",
            "-c:a", "libmp3lame", "-q:a", "2", str(out_path),
        ])

    logger.info(f"Voice split {audio_path.name} into {len(out_paths)} segment(s) via content-matched translation")
    return True
